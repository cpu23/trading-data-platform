import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investment_service as service


@contextmanager
def session_context(session):
    yield session


def metric(value, unit="USDm", period="FY2025", evidence="report evidence"):
    return {"value": value, "unit": unit, "period": period, "evidence": evidence}


def sec_index_page(*rows):
    """Build an EDGAR ``*-index.htm`` Document Format Files table body.

    ``rows`` are ``(document_name, doc_type)`` pairs.
    """
    body = "".join(
        f"<tr><td>{index}</td><td>{doc_type}</td>"
        f"<td><a href='/Archives/edgar/data/x/{name}'>{name}</a></td>"
        f"<td>{doc_type}</td><td>1000</td></tr>"
        for index, (name, doc_type) in enumerate(rows, start=1)
    )
    return (
        "<html><body><table class='tableFile' summary='Document Format Files'>"
        "<tr><th scope='col'>Seq</th><th scope='col'>Description</th>"
        "<th scope='col'>Document</th><th scope='col'>Type</th>"
        "<th scope='col'>Size</th></tr>" + body + "</table></body></html>"
    )


def sec_directory_fake_request(index_response, index_page_response, primary_response):
    """Route SEC recovery requests: index.json, then ``*-index.htm``, then the
    selected primary document."""

    def route(method, url, **kwargs):
        if url.endswith("index.json"):
            return index_response
        if "-index.htm" in url or "-index.html" in url:
            return index_page_response
        return primary_response

    return route


