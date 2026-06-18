import hashlib
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from http_client import make_request


class CentralBanksCollector:
    source_id = "central_banks"

    def collect(self, config, correlation_id):
        records = []
        for feed in config["collectors"]["central_banks"].get("feeds", []):
            response = make_request("GET", feed["url"], correlation_id=correlation_id)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in items:
                title = self._text(item, "title")
                url = self._text(item, "link") or item.findtext("{http://www.w3.org/2005/Atom}link")
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
                    "metadata": {"feed": feed["url"]},
                })
        return records

    @staticmethod
    def _text(item, name):
        node = item.find(name) or item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
        return (node.text or "").strip() if node is not None else None

    def health_check(self, config):
        started = time.monotonic()
        feeds = config["collectors"]["central_banks"].get("feeds", [])
        if not feeds:
            return {"healthy": False, "message": "No feeds configured", "latency_ms": 0}
        try:
            response = make_request("GET", feeds[0]["url"], timeout=15)
            return {"healthy": response.status_code < 400, "message": f"HTTP {response.status_code}", "latency_ms": int((time.monotonic()-started)*1000)}
        except Exception as exc:
            return {"healthy": False, "message": str(exc), "latency_ms": int((time.monotonic()-started)*1000)}

    def get_schedule(self, config): return config["collectors"]["central_banks"]["schedule"]
    def get_target_table(self): return "source_documents"
    def get_conflict_columns(self): return ["document_id"]
