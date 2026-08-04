"""Tests for investment_filings discovery and collection."""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import investment_filings as filings


class SecEdgarDiscoveryTests(unittest.TestCase):
    def test_pad_cik(self):
        self.assertEqual(filings._sec_pad_cik("320193"), "0000320193")
        self.assertEqual(filings._sec_pad_cik("0001045810"), "0001045810")

    def test_strip_cik(self):
        self.assertEqual(filings._sec_strip_cik("0000320193"), "320193")
        self.assertEqual(filings._sec_strip_cik("0001045810"), "1045810")

    def test_form_to_doc_type(self):
        self.assertEqual(filings._sec_form_to_doc_type("10-K"), "annual_report")
        self.assertEqual(filings._sec_form_to_doc_type("10-Q"), "quarterly_report")
        self.assertEqual(filings._sec_form_to_doc_type("8-K"), "earnings_release")
        self.assertEqual(filings._sec_form_to_doc_type("20-F"), "annual_report")
        self.assertEqual(filings._sec_form_to_doc_type("40-F/A"), "annual_report")
        self.assertEqual(filings._sec_form_to_doc_type("10-Q/A"), "quarterly_report")
        self.assertEqual(filings._sec_form_to_doc_type("8-K/A"), "earnings_release")

    @patch("investment_filings.get_shared_client")
    def test_discover_sec_filings_returns_metadata(self, mock_client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["10-K", "8-K", "DEF 14A"],
                    "accessionNumber": [
                        "0001045810-25-000001",
                        "0001045810-25-000002",
                        "0001045810-25-000003",
                    ],
                    "filingDate": ["2025-03-15", "2025-02-28", "2025-01-10"],
                    "primaryDocument": [
                        "nvda-20250131.htm",
                        "nvda-8k.htm",
                        "nvda-def14a.htm",
                    ],
                    "primaryDocDescription": ["10-K", "8-K", "DEF 14A"],
                }
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.get.return_value = mock_response

        company = {
            "company": "NVIDIA",
            "symbol": "NVDA",
            "cik": "1045810",
            "region": "US",
            "industry": "Semiconductors",
        }
        results = filings.discover_sec_filings(company, since=date(2025, 1, 1))
        # DEF 14A is not in SEC_FILING_FORMS, so only 2 results
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["form"], "10-K")
        self.assertEqual(results[0]["document_type"], "annual_report")
        self.assertEqual(results[0]["region"], "US")
        self.assertIn("sec.gov/Archives", results[0]["source_url"])
        self.assertEqual(results[0]["filing_id"], "0001045810-25-000001")
        self.assertTrue(results[0]["source_url"].endswith("/000104581025000001/"))
        self.assertEqual(results[1]["form"], "8-K")
        self.assertEqual(results[1]["document_type"], "earnings_release")

    @patch("investment_filings._fetch_sec_submissions")
    def test_selects_latest_filing_from_each_report_category(self, fetch_submissions):
        fetch_submissions.return_value = {
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K/A", "10-Q", "10-Q/A", "10-K"],
                    "accessionNumber": ["a1", "a2", "q1", "q2", "k1"],
                    "filingDate": [
                        "2026-07-05",
                        "2026-07-04",
                        "2026-07-03",
                        "2026-07-02",
                        "2026-07-01",
                    ],
                    "primaryDocument": [
                        "a1.htm",
                        "a2.htm",
                        "q1.htm",
                        "q2.htm",
                        "k1.htm",
                    ],
                    "primaryDocDescription": ["8-K", "8-K/A", "10-Q", "10-Q/A", "10-K"],
                }
            }
        }

        results = filings.discover_sec_filings(
            {"company": "Example", "symbol": "EX", "cik": "1"},
            since=date(2026, 1, 1),
        )

        self.assertEqual(
            [result["filing_id"] for result in results],
            ["a1", "q1", "k1"],
        )

    @patch("investment_filings.get_shared_client")
    def test_discover_sec_filings_empty_when_no_cik(self, mock_client):
        company = {"company": "Test", "symbol": "TST"}
        results = filings.discover_sec_filings(company)
        self.assertEqual(results, [])
        mock_client.assert_not_called()

    @patch("investment_filings.get_shared_client")
    def test_discover_sec_filings_respects_since_cutoff(self, mock_client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "accessionNumber": ["0001045810-24-000001"],
                    "filingDate": ["2024-03-15"],
                    "primaryDocument": ["nvda-20240131.htm"],
                    "primaryDocDescription": ["10-K"],
                }
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.get.return_value = mock_response

        company = {"company": "NVIDIA", "symbol": "NVDA", "cik": "1045810"}
        results = filings.discover_sec_filings(company, since=date(2025, 1, 1))
        self.assertEqual(results, [])

    @patch("investment_filings._sleep_between_requests")
    @patch("investment_filings.make_request")
    @patch("investment_filings.get_shared_client")
    def test_fetches_complete_accession_directory(
        self,
        mock_client,
        mock_request,
        _mock_sleep,
    ):
        index_response = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "primary.htm", "size": 160},
                    {"name": "earnings-exhibit.htm", "size": 180},
                    {"name": "logo.png", "size": 3},
                ]
            }
        }
        primary_response = MagicMock(
            content=b"<html><body>"
            + b"Primary annual report evidence. " * 8
            + b"</body></html>",
            headers={"content-type": "text/html"},
        )
        exhibit_response = MagicMock(
            content=b"<html><body>"
            + b"Investor presentation exhibit evidence. " * 8
            + b"</body></html>",
            headers={"content-type": "text/html"},
        )
        image_response = MagicMock(
            content=b"png",
            headers={"content-type": "image/png"},
        )
        mock_request.side_effect = [
            index_response,
            primary_response,
            exhibit_response,
            image_response,
        ]

        content, filename, mime_type = filings._fetch_sec_directory_bundle(
            {
                "directory_url": "https://www.sec.gov/Archives/edgar/data/1/2/",
                "filing_id": "0000000001-25-000002",
                "filename": "0000000001-25-000002.txt",
            }
        )

        self.assertEqual(mock_request.call_count, 4)
        self.assertIn(b"Investor presentation exhibit evidence", content)
        self.assertIn(b'"filename": "logo.png"', content)
        self.assertEqual(filename, "0000000001-25-000002.txt")
        self.assertEqual(mime_type, "text/plain")

    @patch("investment_filings._sleep_between_requests")
    @patch("investment_filings.make_request")
    @patch("investment_filings.get_shared_client")
    def test_accepts_large_complete_accession_directory(
        self,
        mock_client,
        mock_request,
        _mock_sleep,
    ):
        index_response = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "submission.txt", "size": 180_000_000},
                    {"name": "exhibits.zip", "size": 120_000_000},
                ]
            }
        }
        text_response = MagicMock(
            content=b"complete submission evidence",
            headers={"content-type": "text/plain"},
        )
        zip_response = MagicMock(
            content=b"zip",
            headers={"content-type": "application/zip"},
        )
        mock_request.side_effect = [
            index_response,
            text_response,
            zip_response,
        ]

        content, _, _ = filings._fetch_sec_directory_bundle(
            {
                "directory_url": "https://www.sec.gov/Archives/edgar/data/1/large/",
                "filing_id": "0000000001-25-000003",
                "filename": "0000000001-25-000003.txt",
            }
        )

        self.assertEqual(mock_request.call_count, 3)
        self.assertIn(b'"filename": "exhibits.zip"', content)


