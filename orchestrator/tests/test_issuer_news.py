"""Tests for the issuer_news collector and its issuer_feed ingestion engine.

All network behaviour is mocked: parsing is exercised against real
RSS/Atom/JSON-LD fixtures through the pure functions, the pinned-transport
fetch path is driven with a fake streaming client, and the collector's
fetch seam is patched. No live network calls are made.
"""

import json
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from collectors.base import CollectorSetupRequired
from collectors.issuer_news import IssuerNewsCollector
from contracts.runtime_config import CollectorConfig
from sources.issuer_feed import (
    FeedFetch,
    FeedHTTPError,
    FeedMalformedError,
    FeedOversizeError,
    FeedRedirectError,
    FeedUnsafeOriginError,
    FeedUnsupportedKindError,
    canonicalize_url,
    dedupe_records,
    document_id,
    extract_primary_page_text,
    fetch_feed,
    normalize_feed_records,
    parse_feed_items,
    parse_sec_edgar_title,
)


def _public_dns(host, port, *args, **kwargs):
    """Resolve test hostnames publicly; IP literals resolve to themselves so
    private-address origins fail validation as they would in production."""
    try:
        socket.inet_aton(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 443))]
    except OSError:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ]


def _dns_patch():
    return patch("socket.getaddrinfo", side_effect=_public_dns)


def _feed_url(feed_name: str) -> str:
    return f"https://ir.example.test/{feed_name}.xml"


