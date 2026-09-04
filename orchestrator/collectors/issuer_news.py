"""Issuer and regulatory primary-source update collector.

Bounded RSS/Atom and configured HTML/JSON-LD ingestion for company IR,
SEC/regulator, exchange, and government primary sources. Every configured
origin is operator-allowlisted and validated before fetching; redirects must
stay on the configured origin; bodies, item counts, and text are bounded;
malformed provider data fails explicitly and empty feeds are valid.

Records target ``source_documents`` with ``document_type``
``issuer_update``/``regulatory_update``. Deterministic document IDs derive
from the canonical source URL, so one release syndicated across several
feeds collapses to a single identity (metadata.aliases records the other
feeds). Metadata distinguishes source time (``source_time``) from
acquisition/availability (``acquisition``) and marks primary vs derivative
syndication. Conditional HTTP state (ETag/Last-Modified) is captured per
feed and persisted to an optional state file so later runs send
If-None-Match/If-Modified-Since and treat 304 as an empty, valid result.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from errors import InvalidSourceData, TransientSourceError
from logging_config import get_logger
from provider_origins import validate_configured_origin
from sources.issuer_feed import (
    DEFAULT_MAX_CONTENT_CHARS,
    DEFAULT_MAX_FEED_BYTES,
    DEFAULT_MAX_ITEMS_PER_FEED,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MAX_TITLE_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    IssuerFeedError,
    dedupe_records,
    extract_primary_page_text,
    fetch_feed,
    normalize_feed_records,
    parse_feed_items,
)
from sources.news_storage import atomic_write_json, read_json

from collectors.base import CollectionResult, CollectorSetupRequired, elapsed_ms

logger = get_logger("collector.issuer_news")


def _bounded_int(config: dict, key: str, default: int, low: int, high: int) -> int:
    """Operator-tunable integer setting, clamped to a safe range."""
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(low, min(value, high))


def _error_entry(
    label: str, stage: str, code: str, error_class: str, exception_type: str
) -> dict:
    return {
        "feed": label,
        "stage": stage,
        "code": code,
        "error_class": error_class,
        "exception_type": exception_type,
    }


def _origin_key(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return None
        default_port = 443 if parts.scheme.lower() == "https" else 80
        return parts.scheme.lower(), parts.hostname.lower(), parts.port or default_port
    except ValueError:
        return None


class IssuerNewsCollector:
    source_id = "issuer_news"

    def collect(
        self, config: Mapping[str, Any], correlation_id: str
    ) -> CollectionResult:
        started = time.monotonic()
        issuer_config = self._issuer_config(config)
        feeds = [
            feed
            for feed in issuer_config.get("feeds") or []
            if feed.get("enabled", True)
        ]
        if not feeds:
            raise CollectorSetupRequired("No enabled issuer_news feeds are configured")

        acquired_at = datetime.now(UTC)
        state = self._load_state(issuer_config)
        records: list[dict] = []
        errors: list[dict] = []
        successful = 0
        metrics: dict[str, int] = {
            "feeds_configured": len(feeds),
            "feeds_succeeded": 0,
            "feeds_failed": 0,
            "items_fetched": 0,
            "items_skipped": 0,
            "records": 0,
            "conditional_not_modified": 0,
            "api_calls_made": 0,
            "full_text_attempted": 0,
            "full_text_fetched": 0,
            "full_text_failed": 0,
        }

        for feed in feeds:
            label = self._feed_label(feed)
            try:
                feed_records, feed_errors, feed_meta = self._collect_feed(
                    feed,
                    issuer_config,
                    acquired_at,
                    state,
                    correlation_id,
                )
            except Exception as exc:  # per-feed isolation
                feed_records = []
                feed_errors = [
                    _error_entry(
                        label,
                        "collect",
                        "feed_failed",
                        InvalidSourceData.error_class,
                        type(exc).__name__,
                    )
                ]
                feed_meta = {"items_fetched": 0, "not_modified": False, "api_calls": 1}
            records.extend(feed_records)
            errors.extend(feed_errors)
            if feed_errors:
                metrics["feeds_failed"] += 1
                logger.warning(
                    "issuer_news_feed_failed",
                    action="collect",
                    feed=label,
                    code=feed_errors[0].get("code"),
                    error_class=feed_errors[0].get("error_class"),
                    correlation_id=correlation_id,
                )
            else:
                successful += 1
                metrics["feeds_succeeded"] += 1
            metrics["items_fetched"] += int(feed_meta.get("items_fetched", 0))
            metrics["items_skipped"] += sum(
                int(value) for value in (feed_meta.get("skipped") or {}).values()
            )
            metrics["api_calls_made"] += int(feed_meta.get("api_calls", 0))
            for key in ("full_text_attempted", "full_text_fetched", "full_text_failed"):
                metrics[key] += int(feed_meta.get(key, 0))
            if feed_meta.get("not_modified"):
                metrics["conditional_not_modified"] += 1

        records = dedupe_records(records)
        metrics["records"] = len(records)
        self._save_state(issuer_config, state)

        logger.info(
            "issuer_news_collection_completed",
            action="collect",
            feeds_configured=metrics["feeds_configured"],
            feeds_succeeded=successful,
            feeds_failed=metrics["feeds_failed"],
            records=len(records),
            items_fetched=metrics["items_fetched"],
            items_skipped=metrics["items_skipped"],
            duration_ms=elapsed_ms(started),
            correlation_id=correlation_id,
        )
        return CollectionResult(
            records=records,
            errors=errors,
            total_series=len(feeds),
            successful_series=successful,
            metrics=metrics,
        )

    def _collect_feed(
        self,
        feed: Mapping[str, Any],
        issuer_config: Mapping[str, Any],
        acquired_at: datetime,
        state: dict,
        correlation_id: str,
    ) -> tuple[list[dict], list[dict], dict[str, Any]]:
        """Collect one feed; failures are isolated to a per-feed error entry."""
        label = self._feed_label(feed)
        try:
            feed_url = validate_configured_origin(
                feed.get("url"), issuer_config, label="issuer_news feed"
            )
            allowed_content_origins = {_origin_key(feed_url)}
            for index, configured_url in enumerate(feed.get("content_origins") or []):
                normalized_url = validate_configured_origin(
                    configured_url,
                    issuer_config,
                    label=f"issuer_news content origin {index + 1}",
                )
                allowed_content_origins.add(_origin_key(normalized_url))
            allowed_content_origins.discard(None)
        except ValueError as exc:
            logger.error(
                "issuer_news_invalid_origin",
                action="collect",
                feed=label,
                error=str(exc),
                correlation_id=correlation_id,
            )
            return (
                [],
                [
                    _error_entry(
                        label,
                        "config",
                        "invalid_origin",
                        InvalidSourceData.error_class,
                        "ValueError",
                    )
                ],
                {"items_fetched": 0, "not_modified": False, "api_calls": 0},
            )

        max_bytes = _bounded_int(
            feed, "max_bytes", DEFAULT_MAX_FEED_BYTES, 64_000, 50_000_000
        )
        timeout = _bounded_int(
            feed, "timeout_seconds", int(DEFAULT_TIMEOUT_SECONDS), 5, 120
        )
        max_redirects = _bounded_int(
            feed, "max_redirects", DEFAULT_MAX_REDIRECTS, 0, 10
        )
        conditional = {
            key: state[label][key]
            for key in ("etag", "last_modified")
            if isinstance(state.get(label), dict) and state[label].get(key)
        }

        observed_at = datetime.now(UTC)
        try:
            fetch = fetch_feed(
                feed_url,
                headers=feed.get("headers"),
                timeout=float(timeout),
                cap=max_bytes,
                max_redirects=max_redirects,
                conditional=conditional,
            )
        except IssuerFeedError as exc:
            return (
                [],
                [
                    _error_entry(
                        label,
                        "fetch",
                        exc.code,
                        exc.error_class,
                        type(exc).__name__,
                    )
                ],
                {"items_fetched": 0, "not_modified": False, "api_calls": 1},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return (
                [],
                [
                    _error_entry(
                        label,
                        "fetch",
                        "request_failed",
                        TransientSourceError.error_class,
                        type(exc).__name__,
                    )
                ],
                {"items_fetched": 0, "not_modified": False, "api_calls": 1},
            )
        except Exception as exc:
            return (
                [],
                [
                    _error_entry(
                        label,
                        "fetch",
                        "feed_failed",
                        InvalidSourceData.error_class,
                        type(exc).__name__,
                    )
                ],
                {"items_fetched": 0, "not_modified": False, "api_calls": 1},
            )

        state[label] = {
            "etag": fetch.etag,
            "last_modified": fetch.last_modified,
            "last_polled_at": observed_at.isoformat(),
        }
        if fetch.body is None:  # 304 Not Modified: empty result, valid
            logger.info(
                "issuer_news_feed_not_modified",
                action="collect",
                feed=label,
                status_code=fetch.status_code,
                correlation_id=correlation_id,
            )
            return (
                [],
                [],
                {
                    "items_fetched": 0,
                    "not_modified": True,
                    "api_calls": 1 + fetch.hops,
                },
            )

        max_items = _bounded_int(
            feed, "max_items", DEFAULT_MAX_ITEMS_PER_FEED, 1, 1_000
        )
        max_title_chars = _bounded_int(
            feed, "max_title_chars", DEFAULT_MAX_TITLE_CHARS, 20, 2_000
        )
        max_content_chars = _bounded_int(
            feed, "max_content_chars", DEFAULT_MAX_CONTENT_CHARS, 0, 100_000
        )
        try:
            raw_items = parse_feed_items(
                fetch.body,
                feed,
                max_items=max_items,
                max_title_chars=max_title_chars,
                max_content_chars=max_content_chars,
            )
        except IssuerFeedError as exc:
            return (
                [],
                [
                    _error_entry(
                        label,
                        "parse",
                        exc.code,
                        exc.error_class,
                        type(exc).__name__,
                    )
                ],
                {
                    "items_fetched": 0,
                    "not_modified": False,
                    "api_calls": 1 + fetch.hops,
                },
            )

        feed_records, skipped = normalize_feed_records(
            raw_items,
            feed,
            source=self.source_id,
            acquired_at=acquired_at,
            observed_at=observed_at,
            fetch=fetch,
            feed_url=feed_url,
        )
        full_text_metrics = {
            "full_text_attempted": 0,
            "full_text_fetched": 0,
            "full_text_failed": 0,
            "api_calls": 0,
        }
        if feed.get("fetch_full_text", False):
            full_text_metrics = self._enrich_full_text(
                feed_records,
                feed=feed,
                allowed_origins=frozenset(allowed_content_origins),
                observed_at=observed_at,
                correlation_id=correlation_id,
            )
        feed_errors = []
        if skipped:
            feed_errors.append(
                _error_entry(
                    label,
                    "normalize",
                    "items_skipped",
                    InvalidSourceData.error_class,
                    "NormalizationSkip",
                )
                | {"detail": skipped}
            )
        logger.info(
            "issuer_news_feed_collected",
            action="collect",
            feed=label,
            items_fetched=len(raw_items),
            records=len(feed_records),
            skipped=skipped,
            status_code=fetch.status_code,
            correlation_id=correlation_id,
        )
        return (
            feed_records,
            feed_errors,
            {
                "items_fetched": len(raw_items),
                "skipped": skipped,
                "not_modified": False,
                "api_calls": 1 + fetch.hops + full_text_metrics["api_calls"],
                "full_text_attempted": full_text_metrics["full_text_attempted"],
                "full_text_fetched": full_text_metrics["full_text_fetched"],
                "full_text_failed": full_text_metrics["full_text_failed"],
            },
        )

    def _enrich_full_text(
        self,
        records: list[dict],
        *,
        feed: Mapping[str, Any],
        allowed_origins: frozenset[tuple[str, str, int]],
        observed_at: datetime,
        correlation_id: str,
    ) -> dict[str, int]:
        metrics = {
            "full_text_attempted": 0,
            "full_text_fetched": 0,
            "full_text_failed": 0,
            "api_calls": 0,
        }
        origins = allowed_origins
        limit = _bounded_int(feed, "max_full_text_items", 20, 1, 100)
        max_chars = _bounded_int(
            feed, "max_content_chars", DEFAULT_MAX_CONTENT_CHARS, 0, 100_000
        )
        max_bytes = _bounded_int(
            feed, "max_document_bytes", 2_000_000, 64_000, 10_000_000
        )
        timeout = _bounded_int(
            feed, "timeout_seconds", int(DEFAULT_TIMEOUT_SECONDS), 5, 120
        )
        max_redirects = _bounded_int(
            feed, "max_redirects", DEFAULT_MAX_REDIRECTS, 0, 10
        )
        for record in records[:limit]:
            url = str(record.get("url") or "")
            if _origin_key(url) not in origins:
                continue
            metrics["full_text_attempted"] += 1
            metrics["api_calls"] += 1
            try:
                fetched = fetch_feed(
                    url,
                    headers=feed.get("headers"),
                    timeout=float(timeout),
                    cap=max_bytes,
                    max_redirects=max_redirects,
                )
                metrics["api_calls"] += fetched.hops
                full_text = extract_primary_page_text(fetched.body or b"", max_chars)
            except (
                IssuerFeedError,
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:
                metrics["full_text_failed"] += 1
                logger.warning(
                    "issuer_news_full_text_failed",
                    action="collect",
                    feed=self._feed_label(feed),
                    exception_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )
                continue
            except Exception as exc:
                metrics["full_text_failed"] += 1
                logger.warning(
                    "issuer_news_full_text_failed",
                    action="collect",
                    feed=self._feed_label(feed),
                    exception_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )
                continue
            existing = str(record.get("content") or "")
            if len(full_text) <= len(existing):
                continue
            record["content"] = full_text
            metadata = dict(record.get("metadata") or {})
            metadata["content_extraction"] = {
                "method": "linked_primary_page",
                "url": fetched.final_url,
                "fetched_at": observed_at.isoformat(),
                "feed_content_chars": len(existing),
                "extracted_chars": len(full_text),
            }
            record["metadata"] = metadata
            metrics["full_text_fetched"] += 1
        return metrics

    @staticmethod
    def _issuer_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
        collectors = config.get("collectors") or {}
        if not isinstance(collectors, Mapping):
            raise CollectorSetupRequired(
                "issuer_news collector configuration is missing"
            )
        issuer_config = collectors.get("issuer_news")
        if not isinstance(issuer_config, Mapping):
            raise CollectorSetupRequired(
                "issuer_news collector configuration is missing"
            )
        return issuer_config

    @staticmethod
    def _feed_label(feed: Mapping[str, Any]) -> str:
        name = str(feed.get("name") or "").strip()
        if name:
            return name
        return str(feed.get("url") or "").strip() or "unnamed"

    @staticmethod
    def _load_state(issuer_config: Mapping[str, Any]) -> dict:
        state_path = issuer_config.get("state_path")
        if not state_path:
            return {}
        value = read_json(Path(str(state_path)), {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _save_state(issuer_config: Mapping[str, Any], state: dict) -> None:
        state_path = issuer_config.get("state_path")
        if not state_path:
            return
        try:
            atomic_write_json(Path(str(state_path)), state)
        except OSError as exc:
            logger.warning(
                "issuer_news_state_save_failed",
                action="save_state",
                error_type=type(exc).__name__,
            )

    def health_check(self, config: dict) -> dict:
        started = time.monotonic()
        try:
            issuer_config = self._issuer_config(config)
        except CollectorSetupRequired as exc:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": str(exc),
                "latency_ms": 0,
            }
        feeds = [
            feed
            for feed in issuer_config.get("feeds") or []
            if feed.get("enabled", True)
        ]
        if not feeds:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No feeds configured",
                "latency_ms": 0,
            }
        feed = feeds[0]
        try:
            feed_url = validate_configured_origin(
                feed.get("url"), issuer_config, label="issuer_news feed"
            )
        except ValueError as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": f"invalid origin ({exc})",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        max_bytes = _bounded_int(
            feed, "max_bytes", DEFAULT_MAX_FEED_BYTES, 64_000, 50_000_000
        )
        timeout = min(
            _bounded_int(feed, "timeout_seconds", int(DEFAULT_TIMEOUT_SECONDS), 5, 120),
            20,
        )
        try:
            fetch = fetch_feed(
                feed_url,
                headers=feed.get("headers"),
                timeout=float(timeout),
                cap=min(max_bytes, 1_000_000),
            )
        except IssuerFeedError as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": exc.code,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": type(exc).__name__,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        healthy = fetch.status_code < 400
        return {
            "healthy": healthy,
            "state": "success" if healthy else "failed",
            "message": f"HTTP {fetch.status_code}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["issuer_news"]["schedule"]

    def get_target_table(self) -> str:
        return "source_documents"

    def get_conflict_columns(self) -> list[str]:
        return ["document_id"]