class EdinetDiscoveryTests(unittest.TestCase):
    @patch("investment_filings.get_shared_client")
    def test_discover_edinet_filings_empty_without_key(self, mock_client):
        company = {"company": "Tokyo Electron", "edinet_code": "E01803"}
        results = filings.discover_edinet_filings(company, api_key="")
        self.assertEqual(results, [])
        mock_client.assert_not_called()

    @patch("investment_filings.get_shared_client")
    def test_discover_edinet_filings_empty_without_code(self, mock_client):
        company = {"company": "Test"}
        results = filings.discover_edinet_filings(company, api_key="test-key")
        self.assertEqual(results, [])
        mock_client.assert_not_called()


class CompaniesHouseDiscoveryTests(unittest.TestCase):
    def test_rate_limit_stays_below_service_ceiling(self):
        self.assertGreaterEqual(
            filings.COMPANIES_HOUSE_REQUEST_DELAY_SECONDS,
            0.5,
        )

    @patch("investment_filings.get_shared_client")
    def test_uses_transaction_id_and_document_metadata(self, mock_client):
        response = MagicMock()
        response.json.return_value = {
            "items": [
                {
                    "transaction_id": "MzQxOTQ3MzY5M2FkaXF6a2N4",
                    "date": "2026-07-20",
                    "description": "accounts-with-accounts-type-full",
                    "links": {"document_metadata": "/document/company-accounts-1"},
                },
                {
                    "transaction_id": "old-transaction",
                    "date": "2025-01-01",
                    "links": {"document_metadata": "/document/old"},
                },
            ]
        }
        mock_client.return_value.get.return_value = response

        results = filings.discover_companies_house_filings(
            {
                "company": "AstraZeneca",
                "symbol": "AZN.L",
                "company_number": "02723534",
            },
            "stream-key",
            since=date(2026, 7, 1),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["filing_id"], "MzQxOTQ3MzY5M2FkaXF6a2N4")
        self.assertEqual(results[0]["company_number"], "02723534")
        self.assertEqual(
            results[0]["document_metadata_url"],
            "https://document-api.company-information.service.gov.uk/"
            "document/company-accounts-1",
        )
        _, kwargs = mock_client.return_value.get.call_args
        self.assertEqual(kwargs["params"]["category"], "accounts")
        self.assertEqual(kwargs["auth"], ("stream-key", ""))

    @patch("investment_filings.get_shared_client")
    def test_downloads_document_api_content(self, mock_client):
        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "resources": {
                "application/pdf": {"content_length": 1_024},
            }
        }
        content_response = MagicMock(
            content=b"%PDF-" + b"statutory accounts evidence " * 10,
            headers={"content-type": "application/pdf"},
        )
        mock_client.return_value.get.side_effect = [
            metadata_response,
            content_response,
        ]

        content, filename, mime_type = filings._fetch_companies_house_document(
            {
                "document_metadata_url": "/document/company-accounts-1",
                "filename": "transaction-1.pdf",
            },
            "api-key",
        )

        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertEqual(filename, "transaction-1.pdf")
        self.assertEqual(mime_type, "application/pdf")
        url = mock_client.return_value.get.call_args_list[1].args[0]
        self.assertEqual(
            url,
            "https://document-api.company-information.service.gov.uk/"
            "document/company-accounts-1/content",
        )
        self.assertTrue(
            mock_client.return_value.get.call_args.kwargs["follow_redirects"]
        )

    @patch("investment_filings.get_shared_client")
    def test_prefers_machine_readable_xhtml_resource(self, mock_client):
        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "resources": {
                "application/pdf": {"content_length": 2_048},
                "application/xhtml+xml": {"content_length": 1_024},
            }
        }
        content_response = MagicMock(
            content=b"<html><body>accounts</body></html>",
            headers={"content-type": "application/xhtml+xml"},
        )
        mock_client.return_value.get.side_effect = [
            metadata_response,
            content_response,
        ]

        _, filename, mime_type = filings._fetch_companies_house_document(
            {
                "document_metadata_url": "/document/company-accounts-1",
                "filename": "transaction-1.pdf",
            },
            "api-key",
        )

        content_call = mock_client.return_value.get.call_args_list[1]
        self.assertEqual(
            content_call.kwargs["headers"]["Accept"],
            "application/xhtml+xml",
        )
        self.assertEqual(filename, "transaction-1.html")
        self.assertEqual(mime_type, "application/xhtml+xml")

    @patch("investment_filings.get_shared_client")
    def test_downloads_zip_when_it_is_the_only_resource(self, mock_client):
        metadata_response = MagicMock()
        metadata_response.json.return_value = {
            "resources": {
                "application/zip": {"content_length": 1_024},
            }
        }
        content_response = MagicMock(
            content=b"PK-regulatory-archive",
            headers={"content-type": "application/zip"},
        )
        mock_client.return_value.get.side_effect = [
            metadata_response,
            content_response,
        ]

        _, filename, mime_type = filings._fetch_companies_house_document(
            {
                "document_metadata_url": "/document/company-accounts-1",
                "filename": "transaction-1.pdf",
            },
            "api-key",
        )

        content_call = mock_client.return_value.get.call_args_list[1]
        self.assertEqual(content_call.kwargs["headers"]["Accept"], "application/zip")
        self.assertEqual(filename, "transaction-1.zip")
        self.assertEqual(mime_type, "application/zip")


