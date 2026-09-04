"""Document ingestion, OCR, extraction, sectioning, and CAS storage."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import warnings
import zipfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from http_client import PublicOnlyHTTPTransport
from investment_news import canonicalize_industry
from investment_universe import industry_for
from logging_config import get_logger
from sqlalchemy import text

from contracts.outbound_security import (
    OutboundSecurityError,
    resolve_redirect_url,
    validate_public_url,
)
from db import get_session

logger = get_logger("investment.ingest")


class AnalysisInProgress(RuntimeError):
    pass


MAX_DOCUMENT_BYTES = 20_000_000
MAX_EXTRACTED_CHARS = 1_000_000
MAX_REGULATORY_DOCUMENT_BYTES = 100_000_000
MAX_OCR_PAGES = 500
OCR_WORKERS = 2
OCR_WALL_SECONDS = 900.0
# Defensible bounds for synchronous (HTTP-bound) extraction: a single request
# may sample at most these pages/seconds and each subprocess is individually
# time-boxed. The durable analysis worker may pass the larger budgets above.
SYNC_OCR_PAGE_BUDGET = 50
SYNC_OCR_WALL_SECONDS = 120.0
OCR_SUBPROCESS_TIMEOUT = 30
# Global cap on concurrent OCR subprocesses across the whole process so
# parallel requests/workers cannot exhaust CPU/memory.
_OCR_SUBPROCESS_LIMIT = 4
_ocr_subprocess_semaphore = threading.BoundedSemaphore(_OCR_SUBPROCESS_LIMIT)
FETCH_DEADLINE_SECONDS = 120.0
MAX_ANALYSIS_CHARS = 120_000


def _prlimit_prefix() -> list[str] | None:
    """Optional ``prlimit`` wrapper for per-subprocess OS resource limits.

    ``preexec_fn`` is deliberately avoided: it is a documented deadlock risk
    when spawning from multithreaded parents (ThreadPoolExecutor workers).
    Instead the util-linux ``prlimit`` wrapper applies RLIMIT_AS/FSIZE/CPU in
    the child when installed; the deployment additionally bounds these
    processes via container cgroup/pids limits, and every subprocess runs in
    its own session/process group so a timeout kills the whole group.
    """
    if os.name != "posix" or shutil.which("prlimit") is None:
        return None
    return [
        "prlimit",
        "--as=536870912",
        "--fsize=268435456",
        "--cpu=30",
    ]


def _kill_process_group(process: subprocess.Popen) -> None:
    """Terminate the whole OCR subprocess group (children included).

    ``start_new_session`` makes the child its own session/process-group
    leader, so its pid IS the group id; ``killpg(pid, SIGKILL)`` therefore
    kills the entire group (renderer grandchildren included) without a
    separate ``getpgid`` probe that could fail before the kill is reached
    on the timeout path.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _run_ocr_subprocess(
    args: list[str],
    *,
    capture: bool,
    timeout: float,
    env: dict | None = None,
) -> bytes:
    """Run an OCR subprocess with per-request OS resource limits.

    On POSIX the child runs in its own session/process group
    (``start_new_session``) so a timed-out renderer's whole group is killed;
    when the util-linux ``prlimit`` wrapper is installed it additionally
    caps address space, file size and CPU seconds. No ``preexec_fn`` is used
    (documented deadlock risk in multithreaded parents); deployment-level
    cgroup/pids limits back this up.
    """
    command = args
    prefix = _prlimit_prefix()
    if prefix is not None:
        command = prefix + list(args)
    if os.name == "posix":
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            process.wait()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, args)
        return stdout or b""
    result = subprocess.run(
        args,
        capture_output=capture,
        check=True,
        timeout=timeout,
        env=env,
    )
    return result.stdout or b""


MAX_REDIRECTS = 4
MODEL_ID = "openai/gpt-5.6-luna"
INVESTMENT_ANALYSIS_RULE_VERSION = "7"
MATERIALITY_ASSESSMENT_TOPICS = (
    "forward_guidance",
    "reported_variance_driver",
    "margin_economics",
    "capital_commitment_duration",
)
REGIONS = frozenset({"US", "EU", "ASIA"})
DOCUMENT_TYPES = frozenset(
    {
        "annual_report",
        "quarterly_report",
        "investor_report",
        "earnings_release",
        "investor_presentation",
        "regulatory_filing",
        "other",
    }
)
METRIC_NAMES = (
    "revenue",
    "operating_cash_flow",
    "capex",
    "net_income",
    "diluted_eps",
    "shares_outstanding",
    "market_price",
    "net_debt",
    "gross_margin",
    "inventory",
    "backlog",
    "gross_profit",
    "cash",
    "total_debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
)
QUALITATIVE_NAMES = (
    "ai_demand",
    "datacenter_demand",
    "supply_constraints",
    "pricing_power",
    "guidance_up",
    "guidance_down",
)
FREE_REPORT_SOURCES = (
    {"region": "US", "name": "SEC EDGAR", "url": "https://www.sec.gov/edgar/search/"},
    {"region": "EU", "name": "ESEF filings", "url": "https://filings.xbrl.org/"},
    {
        "region": "ASIA",
        "name": "Japan EDINET",
        "url": "https://disclosure2.edinet-fsa.go.jp/",
    },
    {"region": "ASIA", "name": "HKEXnews", "url": "https://www1.hkexnews.hk/index.htm"},
)
_ANALYSIS_KEYWORDS = re.compile(
    r"revenue|sales|cash flow|capital expenditure|capex|net income|earnings per share|"
    r"gross margin|inventory|backlog|artificial intelligence|\bai\b|data[ -]?cent(?:er|re)|"
    r"supply|capacity|pricing|guidance|outlook|risk|demand",
    re.IGNORECASE,
)
_FINANCIAL_STATEMENT_RE = re.compile(
    r"primary statements|consolidated (?:income statement|balance sheet|cash flow statement|"
    r"statement of (?:comprehensive income|financial position|cash flows))|"
    r"statement of (?:profit or loss|financial position|cash flows)",
    re.IGNORECASE,
)


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def normalize_metadata(metadata: dict, *, default_filename: str = "report.txt") -> dict:
    company = _clean_text(metadata.get("company"), limit=160)
    industry = canonicalize_industry(metadata.get("industry"))
    if not company:
        raise ValueError("company is required")
    if industry == "Unclassified":
        # Checked-in issuer metadata fills gaps: a configured issuer is never
        # left Unclassified just because the intake record omitted a label.
        industry = industry_for(metadata.get("symbol"), company)

    region = _clean_text(metadata.get("region"), limit=16).upper()
    if region not in REGIONS:
        raise ValueError("region must be US, EU, or ASIA")
    document_type = _clean_text(metadata.get("document_type"), limit=40).lower()
    if document_type not in DOCUMENT_TYPES:
        raise ValueError("unsupported document_type")

    raw_date = _clean_text(metadata.get("report_date"), limit=10)
    report_date = None
    if raw_date:
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError("report_date must use YYYY-MM-DD") from exc

    filename = PurePath(
        unquote(_clean_text(metadata.get("filename"), limit=240) or default_filename)
    ).name
    if not filename or filename in {".", ".."}:
        filename = default_filename

    source_url = _clean_text(metadata.get("source_url"), limit=2048) or None
    symbol = _clean_text(metadata.get("symbol"), limit=24).upper() or None
    filing_source = _clean_text(metadata.get("filing_source"), limit=40).lower() or None
    filing_id = _clean_text(metadata.get("filing_id"), limit=160) or None
    return {
        "company": company,
        "symbol": symbol,
        "region": region,
        "industry": industry,
        "document_type": document_type,
        "report_date": report_date,
        "source_url": source_url,
        "filing_source": filing_source,
        "filing_id": filing_id,
        "filename": filename,
    }