def _rss_body(*items: dict) -> bytes:
    parts = [
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'><channel>"
        "<title>Test feed</title><link>https://ir.example.test/</link>"
    ]
    for item in items:
        parts.append("<item>")
        for key, value in item.items():
            parts.append(f"<{key}>{value}</{key}>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


RSS20_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Acme IR</title>
    <link>https://ir.example.test/</link>
    <description>Investor news</description>
    <item>
      <title>Acme Announces Q3 Results</title>
      <link>https://ir.example.test/news/q3-results?utm_source=rss&amp;utm_medium=feed</link>
      <guid isPermaLink="false">acme-q3-2026</guid>
      <pubDate>Fri, 07 Aug 2026 12:00:00 +0000</pubDate>
      <description>Acme &lt;b&gt;reported&lt;/b&gt; strong third-quarter results.</description>
      <content:encoded><![CDATA[<p>Full text of the release with <strong>markup</strong>.</p>]]></content:encoded>
    </item>
    <item>
      <title>Acme Board Appoints New Director</title>
      <link>https://ir.example.test/news/board-appointment</link>
      <dc:date>2026-08-10T09:30:00Z</dc:date>
      <description>Board appointment details.</description>
    </item>
  </channel>
</rss>"""

RDF_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://gov.example.test/feed">
    <title>Regulator Updates</title>
    <link>https://gov.example.test/</link>
  </channel>
  <item rdf:about="https://gov.example.test/updates/42">
    <title>New Rule Published</title>
    <link>https://gov.example.test/updates/42</link>
    <dc:date>2026-08-01T10:00:00Z</dc:date>
    <description>Rule text summary.</description>
  </item>
</rdf:RDF>"""

ATOM_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Exchange News</title>
  <id>tag:example.test,2026:feed</id>
  <entry>
    <title>Listing Rule Change Effective</title>
    <link rel="alternate" href="https://exchange.example.test/regulation/rule-change"/>
    <id>tag:example.test,2026:rule-change</id>
    <published>2026-08-12T14:00:00+00:00</published>
    <updated>2026-08-12T15:30:00+00:00</updated>
    <summary type="html">&lt;p&gt;Rule change summary&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>New Member Admission</title>
    <link href="https://exchange.example.test/members/new-admission"/>
    <updated>2026-08-13T08:00:00Z</updated>
    <content type="html">&lt;p&gt;Content text.&lt;/p&gt;</content>
  </entry>
</feed>"""

JSONLD_GRAPH_BODY = json.dumps(
    {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "NewsArticle",
                "headline": "Company A Files Annual Report",
                "url": "https://ir.example.test/reports/annual-2026",
                "datePublished": "2026-08-11T07:00:00Z",
                "description": "Summary of the annual report.",
                "publisher": {"name": "Company A IR"},
                "author": {"name": "Company A"},
            },
            {
                "@type": "Organization",
                "name": "Company A",
                "url": "https://ir.example.test/",
            },
        ],
    }
).encode("utf-8")

JSONLD_ITEMLIST = {
    "@type": "ItemList",
    "itemListElement": [
        {
            "@type": "ListItem",
            "position": 1,
            "item": {
                "@type": "NewsArticle",
                "headline": "Update One",
                "url": "https://ir.example.test/one",
                "datePublished": "2026-08-10T09:00:00Z",
            },
        },
        {
            "@type": "NewsArticle",
            "headline": "Update Two",
            "url": "https://ir.example.test/two",
            "datePublished": "2026-08-10T10:00:00Z",
        },
    ],
}

HTML_BODY = b"""<!DOCTYPE html>
<html><head>
<script type="application/ld+json">
{"@type": "WebPage", "name": "IR News", "url": "https://ir.example.test/news",
 "dateModified": "2026-08-14T12:00:00Z", "description": "Latest investor news."}
</script>
<script type="application/ld+json">
{"@graph": [
  {"@type": "NewsArticle", "headline": "Update A",
   "url": "https://ir.example.test/a", "datePublished": "2026-08-14T08:00:00Z"},
  {"@type": "WebSite", "name": "Example", "url": "https://ir.example.test/"}
]}
</script>
</head><body></body></html>"""


class IssuerFeedParsingTests(unittest.TestCase):
    """RSS/Atom/JSON-LD fixtures normalise deterministically."""

    def test_rss20_normalises_deterministically(self):
        first = parse_feed_items(RSS20_BODY, {"kind": "rss"})
        second = parse_feed_items(RSS20_BODY, {"kind": "rss"})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)

        item = first[0]
        self.assertEqual(item["title"], "Acme Announces Q3 Results")
        self.assertEqual(
            item["url"],
            "https://ir.example.test/news/q3-results?utm_source=rss&utm_medium=feed",
        )
        self.assertEqual(
            item["canonical_url"], "https://ir.example.test/news/q3-results"
        )
        self.assertEqual(item["guid"], "acme-q3-2026")
        self.assertEqual(item["published"], datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
        self.assertIsNone(item["published_fallback"])
        self.assertEqual(item["content"], "Full text of the release with markup.")
        self.assertEqual(item["source_kind"], "rss")

        second_item = first[1]
        self.assertEqual(
            second_item["published"], datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
        )
        self.assertEqual(
            second_item["canonical_url"],
            "https://ir.example.test/news/board-appointment",
        )

    def test_rss1_rdf_normalises(self):
        items = parse_feed_items(RDF_BODY, {"kind": "feed"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "New Rule Published")
        self.assertEqual(
            items[0]["canonical_url"], "https://gov.example.test/updates/42"
        )
        self.assertEqual(items[0]["published"], datetime(2026, 8, 1, 10, 0, tzinfo=UTC))

    def test_atom_normalises(self):
        items = parse_feed_items(ATOM_BODY, {"kind": "atom"})
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Listing Rule Change Effective")
        self.assertEqual(
            items[0]["canonical_url"],
            "https://exchange.example.test/regulation/rule-change",
        )
        self.assertEqual(items[0]["guid"], "tag:example.test,2026:rule-change")
        self.assertEqual(
            items[0]["published"], datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
        )
        self.assertEqual(items[0]["updated"], datetime(2026, 8, 12, 15, 30, tzinfo=UTC))
        self.assertEqual(items[0]["content"], "Rule change summary")
        self.assertEqual(items[0]["source_kind"], "atom")

        # No explicit published: updated is the source time, marked as fallback.
        self.assertEqual(items[1]["published"], datetime(2026, 8, 13, 8, 0, tzinfo=UTC))
        self.assertEqual(items[1]["published_fallback"], "updated")
        self.assertEqual(items[1]["content"], "Content text.")

    def test_jsonld_direct_normalises(self):
        items = parse_feed_items(JSONLD_GRAPH_BODY, {"kind": "jsonld"})
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "Company A Files Annual Report")
        self.assertEqual(
            item["canonical_url"], "https://ir.example.test/reports/annual-2026"
        )
        self.assertEqual(item["published"], datetime(2026, 8, 11, 7, 0, tzinfo=UTC))
        self.assertEqual(item["content"], "Summary of the annual report.")
        self.assertEqual(item["publisher"], "Company A IR")
        self.assertEqual(item["author"], "Company A")
        self.assertEqual(item["source_kind"], "jsonld")

    def test_jsonld_itemlist_normalises(self):
        items = parse_feed_items(
            json.dumps(JSONLD_ITEMLIST).encode("utf-8"), {"kind": "jsonld"}
        )
        self.assertEqual(
            [item["title"] for item in items], ["Update One", "Update Two"]
        )
        self.assertEqual(
            [item["canonical_url"] for item in items],
            ["https://ir.example.test/one", "https://ir.example.test/two"],
        )

    def test_html_jsonld_discovery_normalises(self):
        items = parse_feed_items(HTML_BODY, {"kind": "html"})
        self.assertEqual(len(items), 2)
        page, article = items
        self.assertEqual(page["title"], "IR News")
        self.assertEqual(page["canonical_url"], "https://ir.example.test/news")
        self.assertEqual(page["published"], datetime(2026, 8, 14, 12, 0, tzinfo=UTC))
        self.assertEqual(page["published_fallback"], "dateModified")
        self.assertEqual(article["title"], "Update A")

    def test_html_page_without_jsonld_is_empty_and_valid(self):
        body = b"<html><head><title>No data</title></head><body></body></html>"
        self.assertEqual(parse_feed_items(body, {"kind": "html"}), [])

    def test_canonicalize_url_strips_tracking_noise(self):
        self.assertEqual(
            canonicalize_url(
                "https://IR.Example.TEST/news/q3?utm_source=rss&a=2&a=1&b=x#frag"
            ),
            "https://ir.example.test/news/q3?a=1&a=2&b=x",
        )
        self.assertEqual(
            canonicalize_url("https://ir.example.test:443/x"),
            "https://ir.example.test/x",
        )
        self.assertEqual(
            canonicalize_url("https://ir.example.test"), "https://ir.example.test/"
        )
        self.assertEqual(
            canonicalize_url("https://ir.example.test/a?fbclid=zz&c=1"),
            "https://ir.example.test/a?c=1",
        )

    def test_document_id_is_stable_and_discriminating(self):
        canonical = "https://ir.example.test/q3"
        self.assertEqual(
            document_id("issuer_news", canonical),
            document_id("issuer_news", canonical),
        )
        self.assertNotEqual(
            document_id("issuer_news", canonical),
            document_id("issuer_news", canonical + "/extra"),
        )

    def test_malformed_xml_fails_explicitly(self):
        with self.assertRaises(FeedMalformedError):
            parse_feed_items(b"this is not xml", {"kind": "rss"})

    def test_dtd_and_entities_are_rejected(self):
        body = (
            b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]>'
            b"<rss version='2.0'><channel><item><title>&x;</title></item></channel></rss>"
        )
        with self.assertRaises(FeedMalformedError):
            parse_feed_items(body, {"kind": "rss"})

    def test_malformed_jsonld_fails_explicitly(self):
        with self.assertRaises(FeedMalformedError):
            parse_feed_items(b"{not json", {"kind": "jsonld"})

    def test_unsupported_kind_fails_explicitly(self):
        with self.assertRaises(FeedUnsupportedKindError):
            parse_feed_items(RSS20_BODY, {"kind": "carrier-pigeon"})

    def test_item_without_publish_time_is_skipped_with_reason(self):
        body = _rss_body(
            {
                "title": "Dated item",
                "link": "https://ir.example.test/dated",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            },
            {"title": "Dateless item", "link": "https://ir.example.test/dateless"},
        )
        raw = parse_feed_items(body, {"kind": "rss"})
        self.assertEqual(len(raw), 2)
        self.assertIsNone(raw[1]["published"])

        fetch = FeedFetch(200, _feed_url("acme"), body, etag="e1", last_modified="lm1")
        records, skipped = normalize_feed_records(
            raw,
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "document_type": "issuer_update",
            },
            source="issuer_news",
            acquired_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            observed_at=datetime(2026, 8, 15, 6, 0, 30, tzinfo=UTC),
            fetch=fetch,
            feed_url=_feed_url("acme"),
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(skipped, {"missing_date": 1})

    def test_normalized_records_carry_full_source_documents_contract(self):
        raw = parse_feed_items(RSS20_BODY, {"kind": "rss"})
        acquired_at = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)
        fetch = FeedFetch(
            200, _feed_url("acme"), RSS20_BODY, etag="e1", last_modified="lm1"
        )
        records, skipped = normalize_feed_records(
            raw,
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "document_type": "issuer_update",
                "content_role": "primary",
            },
            source="issuer_news",
            acquired_at=acquired_at,
            observed_at=acquired_at,
            fetch=fetch,
            feed_url=_feed_url("acme"),
        )
        self.assertEqual(skipped, {})
        self.assertEqual(len(records), 2)
        record = records[0]
        self.assertEqual(
            set(record),
            {
                "document_id",
                "source",
                "institution",
                "document_type",
                "title",
                "published_at",
                "url",
                "content",
                "acquired_at",
                "metadata",
            },
        )
        self.assertEqual(record["source"], "issuer_news")
        self.assertEqual(record["institution"], "Acme Corp")
        self.assertEqual(record["document_type"], "issuer_update")
        self.assertEqual(
            record["document_id"],
            document_id("issuer_news", "https://ir.example.test/news/q3-results"),
        )
        self.assertEqual(record["url"], "https://ir.example.test/news/q3-results")
        metadata = record["metadata"]
        self.assertEqual(metadata["origin"]["feed"], "acme_ir")
        self.assertEqual(metadata["origin"]["role"], "primary")
        self.assertTrue(metadata["primary"])
        self.assertEqual(
            metadata["raw_url"], record["url"] + "?utm_source=rss&utm_medium=feed"
        )
        self.assertEqual(metadata["guid"], "acme-q3-2026")
        self.assertEqual(
            metadata["source_time"]["published_at"], "2026-08-07T12:00:00+00:00"
        )
        self.assertEqual(
            metadata["source_time"]["raw_published"], "Fri, 07 Aug 2026 12:00:00 +0000"
        )
        self.assertEqual(
            metadata["acquisition"]["acquired_at"], acquired_at.isoformat()
        )
        self.assertEqual(
            metadata["acquisition"]["observed_at"], acquired_at.isoformat()
        )
        self.assertEqual(metadata["acquisition"]["status_code"], 200)
        self.assertEqual(metadata["acquisition"]["etag"], "e1")
        self.assertEqual(metadata["acquisition"]["last_modified"], "lm1")

    def test_derivative_role_is_labeled(self):
        raw = parse_feed_items(RSS20_BODY, {"kind": "rss"})
        fetch = FeedFetch(200, _feed_url("synd"), RSS20_BODY)
        records, _ = normalize_feed_records(
            raw,
            {
                "name": "syndicated",
                "institution": "Aggregator",
                "content_role": "derivative",
            },
            source="issuer_news",
            acquired_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            observed_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            fetch=fetch,
            feed_url=_feed_url("synd"),
        )
        self.assertEqual(records[0]["metadata"]["content_role"], "derivative")
        self.assertFalse(records[0]["metadata"]["primary"])

    def test_primary_page_text_prefers_jsonld_article_body(self):
        body = b"""
        <html><body><nav>Navigation</nav><article><p>Short visible copy.</p></article>
        <script type="application/ld+json">
          {"@type":"NewsArticle","articleBody":"Complete primary release body with details."}
        </script></body></html>
        """
        self.assertEqual(
            extract_primary_page_text(body, 100_000),
            "Complete primary release body with details.",
        )

    def test_primary_page_text_falls_back_to_semantic_article(self):
        body = b"""
        <html><body><header>Menu</header><article>
          <h1>Results</h1><p>Revenue increased.</p><p>Margins expanded.</p>
        </article><footer>Legal links</footer></body></html>
        """
        self.assertEqual(
            extract_primary_page_text(body, 100_000),
            "Results\nRevenue increased.\nMargins expanded.",
        )


class IssuerFeedDedupeTests(unittest.TestCase):
    def _record(
        self, feed_name: str, role: str, url: str = "https://ir.example.test/release"
    ):
        return {
            "document_id": document_id("issuer_news", url),
            "source": "issuer_news",
            "institution": "Acme Corp",
            "document_type": "issuer_update",
            "title": "Acme Release",
            "published_at": datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
            "url": url,
            "content": "text",
            "acquired_at": datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            "metadata": {
                "origin": {
                    "feed": feed_name,
                    "feed_url": _feed_url(feed_name),
                    "kind": "rss",
                    "role": role,
                },
                "canonical_url": url,
                "raw_url": url,
                "content_role": role,
                "primary": role == "primary",
            },
        }

    def test_same_release_in_two_feeds_has_one_identity(self):
        primary = self._record("company_ir", "primary")
        derivative = self._record("syndicator", "derivative")
        merged = dedupe_records([primary, derivative])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["document_id"], primary["document_id"])
        self.assertEqual(merged[0]["metadata"]["origin"]["feed"], "company_ir")
        aliases = merged[0]["metadata"]["aliases"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["feed"], "syndicator")
        self.assertEqual(aliases[0]["role"], "derivative")

    def test_primary_wins_over_derivative_regardless_of_order(self):
        derivative = self._record("syndicator", "derivative")
        primary = self._record("company_ir", "primary")
        merged = dedupe_records([derivative, primary])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["metadata"]["origin"]["feed"], "company_ir")
        self.assertEqual(merged[0]["metadata"]["aliases"][0]["feed"], "syndicator")

    def test_first_occurrence_wins_for_equal_roles(self):
        first = self._record("feed_a", "primary")
        second = self._record("feed_b", "primary")
        merged = dedupe_records([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["metadata"]["origin"]["feed"], "feed_a")
        self.assertEqual(merged[0]["metadata"]["aliases"][0]["feed"], "feed_b")


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"", chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._chunks = chunks

    def iter_bytes(self):
        if self._chunks is not None:
            return iter(self._chunks)
        return iter([self._body])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(404)


class IssuerFeedFetchTests(unittest.TestCase):
    """Redirects, oversize, malformed, and conditional behaviour fail safely."""

    FEED_URL = "https://ir.example.test/feed.xml"

    def test_same_origin_redirect_is_followed(self):
        client = FakeClient(
            FakeResponse(302, {"location": "/final.xml"}),
            FakeResponse(200, {}, RSS20_BODY),
        )
        fetch = fetch_feed(self.FEED_URL, client=client)
        self.assertEqual(fetch.status_code, 200)
        self.assertEqual(fetch.final_url, "https://ir.example.test/final.xml")
        self.assertEqual(fetch.hops, 1)
        self.assertEqual(fetch.body, RSS20_BODY)
        self.assertEqual(
            [call["url"] for call in client.calls],
            [self.FEED_URL, "https://ir.example.test/final.xml"],
        )

    def test_cross_origin_redirect_fails_closed(self):
        client = FakeClient(
            FakeResponse(302, {"location": "https://evil.test/feed.xml"})
        )
        with self.assertRaises(FeedUnsafeOriginError):
            fetch_feed(self.FEED_URL, client=client)
        self.assertEqual(len(client.calls), 1)

    def test_https_downgrade_redirect_fails_closed(self):
        client = FakeClient(
            FakeResponse(302, {"location": "http://ir.example.test/feed.xml"})
        )
        with self.assertRaises(FeedUnsafeOriginError):
            fetch_feed(self.FEED_URL, client=client)

    def test_redirect_chain_limit_is_bounded(self):
        client = FakeClient(
            *(FakeResponse(302, {"location": "/hop"}) for _ in range(6))
        )
        with self.assertRaises(FeedRedirectError):
            fetch_feed(self.FEED_URL, client=client, max_redirects=5)

    def test_oversize_body_fails_explicitly(self):
        client = FakeClient(FakeResponse(200, {}, chunks=[b"x" * 100]))
        with self.assertRaises(FeedOversizeError):
            fetch_feed(self.FEED_URL, client=client, cap=50)

    def test_304_yields_empty_body_and_uses_conditional_headers(self):
        client = FakeClient(
            FakeResponse(
                304, {"etag": "abc", "last-modified": "Wed, 12 Aug 2026 00:00:00 GMT"}
            )
        )
        fetch = fetch_feed(
            self.FEED_URL,
            client=client,
            conditional={
                "etag": "abc",
                "last_modified": "Wed, 12 Aug 2026 00:00:00 GMT",
            },
        )
        self.assertEqual(fetch.status_code, 304)
        self.assertIsNone(fetch.body)
        self.assertEqual(fetch.etag, "abc")
        sent = client.calls[0]["kwargs"]["headers"]
        self.assertEqual(sent["If-None-Match"], "abc")
        self.assertEqual(sent["If-Modified-Since"], "Wed, 12 Aug 2026 00:00:00 GMT")

    def test_http_errors_carry_deterministic_classes(self):
        with self.assertRaises(FeedHTTPError) as raised:
            fetch_feed(self.FEED_URL, client=FakeClient(FakeResponse(404)))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.error_class, "invalid_source_data")
        with self.assertRaises(FeedHTTPError) as raised:
            fetch_feed(self.FEED_URL, client=FakeClient(FakeResponse(503)))
        self.assertEqual(raised.exception.error_class, "transient_source")

    def test_default_user_agent_is_descriptive(self):
        client = FakeClient(FakeResponse(200, {}, b"<rss/>"))
        fetch_feed(self.FEED_URL, client=client)
        user_agent = client.calls[0]["kwargs"]["headers"]["User-Agent"]
        self.assertIn("TradingDataPlatform", user_agent)


class IssuerNewsCollectorTests(unittest.TestCase):
    def setUp(self):
        self._dns = _dns_patch()
        self._dns.start()
        self.addCleanup(self._dns.stop)

    def _config(self, feeds=None, state_path=None):
        config = {
            "collectors": {
                "issuer_news": {
                    "schedule": "0 6 * * *",
                    "feeds": feeds
                    or [
                        {
                            "name": "acme_ir",
                            "institution": "Acme Corp",
                            "document_type": "issuer_update",
                            "kind": "rss",
                            "url": _feed_url("acme"),
                        },
                        {
                            "name": "sec_updates",
                            "institution": "SEC",
                            "document_type": "regulatory_update",
                            "kind": "rss",
                            "url": _feed_url("sec"),
                        },
                    ],
                }
            }
        }
        if state_path:
            config["collectors"]["issuer_news"]["state_path"] = state_path
        return config

    def test_collect_accepts_validated_runtime_config_mapping(self):
        section = CollectorConfig(
            feeds=[
                {
                    "name": "acme_ir",
                    "institution": "Acme Corp",
                    "kind": "feed",
                    "url": _feed_url("acme"),
                }
            ]
        )
        body = _rss_body(
            {
                "title": "Acme Q3 Results",
                "link": "https://ir.example.test/release/q3",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("acme"), body),
        ):
            result = IssuerNewsCollector().collect(
                {"collectors": {"issuer_news": section}}, "typed-config"
            )

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.successful_series, 1)

    def test_collector_normalizes_and_deduplicates_across_feeds(self):
        body_one = _rss_body(
            {
                "title": "Acme Q3 Results",
                "link": "https://ir.example.test/release/q3?utm_source=rss",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
                "description": "Q3 summary.",
            }
        )
        body_two = _rss_body(
            {
                "title": "Acme Q3 Results",
                "link": "https://ir.example.test/release/q3",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("acme"), body_one),
                FeedFetch(200, _feed_url("sec"), body_two),
            ],
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(), "corr-1")

        self.assertEqual(result.errors, [])
        self.assertEqual(result.total_series, 2)
        self.assertEqual(result.successful_series, 2)
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(
            record["document_id"],
            document_id("issuer_news", "https://ir.example.test/release/q3"),
        )
        self.assertEqual(record["url"], "https://ir.example.test/release/q3")
        self.assertEqual(record["institution"], "Acme Corp")
        self.assertEqual(record["document_type"], "issuer_update")
        aliases = record["metadata"]["aliases"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["feed"], "sec_updates")
        self.assertEqual(result.metrics["records"], 1)
        self.assertEqual(result.metrics["feeds_succeeded"], 2)
        self.assertEqual(fetch.call_count, 2)

    def test_collector_enriches_same_origin_items_with_full_text(self):
        feed_body = _rss_body(
            {
                "title": "Acme Q3 Results",
                "link": "https://ir.example.test/release/q3",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
                "description": "Short summary.",
            }
        )
        page_body = b"""
        <html><body><main><h1>Acme Q3 Results</h1>
        <p>Complete official release body with operating details and guidance.</p>
        </main></body></html>
        """
        feeds = [
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "document_type": "issuer_update",
                "kind": "rss",
                "url": _feed_url("acme"),
                "fetch_full_text": True,
                "max_content_chars": 100_000,
            }
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("acme"), feed_body),
                FeedFetch(200, "https://ir.example.test/release/q3", page_body),
            ],
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(feeds), "full-text")

        self.assertEqual(fetch.call_count, 2)
        self.assertIn("Complete official release body", result.records[0]["content"])
        extraction = result.records[0]["metadata"]["content_extraction"]
        self.assertEqual(extraction["method"], "linked_primary_page")
        self.assertEqual(extraction["feed_content_chars"], len("Short summary."))
        self.assertEqual(result.metrics["full_text_attempted"], 1)
        self.assertEqual(result.metrics["full_text_fetched"], 1)
        self.assertEqual(result.metrics["full_text_failed"], 0)
        self.assertEqual(result.metrics["api_calls_made"], 2)

    def test_collector_never_fetches_cross_origin_item_content(self):
        feed_body = _rss_body(
            {
                "title": "Syndicated release",
                "link": "https://cdn.example.test/release",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
                "description": "Primary feed summary.",
            }
        )
        feeds = [
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "kind": "rss",
                "url": _feed_url("acme"),
                "fetch_full_text": True,
            }
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("acme"), feed_body),
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(feeds), "cross-origin")

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result.records[0]["content"], "Primary feed summary.")
        self.assertEqual(result.metrics["full_text_attempted"], 0)

    def test_collector_fetches_explicit_allowlisted_content_origin(self):
        feed_body = _rss_body(
            {
                "title": "Official engineering release",
                "link": "https://dev.example.test/release",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
                "description": "Short summary.",
            }
        )
        page_body = (
            b"<html><main><p>Complete engineering release body.</p></main></html>"
        )
        feeds = [
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "kind": "rss",
                "url": _feed_url("acme"),
                "fetch_full_text": True,
                "content_origins": ["https://dev.example.test/"],
            }
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("acme"), feed_body),
                FeedFetch(200, "https://dev.example.test/release", page_body),
            ],
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(feeds), "allowlisted")

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            result.records[0]["content"], "Complete engineering release body."
        )
        self.assertEqual(result.metrics["full_text_fetched"], 1)

    def test_collector_primary_wins_over_derivative_feed(self):
        derivative_body = _rss_body(
            {
                "title": "Acme Q3 Results",
                "link": "https://ir.example.test/release/q3",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        feeds = [
            {
                "name": "syndicator",
                "institution": "Aggregator",
                "document_type": "issuer_update",
                "content_role": "derivative",
                "kind": "rss",
                "url": _feed_url("synd"),
            },
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "document_type": "issuer_update",
                "kind": "rss",
                "url": _feed_url("acme"),
            },
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("synd"), derivative_body),
                FeedFetch(200, _feed_url("acme"), derivative_body),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(feeds), "corr-2")

        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record["metadata"]["origin"]["feed"], "acme_ir")
        self.assertTrue(record["metadata"]["primary"])
        self.assertEqual(record["metadata"]["aliases"][0]["feed"], "syndicator")

    def test_empty_feed_is_valid(self):
        empty = b"<rss version='2.0'><channel><title>empty</title></channel></rss>"
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("acme"), empty),
                FeedFetch(200, _feed_url("sec"), empty),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(), "corr-3")

        self.assertEqual(result.records, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.successful_series, 2)
        self.assertFalse(result.all_failed)
        self.assertEqual(result.metrics["records"], 0)

    def test_malformed_feed_fails_explicitly_and_others_survive(self):
        good = _rss_body(
            {
                "title": "Good Item",
                "link": "https://ir.example.test/good",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedFetch(200, _feed_url("acme"), b"this is not xml"),
                FeedFetch(200, _feed_url("sec"), good),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(), "corr-4")

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.successful_series, 1)
        self.assertTrue(result.partial_failure)
        error = result.errors[0]
        self.assertEqual(error["feed"], "acme_ir")
        self.assertEqual(error["stage"], "parse")
        self.assertEqual(error["code"], "malformed_feed")
        self.assertEqual(error["error_class"], "invalid_source_data")

    def test_unsafe_origin_fails_without_fetch(self):
        feeds = [
            {
                "name": "plain_http",
                "institution": "Acme Corp",
                "url": "http://ir.example.test/feed",
            },
            {
                "name": "private_host",
                "institution": "Acme Corp",
                "url": "https://127.0.0.1/feed",
            },
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=AssertionError("must not fetch an unsafe origin"),
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(feeds), "corr-5")

        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(
            all(error["code"] == "invalid_origin" for error in result.errors)
        )
        self.assertTrue(
            all(
                error["error_class"] == "invalid_source_data" for error in result.errors
            )
        )
        self.assertTrue(result.all_failed)

    def test_oversize_feed_fails_explicitly(self):
        good = _rss_body(
            {
                "title": "Good Item",
                "link": "https://ir.example.test/good",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedOversizeError("feed body exceeds 5000000 bytes"),
                FeedFetch(200, _feed_url("sec"), good),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(), "corr-6")

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.errors[0]["feed"], "acme_ir")
        self.assertEqual(result.errors[0]["code"], "feed_oversize")
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    def test_transport_failure_is_transient(self):
        good = _rss_body(
            {
                "title": "Good Item",
                "link": "https://ir.example.test/good",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                httpx.ConnectError("boom"),
                FeedFetch(200, _feed_url("sec"), good),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(), "corr-7")

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.errors[0]["code"], "request_failed")
        self.assertEqual(result.errors[0]["error_class"], "transient_source")

    def test_skipped_items_are_reported_explicitly(self):
        body = _rss_body(
            {
                "title": "Dated item",
                "link": "https://ir.example.test/dated",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            },
            {"title": "Dateless item", "link": "https://ir.example.test/dateless"},
        )
        feeds = [
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "kind": "rss",
                "url": _feed_url("acme"),
            }
        ]
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("acme"), body),
        ):
            result = IssuerNewsCollector().collect(self._config(feeds), "corr-8")

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.errors[0]["code"], "items_skipped")
        self.assertEqual(result.errors[0]["detail"], {"missing_date": 1})
        self.assertEqual(result.metrics["items_skipped"], 1)

    def test_conditional_state_roundtrip(self):
        body = _rss_body(
            {
                "title": "Item",
                "link": "https://ir.example.test/item",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            with patch(
                "collectors.issuer_news.fetch_feed",
                side_effect=[
                    FeedFetch(200, _feed_url("acme"), body, etag="etag-1"),
                    FeedFetch(200, _feed_url("sec"), body, etag="etag-sec"),
                ],
            ):
                first = IssuerNewsCollector().collect(
                    self._config(state_path=state_path), "corr-9"
                )

            self.assertEqual(len(first.records), 1)
            state = json.loads(Path(state_path).read_text())
            self.assertEqual(state["acme_ir"]["etag"], "etag-1")
            self.assertEqual(state["sec_updates"]["etag"], "etag-sec")
            self.assertIn("last_polled_at", state["acme_ir"])

            def conditional_fetch(url, **kwargs):
                conditional = kwargs.get("conditional") or {}
                if url == _feed_url("acme"):
                    self.assertEqual(conditional.get("etag"), "etag-1")
                    return FeedFetch(304, url, None, etag="etag-1")
                self.assertEqual(conditional.get("etag"), "etag-sec")
                return FeedFetch(304, url, None, etag="etag-sec")

            with patch(
                "collectors.issuer_news.fetch_feed",
                side_effect=conditional_fetch,
            ):
                second = IssuerNewsCollector().collect(
                    self._config(state_path=state_path), "corr-10"
                )

            self.assertEqual(second.records, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(second.successful_series, 2)
            self.assertEqual(second.metrics["conditional_not_modified"], 2)

    def test_unsafe_redirect_from_fetch_fails_as_feed_error(self):
        good = _rss_body(
            {
                "title": "Good Item",
                "link": "https://ir.example.test/good",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=[
                FeedUnsafeOriginError("redirect leaves the configured origin"),
                FeedFetch(200, _feed_url("sec"), good),
            ],
        ):
            result = IssuerNewsCollector().collect(self._config(), "corr-11")

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.errors[0]["code"], "unsafe_origin")
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    def test_no_enabled_feeds_raises_setup_required(self):
        with self.assertRaises(CollectorSetupRequired):
            IssuerNewsCollector().collect(
                {"collectors": {"issuer_news": {"feeds": []}}}, "corr-12"
            )
        with self.assertRaises(CollectorSetupRequired):
            IssuerNewsCollector().collect({}, "corr-13")

    def test_disabled_feeds_are_skipped(self):
        feeds = [
            {
                "name": "disabled_feed",
                "institution": "Acme Corp",
                "enabled": False,
                "kind": "rss",
                "url": _feed_url("disabled"),
            },
            {
                "name": "acme_ir",
                "institution": "Acme Corp",
                "kind": "rss",
                "url": _feed_url("acme"),
            },
        ]
        empty = b"<rss version='2.0'><channel><title>empty</title></channel></rss>"
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("acme"), empty),
        ) as fetch:
            result = IssuerNewsCollector().collect(self._config(feeds), "corr-14")

        self.assertEqual(result.total_series, 1)
        self.assertEqual(fetch.call_count, 1)

    def test_collector_sec_edgar_entity_parsing(self):
        feeds = [
            {
                "name": "sec_current_8k",
                "institution": "sec",
                "document_type": "regulatory_update",
                "kind": "rss",
                "entity_parser": "sec_edgar",
                "cik_symbols": {"0000789019": "MSFT"},
                "url": _feed_url("sec"),
            }
        ]
        body = _rss_body(
            {
                "title": "8-K - Microsoft Corp (0000789019) (Filer)",
                "link": "https://www.sec.gov/archives/edgar/data/789019/1",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("sec"), body),
        ):
            result = IssuerNewsCollector().collect(self._config(feeds), "corr-15")

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record["institution"], "Microsoft Corp")
        self.assertEqual(record["document_type"], "regulatory_update")
        self.assertEqual(record["metadata"]["cik"], "0000789019")
        self.assertEqual(record["metadata"]["ticker"], "MSFT")

    def test_health_check(self):
        with patch(
            "collectors.issuer_news.fetch_feed",
            return_value=FeedFetch(200, _feed_url("acme"), b"<rss/>"),
        ) as fetch:
            health = IssuerNewsCollector().health_check(self._config())
        self.assertTrue(health["healthy"])
        self.assertEqual(health["state"], "success")
        self.assertEqual(health["message"], "HTTP 200")
        self.assertIn("latency_ms", health)
        fetch.assert_called_once()

        with patch(
            "collectors.issuer_news.fetch_feed",
            side_effect=FeedHTTPError(503),
        ):
            health = IssuerNewsCollector().health_check(self._config())
        self.assertFalse(health["healthy"])
        self.assertEqual(health["state"], "failed")
        self.assertEqual(health["message"], "http_status")

        health = IssuerNewsCollector().health_check(
            {"collectors": {"issuer_news": {"feeds": []}}}
        )
        self.assertFalse(health["healthy"])
        self.assertEqual(health["state"], "setup_required")

        health = IssuerNewsCollector().health_check({})
        self.assertFalse(health["healthy"])
        self.assertEqual(health["state"], "setup_required")


class IssuerNewsContractTests(unittest.TestCase):
    def test_collector_contract_methods(self):
        collector = IssuerNewsCollector()
        config = {"collectors": {"issuer_news": {"schedule": "25 7 * * *"}}}
        self.assertEqual(collector.source_id, "issuer_news")
        self.assertEqual(collector.get_schedule(config), "25 7 * * *")
        self.assertEqual(collector.get_target_table(), "source_documents")
        self.assertEqual(collector.get_conflict_columns(), ["document_id"])


class SecEdgarEntityParsingTests(unittest.TestCase):
    """SEC EDGAR title entity parsing is opt-in and never invents entities."""

    def test_parse_sec_edgar_title_valid_shapes(self):
        parsed = parse_sec_edgar_title("8-K - Microsoft Corp (0000789019) (Filer)")
        self.assertEqual(
            parsed,
            {
                "form": "8-K",
                "company": "Microsoft Corp",
                "cik": "0000789019",
                "role": "Filer",
            },
        )
        parsed = parse_sec_edgar_title("10-Q - Apple Inc. (0000320193) (Filer)")
        self.assertEqual(parsed["form"], "10-Q")
        self.assertEqual(parsed["company"], "Apple Inc.")
        self.assertEqual(parsed["cik"], "0000320193")

    def test_parse_sec_edgar_title_malformed_returns_none(self):
        for title in (
            "8-K - Microsoft Corp (0000789019)",  # missing role
            "Microsoft Corp (0000789019) (Filer)",  # missing form separator
            "8-K - Microsoft Corp (789019) (Filer)",  # CIK not 10 digits
            "8-K - Microsoft Corp (0000789019) (Filer) extra",  # trailing text
            "8-K -  (0000789019) (Filer)",  # empty company
            "Press Release",
        ):
            with self.subTest(title=title):
                self.assertIsNone(parse_sec_edgar_title(title))

    def _normalize_with_parser(self, title, entity_parser=None, cik_symbols=None):
        body = _rss_body(
            {
                "title": title,
                "link": "https://www.sec.gov/archives/edgar/data/789019/1",
                "pubDate": "Fri, 07 Aug 2026 12:00:00 +0000",
            }
        )
        raw = parse_feed_items(body, {"kind": "rss"})
        feed = {
            "name": "sec_current_8k",
            "institution": "sec",
            "document_type": "regulatory_update",
            "kind": "rss",
        }
        if entity_parser is not None:
            feed["entity_parser"] = entity_parser
        if cik_symbols is not None:
            feed["cik_symbols"] = cik_symbols
        records, skipped = normalize_feed_records(
            raw,
            feed,
            source="issuer_news",
            acquired_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            observed_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            fetch=FeedFetch(200, "https://www.sec.gov/feed", body),
            feed_url="https://www.sec.gov/feed",
        )
        self.assertEqual(skipped, {})
        self.assertEqual(len(records), 1)
        return records[0]

    def test_sec_edgar_parser_resolves_company_entity(self):
        record = self._normalize_with_parser(
            "8-K - Microsoft Corp (0000789019) (Filer)",
            entity_parser="sec_edgar",
            cik_symbols={"0000789019": "MSFT"},
        )
        self.assertEqual(record["institution"], "Microsoft Corp")
        self.assertEqual(record["document_type"], "regulatory_update")
        self.assertEqual(record["metadata"]["cik"], "0000789019")
        self.assertEqual(record["metadata"]["ticker"], "MSFT")

    def test_sec_edgar_parser_unmapped_cik_has_no_ticker(self):
        record = self._normalize_with_parser(
            "8-K - Microsoft Corp (0000789019) (Filer)",
            entity_parser="sec_edgar",
            cik_symbols={},
        )
        self.assertEqual(record["institution"], "Microsoft Corp")
        self.assertEqual(record["metadata"]["cik"], "0000789019")
        self.assertNotIn("ticker", record["metadata"])

    def test_sec_edgar_parser_malformed_title_keeps_feed_institution(self):
        record = self._normalize_with_parser(
            "A completely different title",
            entity_parser="sec_edgar",
            cik_symbols={"0000789019": "MSFT"},
        )
        self.assertEqual(record["institution"], "sec")
        self.assertNotIn("cik", record["metadata"])
        self.assertNotIn("ticker", record["metadata"])

    def test_no_entity_parser_opt_in_keeps_feed_institution(self):
        record = self._normalize_with_parser(
            "8-K - Microsoft Corp (0000789019) (Filer)"
        )
        self.assertEqual(record["institution"], "sec")
        self.assertNotIn("cik", record["metadata"])
        self.assertNotIn("ticker", record["metadata"])


class IssuerNewsFeedConfigValidationTests(unittest.TestCase):
    """Strict model fields for the SEC EDGAR entity parser."""

    @staticmethod
    def _model(**kwargs):
        from contracts.runtime_config import IssuerNewsFeedConfig

        return IssuerNewsFeedConfig(**kwargs)

    def test_cik_symbols_require_entity_parser(self):
        with self.assertRaises(ValueError):
            self._model(
                url="https://www.sec.gov/feed",
                cik_symbols={"0000789019": "MSFT"},
            )

    def test_cik_symbols_keys_must_be_ten_digit_ciks(self):
        with self.assertRaises(ValueError):
            self._model(
                url="https://www.sec.gov/feed",
                entity_parser="sec_edgar",
                cik_symbols={"789019": "MSFT"},
            )

    def test_unknown_entity_parser_rejected(self):
        with self.assertRaises(ValueError):
            self._model(
                url="https://www.sec.gov/feed",
                entity_parser="reverse_lookup",
            )

    def test_checked_in_entity_parser_shape_validates(self):
        feed = self._model(
            name="sec_current_8k",
            url="https://www.sec.gov/feed",
            institution="sec",
            document_type="regulatory_update",
            entity_parser="sec_edgar",
            cik_symbols={"0000789019": "MSFT"},
        )
        self.assertEqual(feed.get("entity_parser"), "sec_edgar")
        self.assertEqual(feed.get("cik_symbols").get("0000789019"), "MSFT")

    def test_full_text_fields_are_bounded(self):
        feed = self._model(
            url="https://ir.example.test/feed",
            fetch_full_text=True,
            max_document_bytes=1_000_000,
            max_full_text_items=10,
            max_content_chars=50_000,
            content_origins=["https://news.example.test/"],
        )
        self.assertTrue(feed.get("fetch_full_text"))
        self.assertEqual(feed.get("max_full_text_items"), 10)
        self.assertEqual(feed.get("content_origins"), ["https://news.example.test/"])


if __name__ == "__main__":
    unittest.main()