class OpenDartDiscoveryTests(unittest.TestCase):
    @patch("investment_filings.get_shared_client")
    def test_discover_opendart_filings_empty_without_key(self, mock_client):
        company = {"company": "Samsung", "dart_code": "00126380"}
        results = filings.discover_opendart_filings(company, api_key="")
        self.assertEqual(results, [])
        mock_client.assert_not_called()

    @patch("investment_filings.get_shared_client")
    def test_discover_opendart_filings_returns_metadata(self, mock_client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "000",
            "list": [
                {
                    "report_code": "11011",
                    "rcept_no": "20250315001234",
                    "rcept_dt": "20250315",
                    "report_nm": "Annual Report",
                },
                {
                    "report_code": "11013",
                    "rcept_no": "20250515001234",
                    "rcept_dt": "20250515",
                    "report_nm": "Q1 Report",
                },
                {
                    "report_code": "99999",
                    "rcept_no": "20250601001234",
                    "rcept_dt": "20250601",
                    "report_nm": "Other",
                },
            ],
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.get.return_value = mock_response

        company = {
            "company": "Samsung",
            "symbol": "005930.KS",
            "dart_code": "00126380",
            "region": "ASIA",
            "industry": "Semiconductors",
        }
        results = filings.discover_opendart_filings(company, api_key="test-key")
        # Only 11011 and 11013 are valid report codes
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["document_type"], "annual_report")
        self.assertEqual(results[1]["document_type"], "quarterly_report")


