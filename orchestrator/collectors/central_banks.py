import hashlib
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from collectors.base import CollectorNoData, CollectorSetupRequired
from http_client import make_request
from logging_config import get_logger

logger = get_logger("collector.central_banks")


class CentralBanksCollector:
    source_id = "central_banks"

    def collect(self, config, correlation_id):
        records = []
        feeds = config["collectors"]["central_banks"].get("feeds", [])
        if not feeds:
            raise CollectorSetupRequired("No central-bank feeds are configured")
        acquired_at = datetime.now(timezone.utc)
        failures = []
        for feed in feeds:
            try:
                response = make_request("GET", feed["url"], correlation_id=correlation_id)
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
                items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                for item in items:
                    title = self._text(item, "title")
                    url = self._link(item)
                    published = self._text(item, "pubDate") or self._text(item, "updated")
                    if not title or not published:
                        continue
                    try:
                        when = parsedate_to_datetime(published)
                    except (TypeError, ValueError):
                        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    identity = hashlib.sha256(f"{feed['institution']}|{title}|{published}".encode()).hexdigest()
                    records.append({
                        "document_id": identity, "source": "central_banks",
                        "institution": feed["institution"],
                        "document_type": feed.get("document_type", "communication"),
                        "title": title, "published_at": when, "url": url,
                        "content": self._text(item, "description") or self._text(item, "summary"),
                        "acquired_at": acquired_at,
                        "metadata": {"feed": feed["url"]},
                    })
            except Exception as exc:
                failures.append({"feed": feed["url"], "error": str(exc)})
                logger.error(
                    "central_bank_feed_failed",
                    feed=feed["url"],
                    institution=feed.get("institution"),
                    error=str(exc),
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
        node = item.find(name) or item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
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
            response = make_request("GET", feeds[0]["url"], timeout=15)
            return {
                "healthy": response.status_code < 400,
                "state": "success" if response.status_code < 400 else "failed",
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic()-started)*1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic()-started)*1000),
            }

    def get_schedule(self, config): return config["collectors"]["central_banks"]["schedule"]
    def get_target_table(self): return "source_documents"
    def get_conflict_columns(self): return ["document_id"]