def _extract_pdf(
    path: str | Path,
    *,
    page_budget: int = SYNC_OCR_PAGE_BUDGET,
    wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> str:
    """Extract embedded text with bounded poppler subprocesses.

    Page count comes from ``pdfinfo`` and text from ``pdftotext -f 1 -l
    <page_budget>``; both run through the prlimit/process-group bounded
    subprocess runner with timeouts clamped to the remaining wall budget, so
    a hostile PDF cannot hang the caller (hard wall isolation: extraction
    never runs in-process). Falls back to OCR when too little text is
    embedded or the text pass times out.
    """
    if not shutil.which("pdfinfo") or not shutil.which("pdftotext"):
        return ""
    deadline = time.monotonic() + max(0.0, float(wall_seconds))
    page_limit = max(1, min(MAX_OCR_PAGES, page_budget))
    try:
        info_out = _run_ocr_subprocess(
            ["pdfinfo", str(path)],
            capture=True,
            timeout=min(
                OCR_SUBPROCESS_TIMEOUT,
                max(0.5, deadline - time.monotonic()),
            ),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    page_count = MAX_OCR_PAGES
    for line in info_out.decode("utf-8", errors="replace").splitlines():
        if line.startswith("Pages:"):
            try:
                page_count = min(int(line.split(":", 1)[1].strip()), MAX_OCR_PAGES)
            except ValueError:
                pass
            break
    try:
        direct = _run_ocr_subprocess(
            [
                "pdftotext",
                "-f",
                "1",
                "-l",
                str(page_limit),
                "-layout",
                str(path),
                "-",
            ],
            capture=True,
            timeout=min(
                OCR_SUBPROCESS_TIMEOUT,
                max(0.5, deadline - time.monotonic()),
            ),
        ).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        # Text pass timed out or failed mid-way: fall through to OCR with the
        # remaining budget instead of hanging the caller.
        direct = ""
    direct = "\n".join(line.rstrip() for line in direct.splitlines())
    direct = re.sub(r"\n{3,}", "\n\n", direct).strip()
    if len(direct) >= 100:
        return direct
    # The OCR fallback inherits only the REMAINING wall budget so direct
    # text + OCR together stay within the configured wall cap (never a
    # fresh full deadline).
    remaining_wall = max(0.0, deadline - time.monotonic())
    if remaining_wall <= 0:
        return ""
    return _ocr_pdf(
        Path(path),
        page_count,
        page_budget=page_budget,
        wall_seconds=remaining_wall,
    )


def _remaining_subprocess_timeout(deadline: float | None) -> float | None:
    """Clamp one subprocess timeout to the remaining wall budget.

    Recomputed immediately before each launch (including after any blocking
    semaphore wait), so a long queue cannot launch with a stale, oversized
    timeout. Returns None when the deadline has already passed.
    """
    if deadline is None:
        return float(OCR_SUBPROCESS_TIMEOUT)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(float(OCR_SUBPROCESS_TIMEOUT), max(0.5, remaining))


def _ocr_pdf_page(
    pdf_path: Path,
    output_dir: Path,
    page_number: int,
    dpi: int,
    *,
    deadline: float | None = None,
) -> str:
    """OCR one page; no subprocess is launched once ``deadline`` has passed.

    The per-subprocess timeout is re-clamped to the remaining budget inside
    the semaphore immediately before each launch (and again before
    tesseract), so a queued batch cannot run far beyond the wall budget.
    """
    now = time.monotonic()
    if deadline is not None and now >= deadline:
        return ""
    prefix = output_dir / f"page-{page_number}-{dpi}"
    image_path = prefix.with_suffix(".pgm")
    try:
        with _ocr_subprocess_semaphore:
            timeout = _remaining_subprocess_timeout(deadline)
            if timeout is None:
                return ""
            _run_ocr_subprocess(
                [
                    "pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-r",
                    str(dpi),
                    "-gray",
                    str(pdf_path),
                    str(prefix),
                ],
                capture=False,
                timeout=timeout,
            )
            timeout = _remaining_subprocess_timeout(deadline)
            if timeout is None:
                return ""
            stdout = _run_ocr_subprocess(
                [
                    "tesseract",
                    str(image_path),
                    "stdout",
                    "--psm",
                    "6",
                    "-c",
                    "preserve_interword_spaces=1",
                ],
                capture=True,
                timeout=timeout,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
            return stdout.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        image_path.unlink(missing_ok=True)


def _sample_pages(page_count: int, budget: int) -> set[int]:
    """Head + tail + stratified-middle page sample within ``budget`` pages."""
    if page_count <= budget:
        return set(range(1, page_count + 1))
    head = max(1, budget // 5)
    tail = max(1, budget // 5)
    middle = max(0, budget - head - tail)
    sampled = set(range(1, head + 1))
    sampled.update(range(page_count - tail + 1, page_count + 1))
    start = head + 1
    end = page_count - tail
    if middle > 0 and end > start:
        step = max(1, math.ceil((end - start) / middle))
        sampled.update(range(start, end + 1, step))
    return sampled


def _ocr_pdf(
    pdf_path: Path,
    page_count: int,
    *,
    page_budget: int = SYNC_OCR_PAGE_BUDGET,
    wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> str:
    """OCR ``pdf_path`` bounded by page, wall-clock and subprocess budgets.

    Total pages across all passes never exceed ``page_budget``, no new
    subprocess is launched after the wall-clock deadline, and every
    pdftoppm/tesseract invocation is individually time-boxed. The sampling
    covers document head, tail and a stratified middle so large documents
    degrade gracefully instead of tying the caller to a 15-minute job.
    """
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    page_budget = max(1, min(page_count, page_budget))
    deadline = time.monotonic() + max(0.0, float(wall_seconds))
    processed: set[int] = set()
    initial_pages = _sample_pages(page_count, page_budget)
    with tempfile.TemporaryDirectory(prefix="investment-ocr-") as directory:
        output_dir = Path(directory)

        def _run_pages(pages: set[int], dpi: int) -> dict[int, str]:
            """Run one OCR pass, bounded by the shared page and wall budgets.

            Pages are selected only while the budget remains, every task
            re-checks the deadline before launching subprocesses, and once
            the deadline expires pending futures are cancelled and the
            executor is shut down without waiting for the tail, so a queued
            batch cannot run hours past ``wall_seconds``.
            """
            selected = []
            for page in sorted(pages):
                if page in processed or len(processed) >= page_budget:
                    break
                if time.monotonic() >= deadline:
                    break
                processed.add(page)
                selected.append(page)
            if not selected:
                return {}
            results: dict[int, str] = {}
            futures: dict = {}
            executor = ThreadPoolExecutor(max_workers=OCR_WORKERS)
            try:
                futures = {
                    executor.submit(
                        _ocr_pdf_page,
                        pdf_path,
                        output_dir,
                        page,
                        dpi,
                        deadline=deadline,
                    ): page
                    for page in selected
                }
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        value = future.result()
                    except Exception:
                        value = ""
                    if value:
                        results[page] = value
                    if time.monotonic() >= deadline:
                        break
            finally:
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
            return results

        first_pass = _run_pages(initial_pages, 140)
        statement_pages = {
            page
            for page, page_text in first_pass.items()
            if _FINANCIAL_STATEMENT_RE.search(page_text)
        }
        focused_pages = {
            page
            for match_page in statement_pages
            for page in range(
                max(1, match_page - 6), min(page_count, match_page + 6) + 1
            )
        }
        focused_text = _run_pages(focused_pages, 180)
        first_pass.update(
            {page: value for page, value in focused_text.items() if value}
        )
    return "\n\n".join(
        f"[Page {page}]\n{page_text}"
        for page, page_text in sorted(first_pass.items())
        if page_text
    )


class _ReportTextHTMLParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "caption",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
        }
    )
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "hidden"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.skip_depth = 0

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.casefold().split(":")[-1]

    def _newline(self) -> None:
        if self.length < MAX_EXTRACTED_CHARS and (
            not self.parts or not self.parts[-1].endswith("\n")
        ):
            self.parts.append("\n")
            self.length += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        local = self._local_name(tag)
        if self.skip_depth or local in self._SKIP_TAGS:
            self.skip_depth += 1
        elif local in self._BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        local = self._local_name(tag)
        if self.skip_depth:
            self.skip_depth -= 1
        elif local in self._BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth or self.length >= MAX_EXTRACTED_CHARS:
            return
        value = data[: MAX_EXTRACTED_CHARS - self.length]
        self.parts.append(value)
        self.length += len(value)


def _extract_report_markup(markup: bytes) -> str:
    parser = _ReportTextHTMLParser()
    parser.feed(markup.decode("utf-8", "replace"))
    parser.close()
    return "".join(parser.parts).strip()


MAX_ARCHIVE_ENTRIES = 10_000
MAX_DOCX_ARCHIVE_ENTRIES = 5_000


def _reject_unsafe_archive(archive: zipfile.ZipFile, *, max_entries: int) -> None:
    """Bound central-directory work and reject encrypted archives early.

    A small upload can carry hundreds of thousands of tiny entries whose
    central directory would exhaust memory/CPU before any uncompressed byte
    cap applies; the entry-count cap bounds that work (infolist() is
    materialized once, but the expensive candidate sort/read work is capped)
    and encrypted members are rejected up front since reading them would fail
    anyway.
    """
    entries = archive.infolist()
    if len(entries) > max_entries:
        raise ValueError(f"archive contains too many entries ({len(entries)})")
    for info in entries:
        if info.flag_bits & 0x1:
            raise ValueError("encrypted archives are not supported")


def _extract_report_package(path: str | Path) -> str:
    sections = []
    total_uncompressed = 0
    with zipfile.ZipFile(path) as archive:
        _reject_unsafe_archive(archive, max_entries=MAX_ARCHIVE_ENTRIES)
        candidates = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and info.filename.casefold().endswith((".xhtml", ".html", ".htm"))
            and info.file_size <= MAX_REGULATORY_DOCUMENT_BYTES
        ]
        for info in sorted(candidates, key=lambda item: item.file_size, reverse=True):
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_REGULATORY_DOCUMENT_BYTES:
                break
            markup = archive.read(info)
            report_text = _extract_report_markup(markup)
            if report_text:
                sections.append(
                    f"[Report file {PurePath(info.filename).name}]\n{report_text}"
                )
            if sum(len(section) for section in sections) >= MAX_EXTRACTED_CHARS:
                break
    return "\n\n".join(sections)


def _extract_docx(path: str | Path) -> str:
    with zipfile.ZipFile(path) as archive:
        _reject_unsafe_archive(archive, max_entries=MAX_DOCX_ARCHIVE_ENTRIES)
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCUMENT_BYTES * 2:
            raise ValueError("DOCX document XML is too large")
        root = ElementTree.fromstring(archive.read(info))
    paragraphs = []
    for paragraph in root.iter(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    ):
        words = [
            node.text or ""
            for node in paragraph.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            )
        ]
        if words:
            paragraphs.append("".join(words))
    return "\n".join(paragraphs)


def _bounded_extracted_text(document_text: str) -> str:
    if len(document_text) <= MAX_EXTRACTED_CHARS:
        return document_text

    prefix_end = 120_000
    suffix_start = len(document_text) - 180_000
    windows = [(0, prefix_end), (suffix_start, len(document_text))]
    remaining = MAX_EXTRACTED_CHARS - 320_000

    for pattern, before, after in (
        (_FINANCIAL_STATEMENT_RE, 25_000, 80_000),
        (_ANALYSIS_KEYWORDS, 2_000, 5_000),
    ):
        for match in pattern.finditer(document_text):
            if remaining <= 0:
                break
            start = max(prefix_end, match.start() - before)
            end = min(suffix_start, match.end() + after)
            if end <= start:
                continue
            length = min(end - start, remaining)
            windows.append((start, start + length))
            remaining -= length

    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 300:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return "\n\n".join(
        f"[Source characters {start}-{end}]\n{document_text[start:end]}"
        for start, end in merged
    )[:MAX_EXTRACTED_CHARS]


def extract_document_text_path(
    path: str | Path,
    filename: str,
    mime_type: str | None,
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    ocr_page_budget: int = SYNC_OCR_PAGE_BUDGET,
    ocr_wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> str:
    """Extract text from a document on disk without a full-file byte read.

    PDF/zip/DOCX formats are consumed path-first (poppler pdfinfo/pdftotext/
    pdftoppm, tesseract, and zipfile all accept a path); only the genuinely
    textual formats read the bounded file bytes. OCR and direct text passes
    are bounded by ``ocr_page_budget`` pages and ``ocr_wall_seconds`` wall
    time; the durable worker may pass the larger
    ``MAX_OCR_PAGES``/``OCR_WALL_SECONDS``.
    """
    if not path or not os.path.exists(path):
        raise ValueError("document is empty")
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError("document is empty")
    if size > max_bytes:
        raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")

    with open(path, "rb") as handle:
        magic = handle.read(4)
    content_type = (mime_type or "").split(";", 1)[0].strip().lower()
    suffix = PurePath(filename.lower()).suffix
    is_docx = (
        content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    )
    if magic.startswith(b"%PDF-"):
        extracted = _extract_pdf(
            path, page_budget=ocr_page_budget, wall_seconds=ocr_wall_seconds
        )
    elif magic.startswith(b"PK\x03\x04"):
        extracted = _extract_docx(path) if is_docx else _extract_report_package(path)
    elif content_type == "application/pdf" or suffix == ".pdf":
        extracted = _extract_pdf(
            path, page_budget=ocr_page_budget, wall_seconds=ocr_wall_seconds
        )
    elif is_docx:
        extracted = _extract_docx(path)
    elif (
        content_type in {"application/zip", "application/x-zip-compressed"}
        or suffix == ".zip"
    ):
        extracted = _extract_report_package(path)
    elif suffix in {".xml", ".xsd"} or content_type in {"application/xml", "text/xml"}:
        extracted = _read_file_text(path, size)
    elif content_type in {"text/html", "application/xhtml+xml"} or suffix in {
        ".html",
        ".htm",
    }:
        with open(path, "rb") as handle:
            markup = handle.read()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(markup, "lxml")
        for node in soup(["script", "style", "noscript", "svg"]):
            node.decompose()
        extracted = soup.get_text("\n")
    elif content_type in {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/octet-stream",
        "",
    } or suffix in {".txt", ".md", ".csv", ".json"}:
        extracted = _read_file_text(path, size)
    else:
        raise ValueError(
            "supported formats are PDF, DOCX, HTML, text, Markdown, CSV, JSON, and XML"
        )

    extracted = extracted.replace("\x00", "")
    extracted = "\n".join(line.rstrip() for line in extracted.splitlines())
    extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
    if len(extracted) < 100:
        raise ValueError("document did not contain enough extractable text")
    return _bounded_extracted_text(extracted)


def _read_file_text(path: str | Path, size: int) -> str:
    """Read a bounded textual document fully (size was already capped)."""
    with open(path, "rb") as handle:
        content = handle.read()
    return content.decode("utf-8", errors="replace")


def extract_document_text(
    content: bytes,
    filename: str,
    mime_type: str | None,
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    ocr_page_budget: int = SYNC_OCR_PAGE_BUDGET,
    ocr_wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> str:
    """Bytes entry point; spools to a temporary file for path-based extraction.

    Keeps the in-memory callers (URL fetches, stored regulatory content) on
    one extraction pipeline without holding the file in memory twice.
    """
    if not content:
        raise ValueError("document is empty")
    if len(content) > max_bytes:
        raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")
    fd, path = tempfile.mkstemp(prefix="investment-extract-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
        return extract_document_text_path(
            path,
            filename,
            mime_type,
            max_bytes=max_bytes,
            ocr_page_budget=ocr_page_budget,
            ocr_wall_seconds=ocr_wall_seconds,
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Analysis excerpt selection
# ---------------------------------------------------------------------------
# Annual-report narrative is ranked by section importance, then content
# quality, before bounded windows are chosen: substantive narrative (Business,
# MD&A / Operating and Financial Review, Risk Factors, Outlook, strategy)
# outranks administrative noise (filing indexes, XBRL contexts,
# certifications, compensation plans, insider-trading policies, subsidiary
# lists, unrelated exhibits) so dense exhibit text cannot starve the real
# report of budget.
EXCERPT_PREFIX_CHARS = 24_000
EXCERPT_SUFFIX_CHARS = 28_000
EXCERPT_WINDOW_BEFORE = 1_600
EXCERPT_WINDOW_AFTER = 3_400
EXCERPT_SECTION_HEAD_CHARS = 4_000
EXCERPT_SECTION_BUDGET = 14_000

EXCERPT_TIER_SUBSTANTIVE = 3
EXCERPT_TIER_FINANCIAL = 2
EXCERPT_TIER_CONTEXT = 1
EXCERPT_TIER_NOISE = -1

_EXCERPT_HEADING_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    # Substantive annual-report narrative.
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(
            r"^\s*item\s+1\s*[.\-–—:)]*\s*(?:business|organization)\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(
            r"^\s*item\s+1a\s*[.\-–—:)]*\s*risk factors\b",
            re.IGNORECASE,
        ),
    ),
    # 10-K Item 7 (MD&A) and 20-F Item 5 (Operating and Financial Review).
    (EXCERPT_TIER_SUBSTANTIVE, re.compile(r"^\s*item\s+7\b", re.IGNORECASE)),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(
            r"^\s*item\s+5\s*[.\-–—:)]*\s*"
            r"(?:operating and financial review|results of operations)\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*management'?s?\s+discussion", re.IGNORECASE),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*operating and financial review\b", re.IGNORECASE),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*risk factors\b", re.IGNORECASE),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*forward[- ]looking statements?\b", re.IGNORECASE),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(
            r"^\s*cautionary note regarding forward[- ]looking statements?\b",
            re.IGNORECASE,
        ),
    ),
    (EXCERPT_TIER_SUBSTANTIVE, re.compile(r"^\s*outlook\b", re.IGNORECASE)),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*(?:business\s+)?strategy\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_SUBSTANTIVE, re.compile(r"^\s*business\s*$", re.IGNORECASE)),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*results of operations\b", re.IGNORECASE),
    ),
    (
        EXCERPT_TIER_SUBSTANTIVE,
        re.compile(r"^\s*liquidity and capital resources\b", re.IGNORECASE),
    ),
    # Financial statements and notes.
    (
        EXCERPT_TIER_FINANCIAL,
        re.compile(
            r"^\s*(?:condensed\s+)?(?:consolidated\s+)?statements?\s+of\s+"
            r"(?:income|comprehensive income|operations|financial position|"
            r"cash flows|changes in (?:stockholders'|shareholders') equity)\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_FINANCIAL,
        re.compile(
            r"^\s*(?:consolidated\s+)?(?:income statement|balance sheet|"
            r"statement of (?:cash flows|financial position))\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_FINANCIAL,
        re.compile(
            r"^\s*notes? to (?:the )?(?:consolidated )?financial statements?\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_FINANCIAL,
        re.compile(
            r"^\s*(?:consolidated\s+)?financial statements?\b",
            re.IGNORECASE,
        ),
    ),
    # Broader report context.
    (
        EXCERPT_TIER_CONTEXT,
        re.compile(
            r"^\s*(?:critical )?accounting (?:estimates?|policies?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_CONTEXT,
        re.compile(r"^\s*quantitative and qualitative disclosures?\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*market risk\b", re.IGNORECASE)),
    (
        EXCERPT_TIER_CONTEXT,
        re.compile(r"^\s*segment(?: information)?\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*competition\b", re.IGNORECASE)),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*employees?\b", re.IGNORECASE)),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*properties?\b", re.IGNORECASE)),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*legal proceedings\b", re.IGNORECASE)),
    (EXCERPT_TIER_CONTEXT, re.compile(r"^\s*management\s*$", re.IGNORECASE)),
    # Administrative noise: filing indexes, XBRL contexts, certifications,
    # compensation plans, insider-trading policies, subsidiary lists, exhibits.
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*table of contents\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*(?:filing\s+)?index\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*xbrl\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*instance document\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*certifications?\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*sarbanes[\s\-]?oxley", re.IGNORECASE)),
    (
        EXCERPT_TIER_NOISE,
        re.compile(r"^\s*exhibits?\s*(?:index)?\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*ex\s*[- ]?\d", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*compensation\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*equity incentive plan\b", re.IGNORECASE)),
    (
        EXCERPT_TIER_NOISE,
        re.compile(
            r"^\s*stock (?:option|incentive|appreciation) plans?\b",
            re.IGNORECASE,
        ),
    ),
    (
        EXCERPT_TIER_NOISE,
        re.compile(r"^\s*employee stock purchase plans?\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*insider trading\b", re.IGNORECASE)),
    (
        EXCERPT_TIER_NOISE,
        re.compile(r"^\s*trading (?:policy|plan|arrangement)s?\b", re.IGNORECASE),
    ),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*10b5[- ]?1", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*subsidiari(?:es|y)s?\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*signatures?\b", re.IGNORECASE)),
    (EXCERPT_TIER_NOISE, re.compile(r"^\s*power of attorney\b", re.IGNORECASE)),
)

# SEC bundle file markers (``===== filename =====``) carry the file name, which
# identifies administrative files (exhibits, certifications, XBRL payloads).
_EXCERPT_FILE_MARKER_RE = re.compile(r"^={5,}\s*(.+?)\s*={5,}$")
_EXCERPT_NOISE_FILENAME_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:ex\d+|exhibit|ex[-_]|idx|index|certif|sarbanes|xbrl|"
    r"subsidi|signature|consent)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _classify_excerpt_heading(line: str) -> int | None:
    """Return the importance tier for a heading line, or None for body text."""
    stripped = line.lstrip()
    if not stripped or len(stripped) > 120:
        return None
    for tier, pattern in _EXCERPT_HEADING_PATTERNS:
        if pattern.match(line):
            return tier
    return None


def _preamble_excerpt_tier(document_text: str, start: int) -> int:
    """Tier the unheaded document prefix; SEC bundle JSON manifests are noise."""
    head = document_text[start : start + 1_000].lstrip()
    if head.startswith("{"):
        return EXCERPT_TIER_NOISE
    return EXCERPT_TIER_CONTEXT


def _iter_excerpt_sections(
    document_text: str,
) -> list[tuple[int, int, int]]:
    """Split text into contiguous (start, end, tier) spans by heading lines and
    SEC bundle file markers, so narrative windows can be ranked by section
    importance instead of first keyword occurrence."""
    boundaries: list[tuple[int, int]] = []
    offset = 0
    tier = EXCERPT_TIER_CONTEXT
    for line in document_text.splitlines(keepends=True):
        stripped = line.strip()
        marker = _EXCERPT_FILE_MARKER_RE.match(stripped)
        if marker:
            filename = marker.group(1)
            tier = (
                EXCERPT_TIER_NOISE
                if _EXCERPT_NOISE_FILENAME_RE.search(filename)
                else EXCERPT_TIER_CONTEXT
            )
            boundaries.append((offset, tier))
        else:
            heading_tier = _classify_excerpt_heading(line)
            if heading_tier is not None:
                tier = heading_tier
                boundaries.append((offset, tier))
        offset += len(line)

    if not boundaries:
        return [(0, len(document_text), _preamble_excerpt_tier(document_text, 0))]

    spans = [
        (
            start,
            boundaries[index + 1][0]
            if index + 1 < len(boundaries)
            else len(document_text),
            start_tier,
        )
        for index, (start, start_tier) in enumerate(boundaries)
    ]
    if boundaries[0][0] > 0:
        spans.insert(
            0,
            (0, boundaries[0][0], _preamble_excerpt_tier(document_text, 0)),
        )
    return spans


def _excerpt_section_index(sections: list[tuple[int, int, int]], offset: int) -> int:
    """Index of the section span containing ``offset`` (sections partition text)."""
    low, high = 0, len(sections)
    while low < high:
        mid = (low + high) // 2
        if sections[mid][0] <= offset:
            low = mid + 1
        else:
            high = mid
    return max(low - 1, 0)


def _excerpt_overlap_tier(
    sections: list[tuple[int, int, int]], start: int, end: int
) -> int:
    """Weakest tier among sections overlapping ``[start, end)``."""
    tiers = [tier for s, e, tier in sections if s < end and e > start]
    return min(tiers) if tiers else EXCERPT_TIER_CONTEXT


def _excerpt_section_quality(tier: int, keyword_count: int) -> int:
    """Section score: tier dominates; keyword density breaks ties within tier."""
    return tier * 100_000 + min(keyword_count, 100)


def _merge_excerpt_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 300:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _excerpt_free_ranges(
    chosen: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Sub-ranges of ``[start, end)`` not already covered by chosen spans, so
    overlapping candidates (section windows vs. fixed head/tail context) do not
    double-charge the budget."""
    covered = sorted(chosen)
    free: list[tuple[int, int]] = []
    cursor = start
    for covered_start, covered_end in covered:
        if covered_end <= cursor or covered_start >= end:
            continue
        if covered_start > cursor:
            free.append((cursor, min(covered_start, end)))
        cursor = max(cursor, covered_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return free


def build_analysis_excerpt(document_text: str) -> str:
    if len(document_text) <= MAX_ANALYSIS_CHARS:
        return document_text

    sections = _iter_excerpt_sections(document_text)
    prefix_end = min(EXCERPT_PREFIX_CHARS, len(document_text))
    suffix_start = max(len(document_text) - EXCERPT_SUFFIX_CHARS, 0)

    # Keyword windows, bucketed by the section they fall in.
    windows_by_section: dict[int, list[tuple[int, int]]] = {}
    keyword_counts: dict[int, int] = {}
    for match in _ANALYSIS_KEYWORDS.finditer(document_text):
        index = _excerpt_section_index(sections, match.start())
        keyword_counts[index] = keyword_counts.get(index, 0) + 1
        start = max(sections[index][0], match.start() - EXCERPT_WINDOW_BEFORE)
        end = min(sections[index][1], match.end() + EXCERPT_WINDOW_AFTER)
        if end > start:
            windows_by_section.setdefault(index, []).append((start, end))

    budget = MAX_ANALYSIS_CHARS
    chosen: list[tuple[int, int]] = []
    head_spent: dict[int, int] = {}

    # Phase 1: every substantive/financial section head receives guaranteed
    # bounded coverage before any keyword window, so no high-priority section
    # is starved by earlier or denser sections when the budget is tight.
    for index, (start, end, tier) in enumerate(sections):
        if budget <= 0:
            break
        if tier not in (EXCERPT_TIER_SUBSTANTIVE, EXCERPT_TIER_FINANCIAL):
            continue
        head_end = min(start + EXCERPT_SECTION_HEAD_CHARS, end)
        take = min(head_end - start, budget)
        if take <= 0:
            continue
        chosen.append((start, start + take))
        head_spent[index] = take
        budget -= take

    # Phase 2: bounded keyword windows ranked by section importance, then
    # content quality. Fixed head/tail context windows inherit the weakest
    # overlapping tier so bundles that open or close with administrative noise
    # do not steal budget. Candidate groups: (quality, windows, section index).
    candidates: list[tuple[int, list[tuple[int, int]], int | None]] = []
    for index, (_start, _end, tier) in enumerate(sections):
        windows = windows_by_section.get(index, ())
        if windows:
            candidates.append(
                (
                    _excerpt_section_quality(tier, keyword_counts.get(index, 0)),
                    _merge_excerpt_spans(windows),
                    index,
                )
            )
    for span, tier in (
        (
            (0, prefix_end),
            _excerpt_overlap_tier(sections, 0, prefix_end),
        ),
        (
            (suffix_start, len(document_text)),
            _excerpt_overlap_tier(sections, suffix_start, len(document_text)),
        ),
    ):
        start, end = span
        if end > start:
            candidates.append((_excerpt_section_quality(tier, 0), [(start, end)], None))

    for _quality, windows, index in sorted(
        candidates,
        key=lambda candidate: (-candidate[0], candidate[1][0][0]),
    ):
        if budget <= 0:
            break
        cap = (
            budget
            if index is None
            else min(EXCERPT_SECTION_BUDGET - head_spent.get(index, 0), budget)
        )
        for start, end in windows:
            if cap <= 0 or budget <= 0:
                break
            for free_start, free_end in _excerpt_free_ranges(chosen, start, end):
                if cap <= 0 or budget <= 0:
                    break
                take = min(free_end - free_start, cap, budget)
                if take <= 0:
                    continue
                chosen.append((free_start, free_start + take))
                cap -= take
                budget -= take

    merged = _merge_excerpt_spans(chosen)
    return "\n\n".join(
        f"[Source characters {start}-{end}]\n{document_text[start:end]}"
        for start, end in merged
    )[:MAX_ANALYSIS_CHARS]


def _validate_public_url(value: str) -> str:
    """Reject non-public HTTP(S) origins before any connection is made.

    Delegates to the shared ``contracts.outbound_security`` policy: scheme/
    host/credential shape first, then DNS resolution where every answer must
    be globally routable (fail-closed on mixed answers).
    """
    try:
        return validate_public_url(value)
    except OutboundSecurityError as exc:
        raise ValueError(f"source URL must be a public HTTP(S) URL ({exc})") from exc


def fetch_document_url_to_path(url: str) -> tuple[str, str, str, str]:
    """Stream a user-supplied document URL to a bounded temporary file.

    Per-hop SSRF validation and per-send pinning (as before), but the body
    streams directly into a temp file with declared Content-Length, per-chunk
    total-size and per-chunk total-deadline caps, so a 20MB remote document
    is never double-buffered in memory. The caller owns the returned temp
    path and must remove it after storing (``store_document_url`` does).
    """
    current = _validate_public_url(url)
    fetch_started = time.monotonic()
    fd, temp_path = tempfile.mkstemp(prefix="investment-fetch-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as handle:
            with httpx.Client(
                transport=PublicOnlyHTTPTransport(),
                timeout=30.0,
                follow_redirects=False,
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    if time.monotonic() - fetch_started >= FETCH_DEADLINE_SECONDS:
                        raise ValueError("source URL fetch exceeded the total deadline")
                    try:
                        with client.stream(
                            "GET",
                            current,
                            headers={
                                "User-Agent": "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"
                            },
                            follow_redirects=False,
                        ) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location:
                                    raise ValueError(
                                        "source URL redirected without a location"
                                    )
                                joined = resolve_redirect_url(current, location)
                                try:
                                    current = validate_public_url(joined)
                                except OutboundSecurityError as exc:
                                    raise ValueError(
                                        f"source URL must be a public HTTP(S) URL ({exc})"
                                    ) from exc
                                continue
                            response.raise_for_status()
                            declared = response.headers.get("content-length")
                            try:
                                declared_size = int(declared) if declared else None
                            except ValueError:
                                declared_size = None
                            if (
                                declared_size is not None
                                and declared_size > MAX_DOCUMENT_BYTES
                            ):
                                raise ValueError("remote document exceeds 20 MB")
                            total = 0
                            for chunk in response.iter_bytes():
                                if (
                                    time.monotonic() - fetch_started
                                    >= FETCH_DEADLINE_SECONDS
                                ):
                                    raise ValueError(
                                        "source URL fetch exceeded the total deadline"
                                    )
                                total += len(chunk)
                                if total > MAX_DOCUMENT_BYTES:
                                    raise ValueError("remote document exceeds 20 MB")
                                handle.write(chunk)
                            content_type = response.headers.get(
                                "content-type", "application/octet-stream"
                            )
                            path_name = PurePath(unquote(urlsplit(current).path)).name
                            filename = path_name or "remote-report"
                            return temp_path, filename, content_type, current
                    except OutboundSecurityError as exc:
                        # DNS flipped between validation and send (rebinding).
                        raise ValueError(
                            f"source URL must be a public HTTP(S) URL ({exc})"
                        ) from exc
        raise ValueError("source URL redirected too many times")
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _sha256_file(path: str | Path) -> str:
    """Hash a file in bounded chunks without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _file_root(config: dict) -> Path:
    """Durable, shared-volume root for content-addressed document files.

    Defaults to the news data volume (mounted read-write in the orchestrator
    and worker containers); tests/other deployments override it through
    ``investment_documents.file_root`` or ``INVESTMENT_FILE_ROOT``.
    """
    configured = None
    if isinstance(config, Mapping):
        configured = config.get("investment_documents", {}).get("file_root")
    if configured:
        return Path(str(configured))
    return Path(
        os.environ.get(
            "INVESTMENT_FILE_ROOT",
            "/var/lib/trading-data/news/investment_documents",
        )
    )


def _persist_document_file(source_path: str | Path, config: dict, digest: str) -> str:
    """Atomically copy an upload onto durable storage, content-addressed.

    The copy reads the source in bounded chunks (no full-file read), the
    file is fsynced before an atomic ``os.replace`` into its final
    content-addressed location, and a failed write never leaves a partial
    file. Content addressing makes concurrent writers safe: identical bytes
    land on the same path and the last atomic replace wins. The durable file
    outlives the request spool, so the worker can extract it after the API
    temp file is gone.
    """
    root = _file_root(config)
    bucket = root / digest[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    final_path = bucket / f"{digest}.bin"
    if final_path.exists():
        return str(final_path)
    fd, temp_path = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=bucket)
    try:
        with os.fdopen(fd, "wb") as handle:
            with open(source_path, "rb") as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        return str(final_path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _validated_document_content_path(config: dict, document: Mapping[str, Any]) -> Path:
    """Return the immutable content-addressed path recorded for a document.

    ``content_path`` is persisted state, not authority to read an arbitrary
    worker filesystem path.  Bind it back to the row's SHA-256 identity and
    configured durable root before extraction; reject symlinks, path escapes,
    non-files, oversized replacements, and bytes that no longer match the
    recorded digest.
    """
    digest = str(document.get("content_sha256") or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("document content digest is invalid")
    root = _file_root(config)
    expected = root / digest[:2] / f"{digest}.bin"
    candidate = Path(str(document.get("content_path") or ""))
    if candidate != expected or candidate.is_symlink():
        raise ValueError("document content path is invalid")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("document content file is unavailable") from exc
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise ValueError("document content path is invalid")
    if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("document content file exceeds the upload limit")
    if _sha256_file(resolved) != digest:
        raise ValueError("document content digest does not match")
    return resolved


def _serialize_value(value: object):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    if hasattr(value, "as_tuple"):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return value


def _serialize_row(row: dict) -> dict:
    return {key: _serialize_value(value) for key, value in row.items()}


def store_document_path(
    config: dict,
    metadata: dict,
    path: str | Path,
    mime_type: str | None,
    *,
    preserve_content: bool = False,
    allow_unextractable: bool = False,
    extract: bool = True,
    ocr_page_budget: int = SYNC_OCR_PAGE_BUDGET,
    ocr_wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> dict:
    """Store a document from disk: bounded path-based extraction and hashing.

    The file is never read wholesale: extraction consumes the path directly
    for PDF/zip/DOCX, the SHA-256 digest is computed in chunks, and
    ``raw_content`` is materialized only when explicitly required (bounded by
    the regulatory cap). The caller owns the temporary file and must clean it
    up.

    With ``extract=False`` (async HTTP ingest) no extraction runs at all and
    the bounded upload is persisted to durable content-addressed file storage
    (``content_path``); the durable analysis queue extracts from that path
    later, keeping heavy OCR off the API event loop and large payloads out of
    memory and BYTEA columns.
    """
    normalized = normalize_metadata(metadata)
    max_bytes = (
        MAX_REGULATORY_DOCUMENT_BYTES if preserve_content else MAX_DOCUMENT_BYTES
    )
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")
    if extract:
        try:
            extracted = extract_document_text_path(
                path,
                normalized["filename"],
                mime_type,
                max_bytes=max_bytes,
                ocr_page_budget=ocr_page_budget,
                ocr_wall_seconds=ocr_wall_seconds,
            )
        except Exception as exc:
            if not allow_unextractable:
                raise
            extracted = ""
            logger.warning(
                "document_content_preserved_without_extraction",
                filename=normalized["filename"],
                error=str(exc),
            )
    else:
        extracted = ""
    digest = _sha256_file(path)
    raw_content = None
    content_path = None
    if preserve_content:
        # Explicitly requested DB retention (regulatory flow), bounded.
        with open(path, "rb") as handle:
            raw_content = handle.read(max_bytes + 1)
        if len(raw_content) > max_bytes:
            raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")
    elif not extract:
        # Async uploads go to durable file storage, never BYTEA/memory.
        content_path = _persist_document_file(path, config, digest)
    params = {
        **normalized,
        "report_date": normalized["report_date"],
        "mime_type": (mime_type or "application/octet-stream").split(";", 1)[0][:120],
        "content_sha256": digest,
        "extracted_text": extracted,
        "raw_content": raw_content,
        "content_path": content_path,
    }
    statement = text(
        """
        INSERT INTO investment_documents (
            company, symbol, region, industry, document_type, report_date,
            source_url, filing_source, filing_id, filename, mime_type,
            content_sha256, extracted_text, raw_content, content_path
        ) VALUES (
            :company, :symbol, :region, :industry, :document_type, :report_date,
            :source_url, :filing_source, :filing_id, :filename, :mime_type,
            :content_sha256, :extracted_text, :raw_content, :content_path
        )
        ON CONFLICT (content_sha256) DO UPDATE SET
            filing_source = COALESCE(investment_documents.filing_source, EXCLUDED.filing_source),
            filing_id = COALESCE(investment_documents.filing_id, EXCLUDED.filing_id),
            raw_content = COALESCE(investment_documents.raw_content, EXCLUDED.raw_content),
            content_path = COALESCE(investment_documents.content_path, EXCLUDED.content_path),
            updated_at = NOW()
        RETURNING document_id, company, symbol, region, industry, document_type,
                  report_date, source_url, filing_source, filing_id, filename,
                  mime_type, status, created_at
        """
    )
    with get_session(config) as session:
        row = session.execute(statement, params).fetchone()
    return _serialize_row(dict(row._mapping))


def store_document(
    config: dict,
    metadata: dict,
    content: bytes,
    mime_type: str | None,
    *,
    preserve_content: bool = False,
    allow_unextractable: bool = False,
    extract: bool = True,
) -> dict:
    """Bytes entry point; spools to a temporary file for path-based storage."""
    if not content:
        raise ValueError("document is empty")
    fd, path = tempfile.mkstemp(prefix="investment-doc-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
        return store_document_path(
            config,
            metadata,
            path,
            mime_type,
            preserve_content=preserve_content,
            allow_unextractable=allow_unextractable,
            extract=extract,
        )
    finally:
        os.unlink(path)


def store_document_url(config: dict, metadata: dict) -> dict:
    requested_url = _clean_text(metadata.get("url"), limit=2048)
    if not requested_url:
        raise ValueError("url is required")
    temp_path, filename, mime_type, final_url = fetch_document_url_to_path(
        requested_url
    )
    try:
        enriched = {
            **metadata,
            "filename": metadata.get("filename") or filename,
            "source_url": final_url,
        }
        # URL ingestion follows the same durable handoff as direct uploads:
        # HTTP handlers never run conversion/OCR synchronously.
        return store_document_path(
            config, enriched, temp_path, mime_type, extract=False
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