class FilingCollectionTests(unittest.TestCase):
    def test_disabled_returns_disabled(self):
        config = {"investment_filings": {"enabled": False}}
        result = filings.run_filing_collection(config)
        self.assertEqual(result["status"], "disabled")

    def test_no_companies_returns_no_companies(self):
        config = {"investment_filings": {"enabled": True, "companies": []}}
        result = filings.run_filing_collection(config)
        self.assertEqual(result["status"], "no_companies")

    @patch("investment_filings._already_ingested", return_value=True)
    @patch("investment_filings.discover_sec_filings")
    def test_skips_already_ingested(self, mock_discover, mock_ingested):
        mock_discover.return_value = [
            {
                "source": "sec_edgar",
                "filing_id": "0001045810-25-000001",
                "company": "NVDA",
                "symbol": "NVDA",
                "region": "US",
                "industry": "Semi",
                "document_type": "annual_report",
                "report_date": "2025-03-15",
                "source_url": "https://example.com/filing1/",
                "directory_url": "https://example.com/filing1/",
                "filename": "f1.htm",
                "form": "10-K",
            }
        ]
        config = {
            "investment_filings": {
                "enabled": True,
                "lookback_days": 30,
                "companies": [
                    {
                        "company": "NVDA",
                        "symbol": "NVDA",
                        "cik": "1045810",
                        "region": "US",
                        "industry": "Semi",
                    }
                ],
            }
        }
        result = filings.run_filing_collection(config)
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"], 1)

    @patch("investment_filings.store_document")
    @patch(
        "investment_filings._fetch_sec_directory_bundle",
        return_value=(b"Filing evidence " * 20, "filing.txt", "text/plain"),
    )
    @patch("investment_filings._already_ingested", return_value=False)
    @patch("investment_filings.discover_sec_filings")
    def test_ingests_new_filing(
        self,
        mock_discover,
        mock_ingested,
        mock_fetch_bundle,
        mock_store,
    ):
        mock_discover.return_value = [
            {
                "source": "sec_edgar",
                "filing_id": "0001045810-25-000001",
                "company": "NVDA",
                "symbol": "NVDA",
                "region": "US",
                "industry": "Semi",
                "document_type": "annual_report",
                "report_date": "2025-03-15",
                "source_url": "https://example.com/filing1/",
                "directory_url": "https://example.com/filing1/",
                "filename": "f1.htm",
                "form": "10-K",
            }
        ]
        mock_store.return_value = {"document_id": "abc-123", "status": "stored"}
        config = {
            "investment_filings": {
                "enabled": True,
                "lookback_days": 30,
                "companies": [
                    {
                        "company": "NVDA",
                        "symbol": "NVDA",
                        "cik": "1045810",
                        "region": "US",
                        "industry": "Semi",
                    }
                ],
            }
        }
        result = filings.run_filing_collection(config)
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failed"], 0)
        mock_fetch_bundle.assert_called_once()
        mock_store.assert_called_once()
        metadata = mock_store.call_args.args[1]
        self.assertEqual(metadata["filing_source"], "sec_edgar")
        self.assertEqual(metadata["filing_id"], "0001045810-25-000001")

    @patch("investment_filings._sleep_between_requests")
    @patch("investment_filings.discover_companies_house_filings", return_value=[])
    @patch("investment_filings.discover_sec_filings", return_value=[])
    def test_dual_registrants_query_both_regulators(
        self,
        discover_sec,
        discover_companies_house,
        _sleep,
    ):
        company = {
            "company": "Dual PLC",
            "symbol": "DUAL",
            "region": "EU",
            "cik": "1234",
            "company_number": "01234567",
        }
        config = {
            "investment_filings": {
                "enabled": True,
                "companies_house_api_key": "key",
                "companies": [company],
            }
        }

        filings.run_filing_collection(config)

        discover_sec.assert_called_once()
        discover_companies_house.assert_called_once()