class InvestmentIntakeTests(unittest.TestCase):
    def test_html_extraction_removes_active_content(self):
        raw = (
            b"<html><script>steal()</script><body><h1>Annual report</h1><p>"
            + b"Revenue and cash flow evidence. " * 8
            + b"</p></body></html>"
        )
        extracted = service.extract_document_text(raw, "report.html", "text/html")
        self.assertIn("Annual report", extracted)
        self.assertNotIn("steal", extracted)

    def test_archive_magic_overrides_incorrect_pdf_metadata(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as package:
            package.writestr(
                "annual-report.xhtml",
                "<html><body><h1>Annual report</h1><p>"
                + "Revenue and cash flow evidence. " * 8
                + "</p></body></html>",
            )

        extracted = service.extract_document_text(
            content.getvalue(),
            "incorrect.pdf",
            "application/pdf",
        )

        self.assertIn("Annual report", extracted)

    def test_oversized_text_preserves_financial_statements_and_document_end(self):
        statement = (
            "\nCONSOLIDATED INCOME STATEMENT\nUSD million 2025 2024\nRevenue 120 100\n"
        )
        raw = ("BEGIN\n" + "x" * 600_000 + statement + "y" * 600_000 + "\nEND").encode()

        extracted = service.extract_document_text(
            raw, "annual-report.txt", "text/plain"
        )

        self.assertLessEqual(len(extracted), service.MAX_EXTRACTED_CHARS)
        self.assertIn("BEGIN", extracted)
        self.assertIn("CONSOLIDATED INCOME STATEMENT", extracted)
        self.assertIn("END", extracted)
        excerpt = service.build_analysis_excerpt(extracted)
        self.assertLessEqual(len(excerpt), service.MAX_ANALYSIS_CHARS)
        self.assertIn("CONSOLIDATED INCOME STATEMENT", excerpt)
        self.assertIn("END", excerpt)

    def test_metadata_canonicalizes_regions_and_key_industries(self):
        result = service.normalize_metadata(
            {
                "company": "Memory Co",
                "symbol": "mem",
                "region": "asia",
                "industry": "DRAM chip manufacturing",
                "document_type": "annual_report",
                "report_date": "2025-12-31",
                "filename": "../report.txt",
            }
        )
        self.assertEqual(result["region"], "ASIA")
        self.assertEqual(result["symbol"], "MEM")
        self.assertEqual(result["industry"], "Semiconductors & Compute")
        self.assertEqual(result["filename"], "report.txt")

    def test_metadata_uses_checked_in_issuer_industry_when_intake_omits_label(self):
        result = service.normalize_metadata(
            {
                "company": "Micron Technology",
                "symbol": "MU",
                "region": "US",
                "document_type": "annual_report",
                "report_date": "2025-12-31",
                "filename": "report.txt",
            }
        )
        self.assertEqual(result["symbol"], "MU")
        self.assertEqual(result["industry"], "Semiconductors & Compute")

    def test_metadata_keeps_truly_unknown_issuer_unclassified(self):
        result = service.normalize_metadata(
            {
                "company": "Example PLC",
                "symbol": "EX",
                "region": "EU",
                "document_type": "annual_report",
                "filename": "report.txt",
            }
        )
        self.assertEqual(result["industry"], "Unclassified")

    def test_private_report_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-public address"):
            service._validate_public_url("https://127.0.0.1/internal-report.pdf")

    def test_plain_http_report_url_is_rejected_by_default(self):
        with self.assertRaisesRegex(ValueError, "must use https"):
            service._validate_public_url("http://example.test/internal-report.pdf")

    def test_loopback_ipv6_report_url_is_rejected(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                service._validate_public_url("https://[::1]/internal-report.pdf")

    def test_link_local_metadata_address_is_rejected(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                service._validate_public_url(
                    "https://169.254.169.254/latest/meta-data/"
                )

    def test_multicast_and_reserved_addresses_are_rejected(self):
        for address in ("224.0.0.1", "240.0.0.1", "0.0.0.0", "100.64.0.1"):
            with self.subTest(address=address):
                with patch(
                    "socket.getaddrinfo",
                    side_effect=lambda *args, **kwargs: [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                    ],
                ):
                    with self.assertRaisesRegex(ValueError, "non-public address"):
                        service._validate_public_url(f"https://{address}/doc")

    def test_ipv4_mapped_ipv6_loopback_is_rejected(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                service._validate_public_url(
                    "https://[::ffff:127.0.0.1]/internal-report.pdf"
                )

    def test_mixed_dns_answers_fail_closed(self):
        """One public plus one private answer rejects the whole resolution."""
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                service._validate_public_url("https://mixed.example.test/report")

    def test_public_hostname_with_credentials_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not embed credentials"):
            service._validate_public_url(
                "https://operator:secret@example.test/report.pdf"
            )

    def test_non_http_scheme_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must use http or https"):
            service._validate_public_url("file:///etc/passwd")

    def test_redirect_to_private_host_is_rejected(self):
        from contracts.outbound_security import resolve_redirect_url

        joined = resolve_redirect_url(
            "https://public.example.test/doc/report.pdf", "https://127.0.0.1/steal"
        )
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                service._validate_public_url(joined)

    def test_redirect_scheme_downgrade_is_rejected(self):
        from contracts.outbound_security import OutboundSecurityError

        with self.assertRaises(OutboundSecurityError):
            service.resolve_redirect_url(
                "https://public.example.test/doc", "http://steal.example.test/x"
            )

    def test_redirect_without_location_is_rejected(self):
        from contracts.outbound_security import OutboundSecurityError

        with self.assertRaises(OutboundSecurityError):
            service.resolve_redirect_url("https://public.example.test/doc", "   ")

    @patch("investment_service.httpx.Client")
    def test_slow_drip_response_aborts_at_total_deadline(self, client_class):
        """A server that drips one small chunk per read-timeout window must
        still be cut off at the total fetch deadline, not stream forever."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.headers = {}
        fake_response.raise_for_status = MagicMock()
        fake_response.iter_bytes.return_value = [b"a", b"b", b"c"]
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.stream.return_value.__enter__.return_value = fake_response
        client_class.return_value = fake_client
        ticks = iter([100.0, 100.0, 150.0, 250.0])
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=lambda *args, **kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
                ],
            ),
            patch(
                "investment_service.time.monotonic",
                side_effect=lambda: next(ticks),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "total deadline"):
                service.fetch_document_url_to_path(
                    "https://public.example.test/report.pdf"
                )

    def test_many_entry_report_zip_is_rejected(self):
        """A small ZIP with hundreds of thousands of tiny entries must be
        rejected by the entry-count cap before the candidate sort/read."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(service.MAX_ARCHIVE_ENTRIES + 1):
                archive.writestr(
                    f"e{index}.xhtml",
                    "<html><body>Revenue evidence.</body></html>",
                )
        fd, path = tempfile.mkstemp(prefix="investment-zip-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(buffer.getvalue())
            with self.assertRaisesRegex(ValueError, "too many entries"):
                service.extract_document_text_path(
                    path, "report.zip", "application/zip"
                )
        finally:
            os.unlink(path)

    def test_many_entry_docx_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(service.MAX_DOCX_ARCHIVE_ENTRIES + 1):
                archive.writestr(
                    f"part{index}.xml",
                    "<html><body>Revenue evidence.</body></html>",
                )
        fd, path = tempfile.mkstemp(prefix="investment-docx-", suffix=".docx")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(buffer.getvalue())
            with self.assertRaisesRegex(ValueError, "too many entries"):
                service.extract_document_text_path(
                    path,
                    "report.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
        finally:
            os.unlink(path)

    def test_encrypted_zip_members_are_rejected(self):
        """Any encrypted member (central-directory flag bit 0) rejects the
        archive before any member is read."""
        fake_info = zipfile.ZipInfo("report.xhtml")
        fake_info.flag_bits |= 0x1
        archive = MagicMock()
        archive.infolist.return_value = [fake_info]
        with self.assertRaisesRegex(ValueError, "encrypted"):
            service._reject_unsafe_archive(
                archive, max_entries=service.MAX_ARCHIVE_ENTRIES
            )

    @patch("investment_service.httpx.Client")
    def test_chunked_oversize_response_is_rejected(self, client_class):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.headers = {}
        fake_response.iter_bytes.return_value = [
            b"x" * (service.MAX_DOCUMENT_BYTES + 1)
        ]
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.stream.return_value.__enter__.return_value = fake_response
        client_class.return_value = fake_client
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 20 MB"):
                service.fetch_document_url_to_path(
                    "https://public.example.test/report.pdf"
                )

    @patch("investment_service.httpx.Client")
    def test_fetch_streams_directly_to_temp_file(self, client_class):
        """A remote document is streamed to a temp path, never buffered as a
        full byte blob (the function returns a PATH, not bytes)."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.headers = {"content-type": "application/pdf"}
        fake_response.iter_bytes.return_value = [b"hello ", b"world"]
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.stream.return_value.__enter__.return_value = fake_response
        client_class.return_value = fake_client
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            temp_path, filename, mime_type, final_url = (
                service.fetch_document_url_to_path(
                    "https://public.example.test/report.pdf"
                )
            )
        try:
            with open(temp_path, "rb") as handle:
                self.assertEqual(handle.read(), b"hello world")
        finally:
            os.unlink(temp_path)
        self.assertEqual(filename, "report.pdf")
        self.assertEqual(mime_type, "application/pdf")
        self.assertEqual(final_url, "https://public.example.test/report.pdf")

    @patch("investment_service.httpx.Client")
    def test_declared_oversize_rejected_before_body_is_read(self, client_class):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.headers = {"content-length": str(service.MAX_DOCUMENT_BYTES + 1)}
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.stream.return_value.__enter__.return_value = fake_response
        client_class.return_value = fake_client
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 20 MB"):
                service.fetch_document_url_to_path(
                    "https://public.example.test/report.pdf"
                )
        fake_response.iter_bytes.assert_not_called()

    def test_ocr_page_budget_bounds_total_pages(self):
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._ocr_pdf_page",
                return_value="page text " * 40,
            ) as ocr_page,
        ):
            service._ocr_pdf(b"%PDF-scan", page_count=1200)
        pages = {call.args[2] for call in ocr_page.call_args_list}
        # Synchronous (HTTP-bound) extraction samples at most the sync budget.
        self.assertLessEqual(len(pages), service.SYNC_OCR_PAGE_BUDGET)

    def test_ocr_focused_pass_respects_shared_page_budget(self):
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._ocr_pdf_page",
                return_value="CONSOLIDATED INCOME STATEMENT " * 20,
            ) as ocr_page,
        ):
            service._ocr_pdf(b"%PDF-scan", page_count=500)
        pages = {call.args[2] for call in ocr_page.call_args_list}
        self.assertLessEqual(len(pages), service.SYNC_OCR_PAGE_BUDGET)

    def test_ocr_durable_budget_allows_larger_sample(self):
        """The durable worker may use the full page budget."""
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._ocr_pdf_page",
                return_value="CONSOLIDATED INCOME STATEMENT " * 20,
            ) as ocr_page,
        ):
            service._ocr_pdf(
                b"%PDF-scan",
                page_count=500,
                page_budget=service.MAX_OCR_PAGES,
                wall_seconds=service.OCR_WALL_SECONDS,
            )
        pages = {call.args[2] for call in ocr_page.call_args_list}
        self.assertLessEqual(len(pages), service.MAX_OCR_PAGES)
        self.assertGreater(len(pages), service.SYNC_OCR_PAGE_BUDGET)

    def test_ocr_wall_deadline_prevents_subprocess_launch(self):
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._ocr_pdf_page",
                return_value="text " * 40,
            ) as ocr_page,
            patch(
                "investment_service.time.monotonic",
                side_effect=[100.0, 1000.0],
            ),
        ):
            service._ocr_pdf(b"%PDF-scan", page_count=50)
        ocr_page.assert_not_called()

    @patch("investment_service.httpx.Client")
    def test_redirect_chain_revalidates_each_hop(self, client_class):
        """Every redirect hop resolves and validates through the pinned
        transport, and the final URL is what gets stored."""
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {"location": "https://cdn.example.test/final.pdf"}
        final = MagicMock()
        final.status_code = 200
        final.headers = {"content-type": "application/pdf", "content-length": "5"}
        final.raise_for_status = MagicMock()
        final.iter_bytes.return_value = [b"hello"]
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.stream.return_value.__enter__.side_effect = [redirect, final]
        client_class.return_value = fake_client
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            temp_path, filename, mime_type, final_url = (
                service.fetch_document_url_to_path(
                    "https://public.example.test/report.pdf"
                )
            )
        try:
            with open(temp_path, "rb") as handle:
                self.assertEqual(handle.read(), b"hello")
        finally:
            os.unlink(temp_path)
        self.assertEqual(filename, "final.pdf")
        self.assertEqual(mime_type, "application/pdf")
        self.assertEqual(final_url, "https://cdn.example.test/final.pdf")
        self.assertEqual(fake_client.stream.call_count, 2)

    def test_ocr_subprocess_uses_process_group_and_prlimit_wrapper(self):
        fake = SimpleNamespace(
            returncode=0,
            communicate=lambda timeout=None: (b"ok", b""),
            wait=lambda: 0,
            kill=lambda: None,
            pid=1234,
        )
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/prlimit"),
            patch("investment_service.subprocess.Popen", return_value=fake) as popen,
        ):
            stdout = service._run_ocr_subprocess(
                ["tesseract", "img"], capture=True, timeout=5
            )
        self.assertEqual(stdout, b"ok")
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        # No preexec_fn: documented deadlock risk in multithreaded parents.
        self.assertNotIn("preexec_fn", kwargs)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "prlimit")
        self.assertTrue(any(arg.startswith("--as=") for arg in command))
        self.assertTrue(any(arg.startswith("--fsize=") for arg in command))
        self.assertTrue(any(arg.startswith("--cpu=") for arg in command))
        self.assertEqual(command[-2:], ["tesseract", "img"])

    def test_ocr_subprocess_runs_plain_command_without_prlimit(self):
        fake = SimpleNamespace(
            returncode=0,
            communicate=lambda timeout=None: (b"ok", b""),
            wait=lambda: 0,
            kill=lambda: None,
            pid=1234,
        )
        with (
            patch("investment_service.shutil.which", return_value=None),
            patch("investment_service.subprocess.Popen", return_value=fake) as popen,
        ):
            stdout = service._run_ocr_subprocess(
                ["tesseract", "img"], capture=True, timeout=5
            )
        self.assertEqual(stdout, b"ok")
        command = popen.call_args.args[0]
        self.assertEqual(command, ["tesseract", "img"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("preexec_fn", popen.call_args.kwargs)

    def test_ocr_subprocess_kills_and_reaps_process_group_on_timeout(self):
        reaped = []

        class TimeoutProcess:
            pid = 4242

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired("tesseract", 5)

            def wait(self):
                reaped.append(True)
                return 0

            def kill(self):
                raise OSError("already dead")

        with (
            patch(
                "investment_service.subprocess.Popen",
                return_value=TimeoutProcess(),
            ),
            # start_new_session makes the child the group leader, so the
            # timeout path must killpg(pid, SIGKILL) and then reap via wait.
            patch("investment_service.os.killpg") as killpg,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                service._run_ocr_subprocess(
                    ["tesseract", "img"], capture=True, timeout=5
                )
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(reaped, [True])

    def test_ocr_page_task_skips_when_deadline_expired(self):
        with (
            patch("investment_service.time.monotonic", return_value=500.0),
            patch("investment_service._run_ocr_subprocess") as run,
        ):
            result = service._ocr_pdf_page(
                Path("/tmp/x.pdf"), Path("/tmp"), 3, 140, deadline=400.0
            )
        self.assertEqual(result, "")
        run.assert_not_called()

    def test_ocr_page_task_clamps_subprocess_timeout_to_remaining(self):
        with (
            patch(
                "investment_service.time.monotonic",
                side_effect=[410.0, 415.0, 419.0],
            ),
            patch("investment_service._run_ocr_subprocess") as run,
        ):
            service._ocr_pdf_page(
                Path("/tmp/x.pdf"), Path("/tmp"), 3, 140, deadline=420.0
            )
        self.assertEqual(run.call_count, 2)
        # Timeouts are recomputed after the semaphore: 420-415=5 then 420-419=1.
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 5.0)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 1.0)

    def test_ocr_page_timeout_recomputed_after_semaphore_wait(self):
        """A semaphore wait that consumes nearly the whole budget must not
        launch with a stale oversized timeout."""
        with (
            patch(
                "investment_service.time.monotonic",
                side_effect=[410.0, 419.9, 419.95],
            ),
            patch("investment_service._run_ocr_subprocess") as run,
        ):
            service._ocr_pdf_page(
                Path("/tmp/x.pdf"), Path("/tmp"), 3, 140, deadline=420.0
            )
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertLessEqual(call.kwargs["timeout"], 0.5)

    def test_direct_pdf_extraction_uses_bounded_poppler_subprocesses(self):
        """Direct text extraction runs pdfinfo + pdftotext -l <page_budget>
        through the bounded subprocess runner (no in-process parsing)."""
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._run_ocr_subprocess",
                side_effect=[
                    b"Pages: 200\n",
                    b"financial statement text " * 30,
                ],
            ) as run,
        ):
            result = service._extract_pdf("x.pdf", page_budget=10, wall_seconds=60)
        self.assertIn("financial statement text", result)
        self.assertEqual(len(run.call_args_list), 2)
        first_command = run.call_args_list[0].args[0]
        self.assertEqual(first_command[-1], "x.pdf")
        self.assertTrue("pdfinfo" in first_command)
        second_command = run.call_args_list[1].args[0]
        self.assertTrue("pdftotext" in second_command)
        self.assertEqual(second_command[second_command.index("-l") + 1], "10")

    def test_direct_extraction_timeout_falls_back_without_hanging(self):
        """A hostile PDF that stalls pdftotext mid-way must not hang the
        caller: the bounded runner kills the process group and the function
        falls back to OCR with the remaining budget."""
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._run_ocr_subprocess",
                side_effect=[
                    b"Pages: 5\n",
                    subprocess.TimeoutExpired("pdftotext", 30),
                ],
            ),
            patch("investment_service._ocr_pdf", return_value="") as ocr,
        ):
            result = service._extract_pdf("x.pdf", page_budget=10, wall_seconds=60)
        self.assertEqual(result, "")
        ocr.assert_called_once()

    def test_ocr_fallback_inherits_remaining_wall_budget(self):
        """The OCR fallback must not restart a full wall clock: it receives
        only the time remaining after the direct pass, so direct text + OCR
        together stay within the configured wall cap."""
        captured = {}
        with (
            patch("investment_service.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "investment_service._run_ocr_subprocess",
                side_effect=[b"Pages: 3\n", b"short text"],
            ),
            patch(
                "investment_service._ocr_pdf",
                side_effect=lambda *args, **kwargs: captured.update(kwargs) or "",
            ) as ocr,
            patch(
                "investment_service.time.monotonic",
                side_effect=[100.0, 190.0, 190.0, 190.0],
            ),
        ):
            result = service._extract_pdf("x.pdf", page_budget=10, wall_seconds=120.0)
        self.assertEqual(result, "")
        ocr.assert_called_once()
        self.assertAlmostEqual(captured["wall_seconds"], 30.0, places=6)
        self.assertEqual(captured["page_budget"], 10)

    @patch("investment_service.store_document_path")
    @patch("investment_service.fetch_document_url_to_path")
    def test_remote_url_ingest_defers_extraction_to_worker(self, fetch, store):
        fd, path = tempfile.mkstemp(prefix="investment-url-", suffix=".pdf")
        os.close(fd)
        fetch.return_value = (
            path,
            "report.pdf",
            "application/pdf",
            "https://public.example.test/report.pdf",
        )
        store.return_value = {"document_id": "doc-1"}

        result = service.store_document_url(
            {},
            {
                "url": "https://public.example.test/report.pdf",
                "company": "Example PLC",
            },
        )

        self.assertEqual(result["document_id"], "doc-1")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(store.call_args.kwargs["extract"])

    @patch("investment_service.get_session")
    def test_deferred_extraction_persists_durable_file_not_bytea(self, get_session):
        """extract=False must never bind BYTEA or read the upload wholesale:
        the file lands on durable content-addressed storage, survives the
        request-spool cleanup, and the DB row carries only the path."""
        row = MagicMock()
        row._mapping = {
            "document_id": "doc-1",
            "company": "Example PLC",
            "symbol": "EX",
            "region": "EU",
            "industry": "Unclassified",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": None,
            "filing_source": None,
            "filing_id": None,
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "status": "ingested",
            "created_at": None,
        }
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value = session_context(session)
        fd, path = tempfile.mkstemp(prefix="investment-test-", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"%PDF-test-content")
            with (
                patch("investment_service.extract_document_text_path") as extract,
                tempfile.TemporaryDirectory() as root,
                patch(
                    "investment_service._file_root",
                    return_value=Path(root),
                ),
            ):
                result = service.store_document_path(
                    {},
                    {
                        "company": "Example PLC",
                        "region": "EU",
                        "document_type": "annual_report",
                    },
                    path,
                    "application/pdf",
                    extract=False,
                )
                # The request spool is gone; the durable content-addressed
                # file must still exist and match the upload bytes.
                os.unlink(path)
                extract.assert_not_called()
                self.assertEqual(result["document_id"], "doc-1")
                params = session.execute.call_args.args[1]
                self.assertEqual(params["extracted_text"], "")
                self.assertIsNone(params["raw_content"])
                self.assertIsNotNone(params["content_path"])
                self.assertTrue(os.path.exists(params["content_path"]))
                with open(params["content_path"], "rb") as handle:
                    self.assertEqual(handle.read(), b"%PDF-test-content")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @patch("investment_service.get_session")
    def test_worker_extracts_from_durable_path_after_spool_deleted(self, get_session):
        """The worker re-extracts from the persisted content path even though
        the request spool is long gone and no raw_content BYTEA exists."""
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = None
        get_session.return_value = session_context(session)
        document = {
            "document_id": "doc-1",
            "filename": "report.txt",
            "mime_type": "text/plain",
            "extracted_text": "",
            "raw_content": None,
            "content_path": None,
            "content_sha256": None,
        }
        with tempfile.TemporaryDirectory() as root:
            source_path = Path(root) / "source.txt"
            source_path.write_text("Annual report financial evidence. " * 20)
            digest = service._sha256_file(source_path)
            with patch("investment_service._file_root", return_value=Path(root)):
                document["content_path"] = service._persist_document_file(
                    source_path, {}, digest
                )
                document["content_sha256"] = digest
                source_path.unlink()
                source = service._ensure_extracted_text({}, document)
        self.assertEqual(source, "regulatory_document")
        self.assertIn("Annual report financial evidence", document["extracted_text"])

    @patch("investment_service.get_session")
    def test_worker_rejects_tampered_durable_content_path(self, get_session):
        get_session.return_value = session_context(MagicMock())
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "outside.txt"
            outside.write_text("private worker file")
            document = {
                "document_id": "doc-1",
                "filename": "report.txt",
                "mime_type": "text/plain",
                "extracted_text": "",
                "raw_content": None,
                "content_path": str(outside),
                "content_sha256": service._sha256_file(outside),
            }
            with (
                patch(
                    "investment_service._file_root",
                    return_value=Path(root) / "documents",
                ),
                patch("investment_service.extract_document_text_path") as extract,
            ):
                self.assertEqual(
                    service._ensure_extracted_text({}, document),
                    "missing_report_text",
                )
        extract.assert_not_called()

    def test_enqueue_investment_analysis_uses_durable_queue(self):
        fake_job = SimpleNamespace(id=123, correlation_id="corr-1")
        enqueued = SimpleNamespace(inserted=True, job=fake_job)
        with (
            patch("investment_service.get_session") as get_session,
            patch("analysis_jobs.enqueue_job", return_value=enqueued) as enqueue,
        ):
            session = MagicMock()
            get_session.return_value = session_context(session)
            result = service.enqueue_investment_analysis({}, "doc-1")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["job_id"], "123")
        self.assertEqual(result["correlation_id"], "corr-1")
        self.assertTrue(result["inserted"])
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["job_type"], "investment_analysis")
        self.assertEqual(kwargs["dedupe_key"], "investment-analysis:doc-1")
        self.assertEqual(kwargs["input_fingerprint"], "document:doc-1")
        self.assertEqual(kwargs["payload"]["document_id"], "doc-1")

    def test_investment_analysis_job_handler_routes_and_analyzes(self):
        import analysis_job_handlers

        job = SimpleNamespace(
            job_type="investment_analysis",
            payload={"document_id": "doc-1", "market_inputs": {"price": 100}},
            correlation_id="corr-1",
        )
        with (
            patch("analysis_job_handlers._config", return_value={}),
            patch(
                "investment_service.analyze_document",
                return_value={"analysis_id": "an-1"},
            ) as analyze,
        ):
            result = analysis_job_handlers.run_investment_analysis_job(MagicMock(), job)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["analysis_id"], "an-1")
        analyze.assert_called_once_with(
            {},
            "doc-1",
            {"price": 100},
            ocr_page_budget=service.MAX_OCR_PAGES,
            ocr_wall_seconds=service.OCR_WALL_SECONDS,
        )

    def test_investment_analysis_job_requires_document_id(self):
        import analysis_job_handlers

        job = SimpleNamespace(job_type="investment_analysis", payload={})
        with self.assertRaisesRegex(ValueError, "document_id"):
            analysis_job_handlers.run_investment_analysis_job(MagicMock(), job)

    def test_route_job_dispatches_investment_analysis(self):
        import analysis_job_handlers

        self.assertIn("investment_analysis", analysis_job_handlers._HANDLERS)
        # route_job dispatches through the module-level registry, so the
        # registered entry must be replaced, not the module attribute.
        with patch.dict(
            analysis_job_handlers._HANDLERS,
            {"investment_analysis": MagicMock(return_value={"status": "completed"})},
        ):
            result = analysis_job_handlers.route_job(
                MagicMock(), SimpleNamespace(job_type="investment_analysis")
            )
            dispatched = analysis_job_handlers._HANDLERS["investment_analysis"]
        self.assertEqual(result["status"], "completed")
        dispatched.assert_called_once()

    def test_large_report_excerpt_keeps_financial_and_demand_windows(self):
        report = (
            "A" * 150_000
            + " Revenue increased on AI data-centre demand and capex. "
            + "Z" * 150_000
        )
        excerpt = service.build_analysis_excerpt(report)
        self.assertLessEqual(len(excerpt), service.MAX_ANALYSIS_CHARS + 500)
        self.assertIn("AI data-centre demand", excerpt)

    def test_excerpt_preserves_short_documents_unchanged(self):
        report = "Item 1. Business\nRevenue outlook.\n"
        self.assertEqual(service.build_analysis_excerpt(report), report)

    def test_excerpt_ranks_substantive_sections_over_exhibit_noise(self):
        exhibit = (
            "EXHIBIT 10.1 EMPLOYMENT AGREEMENT\n"
            + "The agreement addresses revenue sharing, risk allocation, cash flow "
            + "guarantees and capex commitments between the parties. " * 200
            + "\n"
        )
        certification = (
            "CERTIFICATION PURSUANT TO SECTION 302 OF THE SARBANES-OXLEY ACT OF 2002\n"
            + "I certify that revenue, net income and gross margin disclosures in "
            + "this report are accurate. " * 200
            + "\n"
        )
        xbrl = (
            "XBRL INSTANCE DOCUMENT\n"
            + "contextref period revenue risk demand cash flow. " * 200
            + "\n"
        )
        business = "ITEM 1. BUSINESS\n" + (
            "Our business focuses on AI data-centre demand, supply capacity and "
            "pricing. " * 400 + "\n"
        )
        risk = "ITEM 1A. RISK FACTORS\n" + (
            "Our risk profile includes pricing pressure, inventory levels, demand "
            "variability and supply disruption. " * 400 + "\n"
        )
        mda = (
            "ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION "
            + "AND RESULTS OF OPERATIONS\n"
            + (
                "Revenue grew as net income and gross margin expanded with operating "
                "cash flow. " * 400 + "\n"
            )
        )
        forward_looking = "FORWARD-LOOKING STATEMENTS\n" + (
            "Management expectations about demand, pricing and capacity are "
            "forward-looking in nature. " * 300 + "\n"
        )
        outlook = "OUTLOOK\n" + (
            "Our outlook targets revenue growth driven by AI demand and capacity "
            "expansion. " * 300 + "\n"
        )
        results = "RESULTS OF OPERATIONS\n" + (
            "Operating results improved with revenue growth, net income and gross "
            "margin expansion. " * 300 + "\n"
        )
        liquidity = "LIQUIDITY AND CAPITAL RESOURCES\n" + (
            "Liquidity needs are funded by operating cash flow, capex plans and "
            "available capacity. " * 300 + "\n"
        )
        strategy = "STRATEGY\n" + (
            "Our strategy prioritises capex efficiency, supply resilience and "
            "pricing discipline. " * 300 + "\n"
        )
        report = (
            business
            + exhibit
            + risk
            + mda
            + certification
            + forward_looking
            + outlook
            + xbrl
            + results
            + liquidity
            + strategy
        )
        self.assertGreater(len(report), service.MAX_ANALYSIS_CHARS)

        excerpt = service.build_analysis_excerpt(report)

        self.assertLessEqual(len(excerpt), service.MAX_ANALYSIS_CHARS)
        for marker in (
            "ITEM 1. BUSINESS",
            "ITEM 1A. RISK FACTORS",
            "MANAGEMENT'S DISCUSSION",
            "FORWARD-LOOKING STATEMENTS",
            "OUTLOOK",
            "Operating results improved",
            "LIQUIDITY AND CAPITAL RESOURCES",
            "STRATEGY",
        ):
            self.assertIn(marker, excerpt)
        for noise in (
            "EXHIBIT 10.1",
            "EMPLOYMENT AGREEMENT",
            "SECTION 302",
            "SARBANES-OXLEY",
            "XBRL",
            "contextref",
            "risk allocation",
        ):
            self.assertNotIn(noise, excerpt)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_prefers_primary_over_largest_eligible(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    # Largest eligible HTML, but an XBRL viewer, not the report.
                    {"name": "Financial_Report.htm", "size": 9_000_000},
                    # The regulator primary document (ticker-date convention).
                    {"name": "abc-20241231.htm", "size": 5_200_000},
                    # Exhibit and certification files stay ineligible.
                    {"name": "abc-ex101_20241231.htm", "size": 8_000_000},
                    {"name": "abc-10k_ex31.htm", "size": 300_000},
                    {"name": "000032019324000123-index.htm", "size": 400_000},
                    {"name": "R1.htm", "size": 200_000},
                ]
            }
        }
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("abc-20241231.htm", "10-K"),
            ("Financial_Report.htm", "XBRL"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>Item 7 narrative</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "Item 7 MD&A narrative " * 100

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": "",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "Item 7 MD&A narrative " * 100)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("abc-20241231.htm", fetched[-1])
        self.assertNotIn("Financial_Report.htm", fetched)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_falls_back_to_largest_eligible(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "report1.htm", "size": 1_000_000},
                    {"name": "report2.htm", "size": 4_000_000},
                    # Oversized and exhibit files stay ineligible.
                    {"name": "huge.htm", "size": 25_000_000},
                    {"name": "report-ex99.htm", "size": 5_000_000},
                    {"name": "000000078926000123-index.htm", "size": 300_000},
                ]
            }
        }
        # No annual-form row in the index page, so naming heuristics apply.
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("report-ex99.htm", "EX-99"),
            ("report1.htm", "GRAPHIC"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>report</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "plain report text " * 50

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/789/000000078926000123/"
                ),
                "extracted_text": "",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "plain report text " * 50)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("report2.htm", fetched[-1])
        self.assertNotIn("huge.htm", fetched)
        self.assertNotIn("report-ex99.htm", fetched)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_honors_known_primary_metadata(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "abc-10k.htm", "size": 4_000_000},
                    {"name": "abc-20241231.htm", "size": 5_200_000},
                    {"name": "000032019324000123-index.htm", "size": 300_000},
                ]
            }
        }
        # The index page would pick abc-10k.htm; known metadata must win.
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("abc-10k.htm", "10-K"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>metadata</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "metadata narrative " * 40

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": "",
                "primary_document": "abc-20241231.htm",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "metadata narrative " * 40)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("abc-20241231.htm", fetched[-1])

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_ignores_exhibit_heavy_stored_text(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        stored = (
            '{"source": "sec_edgar", "files": []}\n'
            + "===== abc-ex101_20241231.htm =====\n"
            + (
                "The agreement addresses revenue sharing, risk allocation and cash "
                "flow guarantees between the parties. " * 300
            )
        )
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "abc-20241231.htm", "size": 5_200_000},
                    {"name": "abc-ex101_20241231.htm", "size": 8_000_000},
                    {"name": "000032019324000123-index.htm", "size": 300_000},
                ]
            }
        }
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("abc-20241231.htm", "10-K"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>Item 7 MD&A</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "Item 7 MD&A narrative " * 100

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": stored,
                "raw_content": b"raw bundle bytes",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "Item 7 MD&A narrative " * 100)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("abc-20241231.htm", fetched[-1])
        self.assertNotIn("abc-ex101_20241231.htm", fetched)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_uses_primary_despite_substantive_stored(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        # STM-style row: stored text carries substantive-looking narrative and
        # raw_content, but the authoritative index page names the primary.
        stored = (
            "ITEM 1. BUSINESS\n"
            + (
                "Our business focuses on AI data-centre demand and supply "
                "capacity. " * 300
            )
            + "\nITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION "
            + "AND RESULTS OF OPERATIONS\n"
            + ("Revenue grew with operating cash flow. " * 300)
        )
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "stm-20251231.htm", "size": 5_200_000},
                    {"name": "stm-20251231_xbrl.htm", "size": 9_000_000},
                    {"name": "000032019324000123-index.htm", "size": 300_000},
                ]
            }
        }
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("stm-20251231.htm", "10-K"),
            ("stm-20251231_xbrl.htm", "XBRL"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>STM primary</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "STM MD&A narrative " * 100

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": stored,
                "raw_content": b"raw bundle bytes",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "STM MD&A narrative " * 100)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("stm-20251231.htm", fetched[-1])
        self.assertNotIn("stm-20251231_xbrl.htm", fetched)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.make_request")
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_prefers_annual_form_row_over_heuristics(
        self,
        validate_url,
        get_shared_client,
        make_request,
        extract_document_text,
    ):
        # Wells-style duplicate report-date filenames: the ticker-date name is
        # the XBRL bundle and the `_d2` suffix name is the Type=10-K primary.
        index_response = MagicMock()
        index_response.raise_for_status = MagicMock()
        index_response.json.return_value = {
            "directory": {
                "item": [
                    {"name": "wfc-20251231.htm", "size": 9_000_000},
                    {"name": "wfc-20251231_d2.htm", "size": 5_200_000},
                    {"name": "wfc-ex10k.htm", "size": 8_000_000},
                    {"name": "000032019324000123-index.htm", "size": 300_000},
                ]
            }
        }
        index_page_response = MagicMock()
        index_page_response.raise_for_status = MagicMock()
        index_page_response.content = sec_index_page(
            ("wfc-20251231_d2.htm", "10-K"),
            ("wfc-20251231.htm", "XBRL"),
            ("wfc-ex10k.htm", "EX-10.K"),
        ).encode()
        index_page_response.headers = {"content-type": "text/html"}
        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.content = b"<html><body>WFC primary</body></html>"
        primary_response.headers = {"content-type": "text/html"}

        make_request.side_effect = sec_directory_fake_request(
            index_response, index_page_response, primary_response
        )
        extract_document_text.return_value = "WFC MD&A narrative " * 100

        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": "",
            },
        )

        self.assertEqual(source, "sec_primary_document")
        self.assertEqual(excerpt, "WFC MD&A narrative " * 100)
        fetched = [str(call.args[1]) for call in make_request.call_args_list]
        self.assertIn("wfc-20251231_d2.htm", fetched[-1])
        self.assertNotIn("wfc-20251231.htm", fetched)
        self.assertNotIn("wfc-ex10k.htm", fetched)

    @patch("investment_service.make_request", side_effect=RuntimeError("boom"))
    @patch("investment_service.get_shared_client")
    @patch(
        "investment_service._validate_public_url",
        side_effect=lambda url: url,
    )
    def test_sec_primary_document_recovery_falls_back_to_stored_on_failure(
        self,
        validate_url,
        get_shared_client,
        make_request,
    ):
        stored = (
            '{"source": "sec_edgar", "files": []}\n'
            + "===== abc-ex101_20241231.htm =====\n"
            + (
                "The agreement addresses revenue sharing, risk allocation and cash "
                "flow guarantees between the parties. " * 300
            )
        )
        excerpt, source = service._load_report_excerpt(
            {},
            {
                "document_id": "doc-1",
                "filing_source": "sec_edgar",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                ),
                "extracted_text": stored,
                "raw_content": b"raw bundle bytes",
            },
        )

        self.assertEqual(source, "stored_document")
        self.assertEqual(excerpt, service.build_analysis_excerpt(stored))

    @patch("investment_service.extract_document_text_path")
    @patch("investment_service.get_session")
    def test_preserves_unextractable_regulatory_content(
        self,
        get_session,
        extract_document_text_path,
    ):
        extract_document_text_path.side_effect = ValueError(
            "document did not contain enough extractable text"
        )
        row = MagicMock()
        row._mapping = {
            "document_id": "doc-1",
            "company": "Example PLC",
            "symbol": "EX",
            "region": "EU",
            "industry": "Unclassified",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.test/document",
            "filing_source": "companies_house",
            "filing_id": "transaction-1",
            "filename": "transaction-1.pdf",
            "mime_type": "application/pdf",
            "status": "ingested",
            "created_at": None,
        }
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value = session_context(session)
        content = b"%PDF-scanned-document"

        result = service.store_document(
            {},
            {
                "company": "Example PLC",
                "symbol": "EX",
                "region": "EU",
                "industry": "Unclassified",
                "document_type": "annual_report",
                "filing_source": "companies_house",
                "filing_id": "transaction-1",
                "filename": "transaction-1.pdf",
            },
            content,
            "application/pdf",
            preserve_content=True,
            allow_unextractable=True,
        )

        params = session.execute.call_args.args[1]
        self.assertEqual(result["document_id"], "doc-1")
        self.assertEqual(params["raw_content"], content)
        self.assertEqual(params["extracted_text"], "")


class InvestmentAggregationTests(unittest.TestCase):
    def test_industry_and_region_use_latest_company_breadth(self):
        analyses = [
            {
                "company": "A",
                "symbol": "A",
                "region": "US",
                "industry": "DRAM",
                "state": {"score": 10},
                "drivers": ["AI demand"],
                "risks": [{"risk": "Oversupply"}],
                "report_date": "2025-12-31",
                "metrics": {
                    "revenue": {"change_pct": 10},
                    "fcf_margin": {"value": 20},
                },
                "fundamentals": {
                    "net_margin_pct": 12,
                    "return_on_equity_pct": 18,
                    "debt_to_equity": 0.5,
                },
                "extraction": {"status": "success"},
            },
            {
                "company": "B",
                "symbol": "B",
                "region": "ASIA",
                "industry": "Semiconductors & Compute",
                "state": {"score": 4},
                "drivers": ["Backlog"],
                "risks": [],
                "report_date": "2025-12-31",
                "metrics": {
                    "revenue": {"change_pct": 2},
                    "fcf_margin": {"value": 12},
                },
                "fundamentals": {
                    "net_margin_pct": 8,
                    "return_on_equity_pct": 10,
                    "debt_to_equity": 1.0,
                },
                "extraction": {"status": "unavailable"},
            },
            {
                "company": "A",
                "symbol": "A",
                "region": "US",
                "industry": "DRAM",
                "state": {"score": -5},
                "drivers": [],
                "risks": [],
                "report_date": "2024-12-31",
                "metrics": {
                    "revenue": {"change_pct": 1},
                    "fcf_margin": {"value": 10},
                },
                "fundamentals": {
                    "net_margin_pct": 6,
                    "return_on_equity_pct": 7,
                    "debt_to_equity": 1.2,
                },
                "extraction": {"status": "success"},
            },
        ]
        industry = next(
            item
            for item in service._aggregate_industries(analyses)
            if item["name"] == "Semiconductors & Compute"
        )
        self.assertEqual(industry["company_count"], 2)
        self.assertEqual(industry["score"], 7.0)
        self.assertEqual(industry["stage"], "confirmed")
        self.assertEqual(industry["breadth_pct"], 50.0)
        self.assertEqual(industry["momentum"]["score_delta"], 12.0)
        self.assertEqual(industry["fundamentals"]["revenue_growth_pct"], 6.0)
        self.assertEqual(industry["fundamentals"]["fcf_margin_pct"], 16.0)
        self.assertEqual(industry["deterministic_company_count"], 1)
        self.assertEqual(industry["deterministic_coverage_pct"], 50.0)
        self.assertEqual(
            industry["driver_claims"][0],
            {"label": "AI demand", "company_count": 1, "breadth_pct": 50.0},
        )
        self.assertEqual(
            industry["risk_claims"][0],
            {"label": "Oversupply", "company_count": 1, "breadth_pct": 50.0},
        )
        comparisons = service._peer_comparisons(analyses)
        comparison = comparisons["a"]
        revenue_peer = comparison["metrics"]["revenue_growth_pct"]
        self.assertEqual(revenue_peer["median"], 2.0)
        self.assertEqual(revenue_peer["delta"], 8.0)
        self.assertIsNone(revenue_peer["percentile"])
        self.assertEqual(revenue_peer["sample_count"], 1)
        self.assertEqual(comparison["company_count"], 1)
        self.assertEqual(comparison["members"][0]["symbol"], "B")
        self.assertNotIn("A", [member["symbol"] for member in comparison["members"]])
        self.assertIn("same canonical industry", comparison["members"][0]["reasons"])

        regions = {item["code"]: item for item in service._aggregate_regions(analyses)}
        self.assertEqual(regions["US"]["company_count"], 1)
        self.assertEqual(regions["US"]["score"], 10.0)

    def test_peer_distance_penalizes_sparse_financial_comparability(self):
        subject = {
            "region": "US",
            "metrics": {
                "revenue": {"change_pct": 10},
                "fcf_margin": {"value": 20},
            },
            "fundamentals": {
                "net_margin_pct": 12,
                "return_on_equity_pct": 18,
                "debt_to_equity": 0.5,
                "capex_to_revenue_pct": 8,
            },
        }
        sparse = {
            "region": "US",
            "metrics": {"revenue": {"change_pct": 10}},
            "fundamentals": {},
        }
        comparable = {
            "region": "EU",
            "metrics": {
                "revenue": {"change_pct": 11},
                "fcf_margin": {"value": 18},
            },
            "fundamentals": {
                "net_margin_pct": 11,
                "return_on_equity_pct": 17,
            },
        }

        sparse_distance, sparse_reasons = service._peer_distance(subject, sparse)
        comparable_distance, _ = service._peer_distance(subject, comparable)

        self.assertEqual(sparse_distance, 10.0)
        self.assertLess(comparable_distance, sparse_distance)
        self.assertIn("limited comparable financial metrics (1/7)", sparse_reasons)

    def test_region_coverage_separates_analyzed_and_configured_companies(self):
        analyses = [
            {
                "company": "Unknown US issuer",
                "symbol": "UNKNOWN-US",
                "region": "US",
                "state": {"score": 3},
            },
            {
                "company": "Unknown Asia issuer",
                "symbol": "UNKNOWN-ASIA",
                "region": "ASIA",
                "state": {"score": 9},
            },
            {
                "region": "EU",
                "state": {"score": 8},
            },
        ]

        regions = {item["code"]: item for item in service._aggregate_regions(analyses)}

        self.assertEqual(regions["US"]["company_count"], 1)
        self.assertEqual(regions["US"]["configured_company_count"], 100)
        self.assertEqual(regions["US"]["coverage_status"], "configured")
        self.assertEqual(regions["EU"]["company_count"], 0)
        self.assertEqual(regions["EU"]["configured_company_count"], 200)
        self.assertEqual(regions["EU"]["coverage_status"], "configured")
        self.assertEqual(regions["ASIA"]["company_count"], 1)
        self.assertEqual(regions["ASIA"]["configured_company_count"], 0)
        self.assertEqual(regions["ASIA"]["coverage_status"], "not_configured")
        self.assertIsNone(regions["ASIA"]["score"])
        self.assertIsNone(regions["ASIA"]["stage"])

    def test_valuation_coverage_counts_only_actual_outputs_and_market_values(self):
        calculated = [
            {
                "valuation": {
                    "dcf": {"status": "calculated", "per_share": 100},
                    "pe_ratio": 20,
                    "margin_of_safety": 0.2,
                }
            }
            for _ in range(38)
        ]
        enterprise_only = [
            {
                "valuation": {
                    "dcf": {
                        "status": "enterprise_value_only",
                        "enterprise_value": 1_000,
                    }
                }
            }
            for _ in range(32)
        ]
        unavailable = [
            {"valuation": {"dcf": {"status": "unavailable"}}} for _ in range(148)
        ]

        coverage = service._valuation_coverage(
            calculated + enterprise_only + unavailable
        )

        self.assertEqual(
            coverage,
            {
                "dcf_calculated_count": 38,
                "dcf_enterprise_value_only_count": 32,
                "dcf_unavailable_count": 148,
                "market_price_count": 0,
                "pe_ratio_count": 0,
                "margin_of_safety_count": 0,
            },
        )

    def test_market_relative_coverage_requires_a_positive_market_price(self):
        analyses = [
            {
                "valuation": {
                    "market_price": 80,
                    "pe_ratio": 16,
                    "margin_of_safety": 0.2,
                    "dcf": {"status": "calculated", "per_share": 100},
                }
            },
            {
                "valuation": {
                    "market_price": None,
                    "pe_ratio": 12,
                    "margin_of_safety": 0.3,
                    "dcf": {"status": "unavailable"},
                }
            },
            {
                "valuation": {
                    "market_price": 0,
                    "pe_ratio": 10,
                    "margin_of_safety": 0.4,
                    "dcf": {"status": "calculated", "per_share": 100},
                }
            },
        ]

        coverage = service._valuation_coverage(analyses)

        self.assertEqual(coverage["market_price_count"], 1)
        self.assertEqual(coverage["pe_ratio_count"], 1)
        self.assertEqual(coverage["margin_of_safety_count"], 1)

    def test_public_close_revalues_only_matching_report_currency(self):
        facts = {
            "metrics": {
                "revenue": metric(1_000),
                "operating_cash_flow": metric(120),
                "capex": metric(20),
                "diluted_eps": metric(2, unit="USD/share"),
                "shares_outstanding": metric(100, unit="million shares"),
                "net_debt": metric(50),
            },
            "prior_metrics": {
                "revenue": metric(900, period="FY2024"),
                "operating_cash_flow": metric(100, period="FY2024"),
                "capex": metric(20, period="FY2024"),
            },
        }
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {},
            "valuation": {"currency_unit": "USDm", "dcf": {"unit": "USDm"}},
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {
                "currency": "USD",
                "provider_symbol": "EX",
                "source_reference": "https://example.test/chart/EX",
                "adjusted": False,
            },
        }

        result = service._attach_public_market_data(payload, facts)

        self.assertEqual(result["valuation"]["market_price"], 50)
        self.assertEqual(result["valuation"]["pe_ratio"], 25)
        self.assertEqual(
            result["valuation"]["market_data"]["comparison_status"], "comparable"
        )
        self.assertEqual(result["metrics"]["market_price"]["source"], "public_equities")
        self.assertIn(
            "unadjusted daily close", result["metrics"]["market_price"]["evidence"]
        )

    def test_public_close_preserves_stored_valuation_inputs(self):
        facts = {
            "metrics": {
                "revenue": metric(1_000),
                "operating_cash_flow": metric(120),
                "capex": metric(20),
                "diluted_eps": metric(2, unit="USD/share"),
            },
            "prior_metrics": {
                "revenue": metric(900, period="FY2024"),
                "operating_cash_flow": metric(100, period="FY2024"),
                "capex": metric(20, period="FY2024"),
            },
        }
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {
                "shares_outstanding": {
                    "value": 100,
                    "unit": "million shares",
                    "source": "manual_input",
                    "period": "valuation input",
                },
                "net_debt": {
                    "value": 20,
                    "unit": "USDm",
                    "source": "manual_input",
                    "period": "valuation input",
                },
            },
            "valuation": {
                "currency_unit": "USDm",
                "dcf": {
                    "unit": "USDm",
                    "assumptions": {
                        "discount_rate": 0.08,
                        "terminal_growth": 0.02,
                        "shares_outstanding": 100,
                        "net_debt": 20,
                    },
                },
            },
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {"currency": "USD"},
        }

        result = service._attach_public_market_data(payload, facts)
        assumptions = result["valuation"]["dcf"]["assumptions"]

        self.assertEqual(assumptions["discount_rate"], 0.08)
        self.assertEqual(assumptions["terminal_growth"], 0.02)
        self.assertEqual(assumptions["shares_outstanding"], 100)
        self.assertEqual(assumptions["net_debt"], 20)
        self.assertEqual(result["valuation"]["dcf"]["status"], "calculated")

    def test_cross_currency_public_close_is_visible_but_not_used(self):
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {},
            "valuation": {"currency_unit": "EURm", "dcf": {"unit": "EURm"}},
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {
                "currency": "USD",
                "source_reference": "https://example.test/chart/EX",
            },
        }

        result = service._attach_public_market_data(payload, {"metrics": {}})

        self.assertEqual(
            result["valuation"]["market_data"]["comparison_status"],
            "currency_mismatch",
        )
        self.assertNotIn("market_price", result["metrics"])
        self.assertNotIn("market_price", result["valuation"])

    def test_gbp_pence_quote_normalizes_before_comparison(self):
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "UK Example",
            "symbol": "EX.L",
            "metrics": {},
            "valuation": {"currency_unit": "GBPm", "dcf": {"unit": "GBPm"}},
            "public_price_timestamp": now,
            "public_price_close": 1_250,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {"currency": "GBp"},
        }

        result = service._attach_public_market_data(payload, {"metrics": {}})

        self.assertEqual(result["valuation"]["market_price"], 12.5)
        self.assertEqual(result["valuation"]["market_data"]["quote_scale"], 0.01)

    def test_analysis_quality_exposes_freshness_completeness_and_warnings(self):
        now = service.datetime.now(service.UTC)
        current_report = service.date.fromordinal(
            service.date.today().toordinal() - 100
        )
        payload = {
            "company": "Example",
            "symbol": "EX",
            "report_date": current_report.isoformat(),
            "source_url": "https://example.test/filing",
            "analysis_updated_at": now,
            "extraction": {
                "status": "success",
                "deterministic_metric_count": 12,
            },
            "metrics": {"revenue": {"value": 100}},
            "evidence": [{"quote": "Revenue increased."}],
            "drivers": ["revenue growth"],
            "risks": ["competition"],
            "valuation": {
                "dcf": {
                    "status": "calculated",
                    "sensitivity": {"status": "calculated"},
                },
                "market_data": {
                    "status": "current",
                    "comparison_status": "comparable",
                },
            },
            "peer_comparison": {
                "company_count": 1,
                "industry_company_count": 5,
                "members": [{"symbol": "PEER"}],
            },
        }

        quality = service._attach_analysis_quality(payload)["quality"]

        self.assertEqual(quality["status"], "ready")
        self.assertEqual(quality["report"]["status"], "current")
        self.assertEqual(quality["deterministic"]["metric_count"], 12)
        self.assertEqual(quality["narrative"]["evidence_quote_count"], 1)
        self.assertEqual(quality["peers"]["selected_count"], 1)
        self.assertEqual(quality["warnings"], [])

        payload["report_date"] = "2020-12-31"
        stale = service._attach_analysis_quality(payload)["quality"]
        self.assertEqual(stale["status"], "stale")
        self.assertIn("annual_report_stale", stale["warnings"])

        payload["report_date"] = None
        unknown = service._attach_analysis_quality(payload)["quality"]
        self.assertEqual(unknown["report"]["status"], "unknown")
        self.assertEqual(unknown["status"], "partial")
        self.assertIn("report_date_unavailable", unknown["warnings"])

    def test_latest_company_baseline_prefers_verified_facts_over_newer_empty_report(
        self,
    ):
        analyses = [
            {
                "company": "Example",
                "symbol": "EX",
                "report_date": "2026-06-30",
                "metrics": {},
                "extraction": {"status": "unavailable"},
            },
            {
                "company": "Example",
                "symbol": "EX",
                "report_date": "2025-12-31",
                "metrics": {"revenue": {"value": 100}},
                "extraction": {
                    "status": "success",
                    "deterministic_metric_count": 1,
                },
            },
        ]

        result = service._latest_company_analyses(analyses)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["report_date"], "2025-12-31")

    @patch("investment_service.load_classified_news", return_value=[])
    @patch("investment_service.get_session")
    def test_dashboard_includes_the_complete_industry_research_ledger(
        self,
        get_session,
        _load_classified_news,
    ):
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = []
        get_session.return_value = session_context(session)

        result = service.get_dashboard({})

        self.assertEqual(
            {item["name"] for item in result["industries"]},
            set(service.ALL_INDUSTRIES),
        )
        self.assertEqual(result["industry_history"], [])
        self.assertEqual(result["research_summary"]["history_point_count"], 0)
        self.assertEqual(
            result["research_summary"]["valuation_coverage"],
            {
                "dcf_calculated_count": 0,
                "dcf_enterprise_value_only_count": 0,
                "dcf_unavailable_count": 0,
                "market_price_count": 0,
                "pe_ratio_count": 0,
                "margin_of_safety_count": 0,
            },
        )
        self.assertEqual(len(result["industries"]), len(service.ALL_INDUSTRIES))

    @patch("investment_service.load_classified_news", return_value=[])
    @patch("investment_service.get_session")
    def test_dashboard_applies_checked_in_industry_to_legacy_rows(
        self,
        get_session,
        _load_classified_news,
    ):
        """Legacy rows with Unclassified or model-derived industries render
        with the checked-in canonical industry at read time (no DB mutation)."""

        def row(mapping):
            return SimpleNamespace(_mapping=mapping)

        def analysis_row(document, industry, classified_industry, analysis_id):
            return {
                "analysis_id": analysis_id,
                "document_id": document["document_id"],
                "facts": {},
                "analysis": {
                    "classification": {
                        "industry": classified_industry,
                        "confidence": "high",
                    },
                    "summary": "Legacy summary",
                    "thesis": "Legacy thesis",
                    "drivers": [],
                    "catalysts": [],
                    "risks": [],
                    "watch_items": [],
                    "score": 4.0,
                    "state": {"score": 4.0},
                },
                "model": "legacy",
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
                "duration_ms": 0,
                "created_at": None,
                "company": document["company"],
                "symbol": document["symbol"],
                "region": document["region"],
                "industry": industry,
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
            }

        def document_row(document_id, company, symbol, region, industry):
            return {
                "document_id": document_id,
                "company": company,
                "symbol": symbol,
                "region": region,
                "industry": industry,
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
                "filename": f"{document_id}.txt",
                "status": "analyzed",
                "error_message": None,
                "created_at": None,
            }

        mu = document_row("d-mu", "Micron Technology", "MU", "US", "Unclassified")
        stm = document_row(
            "d-stm",
            "STMicroelectronics",
            "STM",
            "EU",
            "Software, Cloud & Communications",
        )
        gs = document_row("d-gs", "Goldman Sachs", "GS", "US", "Unclassified")
        ex = document_row("d-ex", "Example PLC", "EX", "EU", "Unclassified")
        document_rows = [mu, stm, gs, ex]
        company_rows = [
            {key: item[key] for key in ("company", "symbol", "industry", "region")}
            for item in document_rows
        ]
        analysis_rows = [
            analysis_row(mu, "Unclassified", "Consumer", "a-mu"),
            analysis_row(
                stm,
                "Software, Cloud & Communications",
                "Software, Cloud & Communications",
                "a-stm",
            ),
            analysis_row(gs, "Unclassified", "Unclassified", "a-gs"),
            analysis_row(ex, "Unclassified", "Unclassified", "a-ex"),
        ]

        documents_result = MagicMock()
        documents_result.fetchall.return_value = [row(item) for item in document_rows]
        companies_result = MagicMock()
        companies_result.fetchall.return_value = [row(item) for item in company_rows]
        analyses_result = MagicMock()
        analyses_result.fetchall.return_value = [row(item) for item in analysis_rows]
        observations_result = MagicMock()
        observations_result.fetchall.return_value = []
        session = MagicMock()
        session.execute.side_effect = [
            documents_result,
            companies_result,
            analyses_result,
            observations_result,
        ]
        get_session.return_value = session_context(session)

        result = service.get_dashboard({})

        documents_by_symbol = {item["symbol"]: item for item in result["documents"]}
        self.assertEqual(
            documents_by_symbol["MU"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            documents_by_symbol["STM"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            documents_by_symbol["GS"]["industry"], "Financials & Real Estate"
        )
        self.assertEqual(documents_by_symbol["EX"]["industry"], "Unclassified")

        analyses_by_symbol = {item["symbol"]: item for item in result["analyses"]}
        self.assertEqual(
            analyses_by_symbol["MU"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            analyses_by_symbol["MU"]["classification"]["industry"],
            "Semiconductors & Compute",
        )
        self.assertEqual(
            analyses_by_symbol["STM"]["classification"]["industry"],
            "Semiconductors & Compute",
        )
        self.assertEqual(
            analyses_by_symbol["GS"]["classification"]["industry"],
            "Financials & Real Estate",
        )
        self.assertEqual(
            analyses_by_symbol["EX"]["classification"]["industry"], "Unclassified"
        )

        industries = {item["name"]: item for item in result["industries"]}
        self.assertEqual(industries["Semiconductors & Compute"]["company_count"], 2)
        self.assertEqual(industries["Financials & Real Estate"]["company_count"], 1)
        self.assertEqual(industries["Unclassified"]["company_count"], 1)


class InvestmentAnalysisServiceTests(unittest.TestCase):
    def test_analysis_industry_precedence_uses_checked_in_metadata_first(self):
        cases = [
            # Checked-in issuer metadata wins over any model label.
            (
                {
                    "symbol": "MU",
                    "company": "Micron Technology",
                    "industry": "Unclassified",
                },
                {"industry": "Consumer", "confidence": "high"},
                "Semiconductors & Compute",
            ),
            (
                {
                    "symbol": "MU",
                    "company": "Micron Technology",
                    "industry": "Unclassified",
                },
                {"industry": "Unclassified", "confidence": "low"},
                "Semiconductors & Compute",
            ),
            # Model Unclassified must not overwrite the document industry.
            (
                {
                    "symbol": "ZZZZ",
                    "company": "Unknown Co",
                    "industry": "Semiconductors & Compute",
                },
                {"industry": "Unclassified", "confidence": "high"},
                "Semiconductors & Compute",
            ),
            # Low-confidence model labels must not overwrite the document industry.
            (
                {
                    "symbol": "ZZZZ",
                    "company": "Unknown Co",
                    "industry": "Semiconductors & Compute",
                },
                {"industry": "Consumer", "confidence": "low"},
                "Semiconductors & Compute",
            ),
            # A concrete, trusted model label still classifies unknown issuers.
            (
                {"symbol": "ZZZZ", "company": "Unknown Co", "industry": "Unclassified"},
                {"industry": "Beverages", "confidence": "moderate"},
                "Consumer",
            ),
            # Unknown issuer with only a low-confidence model label fails closed.
            (
                {"symbol": "ZZZZ", "company": "Unknown Co", "industry": "Unclassified"},
                {"industry": "Consumer", "confidence": "low"},
                "Unclassified",
            ),
        ]
        for document, classification, expected in cases:
            with self.subTest(document=document, classification=classification):
                self.assertEqual(
                    service._resolve_analysis_industry(document, classification),
                    expected,
                )

    def test_analysis_stores_checked_in_industry_over_model_classification(self):
        document_id = "33333333-3333-3333-3333-333333333333"
        analysis_id = "44444444-4444-4444-4444-444444444444"
        document = {
            "document_id": document_id,
            "company": "Micron Technology",
            "symbol": "MU",
            "region": "US",
            "industry": "Unclassified",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": "Annual report evidence. " * 20,
        }
        facts = {
            "classification": {
                "document_type": "annual_report",
                "sector": "Technology",
                "industry": "Consumer",
                "region": "US",
                "confidence": "low",
            },
            "qualitative": {
                "ai_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "AI demand",
                },
                "datacenter_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "data-centre",
                },
                "supply_constraints": {
                    "present": True,
                    "strength": "moderate",
                    "evidence": "tight supply",
                },
                "pricing_power": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "higher pricing",
                },
                "guidance_up": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "raised",
                },
                "guidance_down": {"present": False, "strength": "none", "evidence": ""},
            },
            "summary": "Demand, capex and pricing are accelerating.",
            "thesis": "The cycle is strengthening.",
            "drivers": ["AI demand"],
            "catalysts": [
                {
                    "catalyst": "Capacity ramp",
                    "horizon": "12 months",
                    "evidence": "capex",
                }
            ],
            "risks": [
                {
                    "risk": "Oversupply",
                    "likelihood": "medium",
                    "impact": "high",
                    "mitigation": "Monitor inventory",
                    "evidence": "capacity",
                }
            ],
            "watch_items": ["Inventory growth"],
        }

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        persist_session = MagicMock()
        insert_result = MagicMock()
        insert_result.fetchone.return_value = (analysis_id,)
        persist_session.execute.side_effect = [
            insert_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1000,
            tokens_output_total=400,
            cost_usd_total=0.001,
            first_attempt_duration_ms=150,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.return_value = {"content": json.dumps(facts)}

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(persist_session),
                ],
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage),
            patch.object(
                service, "get_analysis", return_value={"analysis_id": analysis_id}
            ),
        ):
            service.analyze_document({}, document_id)

        statements = [
            str(call.args[0]) for call in persist_session.execute.call_args_list
        ]
        update_index = next(
            index
            for index, statement in enumerate(statements)
            if "UPDATE investment_documents" in statement
        )
        self.assertEqual(
            persist_session.execute.call_args_list[update_index].args[1]["industry"],
            "Semiconductors & Compute",
        )
        insert_index = next(
            index
            for index, statement in enumerate(statements)
            if "INSERT INTO investment_analyses" in statement
        )
        stored_analysis = json.loads(
            persist_session.execute.call_args_list[insert_index].args[1]["analysis"]
        )
        self.assertEqual(
            stored_analysis["classification"]["industry"], "Semiconductors & Compute"
        )

    @patch("investment_service.get_session")
    def test_get_analysis_applies_checked_in_industry_to_legacy_row(self, get_session):
        row = SimpleNamespace(
            _mapping={
                "analysis_id": "a-mu",
                "document_id": "d-mu",
                "previous_document_id": None,
                "facts": {},
                "analysis": {
                    "classification": {
                        "industry": "Unclassified",
                        "confidence": "low",
                    },
                    "summary": "Legacy summary",
                    "thesis": "Legacy thesis",
                    "drivers": [],
                    "catalysts": [],
                    "risks": [],
                    "watch_items": [],
                },
                "model": "legacy",
                "created_at": None,
                "updated_at": None,
                "company": "Micron Technology",
                "symbol": "MU",
                "region": "US",
                "industry": "Unclassified",
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
            }
        )
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value = session_context(session)

        result = service.get_analysis({}, "a-mu")

        self.assertEqual(result["industry"], "Semiconductors & Compute")
        self.assertEqual(
            result["classification"]["industry"], "Semiconductors & Compute"
        )

    def test_analysis_uses_strict_luna_schema_and_records_cost_duration(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        document = {
            "document_id": document_id,
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": "Annual report evidence. " * 20,
        }
        facts = {
            "classification": {
                "document_type": "annual_report",
                "sector": "Technology",
                "industry": "Memory semiconductors",
                "region": "Asia",
                "confidence": "high",
            },
            "qualitative": {
                "ai_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "AI demand",
                },
                "datacenter_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "data-centre",
                },
                "supply_constraints": {
                    "present": True,
                    "strength": "moderate",
                    "evidence": "tight supply",
                },
                "pricing_power": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "higher pricing",
                },
                "guidance_up": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "raised",
                },
                "guidance_down": {"present": False, "strength": "none", "evidence": ""},
            },
            "summary": "Demand, capex and pricing are accelerating.",
            "thesis": "The cycle is strengthening; falling demand would invalidate it.",
            "drivers": ["AI demand"],
            "catalysts": [
                {
                    "catalyst": "Capacity ramp",
                    "horizon": "12 months",
                    "evidence": "capex",
                }
            ],
            "risks": [
                {
                    "risk": "Oversupply",
                    "likelihood": "medium",
                    "impact": "high",
                    "mitigation": "Monitor inventory",
                    "evidence": "capacity",
                }
            ],
            "watch_items": ["Inventory growth"],
        }

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        persist_session = MagicMock()
        insert_result = MagicMock()
        insert_result.fetchone.return_value = (analysis_id,)
        persist_session.execute.side_effect = [
            insert_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1200,
            tokens_output_total=500,
            cost_usd_total=0.002,
            first_attempt_duration_ms=200,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.return_value = {"content": json.dumps(facts)}

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(persist_session),
                ],
            ),
            patch.object(
                service,
                "_load_news_context",
                return_value=[{"source": "Reuters", "title": "AI demand"}],
            ),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage) as stage_class,
            patch.object(
                service, "get_analysis", return_value={"analysis_id": analysis_id}
            ) as get_analysis,
        ):
            result = service.analyze_document({}, document_id)

        self.assertEqual(result["analysis_id"], analysis_id)
        self.assertEqual(stage_class.call_args.args[1], "investment_analysis")
        schema = stage_class.call_args.kwargs["response_schema"]
        self.assertEqual(schema["name"], "investment_report_narrative")
        self.assertNotIn("metrics", schema["schema"]["properties"])
        self.assertNotIn("prior_metrics", schema["schema"]["properties"])
        prompt = stage.call.call_args.args[0]
        self.assertIn("Reuters", prompt)
        self.assertIn("FILING EXCERPT", prompt)
        statements = [
            str(call.args[0]) for call in persist_session.execute.call_args_list
        ]
        self.assertTrue(
            any("INSERT INTO processing_log" in statement for statement in statements)
        )
        processing_params = persist_session.execute.call_args_list[-1].args[1]
        self.assertEqual(processing_params["cost_usd"], 0.002)
        self.assertEqual(processing_params["model_used"], "openai/gpt-5.6-luna")
        get_analysis.assert_called_once_with({}, analysis_id)


class InvestmentUrlIngestModelTests(unittest.TestCase):
    def _model(self):
        from contracts import InvestmentUrlIngestRequest

        return InvestmentUrlIngestRequest

    def test_rejects_unknown_metadata_fields(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {
                    "url": "https://example.test/r",
                    "company": "C",
                    "mystery_field": 1,
                }
            )

    def test_rejects_oversized_url(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {
                    "url": "https://example.test/" + "x" * 2100,
                    "company": "C",
                }
            )

    def test_rejects_non_bool_analyze(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {"url": "https://example.test/r", "company": "C", "analyze": "yes"}
            )

    def test_requires_url_and_company(self):
        with self.assertRaises(ValueError):
            self._model().model_validate({"company": "C"})
        with self.assertRaises(ValueError):
            self._model().model_validate({"url": "https://example.test/r"})

    def test_accepts_known_bounded_shape(self):
        model = self._model().model_validate(
            {
                "url": "https://example.test/r",
                "company": "C",
                "symbol": "X",
                "region": "US",
                "document_type": "annual_report",
                "analyze": True,
            }
        )
        self.assertEqual(model.url, "https://example.test/r")
        self.assertTrue(model.analyze)
        self.assertIsNone(model.filename)


if __name__ == "__main__":
    unittest.main()
