import hashlib
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from http_client import make_request
from http_errors import safe_error_message, scrub_url
from logging_config import get_logger
from provider_origins import validate_configured_origin

from collectors.base import CollectorNoData, CollectorSetupRequired

logger = get_logger("collector.central_banks")


def _validated_feed_url(feed: dict, feeds_config: dict) -> str:
    """Validate one configured central-bank feed origin before fetching."""
    return validate_configured_origin(
        feed.get("url"), feeds_config, label="central_banks feed"
    )


class CentralBanksCollector:
    source_id = "central_banks"

    def collect(self, config, correlation_id):
        records = []
        feeds = config["collectors"]["central_banks"].get("feeds", [])
        if not feeds:
            raise CollectorSetupRequired("No central-bank feeds are configured")
        acquired_at = datetime.now(UTC)
        failures = []
        for feed in feeds:
            feed_label = scrub_url(feed.get("url", ""))
            try:
                feed_url = _validated_feed_url(
                    feed, config["collectors"]["central_banks"]
                )
                response = make_request(
                    "GET",
                    feed_url,
                    headers=feed.get("headers"),
                    correlation_id=correlation_id,
                )
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
                items = root.findall(".//item") or root.findall(
                    ".//{http://www.w3.org/2005/Atom}entry"
                )
                for item in items:
                    title = self._text(item, "title")
                    url = self._link(item)
                    published = self._text(item, "pubDate") or self._text(
                        item, "updated"
                    )
                    if not title or not published:
                        continue
                    try:
                        when = parsedate_to_datetime(published)
                    except (TypeError, ValueError):
                        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    identity = hashlib.sha256(
                        f"{feed['institution']}|{title}|{published}".encode()
                    ).hexdigest()
                    records.append(
                        {
                            "document_id": identity,
                            "source": "central_banks",
                            "institution": feed["institution"],
                            "document_type": feed.get("document_type", "communication"),
                            "title": title,
                            "published_at": when,
                            "url": url,
                            "content": self._text(item, "description")
                            or self._text(item, "summary"),
                            "acquired_at": acquired_at,
                            "metadata": {"feed": feed_label},
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "feed": feed_label,
                        "error": safe_error_message(exc, provider="central_banks"),
                    }
                )
                logger.error(
                    "central_bank_feed_failed",
                    feed=feed_label,
                    institution=feed.get("institution"),
                    error=safe_error_message(exc, provider="central_banks"),
                    correlation_id=correlation_id,
                )
        if not records:
            raise CollectorNoData(
                "Central-bank feeds returned no documents", failed_feeds=failures
            )
        logger.info(
            "central_banks_collection_completed",
            state="partial" if failures else "success",
            feeds_configured=len(feeds),
            failed_feeds=failures,
            records=len(records),
            acquired_at=acquired_at.isoformat(),
            correlation_id=correlation_id,
        )
        return records

    @staticmethod
    def _text(item, name):
        node = item.find(name)
        if node is None:
            node = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
        return (node.text or "").strip() if node is not None else None

    @staticmethod
    def _link(item):
        value = CentralBanksCollector._text(item, "link")
        if value:
            return value
        node = item.find("{http://www.w3.org/2005/Atom}link")
        return node.get("href") if node is not None else None

    def health_check(self, config):
        started = time.monotonic()
        feeds = config["collectors"]["central_banks"].get("feeds", [])
        if not feeds:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No feeds configured",
                "latency_ms": 0,
            }
        try:
            feed_url = _validated_feed_url(
                feeds[0], config["collectors"]["central_banks"]
            )
            response = make_request(
                "GET",
                feed_url,
                headers=feeds[0].get("headers"),
                timeout=15,
            )
            return {
                "healthy": response.status_code < 400,
                "state": "success" if response.status_code < 400 else "failed",
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": safe_error_message(exc, provider="central_banks"),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config):
        return config["collectors"]["central_banks"]["schedule"]

    def get_target_table(self):
        return "source_documents"

    def get_conflict_columns(self):
        return ["document_id"]