class FilingSourceStatusTests(unittest.TestCase):
    @patch("investment_filings.get_session")
    def test_status_returns_sources(self, mock_session):
        mock_session.return_value.__enter__ = MagicMock(
            return_value=MagicMock(
                execute=MagicMock(
                    return_value=MagicMock(fetchone=MagicMock(return_value=None))
                )
            )
        )
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        config = {
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "companies_house_api_key": "test-key",
                "companies": [
                    {
                        "company": "NVDA",
                        "cik": "1045810",
                        "region": "US",
                        "market": "US",
                    },
                    {
                        "company": "AstraZeneca",
                        "company_number": "02723534",
                        "region": "EU",
                        "market": "UK",
                    },
                    {
                        "company": "SAP",
                        "cik": "1000184",
                        "region": "EU",
                        "market": "EU",
                    },
                ],
            }
        }
        status = filings.get_filing_source_status(config)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["companies_configured"], 3)
        self.assertEqual(len(status["sources"]), 5)
        sec = next(s for s in status["sources"] if s["id"] == "sec_edgar")
        self.assertTrue(sec["enabled"])
        self.assertEqual(sec["companies"], 2)
        companies_house = next(
            source for source in status["sources"] if source["id"] == "companies_house"
        )
        self.assertTrue(companies_house["enabled"])
        self.assertEqual(companies_house["companies"], 1)
        self.assertTrue(companies_house["api_key_configured"])
        eu_esef = next(
            source for source in status["sources"] if source["id"] == "eu_esef"
        )
        self.assertFalse(eu_esef["enabled"])
        self.assertEqual(eu_esef["companies"], 1)

    @patch("investment_filings.get_session")
    def test_status_reads_latest_durable_filings_run(self, get_session):
        row = MagicMock()
        row._mapping = {
            "accepted_at": "2026-07-29T13:00:00+00:00",
            "completed_at": "2026-07-29T13:10:00+00:00",
            "status": "completed",
            "result_status": "completed",
            "summary": {"ingested": 104, "failed": 0},
        }
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value.__enter__ = MagicMock(return_value=session)
        get_session.return_value.__exit__ = MagicMock(return_value=False)

        status = filings.get_filing_source_status(
            {
                "investment_filings": {
                    "enabled": True,
                    "companies": [],
                }
            }
        )

        statement = str(session.execute.call_args.args[0])
        self.assertIn("FROM cycle_runs", statement)
        self.assertEqual(status["last_run"]["status"], "completed")
        self.assertEqual(status["last_run"]["summary"]["ingested"], 104)

    @patch("investment_filings.get_session")
    def test_status_keeps_incomplete_run_timestamp_empty(self, get_session):
        row = MagicMock()
        row._mapping = {
            "accepted_at": "2026-07-29T15:40:15+00:00",
            "completed_at": None,
            "status": "running",
            "result_status": None,
            "summary": None,
        }
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value.__enter__ = MagicMock(return_value=session)
        get_session.return_value.__exit__ = MagicMock(return_value=False)

        status = filings.get_filing_source_status(
            {"investment_filings": {"enabled": True, "companies": []}}
        )

        self.assertEqual(status["last_run"]["accepted_at"], "2026-07-29T15:40:15+00:00")
        self.assertEqual(status["last_run"]["completed_at"], "")
        self.assertEqual(status["last_run"]["status"], "running")


class FilingUniverseTests(unittest.TestCase):
    def test_builtin_universe_has_top_100_for_each_market(self):
        universe = filings.top_us_uk_eu_companies()
        us_companies = [company for company in universe if company["market"] == "US"]
        uk_companies = [company for company in universe if company["market"] == "UK"]
        eu_companies = [company for company in universe if company["market"] == "EU"]

        self.assertEqual(len(us_companies), 100)
        self.assertEqual(len(uk_companies), 100)
        self.assertEqual(len(eu_companies), 100)
        self.assertEqual(
            [company["market_rank"] for company in eu_companies], list(range(1, 101))
        )
        self.assertEqual(len({company["symbol"] for company in eu_companies}), 100)
        self.assertEqual(sum(bool(company.get("cik")) for company in eu_companies), 29)
        self.assertEqual(len({company["symbol"] for company in universe}), 300)
        self.assertEqual(len({company["cik"] for company in us_companies}), 100)
        company_numbers = [
            company["company_number"]
            for company in uk_companies
            if company.get("company_number")
        ]
        self.assertEqual(len(company_numbers), len(set(company_numbers)))


if __name__ == "__main__":
    unittest.main()
