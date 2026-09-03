import copy
import hashlib
import json
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
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from sqlalchemy import text

from contracts.outbound_security import (
    OutboundSecurityError,
    resolve_redirect_url,
    validate_public_url,
)
from db import get_session
from http_client import PublicOnlyHTTPTransport, get_shared_client, make_request
from investment_engine import (
    build_deterministic_analysis,
    build_material_relationship_contract,
)
from investment_facts import load_deterministic_facts
from investment_news import (
    ALL_INDUSTRIES,
    canonicalize_industry,
    load_classified_news,
    published_timestamp,
)
from investment_observations import (
    aggregate_industry_history,
    upsert_report_observation,
)
from investment_universe import configured_region_counts, industry_for
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger
from processors._validators import scan_prohibited_language

logger = get_logger("investment.analysis")


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


def enqueue_investment_analysis(
    config: dict, document_id: str, *, market_inputs: dict | None = None
) -> dict:
    """Hand an ingested document to the existing durable analysis queue.

    Uses the shared ``analysis_jobs`` queue (job_type ``investment_analysis``)
    consumed by the durable worker; no second queue is introduced. The job
    identity deduplicates on the document id, so repeated ingests or triggers
    do not stack duplicate analysis work.
    """
    from analysis_jobs import enqueue_job

    document_id = str(document_id).strip()
    if not document_id:
        raise ValueError("document_id is required")
    correlation_id = str(uuid4())
    payload: dict = {"document_id": document_id}
    if isinstance(market_inputs, dict) and market_inputs:
        payload["market_inputs"] = market_inputs
    with get_session(config) as session:
        enqueued = enqueue_job(
            session,
            job_type="investment_analysis",
            dedupe_key=f"investment-analysis:{document_id}",
            input_fingerprint=f"document:{document_id}",
            payload=payload,
            correlation_id=correlation_id,
            priority=80,
            max_attempts=3,
        )
    job = enqueued.job
    return {
        "status": "queued" if enqueued.inserted else "already_queued",
        "job_id": str(job.id) if job is not None else None,
        "correlation_id": (
            str(job.correlation_id) if job is not None else correlation_id
        ),
        "inserted": enqueued.inserted,
    }


def _response_schema() -> dict:
    qualitative_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "present": {"type": "boolean"},
            "strength": {
                "type": "string",
                "enum": ["none", "weak", "moderate", "strong"],
            },
            "evidence": {"type": "string"},
        },
        "required": ["present", "strength", "evidence"],
    }
    risk_properties = {
        "sourced_observation": {"type": "string", "minLength": 1, "maxLength": 600},
        "inference": {"type": "string", "minLength": 1, "maxLength": 600},
        "epistemic_state": {
            "type": "string",
            "enum": ["observed", "supported", "hypothesis"],
        },
        "uncertainty": {"type": "string", "minLength": 1, "maxLength": 400},
        "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
        "impact": {"type": "string", "enum": ["low", "medium", "high"]},
        "mitigation": {"type": "string", "minLength": 1, "maxLength": 600},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 600},
    }
    catalyst_properties = {
        "trigger": {"type": "string", "minLength": 1, "maxLength": 600},
        "expected_outcome": {"type": "string", "minLength": 1, "maxLength": 600},
        "horizon": {"type": "string", "minLength": 1, "maxLength": 200},
        "epistemic_state": {
            "type": "string",
            "enum": ["observed", "supported", "hypothesis"],
        },
        "uncertainty": {"type": "string", "minLength": 1, "maxLength": 400},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 600},
    }
    numeric_claim_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claim_id": {"type": "string", "maxLength": 120},
            "path": {"type": "string", "maxLength": 300},
            "value": {
                "description": (
                    "Exact quantity as rendered in the target text: a finite "
                    "number, or a bounded numeric string such as \"$19B\" or "
                    "\"28%\"."
                )
            },
            "metric": {"type": "string", "maxLength": 200},
            "period": {"type": "string", "maxLength": 200},
            "unit": {"enum": sorted(NUMERIC_CLAIM_UNITS)},
            "currency": {"type": ["string", "null"], "maxLength": 16},
            "source_kind": {"enum": ["text", "fact", "arithmetic"]},
            "quote": {"type": "string", "maxLength": 400},
            "fact_path": {"type": "string", "maxLength": 300},
            "operation": {"enum": ["sum", "difference", "product", "quotient"]},
            "operands": {
                "type": "array",
                "items": {"type": "string", "maxLength": 300},
                "minItems": 2,
                "maxItems": 4,
            },
        },
        "required": [
            "claim_id",
            "path",
            "value",
            "metric",
            "period",
            "unit",
            "currency",
            "source_kind",
        ],
    }
    relationship_reconciliation_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "relationship_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
            "status": {
                "type": "string",
                "enum": ["reconciled", "abstained_incompatible"],
            },
            "fact_paths": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
            },
            "observation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 450,
            },
            "interpretation": {
                "type": "string",
                "maxLength": 350,
            },
            "uncertainty": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "summary_synthesis": {
                "type": "string",
                "maxLength": 350,
            },
            "thesis_synthesis": {
                "type": "string",
                "maxLength": 350,
            },
            "summary_fact_paths": {
                "type": "array",
                "maxItems": 2,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
            },
        },
        "required": [
            "relationship_id",
            "status",
            "fact_paths",
            "observation",
            "interpretation",
            "uncertainty",
            "summary_synthesis",
            "thesis_synthesis",
            "summary_fact_paths",
        ],
    }
    materiality_topic_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["addressed", "not_disclosed"],
            },
            "observation": {"type": "string", "maxLength": 600},
            "implication": {"type": "string", "maxLength": 600},
            "evidence": {"type": "string", "maxLength": 600},
        },
        "required": ["status", "observation", "implication", "evidence"],
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "classification": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "document_type": {"type": "string"},
                    "sector": {"type": "string"},
                    "industry": {"type": "string"},
                    "region": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "moderate", "high"],
                    },
                },
                "required": [
                    "document_type",
                    "sector",
                    "industry",
                    "region",
                    "confidence",
                ],
            },
            "qualitative": {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: qualitative_item for name in QUALITATIVE_NAMES},
                "required": list(QUALITATIVE_NAMES),
            },
            "summary": {"type": "string"},
            "thesis": {"type": "string"},
            "counter_thesis": {"type": "string", "minLength": 1, "maxLength": 1200},
            "materiality_assessment": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    topic: materiality_topic_item
                    for topic in MATERIALITY_ASSESSMENT_TOPICS
                },
                "required": list(MATERIALITY_ASSESSMENT_TOPICS),
            },
            "drivers": {"type": "array", "items": {"type": "string"}},
            "catalysts": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": catalyst_properties,
                    "required": list(catalyst_properties),
                },
            },
            "risks": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": risk_properties,
                    "required": list(risk_properties),
                },
            },
            "watch_items": {"type": "array", "items": {"type": "string"}},
            "relationship_reconciliations": {
                "type": "array",
                "maxItems": 3,
                "items": relationship_reconciliation_item,
            },
            "numeric_claims": {
                "type": "array",
                "maxItems": _MAX_NUMERIC_CLAIM_ROWS,
                "items": numeric_claim_item,
            },
        },
        # The ledger is declared but NOT required: absence and [] both mean
        # an empty binding set, so pre-ledger payloads still validate.
        "required": [
            "classification",
            "qualitative",
            "summary",
            "thesis",
            "counter_thesis",
            "materiality_assessment",
            "drivers",
            "catalysts",
            "risks",
            "watch_items",
            "relationship_reconciliations",
        ],
    }
    return {
        "name": "investment_report_narrative_v7",
        "strict": True,
        "schema": schema,
    }


def _canonical_fingerprint(payload: object) -> str:
    """SHA-256 over canonical JSON (sorted keys, tight separators, UTF-8)."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InvestmentAnalysisRequest(NamedTuple):
    """Frozen, fully-resolved dispatch request for one investment analysis."""

    prompt: str
    schema_name: str
    strict: bool
    schema: MappingProxyType | dict
    relationship_facts: MappingProxyType | dict
    material_relationships: tuple | list
    fingerprint: str

    def packet(self) -> dict:
        """Executor-ready payload: prompt plus a plain independent schema copy.

        Mirrors the blind-judge request packet contract: executors and
        ``LLMStage`` receive ordinary JSON-safe dicts/lists that share no
        mutable structure with the frozen request state.
        """
        return {
            "prompt": self.prompt,
            "schema_name": self.schema_name,
            "strict": self.strict,
            "schema": _plain_json_value(self.schema),
        }


def _freeze_json_value(value: object) -> object:
    """Recursively freeze JSON-native structures into immutable containers.

    Mappings become ``MappingProxyType`` over a fresh plain dict, sequences
    become tuples, and scalars pass through unchanged. Non-JSON values are
    rejected: dispatch material must survive canonical re-encoding exactly.
    """
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("request schema numbers must be finite")
        return value
    raise ValueError(f"request schema contains non-JSON value: {type(value).__name__}")


def _plain_json_value(value: object) -> object:
    """Materialize frozen/plain containers into an independent plain copy.

    The inverse of :func:`_freeze_json_value`: proxies become ordinary dicts,
    frozen tuples become lists, and nested containers are rebuilt so the
    result shares no mutable structure with the source.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def build_investment_analysis_request(
    document: dict,
    excerpt: str,
    news_items: list[dict],
    deterministic_current: dict,
    deterministic_prior: dict,
) -> InvestmentAnalysisRequest:
    """Build one immutable phase-1 contract and its exact dispatch identity."""
    schema_payload = _response_schema()
    relationship_payload = build_material_relationship_contract(
        deterministic_current,
        deterministic_prior,
    ).to_payload()
    frozen_relationship_facts = _freeze_json_value(
        relationship_payload["relationship_facts"]
    )
    frozen_material_relationships = _freeze_json_value(
        relationship_payload["material_relationships"]
    )
    deterministic_metrics = {
        "metrics": deterministic_current,
        "prior_metrics": deterministic_prior,
    }
    prompt = _build_prompt(
        document,
        excerpt,
        news_items,
        deterministic_metrics,
        relationship_payload,
    )
    frozen_schema = _freeze_json_value(schema_payload["schema"])
    return InvestmentAnalysisRequest(
        prompt=prompt,
        schema_name=str(schema_payload["name"]),
        strict=bool(schema_payload["strict"]),
        schema=frozen_schema,
        relationship_facts=frozen_relationship_facts,
        material_relationships=frozen_material_relationships,
        fingerprint=_canonical_fingerprint(
            {
                "prompt": prompt,
                "schema_name": schema_payload["name"],
                "strict": schema_payload["strict"],
                "schema": schema_payload["schema"],
                "inputs": {
                    "document": {
                        key: value
                        for key, value in document.items()
                        if key not in {"extracted_text", "raw_content"}
                    },
                    "excerpt": excerpt,
                    "news_items": news_items,
                    "deterministic_metrics": deterministic_metrics,
                    "relationship_contract": relationship_payload,
                },
            }
        ),
    )


def _schema_type_matches(value: object, expected: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _is_plain_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_schema_node(
    value: object,
    schema: object,
    path: str,
    errors: list[str],
) -> None:
    """Recursively enforce the repository's response-schema subset exactly.

    Supports object/array/string/boolean/number/integer types, required and
    additional properties, enum, string/item-count and numeric bounds, and
    always rejects non-finite numbers.
    """
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if isinstance(declared, str):
        if not _schema_type_matches(value, declared):
            errors.append(f"{path}: expected type {declared}")
            return
    elif isinstance(declared, list) and declared:
        if not any(_schema_type_matches(value, item) for item in declared):
            errors.append(f"{path}: expected one of types {declared!r}")
            return
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{path}: numbers must be finite")
        return
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if (
            isinstance(min_length, int)
            and not isinstance(min_length, bool)
            and len(value) < min_length
        ):
            errors.append(f"{path}: expected at least {min_length} characters")
        max_length = schema.get("maxLength")
        if (
            isinstance(max_length, int)
            and not isinstance(max_length, bool)
            and len(value) > max_length
        ):
            errors.append(f"{path}: expected at most {max_length} characters")
    if _is_plain_number(value):
        minimum = schema.get("minimum")
        if _is_plain_number(minimum) and value < minimum:
            errors.append(f"{path}: {value} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if _is_plain_number(maximum) and value > maximum:
            errors.append(f"{path}: {value} is above maximum {maximum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if _is_plain_number(exclusive_minimum) and value <= exclusive_minimum:
            errors.append(f"{path}: {value} must exceed {exclusive_minimum}")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if _is_plain_number(exclusive_maximum) and value >= exclusive_maximum:
            errors.append(f"{path}: {value} must be below {exclusive_maximum}")
    allowed_enum = schema.get("enum")
    if isinstance(allowed_enum, list) and value not in allowed_enum:
        errors.append(f"{path}: must be one of {allowed_enum!r}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for key, subschema in properties.items():
            if key in value:
                _validate_schema_node(value[key], subschema, f"{path}.{key}", errors)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property {key!r}")
        elif isinstance(additional, dict):
            for key, child in value.items():
                if key not in properties:
                    _validate_schema_node(
                        child, additional, f"{path}.{key}", errors
                    )
    elif isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool) and len(value) < min_items:
            errors.append(f"{path}: expected at least {min_items} items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and not isinstance(max_items, bool) and len(value) > max_items:
            errors.append(f"{path}: expected at most {max_items} items")
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_node(item, items_schema, f"{path}[{index}]", errors)


def risk_catalyst_contract_violations(parsed: object) -> list[str]:
    """Enforce structural separation without pretending to prove entailment."""
    if not isinstance(parsed, dict):
        return []
    violations: list[str] = []
    row_specs = (
        (
            "risks",
            (
                "sourced_observation",
                "inference",
                "uncertainty",
                "mitigation",
                "evidence",
            ),
            ("sourced_observation", "inference"),
        ),
        (
            "catalysts",
            (
                "trigger",
                "expected_outcome",
                "horizon",
                "uncertainty",
                "evidence",
            ),
            ("trigger", "expected_outcome"),
        ),
    )
    for collection, string_fields, distinct_fields in row_specs:
        rows = parsed.get(collection)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for field in string_fields:
                value = row.get(field)
                if isinstance(value, str) and not value.strip():
                    violations.append(
                        f"$.{collection}[{index}].{field}: must be nonblank"
                    )
            left, right = distinct_fields
            normalized_left = _normalize_grounding_text(row.get(left))
            normalized_right = _normalize_grounding_text(row.get(right))
            if normalized_left and normalized_left == normalized_right:
                violations.append(
                    f"$.{collection}[{index}]: {left} and {right} must differ"
                )
    return violations


def materiality_assessment_contract_violations(parsed: object) -> list[str]:
    """Enforce status-dependent materiality fields without inferring disclosure."""
    if not isinstance(parsed, dict):
        return []
    violations: list[str] = []
    counter_thesis = parsed.get("counter_thesis")
    if not isinstance(counter_thesis, str) or not counter_thesis.strip():
        violations.append("$.counter_thesis: must be nonblank")
    assessment = parsed.get("materiality_assessment")
    if not isinstance(assessment, dict):
        return violations
    for topic in MATERIALITY_ASSESSMENT_TOPICS:
        row = assessment.get(topic)
        if not isinstance(row, dict):
            continue
        path = f"$.materiality_assessment.{topic}"
        fields = ("observation", "implication", "evidence")
        if row.get("status") == "addressed":
            for field in fields:
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    violations.append(
                        f"{path}.{field}: must be nonblank when status is addressed"
                    )
        elif row.get("status") == "not_disclosed":
            for field in fields:
                if row.get(field) != "":
                    violations.append(
                        f"{path}.{field}: must be exactly empty when status is not_disclosed"
                    )
    return violations


def validate_investment_report_payload(parsed: object) -> list[str]:
    """Validate parsed model JSON against the exact repository response schema.

    Beyond the generic node walk, risk/catalyst rows enforce nonblank and
    distinct epistemic fields. Numeric-claim ledger rows get a dedicated
    structural pass because the repository validator subset has no ``oneOf``,
    so kind-exclusive row shapes are enforced deterministically.
    """
    errors: list[str] = []
    _validate_schema_node(parsed, _response_schema()["schema"], "$", errors)
    errors.extend(risk_catalyst_contract_violations(parsed))
    errors.extend(materiality_assessment_contract_violations(parsed))
    errors.extend(
        validate_numeric_claim_rows(
            parsed.get("numeric_claims") if isinstance(parsed, dict) else None
        )
    )
    return errors



# Exact scaffold header emitted by ``build_analysis_excerpt`` to delimit one
# filed source region. Header lines are scaffolding, never filing evidence.
_SOURCE_SPAN_HEADER_RE = re.compile(r"(?m)^\[Source characters \d+-\d+\]\n?")

VALIDATION_JSON_SCHEMA = "json_schema"
VALIDATION_FILING_EVIDENCE = "filing_evidence"
VALIDATION_PROHIBITED_LANGUAGE = "prohibited_language"
_VALIDATION_CATEGORY_ORDER = (
    VALIDATION_JSON_SCHEMA,
    VALIDATION_FILING_EVIDENCE,
    VALIDATION_PROHIBITED_LANGUAGE,
)
_MAX_VALIDATION_PROBLEMS_PER_CATEGORY = 10
_MAX_MATERIAL_RELATIONSHIPS = 3
_MAX_REQUIRED_FACTS_PER_RELATIONSHIP = 8
_MAX_MISSING_RELATIONSHIP_BINDINGS = (
    _MAX_MATERIAL_RELATIONSHIPS * _MAX_REQUIRED_FACTS_PER_RELATIONSHIP
)
_MAX_CORRECTION_SUFFIX_LENGTH = 700
_VALIDATION_WARNING_BY_CATEGORY = {
    VALIDATION_JSON_SCHEMA: "response was not valid investment JSON",
    VALIDATION_FILING_EVIDENCE: "filing evidence was blank or ungrounded",
    VALIDATION_PROHIBITED_LANGUAGE: "response contained prohibited advisory language",
}


class InvestmentValidationError(ValueError):
    """A rejected investment response with bounded, ordered category findings.

    ``category`` remains the compatible primary classification used by
    telemetry. ``categories`` and ``problems_by_category`` retain every
    independently detectable validation family so the sole repair prompt can
    state all applicable repository-authored requirements.
    """

    def __init__(
        self,
        category: str,
        problems: list[str],
        *,
        problems_by_category: Mapping[str, list[str]] | None = None,
        missing_relationship_bindings: object = (),
    ) -> None:
        supplied = (
            {category: problems}
            if problems_by_category is None
            else problems_by_category
        )
        bounded = {
            name: list(supplied.get(name, ()))[:_MAX_VALIDATION_PROBLEMS_PER_CATEGORY]
            for name in _VALIDATION_CATEGORY_ORDER
            if supplied.get(name)
        }
        bindings: list[tuple[int, int]] = []
        seen_bindings: set[tuple[int, int]] = set()
        candidates = (
            missing_relationship_bindings
            if isinstance(missing_relationship_bindings, (list, tuple))
            else ()
        )
        for candidate in candidates:
            if (
                not isinstance(candidate, (list, tuple))
                or len(candidate) != 2
                or any(
                    isinstance(index, bool) or not isinstance(index, int)
                    for index in candidate
                )
                or not 0 <= candidate[0] < _MAX_MATERIAL_RELATIONSHIPS
                or not 0 <= candidate[1] < _MAX_REQUIRED_FACTS_PER_RELATIONSHIP
            ):
                continue
            binding = (candidate[0], candidate[1])
            if binding not in seen_bindings:
                seen_bindings.add(binding)
                bindings.append(binding)
            if len(bindings) == _MAX_MISSING_RELATIONSHIP_BINDINGS:
                break
        self.categories = tuple(bounded)
        self.category = self.categories[0] if self.categories else category
        self.problems_by_category = bounded
        self.problems = [
            problem
            for name in self.categories
            for problem in self.problems_by_category[name]
        ]
        self.missing_relationship_bindings = tuple(bindings)
        super().__init__("investment response rejected: " + "; ".join(self.problems))

    @property
    def correction_requirement(self) -> str:
        categories = self.categories or (self.category,)
        use_compact_requirements = (
            len(categories) > 1 or bool(self.missing_relationship_bindings)
        )
        requirement_set = (
            _COMPACT_CORRECTION_REQUIREMENTS
            if use_compact_requirements
            else _CORRECTION_REQUIREMENTS
        )
        requirements = [requirement_set[category] for category in categories]
        if use_compact_requirements:
            requirements[0] = f"CORRECTION: {requirements[0]}"
        if self.missing_relationship_bindings:
            requirements.append(
                "".join(
                    f"r{relationship_index}/f{required_fact_index}"
                    for relationship_index, required_fact_index
                    in self.missing_relationship_bindings
                )
            )

        # Only repository-validated coordinates and field paths may extend the
        # bounded retry suffix. Coordinates are mandatory and therefore precede
        # the optional, incrementally fitted affected-field detail.
        if VALIDATION_FILING_EVIDENCE in categories:
            paths = _failing_field_paths(
                self.problems_by_category.get(VALIDATION_FILING_EVIDENCE, [])
            )
            fitted_paths: list[str] = []
            preserve_legacy_shape = (
                categories == (VALIDATION_FILING_EVIDENCE,)
                and not self.missing_relationship_bindings
            )
            for path in paths:
                candidate_paths = fitted_paths + [path]
                affected = f"Affected fields: {', '.join(candidate_paths)}."
                candidate = (
                    [f"{requirements[0]} {affected}"]
                    if preserve_legacy_shape
                    else requirements + [affected]
                )
                if len("\n" + "\n".join(candidate)) >= _MAX_CORRECTION_SUFFIX_LENGTH:
                    break
                fitted_paths = candidate_paths
            if fitted_paths:
                affected = f"Affected fields: {', '.join(fitted_paths)}."
                if preserve_legacy_shape:
                    requirements[0] = f"{requirements[0]} {affected}"
                else:
                    requirements.append(affected)

        correction = "\n".join(requirements)
        if len("\n" + correction) >= _MAX_CORRECTION_SUFFIX_LENGTH:
            raise AssertionError("repository-authored correction suffix exceeded bound")
        return correction


def filing_content_spans(excerpt: str) -> list[str]:
    """Convert an analysis excerpt into separate filing content spans.

    Without a valid ``[Source characters <start>-<end>]`` scaffold header the
    entire excerpt is one span. Otherwise every header line is excluded and
    each following content block remains its own span, so grounding can never
    match text stitched together across region boundaries or the scaffold
    headers themselves.
    """
    text = excerpt if isinstance(excerpt, str) else ""
    headers = list(_SOURCE_SPAN_HEADER_RE.finditer(text))
    if not headers:
        return [text]
    spans: list[str] = []
    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[start:end]
        if block.strip():
            spans.append(block)
    return spans


# Typographic equivalents that normalize to their ASCII counterparts before
# evidence/source substring comparison: apostrophe-like marks to "'", quote
# marks to '"', hyphen/dash glyphs and minus to "-", and no-break spaces to a
# regular space. Deliberately exhaustive-free: punctuation, accents, digits,
# words, and ordering are untouched, keeping lineage matching exact (no fuzzy).
_GROUNDING_TRANSLATIONS = {
    0x2018: "'",  # left single quotation mark
    0x2019: "'",  # right single quotation mark
    0x201A: "'",  # single low-9 quotation mark
    0x2032: "'",  # prime
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x201E: '"',  # double low-9 quotation mark
    0x2033: '"',  # double prime
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2212: "-",  # minus sign
    0x00A0: " ",  # no-break space
    0x202F: " ",  # narrow no-break space
}

def _normalize_grounding_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.translate(_GROUNDING_TRANSLATIONS)).strip().casefold()


def relationship_reconciliation_problems(
    parsed: Mapping[str, object],
    *,
    material_relationships: object,
) -> list[str]:
    """Enforce the frozen request's v7 relationship response contract exactly."""
    requested = (
        list(material_relationships)
        if isinstance(material_relationships, (list, tuple))
        else []
    )
    reconciliations = parsed.get("relationship_reconciliations")
    rows = reconciliations if isinstance(reconciliations, list) else []
    problems: list[str] = []
    if len(rows) != len(requested):
        problems.append(
            "relationship_reconciliations: expected exactly "
            f"{len(requested)} ordered rows from the request contract"
        )

    summary = _normalize_grounding_text(parsed.get("summary"))
    thesis = _normalize_grounding_text(parsed.get("thesis"))
    for index, (row, relationship) in enumerate(zip(rows, requested, strict=False)):
        path = f"relationship_reconciliations[{index}]"
        if not isinstance(row, dict) or not isinstance(relationship, Mapping):
            continue

        expected_id = relationship.get("relationship_id")
        if row.get("relationship_id") != expected_id:
            problems.append(
                f"{path}.relationship_id: must equal request relationship "
                f"{expected_id!r} at this position"
            )

        required_facts = relationship.get("required_facts")
        required_facts = (
            required_facts
            if isinstance(required_facts, (list, tuple))
            else ()
        )
        expected_paths = [
            ref.get("fact_path")
            for ref in required_facts
            if isinstance(ref, Mapping) and isinstance(ref.get("fact_path"), str)
        ]
        authored_paths = row.get("fact_paths")
        if authored_paths != expected_paths:
            problems.append(
                f"{path}.fact_paths: must equal the complete ordered request "
                "fact path list"
            )
        elif len(set(authored_paths)) != len(authored_paths):
            problems.append(f"{path}.fact_paths: paths must be unique")

        compatibility = relationship.get("compatibility")
        expected_status = (
            "reconciled"
            if compatibility == "compatible"
            else "abstained_incompatible"
        )
        if row.get("status") != expected_status:
            problems.append(
                f"{path}.status: must be {expected_status!r} for request "
                f"compatibility {compatibility!r}"
            )

        observation = _normalize_grounding_text(row.get("observation"))
        interpretation = _normalize_grounding_text(row.get("interpretation"))
        uncertainty = _normalize_grounding_text(row.get("uncertainty"))
        if not observation:
            problems.append(f"{path}.observation: must be nonblank")
        if not uncertainty:
            problems.append(f"{path}.uncertainty: must be nonblank")

        if compatibility == "compatible":
            if not interpretation:
                problems.append(f"{path}.interpretation: must be nonblank")
            for field, container, container_name in (
                ("summary_synthesis", summary, "summary"),
                ("thesis_synthesis", thesis, "thesis"),
            ):
                synthesis = _normalize_grounding_text(row.get(field))
                if not synthesis:
                    problems.append(f"{path}.{field}: must be nonblank")
                elif synthesis not in container:
                    problems.append(
                        f"{path}.{field}: normalized text must occur "
                        f"contiguously in {container_name}"
                    )
            selected = row.get("summary_fact_paths")
            if not isinstance(selected, list) or not 1 <= len(selected) <= 2:
                problems.append(
                    f"{path}.summary_fact_paths: must contain 1 or 2 fact paths"
                )
            elif len(
                {fact_path for fact_path in selected if isinstance(fact_path, str)}
            ) != len(selected):
                problems.append(
                    f"{path}.summary_fact_paths: fact paths must be unique"
                )
            elif any(fact_path not in expected_paths for fact_path in selected):
                problems.append(
                    f"{path}.summary_fact_paths: must be a subset of required fact paths"
                )
        else:
            for field, empty in (
                ("interpretation", ""),
                ("summary_synthesis", ""),
                ("thesis_synthesis", ""),
                ("summary_fact_paths", []),
            ):
                if row.get(field) != empty:
                    problems.append(
                        f"{path}.{field}: must be exactly empty for an "
                        "incompatible request relationship"
                    )
    return problems


def investment_evidence_violations(
    parsed: dict,
    *,
    excerpt: str,
    news_items: object,
) -> list[str]:
    """Reject blank or ungrounded provenance quotes in model output.

    Every nonempty ``evidence`` string must appear verbatim (after whitespace
    and case normalization) within a single filing content span of the
    supplied excerpt (see ``filing_content_spans``); scaffold headers and
    text joined across span boundaries never ground evidence. Present
    qualitative signals, every risk observation and catalyst trigger, and each
    addressed materiality topic must carry a nonblank quote. This exact-source
    gate does not claim semantic entailment of an observation, implication,
    inference, trigger, or outcome. ``news_items``
    stays in the signature for call compatibility but is intentionally excluded:
    related news is separate context and can never ground filing evidence.
    """
    violations: list[str] = []
    # Grounding uses the filing excerpt only; news is context, never evidence.
    spans = [
        _normalize_grounding_text(span) for span in filing_content_spans(excerpt)
    ]

    def check(evidence: object, label: str, *, required: bool) -> None:
        normalized = _normalize_grounding_text(evidence)
        if not normalized:
            if required:
                violations.append(f"{label}: evidence is required and must be nonblank")
            return
        if not any(normalized in span for span in spans):
            violations.append(
                f"{label}: evidence is not grounded in the filing excerpt"
            )

    qualitative = parsed.get("qualitative")
    qualitative = qualitative if isinstance(qualitative, dict) else {}
    for name in QUALITATIVE_NAMES:
        item = qualitative.get(name)
        if not isinstance(item, dict):
            continue
        check(
            item.get("evidence"),
            f"qualitative.{name}",
            required=bool(item.get("present")),
        )
    assessment = parsed.get("materiality_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    for topic in MATERIALITY_ASSESSMENT_TOPICS:
        item = assessment.get(topic)
        if not isinstance(item, dict):
            continue
        check(
            item.get("evidence"),
            f"materiality_assessment.{topic}",
            required=item.get("status") == "addressed",
        )
    for collection, label_prefix in (("catalysts", "catalysts"), ("risks", "risks")):
        entries = parsed.get(collection)
        if not isinstance(entries, list):
            continue
        for index, item in enumerate(entries):
            if isinstance(item, dict):
                check(item.get("evidence"), f"{label_prefix}[{index}]", required=True)
    return violations


def _validated_investment_facts(
    content: object,
    *,
    excerpt: str,
    news_items: object,
    deterministic_current: object = None,
    deterministic_prior: object = None,
    document_metadata: object = None,
    relationship_facts: object = None,
    material_relationships: object = None,
) -> dict:
    """Parse output, then enforce schema, grounding, policy, and source semantics.

    Recorded sub-agent output bypasses provider-side enforcement, so local
    validation is mandatory. After successful object parsing, every defensive
    pass runs before rejection so the one allowed repair receives all
    independently detectable JSON/source-contract, filing-evidence, and
    prohibited-language requirements.
    """
    try:
        facts = _parse_llm_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvestmentValidationError(
            VALIDATION_JSON_SCHEMA,
            ["response was not one JSON object matching the narrative schema"],
        ) from exc

    schema_problems = validate_investment_report_payload(facts)
    relationship_problems = relationship_reconciliation_problems(
        facts,
        material_relationships=material_relationships,
    )
    evidence_problems = investment_evidence_violations(
        facts, excerpt=excerpt, news_items=news_items
    )
    source_problems: list[str] = []
    missing_relationship_bindings: list[tuple[int, int]] = []
    if deterministic_current is not None and deterministic_prior is not None:
        source_problems = numeric_claim_source_problems(
            facts,
            deterministic_current=deterministic_current,
            deterministic_prior=deterministic_prior,
            excerpt=excerpt,
            news_items=news_items,
            document_metadata=document_metadata,
            relationship_facts=relationship_facts,
            material_relationships=material_relationships,
            missing_relationship_bindings=missing_relationship_bindings,
        )

    prohibited_language_problems = (
        ["response contained prohibited advisory language"]
        if scan_prohibited_language(facts)
        else []
    )
    problems_by_category = {
        VALIDATION_JSON_SCHEMA: (
            schema_problems + relationship_problems + source_problems
        ),
        VALIDATION_FILING_EVIDENCE: evidence_problems,
        VALIDATION_PROHIBITED_LANGUAGE: prohibited_language_problems,
    }
    categories = [
        category
        for category in _VALIDATION_CATEGORY_ORDER
        if problems_by_category[category]
    ]
    if categories:
        primary = categories[0]
        raise InvestmentValidationError(
            primary,
            problems_by_category[primary],
            problems_by_category=problems_by_category,
            missing_relationship_bindings=missing_relationship_bindings,
        )
    return facts


# Bounded repair requirements, one per validation category. Repository-
# authored only: no raw model output, no added analytical claims.
_CORRECTION_REQUIREMENTS = {
    VALIDATION_JSON_SCHEMA: (
        "CORRECTION: JSON must match Narrative v7. For each compatible ordered "
        "relationship, keep the full observation audit, add nonblank exact "
        "summary/thesis syntheses, and select 1-2 unique required summary fact "
        "paths; incompatible synthesis fields/list are empty. Bind every required "
        "observation fact and each unique selected summary fact exactly once with "
        "an exact fact_path/metric_label/period/unit/currency row. Materiality has "
        "all four topics: addressed rows are nonblank; not_disclosed rows are empty. "
        "counter_thesis is nonblank. Values are finite scalar/tokens <=64 chars. "
        "Repair rN/fN."
    ),
    VALIDATION_FILING_EVIDENCE: (
        "CORRECTION: The previous response had blank or ungrounded filing evidence. "
        "Each present qualitative signal, risk sourced_observation, catalyst trigger, "
        "and addressed materiality topic needs nonblank evidence: one short exact "
        "contiguous FILING EXCERPT quote in a single source region. Never join "
        "regions; never use scaffold metadata, labels, wrappers, or commentary. News stays "
        "item-bound, not filing evidence. Evidence supports observations/triggers, "
        "not inferences/outcomes. Preserve exact fiscal/time labels; expand them only "
        "from explicit deterministic fiscal-calendar metadata."
    ),
    VALIDATION_PROHIBITED_LANGUAGE: (
        "CORRECTION: The previous response contained prohibited advisory "
        "language. Remove all portfolio sizing, allocation, or exposure "
        "instructions and any trading, entry/exit, stop/target, "
        "technical-analysis, or execution-risk instructions. For each risk, "
        "return only a company-stated mitigation or a non-advisory monitoring "
        "response grounded in the supplied context."
    ),
}

_COMPACT_CORRECTION_REQUIREMENTS = {
    VALIDATION_JSON_SCHEMA: (
        "JSON:v7;rN/fN;ordered facts;compatible exact syntheses+1-2 required "
        "summary paths;incompatible synthesis/list empty;one exact observation "
        "row/fact and one deduped summary row/selected fact;four status-consistent "
        "materiality topics;nonblank counter_thesis;finite token<=64."
    ),
    VALIDATION_FILING_EVIDENCE: (
        "EVIDENCE:present qualitative/risk/catalyst/addressed materiality uses "
        "one exact contiguous filing-region quote;no joins/scaffold/commentary"
    ),
    VALIDATION_PROHIBITED_LANGUAGE: (
        "LANGUAGE:no portfolio/trading/technical/execution instructions;"
        "grounded monitoring only"
    ),
}


# Only validated failing field paths may ride along with the filing-evidence
# correction: repository-known qualitative/materiality names or indexed
# catalyst/risk entries. Free-form message text and quoted values are never
# forwarded.
_FAILING_FIELD_PATH_RE = re.compile(
    r"^(qualitative\.(?:"
    + "|".join(QUALITATIVE_NAMES)
    + r")|materiality_assessment\.(?:"
    + "|".join(MATERIALITY_ASSESSMENT_TOPICS)
    + r")|catalysts\[\d+\]|risks\[\d+\]): "
)


# --- Numeric-claim binding contract -----------------------------------------
# Every model-authored material number must carry a structured ledger row that
# binds it to its exact target text and to one producer-visible source: either
# verbatim producer text (``source_kind="text"``), one deterministic fact
# (``source_kind="fact"``), or an arithmetic combination of named facts
# (``source_kind="arithmetic"``, permitted only when the producer itself
# supplied the operation identity). The grounding gate later resolves both
# pointers against the frozen case; global token presence is never enough.
NUMERIC_CLAIM_UNITS = frozenset(
    {
        "usd_billions",
        "usd_millions",
        "usd_per_share",
        "percent",
        "percentage_points",
        "ratio",
        "count",
    }
)
_MAX_NUMERIC_CLAIM_ROWS = 40
_MAX_NUMERIC_CLAIM_ID_CHARS = 120
_MAX_NUMERIC_CLAIM_PATH_CHARS = 300
_MAX_NUMERIC_CLAIM_ALIAS_CHARS = 200
_MAX_NUMERIC_CLAIM_QUOTE_CHARS = 400
_NUMERIC_VALUE_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:%|bps|bp)?")
_CURRENCY_SYMBOL_RE_FRAGMENT = r"[$€£¥]"
_SIGNED_CURRENCY_SURFACE_RE_FRAGMENT = (
    rf"(?:[+-]?{_CURRENCY_SYMBOL_RE_FRAGMENT}\s*|"
    rf"{_CURRENCY_SYMBOL_RE_FRAGMENT}[+-]\s*|[+-]?)"
)
_NUMERIC_CLAIM_VALUE_RE = re.compile(
    rf"{_SIGNED_CURRENCY_SURFACE_RE_FRAGMENT}\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|bps|bp|trillion|billions|billion|bns|bn|millions|million|mns|mn|thousand|t|b|m|k|x)?",
    re.IGNORECASE,
)
_MAX_NUMERIC_CLAIM_VALUE_CHARS = 64
# Standalone year-like integers (1900-2099) are period labels, not material
# quantities: "in FY2025 ..." never needs its own binding row.
_YEAR_LIKE_TOKEN_RE = re.compile(r"(19|20)\d{2}")



def _numeric_claim_scalar(value: object) -> str:
    """Whitespace-collapsed text of one scalar row field for alias matching."""
    return re.sub(r"\s+", " ", str(value)).strip()


def _numeric_claim_row_problems(
    row: object, index: int, seen_ids: set[str]
) -> list[str]:
    """Structural problems of one ledger row; kind-exclusive and bounded."""
    where = f"numeric_claims[{index}]"
    if not isinstance(row, dict):
        return [f"{where}: row must be an object"]
    problems: list[str] = []
    claim_id = row.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id.strip():
        problems.append(f"{where}: claim_id must be a nonblank string")
    elif len(claim_id) > _MAX_NUMERIC_CLAIM_ID_CHARS:
        problems.append(
            f"{where}: claim_id exceeds {_MAX_NUMERIC_CLAIM_ID_CHARS} characters"
        )
    else:
        if claim_id in seen_ids:
            problems.append(f"{where}: duplicate claim_id {claim_id!r}")
        seen_ids.add(claim_id)
    path = row.get("path")
    if (
        not isinstance(path, str)
        or not path.strip()
        or len(path) > _MAX_NUMERIC_CLAIM_PATH_CHARS
    ):
        problems.append(f"{where}: path must be a nonblank bounded string")
    value = row.get("value")
    if isinstance(value, bool):
        problems.append(f"{where}: value must be a finite number or numeric string")
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            problems.append(f"{where}: value must be finite")
    elif not (
        isinstance(value, str)
        and 0 < len(value.strip()) <= _MAX_NUMERIC_CLAIM_VALUE_CHARS
        and _NUMERIC_CLAIM_VALUE_RE.fullmatch(value.strip())
    ):
        problems.append(
            f"{where}: value must be a finite number or a numeric string "
            f"(e.g. \"19\", \"$19B\", \"28%\") of at most {_MAX_NUMERIC_CLAIM_VALUE_CHARS} characters"
        )
    for field, limit in (("metric", _MAX_NUMERIC_CLAIM_ALIAS_CHARS), ("period", _MAX_NUMERIC_CLAIM_ALIAS_CHARS)):
        text = row.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            problems.append(f"{where}: {field} must be a nonblank string of at most {limit} characters")
    if row.get("unit") not in NUMERIC_CLAIM_UNITS:
        problems.append(
            f"{where}: unit must be one of {sorted(NUMERIC_CLAIM_UNITS)}"
        )
    currency = row.get("currency")
    if currency is not None and (
        not isinstance(currency, str) or len(currency) > 16 or not currency.strip()
    ):
        problems.append(f"{where}: currency must be null or a short string")
    source_kind = row.get("source_kind")
    if source_kind == "text":
        quote = row.get("quote")
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or len(quote) > _MAX_NUMERIC_CLAIM_QUOTE_CHARS
        ):
            problems.append(
                f"{where}: source_kind text requires a verbatim producer "
                f"quote of at most {_MAX_NUMERIC_CLAIM_QUOTE_CHARS} characters"
            )
        for extra in ("fact_path", "operation", "operands"):
            if extra in row:
                problems.append(f"{where}: source_kind text must not carry {extra}")
    elif source_kind == "fact":
        fact_path = row.get("fact_path")
        if (
            not isinstance(fact_path, str)
            or not fact_path.strip()
            or len(fact_path) > _MAX_NUMERIC_CLAIM_PATH_CHARS
            or not _LEDGER_PATH_RE.fullmatch(fact_path.strip())
        ):
            problems.append(
                f"{where}: source_kind fact requires a dotted fact_path into "
                "the deterministic current/prior metrics"
            )
        for extra in ("quote", "operation", "operands"):
            if extra in row:
                problems.append(f"{where}: source_kind fact must not carry {extra}")
    elif source_kind == "arithmetic":
        if row.get("operation") not in ("sum", "difference", "product", "quotient"):
            problems.append(
                f"{where}: source_kind arithmetic requires operation "
                "sum|difference|product|quotient"
            )
        operands = row.get("operands")
        if (
            not isinstance(operands, list)
            or not 2 <= len(operands) <= 4
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > _MAX_NUMERIC_CLAIM_PATH_CHARS
                or not _LEDGER_PATH_RE.fullmatch(item.strip())
                for item in operands
            )
        ):
            problems.append(
                f"{where}: operands must list 2..4 dotted fact paths"
            )
        for extra in ("quote", "fact_path"):
            if extra in row:
                problems.append(f"{where}: source_kind arithmetic must not carry {extra}")
    else:
        problems.append(
            f"{where}: source_kind must be one of text|fact|arithmetic"
        )
    return problems


def validate_numeric_claim_rows(rows: object) -> list[str]:
    """Structural pass over the whole authored ledger; bounded and ordered."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["numeric_claims: must be an array when present"]
    if len(rows) > _MAX_NUMERIC_CLAIM_ROWS:
        return [f"numeric_claims: at most {_MAX_NUMERIC_CLAIM_ROWS} rows are allowed"]
    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        problems.extend(_numeric_claim_row_problems(row, index, seen_ids))
    return problems


def _failing_field_paths(problems: list[str], *, limit: int = 10) -> list[str]:
    """Extract validated failing field paths from bounded problem strings."""
    paths: list[str] = []
    for problem in problems:
        match = _FAILING_FIELD_PATH_RE.match(problem)
        if match is None:
            continue
        path = match.group(1)
        if path not in paths:
            paths.append(path)
            if len(paths) >= limit:
                break
    return paths




# --- Numeric-claim semantic resolution (shared by validation and the
# company hard gate). Deterministic pointer resolution and tuple
# compatibility only: no fuzzy matching, no model-authored arithmetic.
_CLAIM_FACT_ROOTS = ("deterministic_current", "deterministic_prior")
_CLAIM_OPERAND_UNITS = frozenset(NUMERIC_CLAIM_UNITS)
_LEDGER_PATH_RE = re.compile(r"[A-Za-z0-9_$\[\]\-]+(?:\.[A-Za-z0-9_$\[\]\-]+)*")
_SCALE_BY_UNIT = {
    "usd_billions": Decimal("1e9"),
    "usd_millions": Decimal("1e6"),
    "usd_per_share": Decimal(1),
    "percent": Decimal("0.01"),
    "percentage_points": Decimal(1),
    "ratio": Decimal(1),
    "count": Decimal(1),
}
_CURRENCY_UNITS = frozenset(
    {"usd_billions", "usd_millions", "usd_per_share"}
)
_PERCENT_LIKE_UNITS = frozenset({"percent", "percentage_points"})
_UNIT_RENDERINGS = {
    "usd_billions": (("billion", "billions"),),
    "usd_millions": (("million", "millions"),),
    "usd_per_share": ((),),
    "percent": (("%",),),
    "percentage_points": (
        ("point", "points"),
        (
            "percentage point",
            "percentage points",
            "percentage-point",
            "percentage-points",
        ),
        ("bp", "bps"),
        ("basis point", "basis points"),
    ),
    "ratio": (("x",), ()),
    "count": ((),),
}
_METRIC_ALIAS_TOKENS = {
    "capex": ("capital expenditure", "capital expenditures", "capital expense", "capex", "capexes"),
    "free_cash_flow": ("free cash flow", "fcf"),
    "operating_cash_flow": ("cash flow from operations", "operating cash flow"),
    "revenue": ("revenue", "revenues", "sales"),
    "net_income": ("net income", "net earnings"),
    "diluted_eps": ("earnings per share", "eps"),
    "gross_margin_dollars": ("gross margin",),
    "microsoft_cloud_revenue": ("microsoft cloud revenue", "cloud revenue"),
    "azure_growth": (
        "azure",
        "azure and other cloud services",
        "azure and other cloud services revenue",
        "azure revenue growth",
        "azure and other cloud services revenue growth",
    ),
    "azure_ai_growth_contribution": (
        "azure growth contribution from ai services",
        "azure growth from ai services",
        "azure growth from ai services contribution",
        "azure growth from ai services points",
        "azure ai services contribution",
        "ai services contribution",
        "point from ai services",
        "points from ai services",
    ),
    "guidance": ("guidance", "guide", "expect", "expects", "expected", "outlook"),
    "bookings": ("bookings", "commercial bookings"),
    "headcount": ("headcount", "employees"),
    "dividend": ("dividend", "dividends"),
    "buyback": ("share repurchase", "buyback", "repurchase"),
}
_METRIC_ALIAS_LOOKUP = {
    alias: metric
    for metric, aliases in _METRIC_ALIAS_TOKENS.items()
    for alias in aliases
}
_NUMERIC_TARGET_METRIC_ALIAS_LOOKUP = {
    **_METRIC_ALIAS_LOOKUP,
    **{
        f"cash {alias}": "capex"
        for alias in _METRIC_ALIAS_TOKENS["capex"]
    },
    "cash paid for property and equipment": "capex",
    "cash capital expenditures": "capex",
    "capital expenditure including finance lease": "lease_inclusive_capex",
    "capital expenditure including finance leases": "lease_inclusive_capex",
    "capital expenditures including finance lease": "lease_inclusive_capex",
    "capital expenditures including finance leases": "lease_inclusive_capex",
    "capital expenditure including finance lease additions": "lease_inclusive_capex",
    "capital expenditures including finance lease additions": "lease_inclusive_capex",
    "capex including finance lease": "lease_inclusive_capex",
    "capex including finance leases": "lease_inclusive_capex",
    "capex including finance lease additions": "lease_inclusive_capex",
    "lease inclusive capex": "lease_inclusive_capex",
    "lease-inclusive capex": "lease_inclusive_capex",
}
_CONTRIBUTION_ALIAS_MORPHOLOGY_RE = re.compile(
    r"(?<![a-z0-9])contribut(?:e|es|ed|ing|ion)(?![a-z0-9])"
)

_AI_CONTRIBUTION_RECIPIENT_RE = re.compile(
    r"(?<![a-z0-9])ai[ \t]+services[ \t]+"
    r"(?:(?P<scalarless>contribution[ \t]+to)|"
    r"contribut(?:e|es|ed|ing|ion)[ \t]+"
    r"[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?"
    r"(?:[ \t]+(?:percentage(?:[ \t]+|-))?points?|-percentage-points?)"
    r"[ \t]+to)[ \t]+"
    r"(?:(?:year[ \t-]+over[ \t-]+year|yoy)[ \t]+)?"
    r"(?P<azure>azure)[ \t]+growth(?![a-z0-9])",
    re.IGNORECASE,
)


def _ai_contribution_recipient_azure_spans(
    text: str,
    *,
    allow_scalarless_alias: bool = False,
) -> frozenset[tuple[int, int]]:
    """Bare Azure spans serving as recipients in bounded contribution claims."""
    return frozenset(
        match.span("azure")
        for match in _AI_CONTRIBUTION_RECIPIENT_RE.finditer(text)
        if allow_scalarless_alias or match.group("scalarless") is None
    )


_SPLIT_EPS_ALIAS_RE = re.compile(
    r"(?<![a-z0-9])earnings(?: [a-z0-9]+){1,8} per share(?![a-z0-9])"
)
_METRIC_CLAUSE_BOUNDARY_RE = re.compile(
    r"[!?;:\n]+|(?<!\d)[.,]+|[.,]+(?!\d)|\b(?:and|but|while|whereas)\b"
)
_NUMERIC_CLAIM_DISPLAY_RE = re.compile(
    rf"(?<![A-Za-z0-9%_]){_SIGNED_CURRENCY_SURFACE_RE_FRAGMENT}"
    r"\d(?:[\d,]*\d)?(?:\.\d+)?"
    r"(?:\s*(?:%|bps?|basis\s+points?|trillions?|billions?|bns|bn|"
    r"millions?|mns|mn|thousands?|[tbmkx]))?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_NUMERIC_TARGET_POINTS_SUFFIX_RE = re.compile(
    r"(?:[ \t]+(?:percentage(?:[ \t]+|-))?points?|-percentage-points?)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_NUMERIC_CLAIM_EXPLICIT_SCALE_RE = re.compile(
    r"(?:%|bps?|basis\s+points?|trillions?|billions?|bns|bn|"
    r"millions?|mns|mn|thousands?|[tbmkx])\s*$",
    re.IGNORECASE,
)
_NUMERIC_FACT_RANGE_CONNECTOR_RE = re.compile(
    r"\s*(?:-|–|—|to|through|and)\s*",
    re.IGNORECASE,
)
_NUMERIC_TARGET_CLAUSE_BOUNDARY_RE = re.compile(
    r"[!?;:\n]|(?<!\d)\.|[.](?!\d)|(?<!\d),(?!\d)|"
    r"\b(?:and|despite|but|while|whereas)\b",
    re.IGNORECASE,
)
_NUMERIC_TARGET_FORWARD_HORIZON_BRIDGE_RE = re.compile(
    r"\s*(?:(?:year[\s-]+over[\s-]+year|quarter[\s-]+over[\s-]+quarter|"
    r"month[\s-]+over[\s-]+month|yoy|qoq|mom)\s+)?"
    r"(?:during|over|for)\s+(?:the\s+)?(?:next|following|forward)\s*",
    re.IGNORECASE,
)
_NUMERIC_TARGET_COMMA_DIRECTION_BRIDGE_RE = re.compile(
    r"\s*(?:(?:in\s+fy\s*\d{2,4}(?:\s*[- ]?\s*q[1-4])?)|"
    r"(?:for\s+the\s+period\s+ended\s+(?:19|20)\d{2}-\d{2}-\d{2}))?"
    r"\s*,\s*(?:up|down)(?:\s+by)?\s*",
    re.IGNORECASE,
)
_NUMERIC_TARGET_DURATION_UNIT_RE = re.compile(
    r"\s*(?:days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_NUMERIC_TARGET_ISO_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"
)
_NUMERIC_TARGET_CURRENCY_CONTEXT_CHARS = 8

# Shared authored-material scanner. These lexical rules deliberately sit beside
# numeric-claim resolution so live validation and replay classify the same
# surface before either layer renders its own failure type.
_MATERIAL_NUMERIC_TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9%_]){_SIGNED_CURRENCY_SURFACE_RE_FRAGMENT}"
    r"\d[\d,]*(?:\.\d+)?(?:%|bps|bp)?",
    re.IGNORECASE,
)
_MATERIAL_DISPLAYED_NUMBER_RE = re.compile(
    rf"(?<![A-Za-z0-9%_]){_SIGNED_CURRENCY_SURFACE_RE_FRAGMENT}"
    r"\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:%|bps?|basis\s+points?|trillions?|billions?|bns|bn|"
    r"millions?|mns|mn|thousands?|[tbmkx]))?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_MATERIAL_COUNT_RENDERING_RE = re.compile(
    r"\s+(?:days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_MATERIAL_COUNT_NOUN_RE = re.compile(
    r"\s+(?:accounts?|applications?|businesses|clients?|contracts?|customers?|"
    r"devices?|employees?|installations?|licenses?|locations?|members?|orders?|"
    r"people|products?|records?|seats?|shares?|sites?|stores?|subscribers?|"
    r"transactions?|units?|users?|workers?)\b",
    re.IGNORECASE,
)
_MATERIAL_IDENTIFIER_PREFIX_RE = re.compile(
    r"\b(?:form|item|section|version)\s+(?:no\.?\s*)?"
    r"(?:[A-Za-z0-9]+[./-])*$",
    re.IGNORECASE,
)
_MATERIAL_SIGNED_CURRENCY_PREFIX_RE = re.compile(
    r"(?:[+-]\s*[$€£¥]\s*|[$€£¥]\s*[+-]?\s*)$"
)
_MATERIAL_PROPER_NAME_LEFT_RE = re.compile(
    r"(?P<word>[A-Za-z](?:[A-Za-z'’.-]*[A-Za-z])?)\s+$"
)
_MATERIAL_PROPER_NAME_RIGHT_RE = re.compile(
    r"\s+(?P<word>[A-Za-z](?:[A-Za-z'’.-]*[A-Za-z])?)"
)
_MATERIAL_EXPLICIT_SCALE_RE = re.compile(
    r"(?:%|bps?|basis\s+points?|trillions?|billions?|bns|bn|"
    r"millions?|mns|mn|thousands?|[tbmkx])\s*$",
    re.IGNORECASE,
)
_ENGLISH_MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MATERIAL_CALENDAR_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})|"
    r"(?P<named_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) "
    r"(?P<named_day>\d{1,2}), (?P<named_year>\d{4})|"
    r"(?P<leading_day>\d{1,2}) "
    r"(?P<leading_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) (?P<leading_year>\d{4})"
    r")(?![A-Za-z0-9_%])",
    re.IGNORECASE,
)


def _metric_alias_groups(text: str) -> frozenset[str]:
    """Canonical metric groups explicitly named in one bounded text span."""
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
    normalized = _CONTRIBUTION_ALIAS_MORPHOLOGY_RE.sub(
        "contribution", normalized
    )
    occurrences: list[tuple[int, int, str]] = []
    for alias, group in _METRIC_ALIAS_LOOKUP.items():
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        occurrences.extend(
            (match.start(), match.end(), group)
            for match in re.finditer(pattern, normalized)
        )
    # Prefer the most specific alias at each location. For example,
    # "Microsoft cloud revenue" is one metric, not both cloud revenue and
    # generic revenue. A separate "revenue" occurrence remains competing.
    groups = {
        group
        for start, end, group in occurrences
        if not any(
            other_start <= start
            and other_end >= end
            and (other_start, other_end) != (start, end)
            and other_group != group
            for other_start, other_end, other_group in occurrences
        )
    }
    # A bare trailing "Azure" is the recipient, rather than another metric,
    # only in the bounded AI-services contribution construction. Preserve the
    # generic group if any other Azure mention remains in the row field.
    suppressed_azure_spans = _ai_contribution_recipient_azure_spans(
        text, allow_scalarless_alias=True
    )
    azure_mentions = frozenset(
        match.span()
        for match in re.finditer(
            r"(?<![a-z0-9])azure(?![a-z0-9])", text, re.IGNORECASE
        )
    )
    if suppressed_azure_spans and azure_mentions <= suppressed_azure_spans:
        groups.discard("azure_growth")
    # Split EPS wording may put short value language between "earnings" and
    # "per share", but never bridge a bounded clause. Keep this group separate
    # from the occurrence suppression above so "net earnings ... per share"
    # retains both net-income and EPS groups and therefore stays mixed.
    if any(
        _SPLIT_EPS_ALIAS_RE.search(
            " ".join(re.findall(r"[a-z0-9]+", clause))
        )
        for clause in _METRIC_CLAUSE_BOUNDARY_RE.split(text.casefold())
    ):
        groups.add("diluted_eps")
    return frozenset(groups)


def _numeric_target_metric_alias_groups(text: str) -> frozenset[str]:
    """Specificity-filtered target aliases in one bounded text span."""
    lookup_text = text.replace("_", " ")
    return frozenset(
        group
        for _, _, group in _metric_alias_occurrences(
            lookup_text, suppress_scalarless_ai_recipient=True
        )
    )
# Target periods are lexical identities, not substrings.  These expressions
# intentionally recognize only bounded spellings that the ledger contract
# supports; in particular, a calendar date never manufactures a fiscal label.
_TARGET_PERIOD_YEAR_TOKEN_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TARGET_PERIOD_MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_TARGET_PERIOD_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})|"
    r"(?P<named_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) "
    r"(?P<named_day>\d{1,2}), (?P<named_year>\d{4})|"
    r"(?P<leading_day>\d{1,2}) "
    r"(?P<leading_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) (?P<leading_year>\d{4})"
    r")(?![A-Za-z0-9_%])",
    re.IGNORECASE,
)
_TARGET_FISCAL_QUARTER_RES = (
    re.compile(
        r"\bfy\s*(?P<year>\d{4}|\d{2})(?:\s*-\s*|\s+)"
        r"q(?P<quarter>[1-4])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bq(?P<quarter>[1-4])(?:\s*-\s*|\s+)"
        r"fy\s*(?P<year>\d{4}|\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfy\s*(?P<year>\d{4}|\d{2})(?:\s*-\s*|\s+)"
        r"(?P<quarter>first|second|third|fourth)\s+quarter\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfiscal(?:\s+year)?\s+(?P<year>\d{4})(?:\s*-\s*|\s+)"
        r"(?P<quarter>q[1-4]|first|second|third|fourth)"
        r"(?:\s+quarter)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<quarter>first|second|third|fourth)\s+quarter"
        r"(?:\s+of)?\s+fiscal(?:\s+year)?\s+(?P<year>\d{4})\b",
        re.IGNORECASE,
    ),
)
_TARGET_FISCAL_YEAR_RE = re.compile(
    r"\b(?:fy\s*(?P<short>\d{4}|\d{2})|"
    r"fiscal(?:\s+year)?\s+(?P<long>\d{4}))\b",
    re.IGNORECASE,
)
_TARGET_CALENDAR_QUARTER_RES = (
    re.compile(
        r"\bq(?P<quarter>[1-4])\s+(?P<year>(?:19|20)\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<year>(?:19|20)\d{2})\s+q(?P<quarter>[1-4])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<quarter>first|second|third|fourth)\s+quarter"
        r"(?:\s+of)?\s+(?P<year>(?:19|20)\d{2})\b",
        re.IGNORECASE,
    ),
)
_TARGET_RELATIVE_PERIOD_RE = re.compile(
    r"\b(?P<direction>next|following|forward|current|this|prior|previous|last)"
    r"\s+(?:(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve)(?:\s+|-\s*))?"
    r"(?P<unit>days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_TARGET_COMPARISON_PERIOD_RE = re.compile(
    r"\b(?P<kind>year[\s-]+over[\s-]+year|quarter[\s-]+over[\s-]+quarter|"
    r"month[\s-]+over[\s-]+month|yoy|qoq|mom|sequential(?:ly)?)\b",
    re.IGNORECASE,
)
_TARGET_COMPARISON_LABELS = frozenset(
    {
        "relative:year-over-year",
        "relative:quarter-over-quarter",
        "relative:month-over-month",
    }
)
_TARGET_QUARTER_NUMBERS = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
}
_TARGET_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}


def _canonical_claim_number(value: object) -> Decimal | None:
    """Canonical Decimal of an authored row value or source quantity.

    Accepts finite JSON numbers and numeric strings with one optional currency
    token, one adjacent sign, thousands separators, percent/bps suffixes,
    magnitude suffixes, and the dimensionless ``x`` suffix.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace(",", "")
    currency_tokens = re.findall(r"[$€£¥]|usd", text)
    if len(currency_tokens) > 1:
        return None
    if currency_tokens:
        currency = r"(?:[$€£¥]|usd)"
        prefix = re.match(
            rf"(?:(?P<before>[+-])?{currency}\s*|"
            rf"{currency}(?P<after>[+-])\s*)(?=\d)",
            text,
        )
        if prefix is not None:
            sign = prefix.group("before") or prefix.group("after") or ""
            text = sign + text[prefix.end():]
        else:
            currency_match = re.search(currency, text)
            if (
                currency_match is None
                or re.search(r"\d", text[:currency_match.start()]) is None
            ):
                return None
            text = text[:currency_match.start()] + text[currency_match.end():]
    scale = Decimal(1)
    for suffix, factor in (
        ("bps", Decimal("0.0001")),
        ("bp", Decimal("0.0001")),
        ("%", Decimal("0.01")),
        ("basis points", Decimal("0.0001")),
    ):
        if text.endswith(suffix):
            scale = factor
            text = text[: -len(suffix)].strip()
            break
    match = re.fullmatch(
        r"([-+]?\d+(?:\.\d+)?)\s*(trillion|billion|b|bns|bn|million|m|mn|k|thousand|x)?",
        text,
    )
    if match is None:
        return None
    number = Decimal(match.group(1))
    suffix_word = match.group(2)
    if suffix_word:
        scale *= {
            "trillion": Decimal("1e12"),
            "billion": Decimal("1e9"),
            "b": Decimal("1e9"),
            "bns": Decimal("1e9"),
            "bn": Decimal("1e9"),
            "million": Decimal("1e6"),
            "m": Decimal("1e6"),
            "mn": Decimal("1e6"),
            "k": Decimal("1e3"),
            "thousand": Decimal("1e3"),
            "x": Decimal(1),
        }[suffix_word]
    product = number * scale
    return product if product.is_finite() else None


def _unit_for_rendering(text: str, unit: str) -> bool:
    """Does ``text`` render ``unit``? Bounded rendering families only."""
    folded = text.casefold()
    if unit in {"usd_billions", "usd_millions"}:
        return (
            _NUMERIC_CLAIM_EXPLICIT_SCALE_RE.search(folded.strip()) is not None
            and _canonical_claim_number(text) is not None
            and _numeric_target_unit_compatible(text, unit)
        )
    for family in _UNIT_RENDERINGS.get(unit, ()):
        if any(rendering in folded for rendering in family):
            return True
    return False


def _metric_alias_matches(metric_field: str, span_text: str) -> bool:
    """Metric identity between the row's metric field and one text span.

    Repository-known aliases resolve to canonical groups before comparison,
    so a field named ``capital expenditures`` may bind a bounded ``capex``
    clause (and ``free cash flow`` may bind ``FCF``). The complete group sets
    must agree: a clause naming a competing metric never matches merely
    because it also contains the requested one. A bare number or vague prose
    with no metric words never matches.
    """
    metric_groups = _metric_alias_groups(metric_field)
    if metric_groups:
        return _metric_alias_groups(span_text) == metric_groups
    if _metric_alias_groups(span_text):
        return False

    metric_words = {
        word
        for word in re.findall(r"[a-z]+", metric_field.casefold())
        if len(word) >= 4
    } - {"and", "the", "from", "with", "growth"}
    span_words = set(re.findall(r"[a-z]+", span_text.casefold()))
    return bool(metric_words) and metric_words <= span_words


def _numeric_claim_fact_roots(
    deterministic_current: object,
    deterministic_prior: object,
    relationship_facts: object = None,
) -> tuple[object, object]:
    """Overlay the exact frozen request relationship facts immutably."""
    if not isinstance(deterministic_current, Mapping):
        return deterministic_current, deterministic_prior
    if not isinstance(relationship_facts, Mapping):
        return deterministic_current, deterministic_prior
    current = dict(deterministic_current)
    current["relationship_facts"] = relationship_facts
    return MappingProxyType(current), deterministic_prior


def _resolve_claim_fact_path(
    path: str, deterministic_current: object, deterministic_prior: object
) -> tuple[dict, Any, bool]:
    """Resolve a dotted ledger pointer into the frozen deterministic facts.

    The path must name the root (``deterministic_current`` /
    ``deterministic_prior``) explicitly and then descend through mappings
    and indexable sequences only (frozen ``Mapping``/tuple packets included).
    Returns ``(root_name, value, resolved)``; attribute or cross-root
    traversal is never attempted.
    """
    parts = [part for part in path.strip().split(".") if part]
    if len(parts) < 2:
        return "", None, False
    root_name = parts[0]
    if root_name not in _CLAIM_FACT_ROOTS:
        return "", None, False
    root = (
        deterministic_current if root_name == "deterministic_current"
        else deterministic_prior
    )
    if not isinstance(root, Mapping):
        return root_name, None, False
    node: Any = root
    for part in parts[1:]:
        match = re.fullmatch(r"([^\[\]]+)\[(\d+)\]", part)
        if match:
            name, index_text = match.group(1), match.group(2)
            if isinstance(node, Mapping) and name in node:
                node = node[name]
                if not isinstance(node, (list, tuple)):
                    return root_name, None, False
                try:
                    index = int(index_text)
                except ValueError:
                    return root_name, None, False
                node = node[index] if 0 <= index < len(node) else None
                continue
        if isinstance(node, Mapping) and part in node:
            node = node[part]
            continue
        return root_name, None, False
    return root_name, node, True


def _target_period_quarter_number(value: str) -> str:
    folded = value.casefold()
    return (
        folded[1:]
        if folded.startswith("q")
        else _TARGET_QUARTER_NUMBERS.get(folded, folded)
    )


def _target_fiscal_year_number(value: str) -> str:
    """Expand only the bounded modern two-digit fiscal-year spelling."""
    return f"20{value}" if len(value) == 2 else value


def _target_calendar_date_label(match: re.Match[str]) -> str | None:
    try:
        if match.group("iso_year") is not None:
            parsed = date(
                int(match.group("iso_year")),
                int(match.group("iso_month")),
                int(match.group("iso_day")),
            )
        elif match.group("named_month") is not None:
            parsed = date(
                int(match.group("named_year")),
                _TARGET_PERIOD_MONTH_NUMBERS[
                    match.group("named_month").casefold()
                ],
                int(match.group("named_day")),
            )
        else:
            parsed = date(
                int(match.group("leading_year")),
                _TARGET_PERIOD_MONTH_NUMBERS[
                    match.group("leading_month").casefold()
                ],
                int(match.group("leading_day")),
            )
    except (TypeError, ValueError):
        return None
    return f"calendar-date:{parsed.isoformat()}"


def _canonical_period_occurrences(
    period: object,
) -> tuple[tuple[int, int, str], ...]:
    """Extract bounded typed period identities with their source spans."""
    text = str(period or "")
    if not text.strip():
        return ()
    occupied: list[tuple[int, int]] = []
    occurrences: list[tuple[int, int, str]] = []

    def available(span: tuple[int, int]) -> bool:
        return not any(
            start < span[1] and span[0] < end for start, end in occupied
        )

    def add(label: str, span: tuple[int, int]) -> None:
        if available(span):
            occurrences.append((span[0], span[1], label))
            occupied.append(span)

    for pattern in _TARGET_FISCAL_QUARTER_RES:
        for match in pattern.finditer(text):
            year = _target_fiscal_year_number(match.group("year"))
            quarter = _target_period_quarter_number(match.group("quarter"))
            add(f"fiscal-quarter:{year}:q{quarter}", match.span())
    for match in _TARGET_FISCAL_YEAR_RE.finditer(text):
        year = match.group("short") or match.group("long")
        add(
            f"fiscal-year:{_target_fiscal_year_number(year)}",
            match.span(),
        )
    for pattern in _TARGET_CALENDAR_QUARTER_RES:
        for match in pattern.finditer(text):
            quarter = _target_period_quarter_number(match.group("quarter"))
            add(
                f"calendar-quarter:{match.group('year')}:q{quarter}",
                match.span(),
            )
    for match in _TARGET_PERIOD_DATE_RE.finditer(text):
        label = _target_calendar_date_label(match)
        if label is not None:
            add(label, match.span())
    for match in _TARGET_RELATIVE_PERIOD_RE.finditer(text):
        direction = {
            "following": "next",
            "forward": "next",
            "this": "current",
            "previous": "prior",
            "last": "prior",
        }.get(match.group("direction").casefold(), match.group("direction").casefold())
        count = str(match.group("count") or "1").casefold()
        count = _TARGET_NUMBER_WORDS.get(count, count)
        unit = match.group("unit").casefold().rstrip("s")
        add(f"relative:{direction}:{count}:{unit}", match.span())
    for match in _TARGET_COMPARISON_PERIOD_RE.finditer(text):
        kind = re.sub(r"[\s-]+", "-", match.group("kind").casefold())
        kind = {
            "yoy": "year-over-year",
            "qoq": "quarter-over-quarter",
            "mom": "month-over-month",
            "sequential": "quarter-over-quarter",
            "sequentially": "quarter-over-quarter",
        }.get(kind, kind)
        add(f"relative:{kind}", match.span())
    for match in _TARGET_PERIOD_YEAR_TOKEN_RE.finditer(text):
        add(f"calendar-year:{match.group(0)}", match.span())
    return tuple(sorted(occurrences, key=lambda item: (item[0], item[1], item[2])))


def _canonical_period_labels(period: object) -> set[str]:
    """Extract bounded, typed period identities without cross-granularity inference."""
    return {
        label for _, _, label in _canonical_period_occurrences(period)
    }


def _primary_period_labels(labels: set[str]) -> set[str]:
    """Exclude comparison bases from claim-period identities."""
    return labels.difference(_TARGET_COMPARISON_LABELS)

def _period_bundles_compatible(
    left_labels: set[str], right_labels: set[str]
) -> bool:
    """Match unambiguous period identities across every shared family."""
    left = _primary_period_labels(left_labels)
    right = _primary_period_labels(right_labels)
    if left or right:
        if (
            not left
            or not right
            or _period_bundle_conflict(left)
            or _period_bundle_conflict(right)
        ):
            return False
        left_by_family = {
            _period_label_family(label): label for label in left
        }
        right_by_family = {
            _period_label_family(label): label for label in right
        }
        shared_families = left_by_family.keys() & right_by_family.keys()
        return bool(shared_families) and all(
            left_by_family[family] == right_by_family[family]
            for family in shared_families
        )
    left_comparisons = left_labels & _TARGET_COMPARISON_LABELS
    right_comparisons = right_labels & _TARGET_COMPARISON_LABELS
    return bool(left_comparisons) and left_comparisons == right_comparisons


def _period_alias_matches(period_field: str, span_text: str) -> bool:
    """Match canonical period bundles without relying on display spelling."""
    wanted_labels = _canonical_period_labels(period_field)
    rendered_labels = _canonical_period_labels(span_text)
    if wanted_labels or rendered_labels:
        return _period_bundles_compatible(wanted_labels, rendered_labels)
    # Preserve exact opaque period names, but never accept substring overlap.
    wanted_text = re.sub(r"\s+", " ", str(period_field)).strip().casefold()
    rendered_text = re.sub(r"\s+", " ", str(span_text)).strip().casefold()
    return bool(wanted_text) and wanted_text == rendered_text


def _target_period_only_interposition(text: str) -> bool:
    """Accept only one explicit primary period between split alias words."""
    match = re.fullmatch(
        r"\s*(?:in|for|during|as\s+of)\s+(.+?)\s*",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return False
    period_text = match.group(1)
    primary_patterns = (
        *_TARGET_FISCAL_QUARTER_RES,
        _TARGET_FISCAL_YEAR_RE,
        *_TARGET_CALENDAR_QUARTER_RES,
        _TARGET_PERIOD_DATE_RE,
        _TARGET_RELATIVE_PERIOD_RE,
        _TARGET_PERIOD_YEAR_TOKEN_RE,
    )
    return any(
        pattern.fullmatch(period_text) is not None
        for pattern in primary_patterns
    )


def _declared_numeric_claim_derivation(
    metric: str,
    operation: str,
    operands: list[str],
    deterministic_current: object,
    deterministic_prior: object,
) -> tuple[Mapping[str, Any] | None, str]:
    """Find the producer fact whose ``derived:`` concept declares arithmetic."""
    operator = {
        "sum": "+",
        "difference": "-",
        "product": "*",
        "quotient": "/",
    }.get(operation)
    if operator is None:
        return None, "unknown arithmetic operation"
    operand_parts = [
        [part for part in operand.strip().split(".") if part]
        for operand in operands
    ]
    if any(
        len(parts) != 3 or parts[2].casefold() != "value"
        for parts in operand_parts
    ):
        return (
            None,
            "arithmetic operands must reference exact typed fact value leaves",
        )
    roots = {parts[0] for parts in operand_parts}
    if len(roots) != 1:
        return None, "a declared derivation cannot cross deterministic roots"
    root_name = next(iter(roots))
    if root_name not in _CLAIM_FACT_ROOTS:
        return None, "arithmetic operands must name a deterministic fact root"
    root = (
        deterministic_current
        if root_name == "deterministic_current"
        else deterministic_prior
    )
    operand_metrics = [parts[1].casefold() for parts in operand_parts]
    for output_name, candidate in (
        root.items() if isinstance(root, Mapping) else ()
    ):
        if not isinstance(candidate, Mapping):
            continue
        source = str(candidate.get("source") or "").casefold()
        concept = str(candidate.get("concept") or "")
        if not source.startswith("derived") or not concept.startswith("derived:"):
            continue
        expression = concept.removeprefix("derived:").strip().casefold()
        tokens = re.findall(r"[a-z0-9_]+|[+\-*/]", expression)
        declared_names = tokens[::2]
        declared_operators = tokens[1::2]
        if (
            len(declared_names) != len(operand_metrics)
            or declared_operators != [operator] * (len(declared_names) - 1)
        ):
            continue
        names_match = declared_names == operand_metrics
        if operation in {"sum", "product"}:
            names_match = sorted(declared_names) == sorted(operand_metrics)
        if not names_match:
            continue
        if not _metric_alias_matches(
            metric, str(output_name).replace("_", " ")
        ):
            continue
        return candidate, str(output_name)
    return None, "no producer-derived output fact declares this operation and operands"

def _relationship_numeric_target_keys(
    relationship_facts: object,
    material_relationships: object,
    required_fact_indexes: object,
) -> frozenset[tuple[str, str]]:
    """Authored observation/coefficient keys governed by relationship bindings."""
    if not isinstance(relationship_facts, Mapping) or not isinstance(
        material_relationships, (list, tuple)
    ):
        return frozenset()
    selected = (
        {
            (candidate[0], candidate[1])
            for candidate in required_fact_indexes
            if (
                isinstance(candidate, (list, tuple))
                and len(candidate) == 2
                and all(
                    isinstance(index, int) and not isinstance(index, bool)
                    for index in candidate
                )
            )
        }
        if isinstance(required_fact_indexes, (list, tuple, set, frozenset))
        else set()
    )
    keys: set[tuple[str, str]] = set()
    for relationship_index, relationship in enumerate(material_relationships):
        if (
            not isinstance(relationship, Mapping)
            or relationship.get("compatibility") != "compatible"
        ):
            continue
        target = f"/relationship_reconciliations/{relationship_index}/observation"
        required_facts = relationship.get("required_facts")
        required_facts = (
            required_facts
            if isinstance(required_facts, (list, tuple))
            else ()
        )
        for required_fact_index, ref in enumerate(required_facts):
            if not isinstance(ref, Mapping):
                continue
            if (
                relationship_index,
                required_fact_index,
            ) not in selected:
                continue
            fact_path = ref.get("fact_path")
            if not isinstance(fact_path, str):
                continue
            fact = relationship_facts.get(fact_path.rsplit(".", 1)[-1])
            if not isinstance(fact, Mapping):
                continue
            coefficient = _numeric_claim_coefficient_key(
                fact.get("value"), str(fact.get("unit") or "")
            )
            if coefficient is not None:
                keys.add((target, coefficient))
    return frozenset(keys)



def _relationship_numeric_claim_findings(
    facts: Mapping[str, Any],
    rows: list,
    relationship_facts: object,
    material_relationships: object,
    deterministic_current: object,
    deterministic_prior: object,
) -> tuple[list[str], tuple[tuple[int, int], ...]]:
    """Find non-singular exact bindings in frozen relationship/fact order."""
    if not isinstance(relationship_facts, Mapping) or not isinstance(
        material_relationships, (list, tuple)
    ):
        return [], ()
    valid_row_indexes: set[int] = set()
    seen_ids: set[str] = set()
    for row_index, row in enumerate(rows):
        if not _numeric_claim_row_problems(row, row_index, seen_ids):
            valid_row_indexes.add(row_index)

    problems: list[str] = []
    missing_bindings: list[tuple[int, int]] = []
    for relationship_index, relationship in enumerate(material_relationships):
        if (
            not isinstance(relationship, Mapping)
            or relationship.get("compatibility") != "compatible"
        ):
            continue
        target_path = (
            f"/relationship_reconciliations/{relationship_index}/observation"
        )
        required_facts = relationship.get("required_facts")
        required_facts = (
            required_facts
            if isinstance(required_facts, (list, tuple))
            else ()
        )
        for required_fact_index, ref in enumerate(required_facts):
            if not isinstance(ref, Mapping):
                continue
            fact_path = ref.get("fact_path")
            if not isinstance(fact_path, str):
                continue
            normalized_fact = relationship_facts.get(
                fact_path.rsplit(".", 1)[-1]
            )
            if (
                not isinstance(normalized_fact, Mapping)
                or _canonical_claim_number(normalized_fact.get("value")) is None
            ):
                continue
            exact_count = sum(
                row_index in valid_row_indexes
                and isinstance(row, Mapping)
                and row.get("source_kind") == "fact"
                and row.get("fact_path") == fact_path
                and _normalize_claim_path(row.get("path")) == target_path
                and _numeric_fact_claim_tuple_problem(
                    row,
                    facts,
                    deterministic_current,
                    deterministic_prior,
                )
                is None
                for row_index, row in enumerate(rows)
            )
            if exact_count == 1:
                continue
            problems.append(
                f"relationship_reconciliations[{relationship_index}].observation: "
                f"compatible relationship fact {fact_path!r} requires exactly "
                "one numeric_claims fact binding"
            )
            if exact_count == 0:
                missing_bindings.append(
                    (relationship_index, required_fact_index)
                )
    return problems, tuple(missing_bindings)


def _relationship_summary_numeric_claim_findings(
    facts: Mapping[str, Any],
    rows: list,
    relationship_facts: object,
    material_relationships: object,
    deterministic_current: object,
    deterministic_prior: object,
) -> tuple[list[str], frozenset[tuple[str, str]]]:
    """Require one deduplicated summary binding per unique selected fact."""
    if not isinstance(relationship_facts, Mapping) or not isinstance(
        material_relationships, (list, tuple)
    ):
        return [], frozenset()
    reconciliations = facts.get("relationship_reconciliations")
    authored_rows = reconciliations if isinstance(reconciliations, list) else []
    selected_paths: list[str] = []
    seen_paths: set[str] = set()
    for index, relationship in enumerate(material_relationships):
        if (
            not isinstance(relationship, Mapping)
            or relationship.get("compatibility") != "compatible"
            or index >= len(authored_rows)
            or not isinstance(authored_rows[index], Mapping)
        ):
            continue
        selected = authored_rows[index].get("summary_fact_paths")
        if not isinstance(selected, list):
            continue
        for fact_path in selected:
            if isinstance(fact_path, str) and fact_path not in seen_paths:
                seen_paths.add(fact_path)
                selected_paths.append(fact_path)

    valid_row_indexes: set[int] = set()
    seen_ids: set[str] = set()
    for row_index, row in enumerate(rows):
        if not _numeric_claim_row_problems(row, row_index, seen_ids):
            valid_row_indexes.add(row_index)

    problems: list[str] = []
    missing_keys: set[tuple[str, str]] = set()
    for fact_path in selected_paths:
        normalized_fact = relationship_facts.get(fact_path.rsplit(".", 1)[-1])
        if (
            not isinstance(normalized_fact, Mapping)
            or _canonical_claim_number(normalized_fact.get("value")) is None
        ):
            continue
        exact_count = sum(
            row_index in valid_row_indexes
            and isinstance(row, Mapping)
            and row.get("source_kind") == "fact"
            and row.get("fact_path") == fact_path
            and _normalize_claim_path(row.get("path")) == "/summary"
            and _numeric_fact_claim_tuple_problem(
                row,
                facts,
                deterministic_current,
                deterministic_prior,
            )
            is None
            for row_index, row in enumerate(rows)
        )
        if exact_count == 1:
            continue
        problems.append(
            f"summary: selected relationship fact {fact_path!r} requires exactly "
            "one numeric_claims fact binding"
        )
        if exact_count == 0:
            coefficient = _numeric_claim_coefficient_key(
                normalized_fact.get("value"),
                str(normalized_fact.get("unit") or ""),
            )
            if coefficient is not None:
                missing_keys.add(("/summary", coefficient))
    return problems, frozenset(missing_keys)


def _relationship_numeric_claim_problems(
    facts: Mapping[str, Any],
    rows: list,
    relationship_facts: object,
    material_relationships: object,
    deterministic_current: object,
    deterministic_prior: object,
) -> list[str]:
    """Require exactly one valid fact binding for each compatible observation."""
    problems, _ = _relationship_numeric_claim_findings(
        facts,
        rows,
        relationship_facts,
        material_relationships,
        deterministic_current,
        deterministic_prior,
    )
    return problems


def numeric_claim_source_problems(
    facts: dict,
    *,
    deterministic_current: object,
    deterministic_prior: object,
    excerpt: str | None = None,
    news_items: object = None,
    document_metadata: object = None,
    relationship_facts: object = None,
    material_relationships: object = None,
    missing_relationship_bindings: list[tuple[int, int]] | None = None,
) -> list[str]:
    """Semantic pass over the authored ledger against the frozen sources.

    For every structurally valid row, resolve the claimed target and source
    pointer exactly as the hard gate will. A row whose target path is not an
    eligible narrative text leaf in this payload, or whose source cannot be
    resolved/verified, fails closed here — a ledger row is only kept when it
    names real material on both sides.
    """
    authored_rows = facts.get("numeric_claims")
    rows = authored_rows if isinstance(authored_rows, list) else []
    deterministic_current, deterministic_prior = _numeric_claim_fact_roots(
        deterministic_current,
        deterministic_prior,
        relationship_facts,
    )
    problems: list[str] = []
    seen_semantic_bindings: dict[tuple[object, ...], int] = {}
    structurally_valid_indexes: set[int] = set()
    structural_seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not _numeric_claim_row_problems(row, index, structural_seen_ids):
            structurally_valid_indexes.add(index)
    valid_row_indexes: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_problem_start = len(problems)
        where = f"numeric_claims[{index}]"
        claim_id = row.get("claim_id")
        label = (
            f"{where} (claim_id {claim_id!r})"
            if isinstance(claim_id, str) and claim_id.strip()
            else where
        )
        semantic_key = _numeric_claim_semantic_binding_key(row)
        prior_index = (
            seen_semantic_bindings.get(semantic_key)
            if semantic_key is not None
            else None
        )
        if prior_index is not None:
            problems.append(
                f"{label}: duplicate semantic numeric binding already carried "
                f"by numeric_claims[{prior_index}]"
            )
        elif semantic_key is not None:
            seen_semantic_bindings[semantic_key] = index
        source_kind = row.get("source_kind")
        target_eligible = False
        target_path = row.get("path")
        if isinstance(target_path, str) and target_path.strip():
            _, target_eligible = _resolve_numeric_claim_target(facts, target_path)
            if not target_eligible and source_kind != "text":
                problems.append(
                    f"{label}: path {target_path!r} does not resolve to an "
                    "eligible narrative text leaf"
                )
        if source_kind == "text":
            tuple_problem = _numeric_text_claim_tuple_problem(
                row,
                facts,
                excerpt=(
                    str(facts.get("source_excerpt") or "")
                    if excerpt is None
                    else excerpt
                ),
                news_items=(
                    facts.get("news_context")
                    if news_items is None
                    else news_items
                ),
                document_metadata=document_metadata,
            )
            if tuple_problem is not None:
                if tuple_problem.kind == "unresolved":
                    problems.append(f"{label}: {tuple_problem.detail}")
                else:
                    problems.append(
                        f"{label}: text source tuple does not match its "
                        "authored target and bound producer quote: "
                        f"{tuple_problem.detail}"
                    )
        elif source_kind == "fact":
            fact_path = row.get("fact_path")
            if isinstance(fact_path, str) and fact_path.strip():
                if target_eligible:
                    tuple_problem = _numeric_fact_claim_tuple_problem(
                        row,
                        facts,
                        deterministic_current,
                        deterministic_prior,
                    )
                    if tuple_problem is not None:
                        if tuple_problem.kind == "unresolved":
                            problems.append(f"{label}: {tuple_problem.detail}")
                        else:
                            problems.append(
                                f"{label}: fact source tuple does not match "
                                "its authored target and deterministic leaf"
                            )
                elif not _resolve_claim_fact_path(
                    fact_path,
                    deterministic_current,
                    deterministic_prior,
                )[2]:
                    problems.append(
                        f"{label}: fact_path {fact_path!r} does not resolve "
                        "in deterministic current/prior metrics"
                    )
        elif source_kind == "arithmetic" and target_eligible:
            tuple_problem = _numeric_arithmetic_claim_tuple_problem(
                row,
                facts,
                deterministic_current,
                deterministic_prior,
            )
            if tuple_problem is not None:
                if tuple_problem.kind == "unresolved":
                    problems.append(f"{label}: {tuple_problem.detail}")
                elif tuple_problem.kind == "operation_unverified":
                    problems.append(
                        f"{label}: arithmetic is not producer-declared: "
                        f"{tuple_problem.detail}"
                    )
                else:
                    problems.append(
                        f"{label}: arithmetic source tuple does not match "
                        "its authored target and producer-declared output"
                    )
        if (
            index in structurally_valid_indexes
            and len(problems) == row_problem_start
            and target_eligible
        ):
            valid_row_indexes.add(index)
    invalid_row_indexes = set(range(len(rows))) - valid_row_indexes
    relationship_problems, relationship_bindings = (
        _relationship_numeric_claim_findings(
            facts,
            rows,
            relationship_facts,
            material_relationships,
            deterministic_current,
            deterministic_prior,
        )
    )
    summary_problems, summary_missing_keys = (
        _relationship_summary_numeric_claim_findings(
            facts,
            rows,
            relationship_facts,
            material_relationships,
            deterministic_current,
            deterministic_prior,
        )
    )
    for finding in numeric_claim_coverage_findings(
        facts,
        rows,
        valid_row_indexes=valid_row_indexes,
        invalid_row_indexes=invalid_row_indexes,
        specific_finding_keys=(
            _relationship_numeric_target_keys(
                relationship_facts,
                material_relationships,
                relationship_bindings,
            )
            | summary_missing_keys
        ),
    ):
        path = finding.path.removeprefix("$.")
        problems.append(
            f"{path}: material numeric token {finding.coefficient!r} has no "
            "numeric_claims binding"
        )
    problems.extend(relationship_problems)
    problems.extend(summary_problems)
    if missing_relationship_bindings is not None:
        missing_relationship_bindings.extend(relationship_bindings)
    return problems


def _authored_target_segments(path: object) -> tuple[str, ...] | None:
    """Parse one dotted/index or RFC 6901 authored target path.

    JSON Pointer escapes are decoded exactly once.  Dotted bracket indexes are
    kept as string segments so resolution and canonical comparison can apply
    the same container-aware index rules.
    """
    if not isinstance(path, str):
        return None
    text = path
    if text.startswith("/"):
        segments: list[str] = []
        for raw_segment in text[1:].split("/"):
            if re.search(r"~(?:[^01]|$)", raw_segment):
                return None
            segments.append(
                raw_segment.replace("~1", "/").replace("~0", "~")
            )
        return tuple(segments)

    if text in {"", "$"}:
        return ()
    if text.startswith("$."):
        text = text[2:]
    elif text.startswith("$"):
        return None
    if not text:
        return None

    segments = []
    for raw_part in text.split("."):
        match = re.fullmatch(r"([^\[\]]+)(?:\[([^\[\]]*)\])?", raw_part)
        if match is None:
            return None
        name, index = match.groups()
        segments.append(name)
        if index is not None:
            if re.fullmatch(r"(?:0|[1-9][0-9]*)", index) is None:
                return None
            segments.append(index)
    return tuple(segments)


def _resolve_authored_target(facts: object, path: str) -> tuple[Any, bool]:
    """Resolve an authored target using its shared strict path segments."""
    segments = _authored_target_segments(path)
    if segments is None:
        return None, False

    node: Any = facts
    for part in segments:
        if isinstance(node, Mapping):
            if part not in node:
                return None, False
            node = node[part]
            continue
        if isinstance(node, (list, tuple)):
            if re.fullmatch(r"(?:0|[1-9][0-9]*)", part) is None:
                return None, False
            index = int(part)
            if index >= len(node):
                return None, False
            node = node[index]
            continue
        return None, False
    return node, True


_NUMERIC_CLAIM_SCALAR_TARGET_ROOTS = frozenset(
    {"summary", "thesis", "counter_thesis"}
)
_NUMERIC_CLAIM_SEQUENCE_TARGET_ROOTS = frozenset({"drivers", "watch_items"})
_NUMERIC_CLAIM_OBJECT_TARGET_FIELDS = {
    "relationship_reconciliations": frozenset(
        {"observation", "interpretation", "uncertainty"}
    ),
    "catalysts": frozenset(
        {"trigger", "expected_outcome", "horizon", "uncertainty", "evidence"}
    ),
    "risks": frozenset(
        {
            "sourced_observation",
            "inference",
            "uncertainty",
            "likelihood",
            "impact",
            "mitigation",
            "evidence",
        }
    ),
}


def _is_eligible_numeric_claim_target(
    segments: tuple[str, ...] | None, node: object
) -> bool:
    """Whether resolved ``node`` is one permitted authored narrative leaf."""
    if not isinstance(node, str) or not segments:
        return False
    root = segments[0]
    if root in _NUMERIC_CLAIM_SCALAR_TARGET_ROOTS:
        return len(segments) == 1
    if (
        root == "qualitative"
        and len(segments) == 3
        and segments[1] in QUALITATIVE_NAMES
        and segments[2] == "evidence"
    ):
        return True
    if (
        root == "materiality_assessment"
        and len(segments) == 3
        and segments[1] in MATERIALITY_ASSESSMENT_TOPICS
        and segments[2] in {"observation", "implication", "evidence"}
    ):
        return True
    index_pattern = r"(?:0|[1-9][0-9]*)"
    if root in _NUMERIC_CLAIM_SEQUENCE_TARGET_ROOTS:
        return (
            len(segments) == 2
            and re.fullmatch(index_pattern, segments[1]) is not None
        )
    fields = _NUMERIC_CLAIM_OBJECT_TARGET_FIELDS.get(root)
    return (
        len(segments) == 3
        and fields is not None
        and re.fullmatch(index_pattern, segments[1]) is not None
        and segments[2] in fields
    )


def _resolve_numeric_claim_target(
    facts: object, path: object
) -> tuple[Any, bool]:
    """Resolve ``path`` and apply the single shared target-domain policy."""
    segments = _authored_target_segments(path)
    if segments is None or not isinstance(path, str):
        return None, False
    node, resolved = _resolve_authored_target(facts, path)
    return node, resolved and _is_eligible_numeric_claim_target(segments, node)


def _canonical_number_key(number: Decimal) -> str:
    """Comparable string key for one canonical quantity (scale-stripped)."""
    normalized = number.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _numeric_claim_coefficient(value: object, unit: str) -> Decimal | None:
    """Normalize one displayed value to its coefficient in ``unit``."""
    number = _canonical_claim_number(value)
    if number is None:
        return None
    if not isinstance(value, str):
        return number
    explicit_scale = _NUMERIC_CLAIM_EXPLICIT_SCALE_RE.search(value)
    if explicit_scale is None:
        return number
    suffix = explicit_scale.group(0).strip().casefold()
    if suffix == "%":
        scale = Decimal("0.01")
    elif suffix in {"bp", "bps", "basis point", "basis points"}:
        scale = Decimal("0.0001")
    else:
        scale = _SCALE_BY_UNIT.get(unit)
    if scale is None or scale == 0:
        return None
    return number / scale


def _numeric_claim_coefficient_key(value: object, unit: str) -> str | None:
    coefficient = _numeric_claim_coefficient(value, unit)
    return (
        _canonical_number_key(coefficient)
        if coefficient is not None
        else None
    )


def _numeric_claim_lexical_coefficient_key(
    text: str,
    start: int,
    end: int,
    unit: str,
) -> str | None:
    """Canonical coefficient key with one coherent authored lexical sign."""
    surface = text[start:end]
    stripped = surface.lstrip()
    preceding = text[:start].rstrip()
    following = text[end:].lstrip()
    if re.match(r"[+\-−]?\s*[$€£¥]", following):
        return None
    if preceding and preceding[-1] == "−":
        return None
    if (
        stripped
        and stripped[0] in "+-$€£¥"
        and preceding
        and preceding[-1] in "+-$€£¥−"
    ):
        return None
    coefficient = _numeric_claim_coefficient(surface, unit)
    if coefficient is None:
        return None
    has_explicit_sign = (
        re.match(r"(?:[+-][$€£¥]|[$€£¥][+-]|[+-])", stripped) is not None
    )
    lexical_start = start + len(surface) - len(stripped)
    if not has_explicit_sign and re.search(
        r"\bnegative\s+[$€£¥]?\s*$",
        text[:lexical_start],
        re.IGNORECASE,
    ):
        coefficient = -coefficient
    return _canonical_number_key(coefficient)


def _material_calendar_date_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Exact, valid ISO or English month-name calendar dates."""
    spans: list[tuple[int, int]] = []
    for match in _MATERIAL_CALENDAR_DATE_RE.finditer(text):
        if match.group("iso_year") is not None:
            year = int(match.group("iso_year"))
            month = int(match.group("iso_month"))
            day = int(match.group("iso_day"))
        elif match.group("named_month") is not None:
            year = int(match.group("named_year"))
            month = _ENGLISH_MONTH_NUMBERS[match.group("named_month").casefold()]
            day = int(match.group("named_day"))
        else:
            year = int(match.group("leading_year"))
            month = _ENGLISH_MONTH_NUMBERS[match.group("leading_month").casefold()]
            day = int(match.group("leading_day"))
        try:
            date(year, month, day)
        except ValueError:
            continue
        spans.append(match.span())
    return tuple(spans)


def _has_material_quantitative_context(text: str, start: int, end: int) -> bool:
    """Whether explicit syntax makes one numeral unambiguously quantitative."""
    surface = text[start:end]
    displayed = _MATERIAL_DISPLAYED_NUMBER_RE.match(text, start)
    return (
        any(symbol in surface for symbol in "$€£¥")
        or _MATERIAL_SIGNED_CURRENCY_PREFIX_RE.search(text, 0, start) is not None
        or _MATERIAL_EXPLICIT_SCALE_RE.search(surface) is not None
        or (
            displayed is not None
            and displayed.end() > end
            and _MATERIAL_EXPLICIT_SCALE_RE.search(displayed.group(0))
            is not None
        )
        or _MATERIAL_COUNT_RENDERING_RE.match(text, end) is not None
        or _MATERIAL_COUNT_NOUN_RE.match(text, end) is not None
    )


def _is_undecorated_material_identifier(
    text: str, start: int, end: int
) -> bool:
    """Whether one numeral is lexical identifier text, not a quantity."""
    if _has_material_quantitative_context(text, start, end):
        return False
    if _MATERIAL_IDENTIFIER_PREFIX_RE.search(text, 0, start) is not None:
        return True
    left = _MATERIAL_PROPER_NAME_LEFT_RE.search(text, 0, start)
    right = _MATERIAL_PROPER_NAME_RIGHT_RE.match(text, end)
    if left is None or right is None:
        return False
    left_parts = re.split(r"[-'’.]", left.group("word"))
    right_parts = re.split(r"[-'’.]", right.group("word"))
    return all(
        part.isupper() or part.istitle() for part in left_parts if part
    ) and all(
        part.isupper() or part.istitle() for part in right_parts if part
    )


def material_numeric_tokens(text: str) -> list[tuple[int, int, str]]:
    """Material numeric occurrences and canonical coefficients in authored text.

    Complete dates, period years, word fragments, and conservative lexical
    identifiers are excluded. Explicit currency, scale, ratio, duration, or
    count syntax always remains quantitative.
    """
    calendar_date_spans = _material_calendar_date_spans(text)
    found: list[tuple[int, int, str]] = []
    for match in _MATERIAL_NUMERIC_TOKEN_RE.finditer(text):
        start, end = match.span()
        if any(
            date_start <= start and end <= date_end
            for date_start, date_end in calendar_date_spans
        ):
            continue
        if start > 0 and text[start - 1].isalnum():
            continue
        if end < len(text) and text[end].isalpha():
            displayed = _MATERIAL_DISPLAYED_NUMBER_RE.match(text, start)
            if displayed is None or displayed.end() <= end:
                continue
        raw = match.group(0)
        coefficient = _numeric_claim_lexical_coefficient_key(
            text, start, end, ""
        )
        if coefficient is None:
            continue
        if (
            _YEAR_LIKE_TOKEN_RE.fullmatch(raw.removesuffix(","))
            and not _has_material_quantitative_context(text, start, end)
        ):
            continue
        if _is_undecorated_material_identifier(text, start, end):
            continue
        found.append((start, end, coefficient))
    return found


def _numeric_target_numeral_spans(text: str) -> list[tuple[int, int]]:
    """Displayed scalars, excluding structural period and date numerals."""
    period_spans = [
        (start, end)
        for start, end, _ in _canonical_period_occurrences(text)
    ]
    claimable_period_counts = {
        match.span("count")
        for match in _TARGET_RELATIVE_PERIOD_RE.finditer(text)
        if match.group("count") is not None
        and match.group("count").isdigit()
    }
    return [
        match.span()
        for match in _NUMERIC_CLAIM_DISPLAY_RE.finditer(text)
        if match.span() in claimable_period_counts
        or not any(
            period_start <= match.start() and match.end() <= period_end
            for period_start, period_end in period_spans
        )
    ]


def _numeric_target_scalar_clusters(
    text: str,
    occurrences: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Join only adjacent numeral endpoints connected as one scalar range."""
    clusters: list[tuple[int, int]] = []
    for start, end in occurrences:
        if (
            clusters
            and _NUMERIC_FACT_RANGE_CONNECTOR_RE.fullmatch(
                text[clusters[-1][1]:start]
            )
            is not None
        ):
            clusters[-1] = (clusters[-1][0], end)
        else:
            clusters.append((start, end))
    return clusters


def _numeric_target_clause_boundaries(
    text: str,
) -> list[tuple[int, int, str]]:
    """Return bounded hard and coordinated clause edges in source order."""
    conjunctive_alias_spans: list[tuple[int, int]] = []
    for alias in _NUMERIC_TARGET_METRIC_ALIAS_LOOKUP:
        if " and " not in alias:
            continue
        pattern = (
            r"(?<![a-z0-9])"
            + r"\s+".join(re.escape(word) for word in alias.split())
            + r"(?![a-z0-9])"
        )
        conjunctive_alias_spans.extend(
            match.span()
            for match in re.finditer(pattern, text, re.IGNORECASE)
        )
    scalar_range_spans = _numeric_target_scalar_clusters(
        text, _numeric_target_numeral_spans(text)
    )
    period_spans = [
        (period_start, period_end)
        for period_start, period_end, _ in _canonical_period_occurrences(text)
    ]

    boundaries: list[tuple[int, int, str]] = []
    for match in _NUMERIC_TARGET_CLAUSE_BOUNDARY_RE.finditer(text):
        folded = match.group(0).casefold()
        protected: tuple[tuple[int, int], ...] = ()
        if folded == "and":
            protected = tuple(conjunctive_alias_spans)
        if folded in {"and", ","}:
            protected += tuple(scalar_range_spans) + tuple(period_spans)
        if any(
            span_start <= match.start() and match.end() <= span_end
            for span_start, span_end in protected
        ):
            continue
        if folded == ",":
            kind = "comma"
        elif folded in {"and", "despite"}:
            kind = "and"
        else:
            kind = "hard"
        if (
            kind == "and"
            and boundaries
            and boundaries[-1][2] == "comma"
            and not text[boundaries[-1][1]:match.start()].strip()
        ):
            comma_start, _, _ = boundaries[-1]
            boundaries[-1] = (comma_start, match.end(), "and")
        else:
            boundaries.append((match.start(), match.end(), kind))
    return boundaries


def _numeric_target_local_span(
    text: str,
    start: int,
    end: int,
) -> tuple[int, int]:
    """Bound one numeral without splitting aliases, periods, or ranges."""
    boundaries = _numeric_target_clause_boundaries(text)
    left = max(
        (
            boundary_end
            for _, boundary_end, _ in boundaries
            if boundary_end <= start
        ),
        default=0,
    )
    right = min(
        (
            boundary_start
            for boundary_start, _, _ in boundaries
            if boundary_start >= end
        ),
        default=len(text),
    )
    return left, right


def _numeric_target_local_clause(text: str, start: int, end: int) -> str:
    """Clause around one numeral, preserving aliases and scalar ranges."""
    left, right = _numeric_target_local_span(text, start, end)
    return text[left:right]


def _metric_alias_pattern(words: list[str]) -> str:
    """Raw-text pattern for one known alias, retaining source offsets."""
    parts = [
        (
            r"contribut(?:e|es|ed|ing|ion)"
            if word == "contribution"
            else re.escape(word)
        )
        for word in words
    ]
    return (
        r"(?<![a-z0-9])"
        + r"\s+".join(parts)
        + r"(?![a-z0-9])"
    )


def _metric_alias_occurrences(
    text: str,
    *,
    suppress_scalarless_ai_recipient: bool = False,
) -> list[tuple[int, int, str]]:
    """Return specificity-filtered contiguous and bounded split aliases."""
    contiguous: list[tuple[int, int, str]] = []
    for alias, group in _NUMERIC_TARGET_METRIC_ALIAS_LOOKUP.items():
        pattern = _metric_alias_pattern(alias.split())
        contiguous.extend(
            (match.start(), match.end(), group)
            for match in re.finditer(pattern, text, re.IGNORECASE)
        )

    occurrences = list(contiguous)
    for alias, group in _NUMERIC_TARGET_METRIC_ALIAS_LOOKUP.items():
        words = alias.split()
        for split_at in range(1, len(words)):
            left_pattern = _metric_alias_pattern(words[:split_at])
            right_pattern = _metric_alias_pattern(words[split_at:])
            for left_match in re.finditer(left_pattern, text, re.IGNORECASE):
                search_end = min(len(text), left_match.end() + 65)
                for right_match in re.finditer(
                    right_pattern,
                    text[left_match.end():search_end],
                    re.IGNORECASE,
                ):
                    right_start = left_match.end() + right_match.start()
                    right_end = left_match.end() + right_match.end()
                    bridge = text[left_match.end():right_start]
                    numeral_spans = [
                        span
                        for span in _numeric_target_numeral_spans(text)
                        if left_match.end() <= span[0]
                        and span[1] <= right_start
                    ]
                    clusters = _numeric_target_scalar_clusters(
                        text, numeral_spans
                    )
                    if len(clusters) != 1:
                        continue
                    cluster_start, cluster_end = clusters[0]
                    nonnumeric_bridge = (
                        text[left_match.end():cluster_start]
                        + text[cluster_end:right_start]
                    )
                    if (
                        len(bridge) > 64
                        or len(re.findall(r"[a-z]+", nonnumeric_bridge.casefold()))
                        > 6
                        or re.search(
                            r"[,.!?;:\n]|\b(?:and|despite|but|while|whereas)\b",
                            nonnumeric_bridge,
                            re.IGNORECASE,
                        )
                        is not None
                        or any(
                            left_match.end() <= other_start
                            and other_end <= right_start
                            for other_start, other_end, _ in contiguous
                        )
                    ):
                        continue
                    occurrences.append(
                        (left_match.start(), right_end, group)
                    )

    # Contribution wording may place the target's period between the point
    # unit and "from AI services". The interposition is deliberately limited
    # to one recognized primary period; arbitrary qualifiers remain barriers.
    for points_match in re.finditer(
        r"(?<![a-z0-9])(?:percentage\s+)?points?(?![a-z0-9])",
        text,
        re.IGNORECASE,
    ):
        search_end = min(len(text), points_match.end() + 65)
        for source_match in re.finditer(
            r"(?<![a-z0-9])from\s+ai\s+services(?![a-z0-9])",
            text[points_match.end():search_end],
            re.IGNORECASE,
        ):
            source_start = points_match.end() + source_match.start()
            source_end = points_match.end() + source_match.end()
            if _target_period_only_interposition(
                text[points_match.end():source_start]
            ):
                occurrences.append(
                    (
                        points_match.start(),
                        source_end,
                        "azure_ai_growth_contribution",
                    )
                )

    unique = set(occurrences)
    # The recipient recognizer is shared with row-field grouping so raw target
    # occurrences and canonical row aliases apply the same bounded exception.
    suppressed_azure_spans = _ai_contribution_recipient_azure_spans(
        text,
        allow_scalarless_alias=suppress_scalarless_ai_recipient,
    )
    unique = {
        occurrence
        for occurrence in unique
        if not (
            occurrence[2] == "azure_growth"
            and occurrence[:2] in suppressed_azure_spans
        )
    }
    return sorted(
        (
            (start, end, group)
            for start, end, group in unique
            if not any(
                other_start <= start
                and other_end >= end
                and (other_start, other_end) != (start, end)
                for other_start, other_end, _ in unique
            )
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )


def _numeric_target_effect_recipient_pairs(
    text: str,
    clusters: list[tuple[int, int]],
    base_owned: list[list[tuple[int, int, str]]],
) -> frozenset[tuple[int, int]]:
    """Share one recipient alias with its immediately preceding effect scalar."""
    numeral_spans = _numeric_target_numeral_spans(text)
    aliases = _metric_alias_occurrences(text)
    pairs: set[tuple[int, int]] = set()
    for effect_index in range(len(clusters) - 1):
        recipient_index = effect_index + 1
        effect_start, effect_end = clusters[effect_index]
        recipient_start, recipient_end = clusters[recipient_index]
        if (
            sum(
                effect_start <= start and end <= effect_end
                for start, end in numeral_spans
            )
            != 1
            or sum(
                recipient_start <= start and end <= recipient_end
                for start, end in numeral_spans
            )
            != 1
        ):
            continue
        recipient_owned = base_owned[recipient_index]
        recipient_groups = {
            group for _, _, group in recipient_owned
        }
        if len(recipient_owned) != 1 or len(recipient_groups) != 1:
            continue
        occurrence = recipient_owned[0]
        alias_start, alias_end, recipient_group = occurrence
        if (
            alias_end > recipient_start
            or re.fullmatch(
                r"[ \t]+of[ \t]+",
                text[alias_end:recipient_start],
                re.IGNORECASE,
            )
            is None
        ):
            continue
        bridge = text[effect_end:recipient_start]
        pre_alias = text[effect_end:alias_start]
        if len(bridge) > 64:
            continue
        effect_groups = {
            group for _, _, group in base_owned[effect_index]
        }
        if effect_groups and effect_groups != {recipient_group}:
            continue
        if re.fullmatch(
            r"[ \t]*(?:(?:-[ \t]*)?per(?:-|[ \t])+share)?[ \t]+"
            r"(?:impact|effect)[ \t]+on[ \t]+"
            r"(?:[a-z]+[ \t]+){0,3}",
            pre_alias,
            re.IGNORECASE,
        ) is None:
            continue
        if any(
            other != occurrence
            and effect_end <= other[0]
            and other[1] <= recipient_start
            for other in aliases
        ):
            continue
        if any(
            effect_end <= period_start
            and period_end <= recipient_start
            and label not in _TARGET_COMPARISON_LABELS
            for period_start, period_end, label in _canonical_period_occurrences(
                text
            )
        ):
            continue
        base_owned[effect_index].append(occurrence)
        pairs.add((effect_index, recipient_index))
    return frozenset(pairs)


def _numeric_target_metric_assignments(
    text: str,
) -> tuple[
    list[tuple[int, int]],
    list[frozenset[str]],
    list[list[tuple[int, int, str]]],
    frozenset[tuple[int, int]],
]:
    """Assign aliases nearest-first, then share one bounded effect pair."""
    numeral_spans = _numeric_target_numeral_spans(text)
    clusters = _numeric_target_scalar_clusters(text, numeral_spans)
    owned: list[list[tuple[int, int, str]]] = [
        [] for _ in clusters
    ]
    for occurrence in _metric_alias_occurrences(text):
        alias_start, alias_end, _ = occurrence
        distances: list[int | None] = []
        for cluster_start, cluster_end in clusters:
            if alias_end < cluster_start:
                bridge = text[alias_end:cluster_start]
                distance = cluster_start - alias_end
            elif cluster_end < alias_start:
                bridge = text[cluster_end:alias_start]
                distance = alias_start - cluster_end
            else:
                bridge = ""
                distance = 0
            if re.search(
                r"[,.!?;:\n]|\b(?:and|despite|but|while|whereas)\b",
                bridge,
                re.IGNORECASE,
            ):
                distances.append(None)
            else:
                distances.append(distance)
        reachable = [
            distance for distance in distances if distance is not None
        ]
        if not reachable:
            continue
        nearest = min(reachable)
        nearest_indexes = [
            index for index, distance in enumerate(distances)
            if distance == nearest
        ]
        if len(nearest_indexes) == 1:
            owned[nearest_indexes[0]].append(occurrence)
    effect_pairs = _numeric_target_effect_recipient_pairs(
        text, clusters, owned
    )
    return (
        clusters,
        [frozenset(group for _, _, group in aliases) for aliases in owned],
        owned,
        effect_pairs,
    )


def _numeric_target_period_assignments(
    text: str,
) -> list[frozenset[str]]:
    """Assign primary period bundles across coordinated claim cells."""
    clusters, _, owned, effect_pairs = _numeric_target_metric_assignments(text)
    assignments: list[frozenset[str]] = [
        frozenset() for _ in clusters
    ]
    aliases = _metric_alias_occurrences(text)
    boundaries = _numeric_target_clause_boundaries(text)
    cells: list[tuple[int, int, str | None]] = []
    cursor = 0
    incoming: str | None = None
    for boundary_start, boundary_end, kind in boundaries:
        cells.append((cursor, boundary_start, incoming))
        cursor = boundary_end
        incoming = kind
    cells.append((cursor, len(text), incoming))

    owner: frozenset[str] | None = None
    owner_from_claim = False
    for cell_start, cell_end, incoming_kind in cells:
        if incoming_kind == "hard":
            owner = None
            owner_from_claim = False
        if not text[cell_start:cell_end].strip():
            continue
        cell_indexes = [
            index
            for index, (cluster_start, cluster_end) in enumerate(clusters)
            if cell_start <= cluster_start and cluster_end <= cell_end
        ]
        cell_aliases = [
            occurrence
            for occurrence in aliases
            if cell_start <= occurrence[0] and occurrence[1] <= cell_end
        ]
        period_labels = {
            label
            for period_start, period_end, label
            in _canonical_period_occurrences(text)
            if cell_start <= period_start and period_end <= cell_end
        }
        primary = _primary_period_labels(period_labels)
        comparisons = period_labels & _TARGET_COMPARISON_LABELS

        if not cell_indexes and not cell_aliases and primary:
            candidate = frozenset(primary)
            if (
                owner is not None
                and not owner_from_claim
                and incoming_kind in {"comma", "and"}
            ):
                owner = frozenset(owner | candidate)
            else:
                owner = candidate
            owner_from_claim = False
            continue

        effective_groups = {
            index: _numeric_target_metric_groups(
                text, clusters[index][0], clusters[index][1]
            )
            for index in cell_indexes
        }
        is_single_claim = (
            len(cell_indexes) == 1
            and (
                bool(effective_groups[cell_indexes[0]])
                or bool(primary)
            )
        )
        is_effect_claim = (
            len(cell_indexes) == 2
            and (cell_indexes[0], cell_indexes[1]) in effect_pairs
        )
        is_explicit_multi_claim = (
            len(cell_indexes) > 1
            and not is_effect_claim
            and bool(primary)
            and all(effective_groups.values())
        )
        owned_aliases = {
            occurrence
            for index in cell_indexes
            for occurrence in owned[index]
        }
        if (
            not (
                is_single_claim
                or is_effect_claim
                or is_explicit_multi_claim
            )
            or any(alias not in owned_aliases for alias in cell_aliases)
        ):
            owner = None
            owner_from_claim = False
            continue
        if primary:
            claim_primary = set(primary)
            if (
                owner is not None
                and not owner_from_claim
                and incoming_kind in {"comma", "and"}
            ):
                claim_primary.update(owner)
            assigned = frozenset(claim_primary | comparisons)
            for index in cell_indexes:
                assignments[index] = assigned
            classified_owner = all(effective_groups.values())
            owner = frozenset(claim_primary) if classified_owner else None
            owner_from_claim = classified_owner
            continue
        if owner is None or incoming_kind not in {"comma", "and"}:
            for index in cell_indexes:
                assignments[index] = frozenset(comparisons)
            owner = None
            owner_from_claim = False
            continue
        assigned = owner | comparisons
        for index in cell_indexes:
            assignments[index] = frozenset(assigned)
        owner_from_claim = True
    return assignments


def _numeric_target_owned_period_labels(
    text: str,
    start: int,
    end: int,
) -> frozenset[str]:
    """Return the period bundle owned by one scalar cluster occurrence."""
    clusters = _numeric_target_scalar_clusters(
        text, _numeric_target_numeral_spans(text)
    )
    cluster_index = next(
        (
            index
            for index, (cluster_start, cluster_end) in enumerate(clusters)
            if cluster_start <= start and end <= cluster_end
        ),
        None,
    )
    if cluster_index is None:
        return frozenset()
    return _numeric_target_period_assignments(text)[cluster_index]


def _numeric_target_metric_groups(
    text: str, start: int, end: int
) -> frozenset[str]:
    """Resolve exact metric groups owned by, or inherited once by, a cluster."""
    clusters, groups, _, _ = _numeric_target_metric_assignments(text)
    cluster_index = next(
        (
            index
            for index, (cluster_start, cluster_end) in enumerate(clusters)
            if cluster_start <= start and end <= cluster_end
        ),
        None,
    )
    if cluster_index is None:
        return frozenset()
    if groups[cluster_index]:
        return groups[cluster_index]
    if cluster_index == 0 or not groups[cluster_index - 1]:
        return frozenset()
    bridge = text[clusters[cluster_index - 1][1]:clusters[cluster_index][0]]
    current_end = clusters[cluster_index][1]
    is_forward_horizon = (
        _NUMERIC_TARGET_FORWARD_HORIZON_BRIDGE_RE.fullmatch(bridge)
        is not None
        and _NUMERIC_TARGET_DURATION_UNIT_RE.match(text, current_end)
        is not None
    )
    if (
        re.fullmatch(
            r"\s*(?:and\s+)?(?:grew|rose|fell|increased|decreased)"
            r"(?:\s+(?:by|to))?\s*",
            bridge,
            re.IGNORECASE,
        )
        is None
        and _NUMERIC_TARGET_COMMA_DIRECTION_BRIDGE_RE.fullmatch(bridge)
        is None
        and not is_forward_horizon
    ):
        return frozenset()
    return groups[cluster_index - 1]


def _numeric_target_owned_metric_aliases(
    text: str, start: int, end: int
) -> list[tuple[int, int, str]]:
    """Return aliases owned or validly inherited by one scalar cluster."""
    clusters, groups, owned, _ = _numeric_target_metric_assignments(text)
    cluster_index = next(
        (
            index
            for index, (cluster_start, cluster_end) in enumerate(clusters)
            if cluster_start <= start and end <= cluster_end
        ),
        None,
    )
    if cluster_index is None:
        return []
    if owned[cluster_index]:
        return owned[cluster_index]
    if (
        cluster_index > 0
        and groups[cluster_index - 1]
        and _numeric_target_metric_groups(text, start, end)
    ):
        return owned[cluster_index - 1]
    return []


def _numeric_target_metric_clause(text: str, start: int, end: int) -> str:
    """Minimal owned-alias span, with a midpoint cell for unknown metrics."""
    clusters, _, owned, _ = _numeric_target_metric_assignments(text)
    cluster_index = next(
        (
            index
            for index, (cluster_start, cluster_end) in enumerate(clusters)
            if cluster_start <= start and end <= cluster_end
        ),
        None,
    )
    if cluster_index is None:
        left, right = _numeric_target_local_span(text, start, end)
        return text[left:right]
    cluster_start, cluster_end = clusters[cluster_index]
    if owned[cluster_index]:
        left = min(
            cluster_start,
            *(alias_start for alias_start, _, _ in owned[cluster_index]),
        )
        right = max(
            cluster_end,
            *(alias_end for _, alias_end, _ in owned[cluster_index]),
        )
        return text[left:right]
    left, right = _numeric_target_local_span(text, start, end)
    local_clusters = [
        cluster for cluster in clusters
        if left <= cluster[0] and cluster[1] <= right
    ]
    local_index = local_clusters.index((cluster_start, cluster_end))
    if local_index:
        left = (local_clusters[local_index - 1][1] + cluster_start) // 2
    if local_index + 1 < len(local_clusters):
        right = (cluster_end + local_clusters[local_index + 1][0] + 1) // 2
    return text[left:right]


def _numeric_target_metric_context_compatible(
    metric: str,
    text: str,
    start: int,
    end: int,
) -> bool:
    """Bind one source metric to exact aliases owned by one scalar occurrence."""
    wanted = _metric_alias_groups(metric)
    if not wanted:
        wanted = _numeric_target_metric_alias_groups(metric)
    resolved = _numeric_target_metric_groups(text, start, end)
    local_left, local_right = _numeric_target_local_span(
        text, start, end
    )
    has_unowned_local_alias = (
        not resolved
        and any(
            alias_start < local_right and alias_end > local_left
            for alias_start, alias_end, _ in _metric_alias_occurrences(text)
        )
    )
    if wanted:
        if resolved:
            return resolved == wanted or (
                wanted == {"capex"}
                and resolved == {"lease_inclusive_capex"}
            )
    elif resolved or has_unowned_local_alias:
        return False
    elif _metric_alias_matches(
        metric, _numeric_target_metric_clause(text, start, end)
    ):
        return True
    return False


def _numeric_target_cash_basis_compatible(
    cash_basis: str | None,
    text: str,
    start: int,
    end: int,
) -> bool:
    """Bind scalar-owned explicit capex wording to a normalized source basis."""
    if cash_basis is None:
        return True
    explicit_bases: set[str] = set()
    for alias_start, alias_end, group in _numeric_target_owned_metric_aliases(
        text, start, end
    ):
        if group == "lease_inclusive_capex":
            explicit_bases.add("cash_plus_finance_leases")
        elif group == "capex" and re.search(
            r"(?<![a-z0-9])cash(?![a-z0-9])",
            text[alias_start:alias_end],
            re.IGNORECASE,
        ):
            explicit_bases.add("cash")
    return not explicit_bases or explicit_bases == {cash_basis}


def _numeric_target_currency_compatible(
    text: str, currency: object, unit: str
) -> bool:
    """Require explicit target currency to equal the row currency."""
    declared = str(currency or "").strip().upper() or None
    if unit in _CURRENCY_UNITS and declared != "USD":
        return False
    explicit: set[str] = set()
    for symbol, code in {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
    }.items():
        if symbol in text:
            explicit.add(code)
    explicit.update(
        match.group(1).upper()
        for match in re.finditer(
            r"\b(USD|EUR|GBP|JPY|CNY|RMB)\b", text, re.IGNORECASE
        )
    )
    return not explicit or (declared is not None and explicit == {declared})


def _numeric_target_unit_compatible(display: str, unit: str) -> bool:
    """Check explicit rendering semantics without inventing a missing scale."""
    folded = display.casefold()
    has_percent = "%" in folded
    has_percentage_points = bool(
        re.search(
            r"(?<![a-z0-9])(?:percentage(?:[ \t]+|-)+points?|points?)"
            r"(?![a-z0-9])",
            folded,
        )
    )
    has_basis_points = bool(
        re.search(
            r"(?<![a-z0-9])(?:bp|bps|basis[ \t]+points?)(?![a-z0-9])",
            folded,
        )
    )
    has_ratio_suffix = bool(re.search(r"(?<=\d)\s*x\b", folded))
    magnitude = re.search(
        r"\b(trillions?|billions?|bns?|bn|millions?|mns?|thousands?)\b|"
        r"(?<=\d)\s*[tbmk]\b",
        folded,
    )
    if unit in _CURRENCY_UNITS:
        if has_percent or has_percentage_points or has_basis_points:
            return False
        if magnitude is None:
            return True
        rendered = magnitude.group(0).strip()
        if unit == "usd_billions":
            return bool(re.fullmatch(r"(?:billions?|bns?|bn|b)", rendered))
        if unit == "usd_millions":
            return bool(re.fullmatch(r"(?:millions?|mns?|m)", rendered))
        return False
    if has_ratio_suffix and unit != "ratio":
        return False
    if any(symbol in display for symbol in "$€£¥") or magnitude is not None:
        return False
    if unit == "percent":
        return has_percent
    if unit == "percentage_points":
        return has_percentage_points and not has_basis_points
    if unit == "ratio":
        return not (has_percent or has_percentage_points or has_basis_points)
    return not (
        unit not in {"percent", "percentage_points"}
        and (has_percent or has_percentage_points or has_basis_points)
    )


def _numeric_target_period_context_compatible(
    period: str,
    text: str,
    start: int,
    end: int,
) -> bool:
    """Match only the unambiguous period bundle owned by this occurrence."""
    wanted_labels = _canonical_period_labels(period)
    rendered_labels = set(
        _numeric_target_owned_period_labels(text, start, end)
    )
    return _period_bundles_compatible(wanted_labels, rendered_labels)


def _numeric_claim_target_occurrence_compatible(
    row: Mapping[str, Any],
    text: str,
    start: int,
    end: int,
    *,
    source_cash_basis: str | None = None,
) -> bool:
    """Match one rendered numeral to its occurrence-local target tuple."""
    numeric_start, numeric_end = start, end
    for match in _NUMERIC_CLAIM_DISPLAY_RE.finditer(text):
        if match.start() <= start and match.end() >= end:
            numeric_start, numeric_end = match.span()
            break
    cluster_start, cluster_end = next(
        (
            cluster
            for cluster in _numeric_target_scalar_clusters(
                text, _numeric_target_numeral_spans(text)
            )
            if cluster[0] <= numeric_start and numeric_end <= cluster[1]
        ),
        (numeric_start, numeric_end),
    )
    unit_display_end = cluster_end
    display = text[cluster_start:unit_display_end]
    points_suffix = _NUMERIC_TARGET_POINTS_SUFFIX_RE.match(
        text, unit_display_end
    )
    if points_suffix is not None:
        unit_display_end = points_suffix.end()
        display = text[cluster_start:unit_display_end]

    metric = str(row.get("metric") or "")
    if not _numeric_target_metric_context_compatible(
        metric, text, numeric_start, numeric_end
    ):
        return False
    if not _numeric_target_cash_basis_compatible(
        source_cash_basis,
        text,
        numeric_start,
        numeric_end,
    ):
        return False

    period = str(row.get("period") or "")
    if not _numeric_target_period_context_compatible(
        period, text, numeric_start, numeric_end
    ):
        return False

    unit = str(row.get("unit") or "")
    currency_surface = text[
        max(0, numeric_start - _NUMERIC_TARGET_CURRENCY_CONTEXT_CHARS):
        min(
            len(text),
            unit_display_end + _NUMERIC_TARGET_CURRENCY_CONTEXT_CHARS,
        )
    ]
    return _numeric_target_unit_compatible(
        display, unit
    ) and _numeric_target_currency_compatible(
        currency_surface, row.get("currency"), unit
    )


class NumericClaimCoverageFinding(NamedTuple):
    """One authored material token lacking a verified ledger binding."""

    path: str
    coefficient: str
    snippet: str


def _iter_authored_strings(node: object, path: str = "$"):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, child in node.items():
            yield from _iter_authored_strings(child, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, child in enumerate(node):
            yield from _iter_authored_strings(child, f"{path}[{index}]")


def _is_evidence_quote_target(path: str) -> bool:
    """Whether an eligible target is producer-authored evidence text."""
    return bool(
        re.fullmatch(r"\$\.qualitative\.[a-z_]+\.evidence", path)
        or re.fullmatch(r"\$\.(?:catalysts|risks)\[\d+\]\.evidence", path)
    )


def numeric_claim_coverage_findings(
    facts: Mapping[str, Any],
    rows: object,
    *,
    valid_row_indexes: object,
    invalid_row_indexes: object,
    specific_finding_keys: object = (),
) -> list[NumericClaimCoverageFinding]:
    """Find uncovered material numbers in eligible authored narrative leaves.

    Callers own source verification and pass the resulting row-index sets.
    Invalid rows suppress a second unbound finding for the same normalized
    target path and coefficient; their specific row error remains authoritative.
    """
    ledger = rows if isinstance(rows, list) else []
    valid_indexes = {
        index
        for index in (
            valid_row_indexes
            if isinstance(valid_row_indexes, (set, frozenset, list, tuple))
            else ()
        )
        if isinstance(index, int) and not isinstance(index, bool)
    }
    invalid_indexes = {
        index
        for index in (
            invalid_row_indexes
            if isinstance(invalid_row_indexes, (set, frozenset, list, tuple))
            else ()
        )
        if isinstance(index, int) and not isinstance(index, bool)
    }
    specific_keys = {
        (str(candidate[0]), str(candidate[1]))
        for candidate in (
            specific_finding_keys
            if isinstance(specific_finding_keys, (set, frozenset, list, tuple))
            else ()
        )
        if isinstance(candidate, (list, tuple)) and len(candidate) == 2
    }
    valid_bindings: dict[str, list[Mapping[str, Any]]] = {}
    invalid_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(ledger):
        if not isinstance(row, Mapping):
            continue
        path = _normalize_claim_path(row.get("path") or "")
        coefficient = _numeric_claim_coefficient_key(
            row.get("value"), str(row.get("unit") or "")
        )
        if not path or coefficient is None:
            continue
        if index in valid_indexes:
            valid_bindings.setdefault(path, []).append(row)
        elif index in invalid_indexes:
            invalid_keys.add((path, coefficient))

    findings: list[NumericClaimCoverageFinding] = []
    for path, text in _iter_authored_strings(facts):
        _, eligible = _resolve_numeric_claim_target(facts, path)
        if not eligible or _is_evidence_quote_target(path):
            continue
        normalized_path = _normalize_claim_path(path)
        bindings = valid_bindings.get(normalized_path, ())
        tokens = material_numeric_tokens(text)
        for start, end, coefficient in tokens:
            if any(
                _numeric_claim_coefficient_key(
                    binding.get("value"), str(binding.get("unit") or "")
                )
                == coefficient
                and _numeric_claim_target_occurrence_compatible(
                    binding, text, start, end
                )
                for binding in bindings
            ):
                continue
            if (normalized_path, coefficient) in invalid_keys:
                continue
            if (normalized_path, coefficient) in specific_keys:
                continue
            snippet_start = max(0, start - 60)
            snippet_end = min(len(text), end + 60)
            snippet = re.sub(
                r"\s+", " ", text[snippet_start:snippet_end]
            ).strip()
            findings.append(
                NumericClaimCoverageFinding(path, coefficient, snippet)
            )
    return findings


def _numeric_claim_target_problem(
    row: Mapping[str, Any],
    fact_roots: Mapping[str, Any],
    *,
    source_cash_basis: str | None = None,
) -> str | None:
    """Validate the exact authored occurrence and its local tuple context."""
    target_path = str(row.get("path") or "")
    target, eligible = _resolve_numeric_claim_target(fact_roots, target_path)
    if not eligible:
        return (
            f"target path {target_path!r} does not resolve to an eligible "
            "narrative text leaf"
        )
    unit = str(row.get("unit") or "")
    claimed_key = _numeric_claim_coefficient_key(row.get("value"), unit)
    if claimed_key is None:
        return "claimed coefficient does not normalize to a finite quantity"
    matching_occurrence = False
    for match in _NUMERIC_CLAIM_DISPLAY_RE.finditer(target):
        if (
            _numeric_claim_lexical_coefficient_key(
                target,
                match.start(),
                match.end(),
                unit,
            )
            != claimed_key
        ):
            continue
        matching_occurrence = True
        if not _numeric_claim_target_occurrence_compatible(
            row,
            target,
            match.start(),
            match.end(),
            source_cash_basis=source_cash_basis,
        ):
            continue
        return None
    if not matching_occurrence:
        return "claimed coefficient is not rendered at the authored target"
    return (
        f"metric {row.get('metric')!r}, period {row.get('period')!r}, "
        f"unit {unit!r}, and currency {row.get('currency')!r} do not match "
        "the authored target around the claimed numeral"
    )


def _numeric_fact_leaf_unit(name: str) -> str | None:
    """Dimension carried by a bounded family of scalar fact leaf names."""
    folded = name.casefold()
    if re.search(
        r"(?:percentage|growth|margin|impact|drag)_points?(?:_|$)",
        folded,
    ):
        return "percentage_points"
    if re.search(
        r"(?:^|_)(?:percent|percentage|change_pct)(?:_|$)",
        folded,
    ):
        return "percent"
    if re.search(r"(?:^|_)(?:ratio|multiple)(?:_|$)", folded):
        return "ratio"
    if re.search(r"(?:^|_)count(?:_|$)", folded):
        return "count"
    return None


def _numeric_claim_fact_path_semantics(
    path: str,
    deterministic_current: object,
    deterministic_prior: object,
) -> tuple[Any, str, object, str, str, bool]:
    """Resolve one typed deterministic leaf and its exact path identity."""
    root_name, resolved_value, resolved = _resolve_claim_fact_path(
        path,
        deterministic_current,
        deterministic_prior,
    )
    raw_parts = [part for part in path.strip().split(".") if part]
    if not resolved or len(raw_parts) < 2:
        return None, "", None, "", "", False
    root = (
        deterministic_current
        if root_name == "deterministic_current"
        else deterministic_prior
    )
    node: Any = root
    parents: list[Mapping[str, Any]] = []
    names: list[str] = []
    for raw_part in raw_parts[1:]:
        match = re.fullmatch(r"([^\[\]]+)(?:\[(\d+)\])?", raw_part)
        if match is None or not isinstance(node, Mapping):
            return None, "", None, "", "", False
        name, index_text = match.groups()
        if name not in node:
            return None, "", None, "", "", False
        parents.append(node)
        names.append(name)
        node = node[name]
        if index_text is not None:
            if not isinstance(node, (list, tuple)):
                return None, "", None, "", "", False
            index = int(index_text)
            if index >= len(node):
                return None, "", None, "", "", False
            node = node[index]

    leaf_name = names[-1]
    typed: Mapping[str, Any] | None = (
        resolved_value
        if isinstance(resolved_value, Mapping) and "value" in resolved_value
        else None
    )
    relationship_fact_path = (
        root_name == "deterministic_current"
        and bool(names)
        and names[0] == "relationship_facts"
    )
    if relationship_fact_path and (len(names) != 2 or typed is None):
        return None, "", None, "", "", False
    scalar = typed.get("value") if typed is not None else resolved_value
    parent = parents[-1] if parents else None
    bounded_unit = _numeric_fact_leaf_unit(leaf_name)
    if typed is not None:
        unit = str(typed.get("unit") or bounded_unit or "")
        if not unit:
            return scalar, "", None, "", "", False
        currency = typed.get("currency")
        period = str(typed.get("period") or "")
    elif leaf_name == "value" and isinstance(parent, Mapping):
        unit = str(parent.get("unit") or "")
        if not unit:
            return scalar, "", None, "", "", False
        currency = parent.get("currency")
        period = str(parent.get("period") or "")
    elif bounded_unit is not None:
        unit = bounded_unit
        currency = None
        period = ""
    else:
        return scalar, "", None, "", "", False
    if currency is None and unit.startswith("usd_"):
        currency = "USD"
    if not period:
        for ancestor in reversed(parents):
            if ancestor.get("period"):
                period = str(ancestor["period"])
                break
    if relationship_fact_path and typed is not None:
        metric_name = str(typed.get("metric_label") or "")
        if not metric_name:
            return scalar, unit, currency, period, "", False
    else:
        metric_names = names[:-1] if leaf_name == "value" else names
        metric_name = " ".join(
            part.replace("_", " ") for part in metric_names
        )
    return scalar, unit, currency, period, metric_name, True


def _numeric_fact_range_endpoint_keys(
    value: object, unit: str
) -> frozenset[str]:
    """Exact coefficient keys when ``value`` is solely a scalar range."""
    if not isinstance(value, str):
        return frozenset()
    occurrences = list(_NUMERIC_CLAIM_DISPLAY_RE.finditer(value))
    if len(occurrences) < 2:
        return frozenset()
    if (
        value[:occurrences[0].start()].strip()
        or value[occurrences[-1].end():].strip()
    ):
        return frozenset()
    if any(
        _NUMERIC_FACT_RANGE_CONNECTOR_RE.fullmatch(
            value[left.end():right.start()]
        )
        is None
        for left, right in zip(occurrences, occurrences[1:])
    ):
        return frozenset()
    keys = {
        key
        for occurrence in occurrences
        if (
            key := _numeric_claim_coefficient_key(
                occurrence.group(0),
                unit,
            )
        )
        is not None
    }
    return frozenset(keys) if len(keys) == len(occurrences) else frozenset()

def _numeric_fact_metric_matches(
    path: str, row_metric: str, fact_metric: str
) -> bool:
    """Match normalized relationship labels exactly; preserve other aliases."""
    parts = [part for part in path.strip().split(".") if part]
    if (
        len(parts) == 3
        and parts[:2] == ["deterministic_current", "relationship_facts"]
    ):
        return re.sub(r"\s+", " ", row_metric).strip().casefold() == re.sub(
            r"\s+", " ", fact_metric
        ).strip().casefold()
    return _metric_alias_matches(row_metric, fact_metric)


def _numeric_relationship_fact_cash_basis(
    path: str,
    deterministic_current: object,
    deterministic_prior: object,
) -> str | None:
    """Read basis only from an exact normalized relationship-fact source."""
    parts = [part for part in path.strip().split(".") if part]
    if (
        len(parts) != 3
        or parts[:2] != ["deterministic_current", "relationship_facts"]
    ):
        return None
    _, resolved_value, resolved = _resolve_claim_fact_path(
        path,
        deterministic_current,
        deterministic_prior,
    )
    if not resolved or not isinstance(resolved_value, Mapping):
        return None
    return str(resolved_value.get("cash_basis") or "")



class _NumericFactClaimTupleProblem(NamedTuple):
    """One shared tuple rejection consumed by live and replay gates."""

    kind: str
    detail: str


def _numeric_fact_claim_tuple_problem(
    row: Mapping[str, Any],
    fact_roots: Mapping[str, Any],
    deterministic_current: object,
    deterministic_prior: object,
) -> _NumericFactClaimTupleProblem | None:
    """Validate one fact row's exact source and authored-target tuple."""
    fact_path = str(row.get("fact_path") or "")
    (
        fact_scalar,
        fact_unit,
        fact_currency,
        fact_period,
        fact_metric,
        resolved,
    ) = _numeric_claim_fact_path_semantics(
        fact_path,
        deterministic_current,
        deterministic_prior,
    )
    if not resolved:
        return _NumericFactClaimTupleProblem(
            "unresolved",
            f"fact_path {fact_path!r} does not resolve in deterministic "
            "current/prior metrics",
        )
    target_problem = _numeric_claim_target_problem(
        row,
        fact_roots,
        source_cash_basis=_numeric_relationship_fact_cash_basis(
            fact_path,
            deterministic_current,
            deterministic_prior,
        ),
    )
    if target_problem is not None:
        return _NumericFactClaimTupleProblem("mismatch", target_problem)
    row_metric = str(row.get("metric") or "")
    if not _numeric_fact_metric_matches(fact_path, row_metric, fact_metric):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"metric {row_metric!r} does not match deterministic fact "
            f"identity {fact_metric!r}",
        )
    row_unit = str(row.get("unit") or "")
    units_match = row_unit == fact_unit or (
        row_unit == "percent" and fact_unit.startswith("percent_")
    )
    if not units_match:
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"unit {row_unit!r} does not match deterministic fact unit "
            f"{fact_unit!r}",
        )
    if (row.get("currency") or None) != (fact_currency or None):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"currency {row.get('currency')!r} does not match deterministic "
            f"fact currency {fact_currency!r}",
        )
    row_period = str(row.get("period") or "")
    if not _period_alias_matches(row_period, fact_period):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"period {row_period!r} does not match deterministic fact period "
            f"{fact_period!r}",
        )
    claimed_key = _numeric_claim_coefficient_key(row.get("value"), row_unit)
    endpoint_keys = _numeric_fact_range_endpoint_keys(fact_scalar, row_unit)
    fact_key = _numeric_claim_coefficient_key(fact_scalar, row_unit)
    if endpoint_keys:
        if claimed_key not in endpoint_keys:
            return _NumericFactClaimTupleProblem(
                "mismatch",
                "claimed coefficient is not an exact deterministic fact "
                "range endpoint",
            )
    elif fact_key is None:
        return _NumericFactClaimTupleProblem(
            "unresolved",
            "resolved fact carries no finite numeric value or range",
        )
    elif claimed_key != fact_key:
        return _NumericFactClaimTupleProblem(
            "mismatch",
            "claimed coefficient does not exactly equal the deterministic "
            "fact coefficient",
        )
    return None


def _dimensionally_computed_claim_value(
    operation: str,
    values: list[Decimal],
    units: list[str],
    currencies: list[object],
    output_unit: str,
    output_currency: object,
) -> tuple[Decimal | None, str | None]:
    """Recompute one declared operation without changing its dimensions."""
    signatures = [
        (unit, str(currency or "").upper() or None)
        for unit, currency in zip(units, currencies)
    ]
    output_signature = (
        output_unit,
        str(output_currency or "").upper() or None,
    )
    if operation in {"sum", "difference"}:
        if (
            not signatures
            or any(signature != output_signature for signature in signatures)
        ):
            return (
                None,
                "sum/difference requires identical operand and output "
                "units/currencies",
            )
        computed = values[0]
        for value in values[1:]:
            computed = computed + value if operation == "sum" else computed - value
        return computed, None
    if operation == "product":
        non_ratios = [
            signature for signature in signatures if signature[0] != "ratio"
        ]
        expected = non_ratios[0] if len(non_ratios) == 1 else ("ratio", None)
        if len(non_ratios) > 1 or output_signature != expected:
            return None, "product output dimension is not declared by its operands"
        computed = Decimal(1)
        for value in values:
            computed *= value
        return computed, None
    if operation == "quotient":
        if len(values) != 2:
            return None, "quotient derivations require exactly two operands"
        numerator, denominator = signatures
        if denominator[0] == "ratio":
            expected = numerator
        elif numerator == denominator:
            expected = ("ratio", None)
        else:
            return (
                None,
                "quotient operands do not produce a supported output dimension",
            )
        if output_signature != expected:
            return (
                None,
                "quotient output unit/currency does not match its dimensions",
            )
        if values[1] == 0:
            return None, "division by zero in declared operation"
        numerator_scale = _SCALE_BY_UNIT.get(units[0], Decimal(1))
        denominator_scale = _SCALE_BY_UNIT.get(units[1], Decimal(1))
        output_scale = _SCALE_BY_UNIT.get(output_unit, Decimal(1))
        return (
            (values[0] * numerator_scale)
            / (values[1] * denominator_scale)
            / output_scale,
            None,
        )
    return None, "unknown arithmetic operation"


def _numeric_arithmetic_claim_tuple_problem(
    row: Mapping[str, Any],
    fact_roots: Mapping[str, Any],
    deterministic_current: object,
    deterministic_prior: object,
) -> _NumericFactClaimTupleProblem | None:
    """Validate one arithmetic row's declaration, result, and target tuple."""
    operation = str(row.get("operation") or "")
    operands = row.get("operands")
    operand_paths = (
        [str(operand) for operand in operands]
        if isinstance(operands, list)
        else []
    )
    if len(operand_paths) < 2 or operation not in {
        "sum",
        "difference",
        "product",
        "quotient",
    }:
        return _NumericFactClaimTupleProblem(
            "unresolved",
            "operation must be sum|difference|product|quotient over at least "
            "two resolvable operands",
        )

    values: list[Decimal] = []
    units: list[str] = []
    currencies: list[object] = []
    for operand in operand_paths:
        (
            operand_value,
            operand_unit,
            operand_currency,
            _,
            _,
            resolved,
        ) = _numeric_claim_fact_path_semantics(
            operand,
            deterministic_current,
            deterministic_prior,
        )
        number = _canonical_claim_number(operand_value)
        if not resolved or number is None:
            return _NumericFactClaimTupleProblem(
                "unresolved",
                f"operand {operand!r} does not resolve to a finite typed "
                "deterministic leaf",
            )
        values.append(number)
        units.append(operand_unit)
        currencies.append(operand_currency)

    declaration, declaration_detail = _declared_numeric_claim_derivation(
        str(row.get("metric") or ""),
        operation,
        operand_paths,
        deterministic_current,
        deterministic_prior,
    )
    if declaration is None:
        return _NumericFactClaimTupleProblem(
            "operation_unverified",
            declaration_detail,
        )

    output_unit = str(declaration.get("unit") or "")
    output_currency = declaration.get("currency")
    if output_currency is None and output_unit.startswith("usd_"):
        output_currency = "USD"
    computed, dimension_problem = _dimensionally_computed_claim_value(
        operation,
        values,
        units,
        currencies,
        output_unit,
        output_currency,
    )
    if dimension_problem is not None or computed is None:
        return _NumericFactClaimTupleProblem(
            "operation_unverified",
            dimension_problem or "operation has no valid output dimension",
        )

    declared_value = _canonical_claim_number(declaration.get("value"))
    if declared_value is None or declared_value != computed:
        return _NumericFactClaimTupleProblem(
            "operation_unverified",
            f"declared {operation} of {[str(value) for value in values]} "
            f"computes to {computed}; producer output is {declared_value}",
        )

    target_problem = _numeric_claim_target_problem(row, fact_roots)
    if target_problem is not None:
        return _NumericFactClaimTupleProblem("mismatch", target_problem)
    row_unit = str(row.get("unit") or "")
    if row_unit != output_unit:
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"unit {row_unit!r} does not match producer-declared output unit "
            f"{output_unit!r}",
        )
    if (row.get("currency") or None) != (output_currency or None):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"currency {row.get('currency')!r} does not match producer-declared "
            f"output currency {output_currency!r}",
        )
    row_period = str(row.get("period") or "")
    output_period = str(declaration.get("period") or "")
    if not _period_alias_matches(row_period, output_period):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"period {row_period!r} does not match producer-declared output "
            f"period {output_period!r}",
        )
    claimed_coefficient = _numeric_claim_coefficient(row.get("value"), row_unit)
    if claimed_coefficient is None or claimed_coefficient != computed:
        return _NumericFactClaimTupleProblem(
            "mismatch",
            f"claimed coefficient {claimed_coefficient} does not exactly equal "
            f"the recomputed {operation} result {computed}",
        )
    return None


_QUOTE_NORMALIZATION = {
    0x2018: "'", 0x2019: "'", 0x201A: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"',
    0x2013: "-", 0x2014: "-", 0x2212: "-",
    0x00A0: " ", 0x202F: " ",
}


def _quote_in_producer_text(
    quote: str,
    *,
    excerpt: str = "",
    news_items: object = None,
) -> bool:
    """Is the quote verbatim inside ONE producer-visible text surface?

    Producer-visible surfaces are the filing excerpt and recorded news
    items. Comparison normalizes only typographic glyphs and whitespace —
    never wording, ordering, or digits, so a stitched or paraphrased quote
    never matches.
    """

    normalized_quote = re.sub(
        r"\s+", " ", str(quote).translate(_QUOTE_NORMALIZATION)
    ).strip().casefold()
    if not normalized_quote:
        return False
    surfaces: list[str] = [excerpt if isinstance(excerpt, str) else ""]
    for item in news_items if isinstance(news_items, (list, tuple)) else ():
        surfaces.extend(
            value for value in _iter_surface_strings(item) if isinstance(value, str)
        )
    for surface in surfaces:
        normalized_surface = re.sub(
            r"\s+", " ", surface.translate(_QUOTE_NORMALIZATION)
        ).strip().casefold()
        if normalized_quote in normalized_surface:
            return True
    return False


def _normalize_claim_path(path: object) -> str:
    """Return an RFC 6901 comparison key for one valid authored path."""
    segments = _authored_target_segments(path)
    if segments is None:
        return ""
    return "".join(
        f"/{segment.replace('~', '~0').replace('/', '~1')}"
        for segment in segments
    )


def _numeric_claim_semantic_binding_key(
    row: Mapping[str, Any],
) -> tuple[object, ...] | None:
    """Canonical identity of one target/source numeric semantic binding."""
    target = _normalize_claim_path(row.get("path"))
    unit = _numeric_claim_scalar(row.get("unit") or "").casefold()
    coefficient = _numeric_claim_coefficient_key(row.get("value"), unit)
    source_kind = _numeric_claim_scalar(row.get("source_kind") or "").casefold()
    if not target or coefficient is None:
        return None

    metric_text = _numeric_claim_scalar(row.get("metric") or "")
    metric_groups = _metric_alias_groups(metric_text)
    metric_identity: tuple[str, ...] = (
        tuple(sorted(metric_groups))
        if metric_groups
        else tuple(re.findall(r"[a-z0-9]+", metric_text.casefold()))
    )
    period_text = _numeric_claim_scalar(row.get("period") or "")
    period_labels = _canonical_period_labels(period_text)
    period_identity: tuple[str, ...] = (
        tuple(sorted(period_labels))
        if period_labels
        else tuple(re.findall(r"[a-z0-9]+", period_text.casefold()))
    )

    if source_kind == "text":
        quote = re.sub(
            r"\s+",
            " ",
            str(row.get("quote") or "").translate(_QUOTE_NORMALIZATION),
        ).strip().casefold()
        source_identity: tuple[object, ...] = ("text", quote)
    elif source_kind == "fact":
        source_identity = (
            "fact",
            _numeric_claim_scalar(row.get("fact_path") or ""),
        )
    elif source_kind == "arithmetic":
        operation = _numeric_claim_scalar(
            row.get("operation") or ""
        ).casefold()
        operands = row.get("operands")
        operand_identity = (
            tuple(_numeric_claim_scalar(operand) for operand in operands)
            if isinstance(operands, (list, tuple))
            else ()
        )
        if operation in {"sum", "product"}:
            operand_identity = tuple(sorted(operand_identity))
        source_identity = (
            "arithmetic",
            operation,
            operand_identity,
        )
    else:
        return None

    return (
        target,
        coefficient,
        metric_identity,
        unit,
        _numeric_claim_scalar(row.get("currency") or "").upper(),
        period_identity,
        source_kind,
        source_identity,
    )


def _quote_span(source: str, quote: str) -> str:
    """Bounded context window around a verbatim quote inside its source.

    Returns the quote plus up to 120 characters of surrounding source text
    on each side, so unit/metric/period renderings co-occurring with the
    number in the producer's own sentence can be checked deterministically.
    """
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.translate(_QUOTE_NORMALIZATION)).casefold()

    haystack = source
    needle = _norm(quote).strip()
    if not needle:
        return ""
    lowered = _norm(haystack)
    position = lowered.find(needle)
    if position < 0:
        # Whitespace normalization may shift offsets; fall back to the raw
        # source when the normalized search cannot locate the quote.
        position = haystack.casefold().find(str(quote).casefold())
        if position < 0:
            return ""
    start = max(0, position - 120)
    end = min(len(haystack), position + len(needle) + 120)
    return haystack[start:end]


def _normalized_quote_occurrences(
    source: str, quote: str
) -> tuple[tuple[int, int], ...]:
    """Raw source offsets for exact quote matches under bounded normalization."""
    translated_quote = str(quote).translate(_QUOTE_NORMALIZATION).strip()
    if not translated_quote:
        return ()
    parts = re.split(r"\s+", translated_quote)
    pattern = r"\s+".join(re.escape(part) for part in parts)
    translated_source = source.translate(_QUOTE_NORMALIZATION)
    return tuple(
        match.span()
        for match in re.finditer(pattern, translated_source, re.IGNORECASE)
    )


def _numeric_text_claim_occurrence_compatible(
    row: Mapping[str, Any], text: str
) -> bool:
    """Bind coefficient, metric, unit, and currency at one quote occurrence."""
    unit = str(row.get("unit") or "")
    claimed_key = _numeric_claim_coefficient_key(row.get("value"), unit)
    if claimed_key is None:
        return False
    metric = str(row.get("metric") or "")
    for match in _NUMERIC_CLAIM_DISPLAY_RE.finditer(text):
        start, end = match.span()
        if (
            _numeric_claim_lexical_coefficient_key(text, start, end, unit)
            != claimed_key
        ):
            continue
        if not _numeric_target_metric_context_compatible(
            metric, text, start, end
        ):
            continue
        display_end = end
        points_suffix = _NUMERIC_TARGET_POINTS_SUFFIX_RE.match(text, end)
        if points_suffix is not None:
            display_end = points_suffix.end()
        display = text[start:display_end]
        if not _numeric_target_unit_compatible(display, unit):
            continue
        unit_is_rendered = (
            unit == "count"
            or _unit_for_rendering(display, unit)
            or (
                unit == "usd_per_share"
                and re.search(r"\bper\s+share\b", display, re.IGNORECASE)
                is not None
            )
        )
        if not unit_is_rendered:
            continue
        currency_surface = text[
            max(0, start - _NUMERIC_TARGET_CURRENCY_CONTEXT_CHARS):
            min(
                len(text),
                display_end + _NUMERIC_TARGET_CURRENCY_CONTEXT_CHARS,
            )
        ]
        if _numeric_target_currency_compatible(
            currency_surface, row.get("currency"), unit
        ):
            return True
    return False


def _numeric_text_occurrence_period_labels(
    row: Mapping[str, Any],
    text: str,
    *,
    inside: tuple[int, int] | None = None,
) -> set[str]:
    """Periods owned by exact metric/value occurrences of one source row."""
    unit = str(row.get("unit") or "")
    claimed_key = _numeric_claim_coefficient_key(row.get("value"), unit)
    if claimed_key is None:
        return set()
    metric = str(row.get("metric") or "")
    bundles: list[frozenset[str]] = []
    for match in _NUMERIC_CLAIM_DISPLAY_RE.finditer(text):
        if inside is not None and not (
            inside[0] <= match.start() and match.end() <= inside[1]
        ):
            continue
        if (
            _numeric_claim_lexical_coefficient_key(
                text, match.start(), match.end(), unit
            )
            != claimed_key
            or not _numeric_target_metric_context_compatible(
                metric, text, match.start(), match.end()
            )
        ):
            continue
        bundle = _numeric_target_owned_period_labels(
            text, match.start(), match.end()
        )
        if bundle:
            bundles.append(bundle)
    if not bundles:
        return set()
    wanted_labels = _canonical_period_labels(row.get("period"))
    matching = [
        bundle
        for bundle in bundles
        if _period_bundles_compatible(wanted_labels, set(bundle))
    ]
    selected = matching or bundles
    return set().union(*selected)


def _metadata_calendar_period_labels(value: object) -> set[str]:
    """Exact date/year identities from one ISO metadata date."""
    if value is None:
        return set()
    match = re.fullmatch(
        r"(?P<date>\d{4}-\d{2}-\d{2})(?:[T ].*)?",
        str(value).strip(),
    )
    if match is None:
        return set()
    try:
        parsed = date.fromisoformat(match.group("date"))
    except ValueError:
        return set()
    return {
        f"calendar-date:{parsed.isoformat()}",
        f"calendar-year:{parsed.year}",
    }


def _period_label_family(label: str) -> str:
    """Conflict family for one canonical period identity."""
    if label.startswith("relative:"):
        return "relative"
    return label.split(":", 1)[0]


def _period_bundle_conflict(labels: set[str]) -> bool:
    """Whether one source declares two identities in the same period family."""
    by_family: dict[str, set[str]] = {}
    for label in labels:
        by_family.setdefault(_period_label_family(label), set()).add(label)
    return any(len(values) > 1 for values in by_family.values())


def _numeric_text_period_problem(
    row: Mapping[str, Any],
    quote: str,
    source_text: str,
    quote_occurrence: tuple[int, int],
    *,
    source_kind: str,
    metadata: Mapping[str, Any],
) -> str | None:
    """Verify explicit quote period first, then only same-source metadata."""
    period = str(row.get("period") or "")
    wanted_labels = _canonical_period_labels(period)
    wanted = _primary_period_labels(wanted_labels)
    if _period_bundle_conflict(wanted):
        return f"period {period!r} carries ambiguous or conflicting periods"

    rendered = _numeric_text_occurrence_period_labels(row, quote)
    if not rendered:
        context_start = max(0, quote_occurrence[0] - 120)
        context_end = min(len(source_text), quote_occurrence[1] + 120)
        context = source_text[context_start:context_end]
        inside = (
            quote_occurrence[0] - context_start,
            quote_occurrence[1] - context_start,
        )
        rendered = _numeric_text_occurrence_period_labels(
            row, context, inside=inside
        )
    if rendered:
        rendered_primary = _primary_period_labels(rendered)
        if _period_bundle_conflict(rendered_primary):
            return "bound source occurrence carries ambiguous or conflicting periods"
        if rendered_primary:
            if not _period_bundles_compatible(wanted_labels, rendered):
                return (
                    f"period {period!r} conflicts with the explicit "
                    "bound-source period"
                )
            return None
        wanted_basis = wanted_labels.intersection(_TARGET_COMPARISON_LABELS)
        rendered_basis = rendered.intersection(_TARGET_COMPARISON_LABELS)
        if (
            wanted
            or not wanted_basis
            or wanted_basis != rendered_basis
        ):
            return (
                f"period {period!r} conflicts with the explicit "
                "bound-source period"
            )
        return None

    titles: list[str] = []
    for key in (
        ("title",) if source_kind == "primary excerpt" else ("title", "headline")
    ):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if len(value.strip()) > _MAX_NUMERIC_CLAIM_ALIAS_CHARS:
            return f"{source_kind} title/headline is not bounded period metadata"
        if value.strip().casefold() not in {
            title.casefold() for title in titles
        }:
            titles.append(value.strip())

    metadata_labels: set[str] = set()
    for title in titles:
        metadata_labels.update(_canonical_period_labels(title))
    date_value = (
        metadata.get("report_date")
        if metadata.get("report_date") is not None
        else metadata.get("published_at")
    )
    metadata_labels.update(_metadata_calendar_period_labels(date_value))
    metadata_labels = _primary_period_labels(metadata_labels)
    if metadata_labels:
        if _period_bundle_conflict(metadata_labels):
            return f"{source_kind} metadata carries ambiguous periods"
        if not _period_bundles_compatible(wanted_labels, metadata_labels):
            return (
                f"period {period!r} conflicts with the same-source "
                "document/news metadata period"
            )
        return None

    opaque_period = re.sub(r"\s+", " ", period).strip().casefold()
    if opaque_period and any(
        re.sub(r"\s+", " ", title).strip().casefold() == opaque_period
        for title in titles
    ):
        return None
    return f"{source_kind} carries no explicit or same-source metadata period"


def _numeric_text_claim_tuple_problem(
    row: Mapping[str, Any],
    fact_roots: Mapping[str, Any],
    *,
    excerpt: str,
    news_items: object,
    document_metadata: object = None,
) -> _NumericFactClaimTupleProblem | None:
    """Validate one text row against one uniquely bound producer source."""
    target_problem = _numeric_claim_target_problem(row, fact_roots)
    if target_problem is not None:
        return _NumericFactClaimTupleProblem("mismatch", target_problem)

    quote = row.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return _NumericFactClaimTupleProblem(
            "unresolved", "quote does not resolve to producer-visible text"
        )
    candidates: list[
        tuple[str, str, Mapping[str, Any], tuple[tuple[int, int], ...]]
    ] = []
    excerpt_text = excerpt if isinstance(excerpt, str) else ""
    excerpt_matches = _normalized_quote_occurrences(excerpt_text, quote)
    if excerpt_matches:
        candidates.append(
            (
                "primary excerpt",
                excerpt_text,
                (
                    document_metadata
                    if isinstance(document_metadata, Mapping)
                    else {}
                ),
                excerpt_matches,
            )
        )
    for index, item in enumerate(
        news_items if isinstance(news_items, (list, tuple)) else ()
    ):
        metadata = item if isinstance(item, Mapping) else {}
        for surface in _iter_surface_strings(item):
            if not isinstance(surface, str):
                continue
            matches = _normalized_quote_occurrences(surface, quote)
            if matches:
                candidates.append(
                    (f"news item {index}", surface, metadata, matches)
                )
    if not candidates:
        return _NumericFactClaimTupleProblem(
            "unresolved",
            "quote is not verbatim inside any single producer-visible "
            "surface (filing excerpt or recorded news item)",
        )
    if len(candidates) != 1:
        return _NumericFactClaimTupleProblem(
            "unresolved", "quote matches more than one producer-visible source"
        )
    source_kind, source_text, metadata, matches = candidates[0]
    if len(matches) != 1:
        return _NumericFactClaimTupleProblem(
            "unresolved",
            f"quote does not identify one occurrence inside {source_kind}",
        )
    if not _numeric_text_claim_occurrence_compatible(row, quote):
        return _NumericFactClaimTupleProblem(
            "mismatch",
            "claimed coefficient, unit, currency, and metric do not co-occur "
            "at one compatible occurrence in the bound quote",
        )
    period_problem = _numeric_text_period_problem(
        row,
        quote,
        source_text,
        matches[0],
        source_kind=source_kind,
        metadata=metadata,
    )
    if period_problem is not None:
        return _NumericFactClaimTupleProblem("mismatch", period_problem)
    return None


def _iter_surface_strings(node: object):
    """Yield every string inside one recorded news item."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for child in node.values():
            yield from _iter_surface_strings(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from _iter_surface_strings(child)


class InvestmentFinalizedAnalysis(NamedTuple):
    """Pure final-analysis components consumed by ``analyze_document``."""

    facts: dict
    classified_industry: str
    previous_facts: dict | None
    analysis: dict


def finalize_investment_analysis(
    parsed_facts: dict,
    *,
    document: dict,
    deterministic_current: dict,
    deterministic_prior: dict,
    market_inputs: dict,
    stored_previous_facts: dict,
    previous_state: object,
    prior_count: int,
    news_items: list[dict],
    extraction: dict,
    relationship_facts: Mapping,
    material_relationships: list | tuple,
) -> InvestmentFinalizedAnalysis:
    """Merge deterministic metrics and assemble the final analysis without I/O.

    Accepts already-prepared facts (document metadata, deterministic
    current/prior metrics, prior-state summary) and returns the facts plus
    final analysis components; database and telemetry concerns stay with the
    caller.
    """
    # Pure-finalization boundary: take deep ownership of every mutable
    # caller-owned input that reaches the returned facts/analysis so repeated
    # finalization neither aliases nor mutates caller state.
    parsed_facts = copy.deepcopy(parsed_facts)
    deterministic_current = copy.deepcopy(deterministic_current)
    deterministic_prior = copy.deepcopy(deterministic_prior)
    market_inputs = copy.deepcopy(market_inputs)
    stored_previous_facts = copy.deepcopy(stored_previous_facts)
    news_items = copy.deepcopy(news_items)
    extraction = copy.deepcopy(extraction)
    relationship_facts = _plain_json_value(relationship_facts)
    material_relationships = _plain_json_value(material_relationships)
    facts = _merge_metric_facts(
        parsed_facts, deterministic_current, deterministic_prior
    )
    facts["relationship_facts"] = relationship_facts
    facts["material_relationships"] = material_relationships
    facts["relationship_reconciliations"] = copy.deepcopy(
        facts.get("relationship_reconciliations") or []
    )

    classification = facts["classification"]
    classified_industry = _resolve_analysis_industry(document, classification)
    classification["industry"] = classified_industry
    classification["region"] = document["region"]
    classification["document_type"] = document["document_type"]

    previous_facts = (
        stored_previous_facts if isinstance(stored_previous_facts, dict) else {}
    )
    if any(
        isinstance(item, dict) and item.get("value") is not None
        for item in facts.get("prior_metrics", {}).values()
    ):
        previous_facts = {
            "metrics": facts["prior_metrics"],
            "qualitative": previous_facts.get("qualitative", {}),
        }
    previous_facts = previous_facts or None
    deterministic = build_deterministic_analysis(
        facts,
        previous_facts=previous_facts,
        market_inputs=market_inputs if isinstance(market_inputs, dict) else {},
        previous_state=previous_state,
        prior_analysis_count=prior_count,
        news_items=news_items,
    )
    evidence = []
    for metric_name, item in facts.get("metrics", {}).items():
        if isinstance(item, dict) and item.get("evidence"):
            evidence.append(
                {
                    "source": item.get("source") or "report",
                    "metric": metric_name,
                    "quote": _clean_text(item["evidence"], limit=500),
                }
            )
    for signal_name, item in facts.get("qualitative", {}).items():
        if isinstance(item, dict) and item.get("present") and item.get("evidence"):
            evidence.append(
                {
                    "source": "report",
                    "signal": signal_name,
                    "quote": _clean_text(item["evidence"], limit=500),
                }
            )

    analysis = {
        **deterministic,
        "summary": _clean_text(facts.get("summary"), limit=2400),
        "thesis": _clean_text(facts.get("thesis"), limit=1600),
        "counter_thesis": _clean_text(facts.get("counter_thesis"), limit=1200),
        "materiality_assessment": copy.deepcopy(
            facts.get("materiality_assessment") or {}
        ),
        "classification": facts.get("classification", {}),
        "drivers": _dedupe_strings(
            facts.get("drivers"), deterministic.get("drivers")
        ),
        "catalysts": facts.get("catalysts", [])[:12]
        if isinstance(facts.get("catalysts"), list)
        else [],
        "risks": facts.get("risks", [])[:12]
        if isinstance(facts.get("risks"), list)
        else [],
        "watch_items": _merge_watch_items(
            facts.get("watch_items"), deterministic.get("watch_items")
        ),
        "evidence": evidence[:40],
        "news_context": news_items,
        "extraction": extraction,
        # The settled numeric-claim ledger rides with the analysis so every
        # downstream consumer (judges, hard gates, artifacts) sees exactly
        # the bindings the narrative was validated against.
        "numeric_claims": copy.deepcopy(facts.get("numeric_claims") or []),
        "relationship_facts": relationship_facts,
        "material_relationships": material_relationships,
        "relationship_reconciliations": copy.deepcopy(
            facts["relationship_reconciliations"]
        ),
    }
    return InvestmentFinalizedAnalysis(
        facts=facts,
        classified_industry=classified_industry,
        previous_facts=previous_facts,
        analysis=analysis,
    )


def _load_news_context(config: dict, metadata: dict) -> list[dict]:
    industry = canonicalize_industry(metadata.get("industry"))
    if industry == "Unclassified":
        # Deterministic issuer metadata keeps company-linked news relevance
        # working even when the stored document predates the canonical labels.
        industry = industry_for(metadata.get("symbol"), metadata.get("company"))
    symbol = str(metadata.get("symbol") or "").upper()
    company = str(metadata.get("company") or "").casefold()
    selected = []
    for item in load_classified_news(config, [metadata], limit=500):
        symbols = {value.upper() for value in item.get("symbols", [])}
        companies = {value.casefold() for value in item.get("companies", [])}
        direct = bool(symbol and symbol in symbols) or bool(
            company and company in companies
        )
        industry_match = industry in item.get("industries", [])
        if not direct and not industry_match:
            continue
        selected.append(
            {
                **item,
                "relevance": "company" if direct else "industry",
            }
        )
        if len(selected) >= 20:
            break
    return selected


def _correct_interim_document_type(
    config: dict,
    document: dict,
    extracted_text: str,
) -> None:
    cover_text = extracted_text[:8_000]
    if (
        document.get("filing_source") != "companies_house"
        or document.get("document_type") != "annual_report"
        or not re.search(
            r"\b(?:unaudited\s+interim\s+accounts|interim\s+financial\s+statements|"
            r"half[- ]year(?:ly)?\s+(?:report|results))\b",
            cover_text,
            re.IGNORECASE,
        )
    ):
        return
    document["document_type"] = "quarterly_report"
    with get_session(config) as session:
        session.execute(
            text(
                "UPDATE investment_documents SET document_type = 'quarterly_report', "
                "updated_at = NOW() WHERE document_id = :document_id"
            ),
            {"document_id": document["document_id"]},
        )
        session.execute(
            text(
                "DELETE FROM investment_research_observations "
                "WHERE source_kind = 'report' AND source_id = :document_id"
            ),
            {"document_id": str(document["document_id"])},
        )


def _ensure_extracted_text(
    config: dict,
    document: dict,
    *,
    ocr_page_budget: int = SYNC_OCR_PAGE_BUDGET,
    ocr_wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> str:
    existing = str(document.get("extracted_text") or "")
    if len(existing.strip()) >= 100:
        _correct_interim_document_type(config, document, existing)
        return "stored_document"
    raw_content = document.get("raw_content")
    content_path = document.get("content_path")
    if isinstance(raw_content, (bytes, bytearray, memoryview)):
        try:
            extracted = extract_document_text(
                bytes(raw_content),
                str(document.get("filename") or "report"),
                str(document.get("mime_type") or "application/octet-stream"),
                max_bytes=MAX_REGULATORY_DOCUMENT_BYTES,
                ocr_page_budget=ocr_page_budget,
                ocr_wall_seconds=ocr_wall_seconds,
            )
        except Exception as exc:
            logger.warning(
                "regulatory_document_extraction_failed",
                document_id=str(document.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            return "missing_report_text"
    elif isinstance(content_path, str) and content_path:
        # Durable file storage: validate the persisted path against the
        # content-addressed root and row digest before reading from disk.
        try:
            validated_path = _validated_document_content_path(config, document)
            extracted = extract_document_text_path(
                validated_path,
                str(document.get("filename") or "report"),
                str(document.get("mime_type") or "application/octet-stream"),
                max_bytes=MAX_REGULATORY_DOCUMENT_BYTES,
                ocr_page_budget=ocr_page_budget,
                ocr_wall_seconds=ocr_wall_seconds,
            )
        except Exception as exc:
            logger.warning(
                "document_content_extraction_failed",
                document_id=str(document.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            return "missing_report_text"
    else:
        return "missing_report_text"
    document["extracted_text"] = extracted
    _correct_interim_document_type(config, document, extracted)
    with get_session(config) as session:
        session.execute(
            text(
                "UPDATE investment_documents SET extracted_text = :extracted_text, "
                "updated_at = NOW() WHERE document_id = :document_id"
            ),
            {
                "document_id": document["document_id"],
                "extracted_text": extracted,
            },
        )
    mime_type = str(document.get("mime_type") or "").casefold()
    if "pdf" in mime_type:
        return "regulatory_pdf_ocr"
    if "zip" in mime_type:
        return "inline_xbrl_report_package"
    return "regulatory_document"


def _sec_primary_document_rank(name: str) -> int | None:
    """Deterministic primary-document signal rank for an SEC directory entry.

    Lower rank means a stronger signal; names carrying no signal return None so
    the caller keeps the legacy largest-eligible-HTML fallback.
    """
    stem = name.casefold()
    for suffix in (".htm", ".html"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem:
        return None
    # Explicit 10-K naming (``d753577d10k.htm``, ``form10-k.htm``,
    # ``tv506844_10k.htm``, ``abc-10k_20241231.htm``).
    if re.search(r"10-?k(?:$|[._-]\d{8}$)", stem):
        return 0
    # Registrant/ticker + report-date convention (``aapl-20240928.htm``).
    if re.fullmatch(r"[a-z0-9]+-\d{8}", stem):
        return 1
    return None


_SEC_ANNUAL_FORM_TYPES = frozenset({"10-k", "20-f", "40-f"})


def _select_sec_primary_document(
    candidates: list[tuple[int, str]],
    known_primary: object = "",
    document_types: Mapping[str, str] | None = None,
) -> str | None:
    """Select the regulator primary document from eligible SEC directory files.

    Priority: exact known primary-document metadata, then the annual form
    (10-K/20-F/40-F) row of the accession ``*-index.html`` Document Format
    Files table (authoritative filing-detail metadata), then deterministic SEC
    primary-document naming conventions, then the legacy largest-eligible
    fallback. Ties are broken deterministically (larger file, then name).
    """
    known = PurePath(str(known_primary or "")).name
    by_name = {name: size for size, name in candidates}
    if known and known in by_name:
        return known
    if document_types:
        annual = [
            name
            for name, doc_type in document_types.items()
            if doc_type.casefold().split("/", 1)[0] in _SEC_ANNUAL_FORM_TYPES
            and name in by_name
        ]
        if annual:
            return min(
                annual,
                key=lambda name: (
                    _sec_primary_document_rank(name) is None,
                    _sec_primary_document_rank(name) or 0,
                    -by_name[name],
                    name.casefold(),
                ),
            )
    ranked = [
        (size, name)
        for size, name in candidates
        if _sec_primary_document_rank(name) is not None
    ]
    if ranked:
        return min(
            ranked,
            key=lambda pair: (
                _sec_primary_document_rank(pair[1]),
                -pair[0],
                pair[1].casefold(),
            ),
        )[1]
    if candidates:
        return max(candidates)[1]
    return None


def _parse_sec_index_document_types(markup: str) -> dict[str, str]:
    """Map document file names to EDGAR document types from the accession
    ``*-index.html`` Document Format Files table (``name -> type``)."""
    soup = BeautifulSoup(markup, "html.parser")
    document_types: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [
            cell.get_text(" ", strip=True).casefold() for cell in rows[0].find_all("th")
        ]
        if "document" not in headers or "type" not in headers:
            continue
        document_index = headers.index("document")
        type_index = headers.index("type")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(document_index, type_index):
                continue
            link = cells[document_index].find("a")
            if link is None or not link.get("href"):
                continue
            name = PurePath(str(link["href"])).name
            doc_type = cells[type_index].get_text(" ", strip=True)
            if name and doc_type and name not in document_types:
                document_types[name] = doc_type
    return document_types


def _load_sec_index_document_types(
    source_url: str, index_page_name: str, user_agent: str
) -> dict[str, str] | None:
    """Fetch and parse the accession ``*-index.html`` Document Format Files
    table. Returns None when the page is missing or unparseable so the caller
    falls back to deterministic naming heuristics."""
    try:
        page_url = _validate_public_url(source_url.rstrip("/") + "/" + index_page_name)
        response = make_request(
            "GET",
            page_url,
            headers={"User-Agent": user_agent, "Accept": "text/html"},
            timeout=30.0,
            max_retries=2,
            client=get_shared_client(),
            max_response_bytes=MAX_DOCUMENT_BYTES,
        )
        response.raise_for_status()
        markup = response.content.decode("utf-8", errors="replace")
        return _parse_sec_index_document_types(markup) or None
    except Exception as exc:
        logger.info(
            "sec_index_document_types_unavailable",
            error_type=type(exc).__name__,
        )
        return None


def _load_report_excerpt(config: dict, document: dict) -> tuple[str, str]:
    """Use the primary SEC filing for legacy bundles that lost file priority.

    For SEC source URLs the regulator primary document is recovered
    authoritatively regardless of stored/raw-content quality: the accession
    ``*-index.html`` Document Format Files table is consulted first (annual
    form 10-K/20-F/40-F row), then deterministic primary-document naming
    conventions, then the largest eligible HTML. URL/size bounds are strict
    and the stored bundle excerpt is the fallback on any failure.
    """
    existing = build_analysis_excerpt(document.get("extracted_text") or "")
    if document.get("filing_source") != "sec_edgar":
        return existing, "stored_document"
    source_url = str(document.get("source_url") or "")
    if not source_url.startswith("https://www.sec.gov/Archives/edgar/data/"):
        return existing, "stored_document"
    user_agent = config.get("investment_filings", {}).get(
        "sec_user_agent",
        "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)",
    )
    try:
        # DB-derived URLs are re-validated against the public-origin policy
        # before any connection (defense in depth for stored metadata).
        index_url = _validate_public_url(source_url.rstrip("/") + "/index.json")
        index_response = make_request(
            "GET",
            index_url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            max_retries=2,
            client=get_shared_client(),
        )
        index_response.raise_for_status()
        items = index_response.json().get("directory", {}).get("item", [])
        candidates = []
        index_page_name = ""
        for item in items if isinstance(items, list) else []:
            name = PurePath(str(item.get("name") or "")).name
            lowered = name.lower()
            if re.fullmatch(r".*-index\.html?", lowered):
                if not index_page_name:
                    index_page_name = name
                continue
            if not lowered.endswith((".htm", ".html")):
                continue
            if (
                "-index" in lowered
                or re.fullmatch(r"r\d+\.html?", lowered)
                or "exhibit" in lowered
                or re.search(r"(?:^|[-_])ex\d", lowered)
            ):
                continue
            try:
                size = int(item.get("size"))
            except (TypeError, ValueError):
                continue
            if 0 < size <= MAX_DOCUMENT_BYTES:
                candidates.append((size, name))
        if not candidates:
            return existing, "stored_document"
        document_types = (
            _load_sec_index_document_types(source_url, index_page_name, user_agent)
            if index_page_name
            else None
        )
        primary_name = _select_sec_primary_document(
            candidates,
            document.get("primary_document"),
            document_types,
        )
        if not primary_name:
            return existing, "stored_document"
        primary_url = _validate_public_url(source_url.rstrip("/") + "/" + primary_name)
        response = make_request(
            "GET",
            primary_url,
            headers={"User-Agent": user_agent, "Accept": "text/html"},
            timeout=90.0,
            max_retries=2,
            client=get_shared_client(),
            max_response_bytes=MAX_DOCUMENT_BYTES,
        )
        response.raise_for_status()
        primary_text = extract_document_text(
            response.content,
            primary_name,
            response.headers.get("content-type", "text/html"),
        )
        return build_analysis_excerpt(primary_text), "sec_primary_document"
    except Exception as exc:
        logger.warning(
            "sec_primary_document_recovery_failed",
            document_id=str(document.get("document_id") or ""),
            error_type=type(exc).__name__,
        )
        return existing, "stored_document"


def _build_prompt(
    document: dict,
    excerpt: str,
    news_items: list[dict],
    deterministic_metrics: dict,
    relationship_contract: dict,
) -> str:
    news_text = json.dumps(news_items, ensure_ascii=False, sort_keys=True)
    deterministic_text = json.dumps(
        deterministic_metrics,
        ensure_ascii=False,
        sort_keys=True,
    )
    relationship_text = json.dumps(
        relationship_contract,
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"""You are a professional buy-side analyst reviewing report narrative.
Return only the strict JSON response. Use the exact model schema supplied by the caller.

Rules:
- Financial figures are extracted and calculated by deterministic code. Do not return metric objects, infer a missing figure, recalculate a value, or contradict the supplied deterministic facts.
- The filing is authoritative for company statements. Each evidence value must be one short contiguous substring copied exactly from a single region of the FILING EXCERPT: never combine text from multiple regions, never include field labels, never wrap it in quotation marks that are not in the source, and never append commentary.
- Qualitative signals are present only when explicitly supported. AI demand and data-centre demand are distinct.
- Separate company-stated facts from your interpretation. Summary and thesis must identify uncertainty and what would invalidate the thesis. `counter_thesis` must be nonblank and state the strongest evidence-grounded disconfirming case, not generic caution.
- Reconcile every supplied material relationship once, in its given order, using exactly its relationship ID and required fact paths. Preserve every fact qualifier; do not blend sourced observations with interpretations.
- For each compatible relationship, set `reconciled`; keep the complete audit rendering in nonblank observation, interpretation, and uncertainty; write a concise nonblank `summary_synthesis` and `thesis_synthesis`; and copy those exact synthesis strings contiguously into summary and thesis respectively. Select 1-2 unique `summary_fact_paths` from that relationship's required fact paths. Do not copy the full observation, interpretation, or uncertainty into the top-level narrative merely to satisfy inclusion. For each incompatible relationship, set `abstained_incompatible` and use exactly empty interpretation, summary_synthesis, thesis_synthesis, and summary_fact_paths.
- For each compatible relationship at index `i`, render every numeric `required_fact` in `required_facts` order. Give each fact its own atomic observation clause with metric, rendered value/unit or currency, exact period, and, for growth or change, comparison basis. Each required fact has exactly one `source_kind="fact"` row targeting `relationship_reconciliations[i].observation`. Summary must render every selected summary fact; across relationships, shared selected facts and shared summary segments are written once, with exactly one deduplicated fact row targeting `summary` for each unique selected fact path.
- Complete all four `materiality_assessment` topics: `forward_guidance` covers explicit quantified guidance and its period; `reported_variance_driver` covers the reported variance explanation; `margin_economics` separates price, mix, cost, and margin effects; `capital_commitment_duration` distinguishes one-time, multi-period, and recurring commitments. Use `addressed` truthfully whenever the source contains the topic, with nonblank observation, implication, and one exact filing evidence quote. Use `not_disclosed` only when the source does not address it, with all three text fields empty. Incorporate addressed material into thesis, counter_thesis, risks, and catalysts where supported. Numeric materiality observations require `numeric_claims` rows bound to the exact target.
- Keep the response concise. Evidence grounds only sourced observations, catalyst triggers, and addressed materiality observations; it does not prove interpretations or outcomes.
- A risk's `sourced_observation` must preserve the source's material scope, basis, hedges, conditions, exclusions, and time qualifiers. Its `inference` separately states the possible thesis consequence; `epistemic_state` conservatively qualifies that inference; `uncertainty` identifies the unresolved condition rather than repeating likelihood. Use only a company-stated mitigation or a non-advisory monitoring response. Do not provide portfolio sizing, allocation, or exposure instructions.
- A catalyst's `trigger` must be a future observable event with a concrete `horizon`. Preserve the filing's fiscal/time label exactly; expand it to calendar dates or months only when explicit deterministic fiscal-calendar metadata provides the expansion. Its `expected_outcome` states the directional thesis-moving consequence; `epistemic_state` and `uncertainty` describe the outcome linkage. Evidence supports the trigger only, not the expected outcome.
- Planned spending, capacity, hiring, or another input alone is not a catalyst. Without a supported thesis-moving outcome, place it in `watch_items`.
- News is separate context. It may inform a catalyst, external risk, or crowding check, but never present news wording as filing evidence.
- When the filing excerpt has no relevant narrative evidence, return an explicit insufficient-evidence summary and empty optional arrays rather than generic investment commentary.
- Fiscal/calendar labels (for example `H2`, `FY25`, `Q4 FY25`, or a valid date) are period metadata, not quantitative coefficients. Do not create a `numeric_claims` row solely for digits embedded in such a label. Actual quantities (for example `29%`, `$2B`, `2%`, `2–3 quarters`, `2025%`, or `$2,025 million`) remain material and require rows, including when mixed with a period label.
- Every unique material numeric binding you write in summary, thesis, counter_thesis, relationship reconciliations, materiality assessment, drivers, catalysts, risks, watch_items, or qualitative evidence must have one `numeric_claims` row binding it to its exact target text and to ONE producer-visible source. Repeated copies of the same fact/value binding in one target leaf share one row; distinct target paths retain distinct rows. Use `source_kind="text"` with `quote` copied verbatim from the FILING EXCERPT or recorded news; `source_kind="fact"` with `fact_path` pointing into the supplied deterministic metrics (`deterministic_current.<metric>.<field>` or `deterministic_prior....`); or `source_kind="arithmetic"` only for a combination the deterministic facts themselves define, naming `operation` and at least two `operands`.
- For a `source_kind="fact"` row referencing a normalized relationship fact, copy ledger fields exactly: use its exact `fact_path` (never a `.value` child or original metric alias), copy `metric_label` verbatim into `metric`, and copy its `period`, `unit`, and `currency`; keep `value` exactly as rendered in the target and ensure it normalizes to the fact's `value`. `value` must be a finite numeric scalar or a compact numeric token of at most 64 characters, never explanatory prose.
- Exact enum copying applies to ledger fields, not target prose. Use professional prose surfaces with no underscores: render `percent` as `%`, `percentage_points` as `percentage points`, and currency units in professional currency form. Every material numeral must locally state its metric, unit or currency, and period; growth or change must also state its comparison basis. Source metadata and `numeric_claims` rows document provenance but cannot silently supply target semantics omitted from the prose. Prefer concise atomic clauses that make each numeral self-describing.
- For a `source_kind="text"` row, an explicit fiscal, calendar, relative, prior, or forward period in the quote is authoritative and must match `period`; metadata cannot override it. Only when the quote is period-silent may `period` use context from that exact source: the document `title` with exact `report_date` for the FILING EXCERPT, or the matched news item's own title/headline with its own date fields. Never infer a fiscal quarter from a date alone, invent a period, or borrow one from another source.

Metadata:
{json.dumps({key: str(value) if value is not None else None for key, value in document.items() if key not in {"extracted_text", "raw_content"}}, sort_keys=True)}

Deterministic filing facts (read-only context):
{deterministic_text}

Material relationship contract (read-only; reconcile exactly):
{relationship_text}

Related classified news (separate context):
{news_text}

FILING EXCERPT:
{excerpt}
"""


def _parse_llm_json(content: object) -> dict:
    """Decode one JSON object; nested shape belongs to schema validation."""
    if not isinstance(content, str):
        raise ValueError("LLM response was not text")
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE
        )
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not an object")
    return parsed


def _as_json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _previous_analysis(config: dict, document: dict) -> tuple[dict | None, int]:
    identity_clause = "(d.symbol = :symbol OR LOWER(d.company) = LOWER(:company))"
    params = {
        "document_id": document["document_id"],
        "symbol": document.get("symbol"),
        "company": document["company"],
        "report_date": document.get("report_date"),
    }
    prior_sql = text(
        f"""
        SELECT d.document_id, a.facts, a.analysis
        FROM investment_documents d
        JOIN investment_analyses a ON a.document_id = d.document_id
        WHERE d.document_id <> :document_id
          AND {identity_clause}
          AND (:report_date IS NULL OR d.report_date IS NULL OR d.report_date < :report_date)
        ORDER BY d.report_date DESC NULLS LAST, a.created_at DESC
        LIMIT 1
        """
    )
    count_sql = text(
        f"""
        SELECT COUNT(*)
        FROM investment_documents d
        JOIN investment_analyses a ON a.document_id = d.document_id
        WHERE d.document_id <> :document_id
          AND {identity_clause}
          AND (:report_date IS NULL OR d.report_date IS NULL OR d.report_date < :report_date)
        """
    )
    with get_session(config) as session:
        row = session.execute(prior_sql, params).fetchone()
        count = int(session.execute(count_sql, params).scalar() or 0)
    if row is None:
        return None, count
    result = dict(row._mapping)
    result["facts"] = _as_json_object(result.get("facts"))
    result["analysis"] = _as_json_object(result.get("analysis"))
    return result, count


def _load_document(config: dict, document_id: str) -> dict | None:
    with get_session(config) as session:
        row = session.execute(
            text("SELECT * FROM investment_documents WHERE document_id = :document_id"),
            {"document_id": document_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def _dedupe_strings(*groups: object, limit: int = 12) -> list[str]:
    values = []
    seen = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict):
                item = item.get("label") or item.get("driver")
            cleaned = _clean_text(item, limit=400)
            marker = cleaned.casefold()
            if cleaned and marker not in seen:
                seen.add(marker)
                values.append(cleaned)
            if len(values) >= limit:
                return values
    return values


def _merge_watch_items(model_items: object, deterministic_items: object) -> list[str]:
    """Do not let stale model warnings undo deterministic direct-fact coverage."""
    deterministic = _dedupe_strings(deterministic_items)
    deterministic_markers = {item.casefold() for item in deterministic}
    direct_fact_warnings = {
        "gross margin: missing comparable evidence",
        "guidance: missing comparable evidence",
    }
    filtered_model = (
        [
            item
            for item in model_items
            if not (
                isinstance(item, str)
                and item.strip().casefold() in direct_fact_warnings
                and item.strip().casefold() not in deterministic_markers
            )
        ]
        if isinstance(model_items, list)
        else []
    )
    return _dedupe_strings(filtered_model, deterministic)


def _merge_metric_facts(
    facts: dict,
    deterministic_current: dict,
    deterministic_prior: dict,
) -> dict:
    """Overlay deterministic facts on model extraction without mutating inputs."""
    merged = dict(facts)
    current = (
        dict(facts.get("metrics", {})) if isinstance(facts.get("metrics"), dict) else {}
    )
    prior = (
        dict(facts.get("prior_metrics", {}))
        if isinstance(facts.get("prior_metrics"), dict)
        else {}
    )
    current.update(deterministic_current)
    prior.update(deterministic_prior)
    merged["metrics"] = current
    merged["prior_metrics"] = prior
    return merged


def _record_processing_log(
    session,
    *,
    status: str,
    started_at: datetime,
    correlation_id: str,
    document_id: str,
    stage,
    output_id: str | None = None,
    error_type: str | None = None,
) -> None:
    telemetry = stage.telemetry if stage is not None else None
    duration_ms = max(
        0,
        round((datetime.now(UTC) - started_at).total_seconds() * 1000),
    )
    session.execute(
        text(
            """
            INSERT INTO processing_log (
                log_id, started_at, completed_at, processor, status, input_summary,
                output_id, model_used, tokens_input, tokens_output, cost_usd,
                duration_ms, error_message, correlation_id, request_metadata
            ) VALUES (
                :log_id, :started_at, NOW(), 'investment_analysis', :status,
                CAST(:input_summary AS JSONB), :output_id, :model_used,
                :tokens_input, :tokens_output, :cost_usd, :duration_ms,
                :error_message, :correlation_id, CAST(:request_metadata AS JSONB)
            )
            """
        ),
        {
            "log_id": str(uuid4()),
            "started_at": started_at,
            "status": status,
            "input_summary": json.dumps({"document_id": document_id}),
            "output_id": output_id,
            "model_used": stage.policy.model if stage is not None else MODEL_ID,
            "tokens_input": telemetry.tokens_input_total
            if telemetry is not None
            else 0,
            "tokens_output": telemetry.tokens_output_total
            if telemetry is not None
            else 0,
            "cost_usd": telemetry.cost_usd_total if telemetry is not None else 0.0,
            "duration_ms": duration_ms,
            "error_message": f"analysis failed ({error_type})" if error_type else None,
            "correlation_id": correlation_id,
            "request_metadata": json.dumps(
                {
                    "rule_version": INVESTMENT_ANALYSIS_RULE_VERSION,
                    "structured_response": True,
                    "validation_warnings": (
                        telemetry.validation_warnings if telemetry is not None else []
                    ),
                }
            ),
        },
    )


def _resolve_analysis_industry(document: dict, classification: dict) -> str:
    """Resolve the canonical industry for a stored analysis.

    Checked-in issuer metadata is authoritative for configured issuers and
    wins over any model label. Otherwise a model label is trusted only when
    it is concrete and not low-confidence; model ``Unclassified`` or
    low-confidence output never overwrites the document's existing industry,
    and truly unknown issuers fail closed to ``Unclassified``.
    """
    deterministic = industry_for(document.get("symbol"), document.get("company"))
    if deterministic != "Unclassified":
        return deterministic
    model_industry = canonicalize_industry(classification.get("industry"))
    if model_industry != "Unclassified" and classification.get("confidence") != "low":
        return model_industry
    return canonicalize_industry(document.get("industry"))


def _apply_deterministic_industry(payload: dict) -> dict:
    """Apply checked-in issuer metadata to a read-time document/analysis.

    Legacy rows stored before the canonical mapping may carry an
    Unclassified or model-derived industry. For configured issuers the
    checked-in canonical industry overrides both the stored industry and any
    nested analysis classification, without mutating the database. Unknown
    issuers keep their stored label canonicalized as the fallback.
    """
    normalized = dict(payload)
    classification = normalized.get("classification")
    if isinstance(classification, dict):
        classification = dict(classification)
        normalized["classification"] = classification
    resolved = industry_for(payload.get("symbol"), payload.get("company"))
    if resolved != "Unclassified":
        normalized["industry"] = resolved
        if isinstance(classification, dict):
            classification["industry"] = resolved
        return normalized
    normalized["industry"] = canonicalize_industry(payload.get("industry"))
    if isinstance(classification, dict):
        classification["industry"] = canonicalize_industry(
            classification.get("industry")
        )
    return normalized


def analyze_document(
    config: dict,
    document_id: str,
    market_inputs: dict | None = None,
    *,
    ocr_page_budget: int = SYNC_OCR_PAGE_BUDGET,
    ocr_wall_seconds: float = SYNC_OCR_WALL_SECONDS,
) -> dict:
    document = _load_document(config, document_id)
    if document is None:
        raise LookupError("investment document not found")
    correlation_id = str(uuid4())

    with get_session(config) as session:
        claimed = session.execute(
            text(
                "UPDATE investment_documents "
                "SET status = 'analyzing', error_message = NULL, updated_at = NOW() "
                "WHERE document_id = :document_id AND status <> 'analyzing' "
                "RETURNING document_id"
            ),
            {"document_id": document_id},
        ).fetchone()
    if claimed is None:
        raise AnalysisInProgress("investment analysis is already running")

    started_at = datetime.now(UTC)
    stage = None
    try:
        prepared_text_source = _ensure_extracted_text(
            config,
            document,
            ocr_page_budget=ocr_page_budget,
            ocr_wall_seconds=ocr_wall_seconds,
        )
        news_items = _load_news_context(config, document)
        deterministic_current, deterministic_prior, extraction = (
            load_deterministic_facts(config, document)
        )
        frozen_deterministic_current = _freeze_json_value(deterministic_current)
        frozen_deterministic_prior = _freeze_json_value(deterministic_prior)
        frozen_news_items = _freeze_json_value(news_items)
        for metric_name in ("revenue", "net_income", "total_assets"):
            metric = deterministic_current.get(metric_name, {})
            try:
                report_period = date.fromisoformat(str(metric.get("period") or ""))
            except ValueError:
                continue
            document["report_date"] = report_period
            extraction = {**extraction, "report_period": report_period.isoformat()}
            break
        excerpt, recovered_text_source = _load_report_excerpt(config, document)
        report_text_source = (
            recovered_text_source
            if recovered_text_source != "stored_document"
            else prepared_text_source
        )
        extraction = {**extraction, "report_text_source": report_text_source}
        # The budget is reserved by LLMStage.call immediately before the paid
        # dispatch (never eagerly at entry: OCR/preprocessing can outlive the
        # reservation TTL, and no paid call may use an expired reservation).
        request = build_investment_analysis_request(
            document,
            excerpt,
            news_items,
            deterministic_current,
            deterministic_prior,
        )
        stage = LLMStage(
            config,
            "investment_analysis",
            correlation_id=correlation_id,
            response_schema={
                "name": request.schema_name,
                "strict": request.strict,
                "schema": _plain_json_value(request.schema),
            },
        )
        result = stage.call(request.prompt)
        try:
            facts = _validated_investment_facts(
                result.get("content"),
                excerpt=excerpt,
                news_items=frozen_news_items,
                deterministic_current=frozen_deterministic_current,
                deterministic_prior=frozen_deterministic_prior,
                document_metadata=document,
                relationship_facts=request.relationship_facts,
                material_relationships=request.material_relationships,
            )
        except InvestmentValidationError as exc:
            stage.add_validation_warnings(
                [
                    _VALIDATION_WARNING_BY_CATEGORY[category]
                    for category in exc.categories
                ]
            )
            if stage.policy.validation_retries < 1:
                raise LLMValidationError(
                    "Investment response validation failed", stage.telemetry
                ) from exc
            result = stage.call(f"{request.prompt}\n{exc.correction_requirement}")
            try:
                facts = _validated_investment_facts(
                    result.get("content"),
                    excerpt=excerpt,
                    news_items=frozen_news_items,
                    deterministic_current=frozen_deterministic_current,
                    deterministic_prior=frozen_deterministic_prior,
                    document_metadata=document,
                    relationship_facts=request.relationship_facts,
                    material_relationships=request.material_relationships,
                )
            except InvestmentValidationError as exc:
                raise LLMValidationError(
                    "Investment response validation failed", stage.telemetry
                ) from exc

        prior, prior_count = _previous_analysis(config, document)
        previous_analysis = prior["analysis"] if prior else {}
        previous_state = (
            previous_analysis.get("state")
            if isinstance(previous_analysis, dict)
            else None
        )
        finalized = finalize_investment_analysis(
            facts,
            document=document,
            deterministic_current=deterministic_current,
            deterministic_prior=deterministic_prior,
            market_inputs=market_inputs,
            stored_previous_facts=prior["facts"] if prior else {},
            previous_state=previous_state,
            prior_count=prior_count,
            news_items=news_items,
            extraction=extraction,
            relationship_facts=request.relationship_facts,
            material_relationships=request.material_relationships,
        )
        facts = finalized.facts
        classified_industry = finalized.classified_industry
        document["industry"] = classified_industry

        llm_duration_ms = sum(
            value or 0
            for value in (
                stage.telemetry.first_attempt_duration_ms,
                stage.telemetry.validation_retry_duration_ms,
            )
        )
        duration_ms = max(
            0,
            round((datetime.now(UTC) - started_at).total_seconds() * 1000),
        )
        analysis = {
            **finalized.analysis,
            "llm_usage": {
                "model": stage.policy.model,
                "tokens_input": stage.telemetry.tokens_input_total,
                "tokens_output": stage.telemetry.tokens_output_total,
                "cost_usd": stage.telemetry.cost_usd_total,
                "duration_ms": llm_duration_ms,
            },
            "model": stage.policy.model,
        }
        analysis_id = str(uuid4())
        with get_session(config) as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO investment_analyses (
                        analysis_id, document_id, previous_document_id, facts, analysis,
                        model, tokens_input, tokens_output, cost_usd, duration_ms
                    ) VALUES (
                        :analysis_id, :document_id, :previous_document_id,
                        CAST(:facts AS JSONB), CAST(:analysis AS JSONB), :model,
                        :tokens_input, :tokens_output, :cost_usd, :duration_ms
                    )
                    ON CONFLICT (document_id) DO UPDATE SET
                        previous_document_id = EXCLUDED.previous_document_id,
                        facts = EXCLUDED.facts,
                        analysis = EXCLUDED.analysis,
                        model = EXCLUDED.model,
                        tokens_input = EXCLUDED.tokens_input,
                        tokens_output = EXCLUDED.tokens_output,
                        cost_usd = EXCLUDED.cost_usd,
                        duration_ms = EXCLUDED.duration_ms,
                        updated_at = NOW()
                    RETURNING analysis_id
                    """
                ),
                {
                    "analysis_id": analysis_id,
                    "document_id": document_id,
                    "previous_document_id": prior.get("document_id") if prior else None,
                    "facts": json.dumps(facts),
                    "analysis": json.dumps(analysis),
                    "model": stage.policy.model,
                    "tokens_input": stage.telemetry.tokens_input_total,
                    "tokens_output": stage.telemetry.tokens_output_total,
                    "cost_usd": stage.telemetry.cost_usd_total,
                    "duration_ms": duration_ms,
                },
            ).fetchone()
            session.execute(
                text(
                    "UPDATE investment_documents "
                    "SET status = 'analyzed', industry = :industry, "
                    "report_date = COALESCE(:report_date, report_date), "
                    "error_message = NULL, updated_at = NOW() "
                    "WHERE document_id = :document_id"
                ),
                {
                    "document_id": document_id,
                    "industry": classified_industry,
                    "report_date": document.get("report_date"),
                },
            )
            upsert_report_observation(
                session,
                document,
                facts,
                analysis,
                model=stage.policy.model,
            )
            _record_processing_log(
                session,
                status="success",
                started_at=started_at,
                correlation_id=correlation_id,
                document_id=document_id,
                stage=stage,
                output_id=str(row[0]),
            )
    except Exception as exc:
        with get_session(config) as session:
            session.execute(
                text(
                    "UPDATE investment_documents SET status = 'failed', error_message = 'analysis failed', updated_at = NOW() WHERE document_id = :document_id"
                ),
                {"document_id": document_id},
            )
            _record_processing_log(
                session,
                status="failed",
                started_at=started_at,
                correlation_id=correlation_id,
                document_id=document_id,
                stage=stage,
                error_type=type(exc).__name__,
            )
        raise
    payload = get_analysis(config, str(row[0]))
    if payload is None:
        raise RuntimeError("Investment analysis was stored but could not be read")
    return payload


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


_DERIVED_METRIC_NAMES = frozenset(
    {"fcf", "free_cash_flow", "fcf_margin", "net_margin", "return_on_equity"}
)


def _attach_metric_provenance(analysis: dict, facts: dict) -> dict:
    metrics = analysis.get("metrics")
    if not isinstance(metrics, dict):
        return analysis
    fact_metrics = facts.get("metrics")
    fact_metrics = fact_metrics if isinstance(fact_metrics, dict) else {}
    enriched = {}
    for name, value in metrics.items():
        if not isinstance(value, dict):
            enriched[name] = value
            continue
        record = dict(value)
        fact_record = fact_metrics.get(name)
        if isinstance(fact_record, dict):
            if record.get("source") is None:
                record["source"] = fact_record.get("source")
            if record.get("concept") is None:
                record["concept"] = fact_record.get("concept")
        elif name in _DERIVED_METRIC_NAMES and record.get("source") is None:
            record["source"] = "derived"
        enriched[name] = record
    return {**analysis, "metrics": enriched}


def get_analysis(config: dict, analysis_id: str) -> dict | None:
    with get_session(config) as session:
        row = session.execute(
            text(
                """
                SELECT a.analysis_id, a.document_id, a.previous_document_id,
                       a.facts, a.analysis, a.model, a.created_at, a.updated_at,
                       d.company, d.symbol, d.region, d.industry, d.document_type,
                       d.report_date, d.source_url,
                       p.timestamp AS public_price_timestamp,
                       p.close AS public_price_close,
                       p.source AS public_price_source,
                       p.metadata AS public_price_metadata,
                       p.created_at AS public_price_created_at
                FROM investment_analyses a
                JOIN investment_documents d ON d.document_id = a.document_id
                LEFT JOIN LATERAL (
                    SELECT timestamp, close, source, metadata, created_at
                    FROM market_data
                    WHERE symbol = d.symbol
                      AND source = 'public_equities'
                      AND timeframe = '1d'
                      AND close > 0
                    ORDER BY timestamp DESC, created_at DESC
                    LIMIT 1
                ) p ON TRUE
                WHERE a.analysis_id = :analysis_id
                """
            ),
            {"analysis_id": analysis_id},
        ).fetchone()
    if row is None:
        return None
    payload = _serialize_row(dict(row._mapping))
    facts = _as_json_object(payload.pop("facts", {}))
    analysis = _attach_metric_provenance(
        _as_json_object(payload.pop("analysis", {})),
        facts,
    )
    enriched = _attach_public_market_data({**payload, **analysis}, facts)
    return _apply_deterministic_industry(_attach_analysis_quality(enriched))


def _industry_stage(score: float) -> str:
    if score <= -2:
        return "weakening"
    if score < 2:
        return "monitor"
    if score < 5:
        return "forming"
    if score < 8:
        return "confirmed"
    return "accelerating"


def _analysis_score(payload: dict) -> float:
    state = payload.get("state")
    candidate = state.get("score") if isinstance(state, dict) else payload.get("score")
    parsed = float(candidate) if isinstance(candidate, (int, float)) else 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _latest_company_analyses(analyses: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}

    def quality(payload: dict) -> tuple[bool, str, int, str]:
        extraction = payload.get("extraction")
        extraction = extraction if isinstance(extraction, dict) else {}
        try:
            deterministic_count = int(extraction.get("deterministic_metric_count") or 0)
        except (TypeError, ValueError):
            deterministic_count = 0
        metrics = payload.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        populated_count = sum(
            isinstance(item, dict) and item.get("value") is not None
            for item in metrics.values()
        )
        fact_count = max(deterministic_count, populated_count)
        return (
            fact_count > 0,
            str(payload.get("report_date") or ""),
            fact_count,
            str(payload.get("created_at") or ""),
        )

    for payload in analyses:
        identity = str(payload.get("symbol") or payload.get("company") or "").casefold()
        if not identity:
            continue
        existing = latest.get(identity)
        if existing is None or quality(payload) > quality(existing):
            latest[identity] = payload
    return sorted(
        latest.values(),
        key=lambda payload: (
            quality(payload)[0],
            str(payload.get("report_date") or ""),
            str(payload.get("company") or "").casefold(),
        ),
        reverse=True,
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _currency_code(value: object) -> str | None:
    normalized = str(value or "").strip().upper()
    return next(
        (
            code
            for code in ("USD", "GBP", "EUR", "CHF", "JPY", "CAD", "AUD", "SEK", "NOK")
            if normalized.startswith(code)
        ),
        None,
    )


def _public_market_snapshot(payload: dict) -> dict:
    close = _finite_number(payload.pop("public_price_close", None))
    timestamp = _utc_datetime(payload.pop("public_price_timestamp", None))
    source = str(payload.pop("public_price_source", "") or "public_equities")
    created_at = _utc_datetime(payload.pop("public_price_created_at", None))
    metadata = _as_json_object(payload.pop("public_price_metadata", {}))
    if close is None or close <= 0 or timestamp is None:
        return {
            "status": "unavailable",
            "reason": "public daily close unavailable",
            "source": source,
            "price": None,
            "currency": None,
            "timestamp": None,
            "available_at": created_at.isoformat() if created_at else None,
            "source_reference": metadata.get("source_reference"),
        }

    raw_currency = str(metadata.get("currency") or "").strip()
    quote_scale = (
        0.01 if raw_currency == "GBp" or raw_currency.upper() == "GBX" else 1.0
    )
    currency = "GBP" if quote_scale == 0.01 else _currency_code(raw_currency)
    price = close * quote_scale
    age_days = max(0.0, (datetime.now(UTC) - timestamp).total_seconds() / 86_400.0)
    stale = age_days > 7
    return {
        "status": "stale" if stale else "current",
        "reason": "public daily close is older than seven days" if stale else None,
        "source": source,
        "price": price,
        "raw_price": close,
        "currency": currency,
        "raw_currency": raw_currency or None,
        "quote_scale": quote_scale,
        "timestamp": timestamp.isoformat(),
        "available_at": created_at.isoformat() if created_at else None,
        "age_days": round(age_days, 2),
        "exchange": metadata.get("exchange_name"),
        "provider_symbol": metadata.get("provider_symbol"),
        "source_reference": metadata.get("source_reference"),
        "adjusted": metadata.get("adjusted"),
    }


def _attach_public_market_data(payload: dict, facts: dict) -> dict:
    """Attach a traceable public close and revalue only comparable currencies."""
    enriched = dict(payload)
    market = _public_market_snapshot(enriched)
    valuation = _as_json_object(enriched.get("valuation"))
    report_currency = _currency_code(
        valuation.get("currency_unit")
        or _as_json_object(valuation.get("dcf")).get("unit")
    )
    market["report_currency"] = report_currency
    comparison_status = "comparable"
    if market["status"] == "unavailable":
        comparison_status = "price_unavailable"
    elif market["status"] == "stale":
        comparison_status = "price_stale"
    elif report_currency is None:
        comparison_status = "report_currency_unavailable"
    elif market.get("currency") != report_currency:
        comparison_status = "currency_mismatch"
    market["comparison_status"] = comparison_status

    if comparison_status == "comparable":
        prior_metrics = facts.get("prior_metrics")
        previous_facts = (
            {"metrics": prior_metrics} if isinstance(prior_metrics, Mapping) else None
        )
        stored_metrics = _as_json_object(enriched.get("metrics"))
        stored_dcf = _as_json_object(valuation.get("dcf"))
        stored_assumptions = _as_json_object(
            stored_dcf.get("assumptions") or valuation.get("assumptions")
        )
        market_inputs = {
            "market_price": market["price"],
            "market_price_source": market["source"],
            "market_price_period": market["timestamp"],
            "market_price_evidence": (
                f"{enriched.get('symbol') or enriched.get('company')} "
                f"unadjusted daily close {market['raw_price']} "
                f"{market.get('raw_currency') or market.get('currency')}"
            ),
            "market_price_unit": f"{market['currency']}/share",
        }
        for name in ("shares_outstanding", "net_debt"):
            metric = _as_json_object(stored_metrics.get(name))
            value = _finite_number(stored_assumptions.get(name, metric.get("value")))
            if value is None:
                continue
            market_inputs[name] = value
            market_inputs[f"{name}_source"] = metric.get("source") or "report"
            market_inputs[f"{name}_period"] = metric.get("period") or "filing"
            market_inputs[f"{name}_evidence"] = metric.get("evidence") or (
                f"{name} retained from stored filing analysis"
            )
            market_inputs[f"{name}_unit"] = metric.get("unit")
        discount_rate = _finite_number(
            stored_assumptions.get(
                "discount_rate",
                stored_assumptions.get("wacc"),
            )
        )
        terminal_growth = _finite_number(stored_assumptions.get("terminal_growth"))
        if discount_rate is not None:
            market_inputs["discount_rate"] = discount_rate
        if terminal_growth is not None:
            market_inputs["terminal_growth"] = terminal_growth
        rebuilt = build_deterministic_analysis(
            facts,
            previous_facts=previous_facts,
            market_inputs=market_inputs,
        )
        stored_metrics["market_price"] = rebuilt["metrics"]["market_price"]
        enriched["metrics"] = stored_metrics
        rebuilt_valuation = _as_json_object(rebuilt.get("valuation"))
        if str(stored_dcf.get("status") or "").casefold() == "unavailable":
            for field in (
                "dcf",
                "dcf_per_share",
                "intrinsic_value",
                "margin_of_safety",
                "assumptions",
            ):
                rebuilt_valuation[field] = valuation.get(field)
        valuation = rebuilt_valuation
    valuation["market_data"] = market
    enriched["valuation"] = valuation
    return enriched


def _attach_analysis_quality(payload: dict) -> dict:
    """Expose freshness and completeness facts without collapsing them to a score."""
    enriched = dict(payload)
    now = datetime.now(UTC)
    report_date = None
    raw_report_date = enriched.get("report_date")
    if isinstance(raw_report_date, date):
        report_date = raw_report_date
    elif raw_report_date:
        try:
            report_date = date.fromisoformat(str(raw_report_date)[:10])
        except ValueError:
            report_date = None
    report_age_days = (
        max(0, (now.date() - report_date).days) if report_date is not None else None
    )
    report_status = (
        "current"
        if report_age_days is not None and report_age_days <= 550
        else "stale"
        if report_age_days is not None
        else "unknown"
    )

    analysis_timestamp = _utc_datetime(
        enriched.get("analysis_updated_at")
        or enriched.get("updated_at")
        or enriched.get("created_at")
    )
    analysis_age_days = (
        max(0.0, (now - analysis_timestamp).total_seconds() / 86_400.0)
        if analysis_timestamp is not None
        else None
    )
    extraction = _as_json_object(enriched.get("extraction"))
    extraction_status = str(extraction.get("status") or "unavailable").lower()
    try:
        deterministic_metric_count = int(
            extraction.get("deterministic_metric_count") or 0
        )
    except (TypeError, ValueError):
        deterministic_metric_count = 0
    metrics = _as_json_object(enriched.get("metrics"))
    populated_metric_count = sum(
        isinstance(item, dict) and item.get("value") is not None
        for item in metrics.values()
    )
    evidence = enriched.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    evidence_quote_count = sum(
        isinstance(item, dict) and bool(str(item.get("quote") or "").strip())
        for item in evidence
    )
    valuation = _as_json_object(enriched.get("valuation"))
    dcf = _as_json_object(valuation.get("dcf"))
    sensitivity = _as_json_object(dcf.get("sensitivity"))
    market = _as_json_object(valuation.get("market_data"))
    peer = _as_json_object(enriched.get("peer_comparison"))

    warnings: list[str] = []
    if report_status == "unknown":
        warnings.append("report_date_unavailable")
    elif report_status == "stale":
        warnings.append("annual_report_stale")
    if extraction_status != "success":
        warnings.append("deterministic_extraction_unavailable")
    if evidence_quote_count == 0:
        warnings.append("narrative_evidence_missing")
    comparison_status = str(market.get("comparison_status") or "price_unavailable")
    if comparison_status != "comparable":
        warnings.append(f"market_{comparison_status}")
    dcf_status = str(dcf.get("status") or "unavailable")
    if dcf_status != "calculated":
        warnings.append(f"dcf_{dcf_status}")
    if "peer_comparison" in enriched and not peer.get("members"):
        warnings.append("peer_group_unavailable")

    if report_status == "stale":
        status = "stale"
    elif report_status == "unknown":
        status = (
            "partial"
            if extraction_status == "success" or evidence_quote_count > 0
            else "unavailable"
        )
    elif extraction_status == "success" and evidence_quote_count > 0:
        status = "ready"
    elif extraction_status == "success" or evidence_quote_count > 0:
        status = "partial"
    else:
        status = "unavailable"
    enriched["quality"] = {
        "status": status,
        "report": {
            "status": report_status,
            "report_date": report_date.isoformat() if report_date else None,
            "age_days": report_age_days,
            "source_url": enriched.get("source_url"),
        },
        "analysis": {
            "completed_at": (
                analysis_timestamp.isoformat() if analysis_timestamp else None
            ),
            "age_days": round(analysis_age_days, 2)
            if analysis_age_days is not None
            else None,
        },
        "deterministic": {
            "status": extraction_status,
            "metric_count": max(
                deterministic_metric_count,
                populated_metric_count,
            ),
        },
        "narrative": {
            "evidence_quote_count": evidence_quote_count,
            "driver_count": len(enriched.get("drivers") or []),
            "risk_count": len(enriched.get("risks") or []),
        },
        "market": market,
        "valuation": {
            "dcf_status": dcf_status,
            "sensitivity_status": sensitivity.get("status") or "unavailable",
            "market_relative_available": comparison_status == "comparable",
        },
        "peers": {
            "selected_count": int(peer.get("company_count") or 0),
            "available_count": int(peer.get("industry_company_count") or 0),
        },
        "warnings": warnings,
    }
    return enriched


def _claim_label(value: object, *, risk: bool = False) -> str:
    if isinstance(value, dict):
        value = value.get("inference" if risk else "label")
    return _clean_text(value, limit=160)


def _trend_snapshot(
    trend_series: list[dict], industry_name: str
) -> tuple[dict, dict | None]:
    series = next(
        (
            item
            for item in trend_series
            if isinstance(item, dict)
            and canonicalize_industry(item.get("industry")) == industry_name
        ),
        None,
    )
    points = series.get("points", []) if isinstance(series, dict) else []
    points = [point for point in points if isinstance(point, dict)]
    points.sort(key=lambda point: str(point.get("date") or ""))
    return (
        (points[-1], points[-2] if len(points) > 1 else None) if points else ({}, None)
    )


def _aggregate_industries(
    analyses: list[dict],
    trend_series: list[dict] | None = None,
) -> list[dict]:
    latest = _latest_company_analyses(analyses)
    grouped: dict[str, dict] = {}
    for payload in latest:
        industry_name = canonicalize_industry(payload.get("industry"))
        company_id = str(
            payload.get("symbol") or payload.get("company") or ""
        ).casefold()
        if not company_id:
            continue
        aggregate = grouped.setdefault(
            industry_name,
            {
                "regions": set(),
                "companies": set(),
                "scores": [],
                "drivers": Counter(),
                "risks": Counter(),
                "driver_companies": {},
                "risk_companies": {},
                "deterministic_companies": set(),
            },
        )
        aggregate["regions"].add(str(payload.get("region") or "").upper())
        aggregate["companies"].add(company_id)
        aggregate["scores"].append(_analysis_score(payload))
        extraction = payload.get("extraction")
        extraction_status = (
            extraction.get("status")
            if isinstance(extraction, dict)
            else payload.get("extraction_status")
        )
        if extraction_status == "success":
            aggregate["deterministic_companies"].add(company_id)

        driver_labels = (
            {
                _claim_label(driver)
                for driver in payload.get("drivers", [])
                if _claim_label(driver)
            }
            if isinstance(payload.get("drivers"), list)
            else set()
        )
        risk_labels = (
            {
                _claim_label(risk, risk=True)
                for risk in payload.get("risks", [])
                if _claim_label(risk, risk=True)
            }
            if isinstance(payload.get("risks"), list)
            else set()
        )
        for label in driver_labels:
            aggregate["drivers"][label] += 1
            aggregate["driver_companies"].setdefault(label, set()).add(company_id)
        for label in risk_labels:
            aggregate["risks"][label] += 1
            aggregate["risk_companies"].setdefault(label, set()).add(company_id)

    trend_series = _trend_series(analyses) if trend_series is None else trend_series
    results = []
    for industry_name in ALL_INDUSTRIES:
        aggregate = grouped.get(industry_name)
        companies = aggregate["companies"] if aggregate else set()
        company_count = len(companies)
        scores = aggregate["scores"] if aggregate else []
        score = round(sum(scores) / len(scores), 2) if scores else 0.0
        current_point, prior_point = _trend_snapshot(trend_series, industry_name)
        current_score = _finite_number(current_point.get("score"))
        prior_score = _finite_number(prior_point.get("score")) if prior_point else None
        score_delta = (
            current_score - prior_score
            if current_score is not None and prior_score is not None
            else None
        )
        if score_delta is not None and not math.isfinite(score_delta):
            score_delta = None
        revenue_growth = _finite_number(current_point.get("revenue_growth_pct"))
        fcf_margin = _finite_number(current_point.get("fcf_margin_pct"))
        revenue_count = current_point.get("revenue_growth_company_count", 0)
        fcf_count = current_point.get("fcf_margin_company_count", 0)
        revenue_count = (
            int(revenue_count)
            if isinstance(revenue_count, (int, float)) and revenue_count >= 0
            else 0
        )
        fcf_count = (
            int(fcf_count)
            if isinstance(fcf_count, (int, float)) and fcf_count >= 0
            else 0
        )
        driver_claims = []
        risk_claims = []
        if aggregate and company_count:
            driver_claims = [
                {
                    "label": label,
                    "company_count": len(aggregate["driver_companies"][label]),
                    "breadth_pct": round(
                        len(aggregate["driver_companies"][label]) / company_count * 100,
                        1,
                    ),
                }
                for label, _ in sorted(
                    aggregate["drivers"].items(),
                    key=lambda item: (-item[1], item[0].casefold()),
                )[:4]
            ]
            risk_claims = [
                {
                    "label": label,
                    "company_count": len(aggregate["risk_companies"][label]),
                    "breadth_pct": round(
                        len(aggregate["risk_companies"][label]) / company_count * 100,
                        1,
                    ),
                }
                for label, _ in sorted(
                    aggregate["risks"].items(),
                    key=lambda item: (-item[1], item[0].casefold()),
                )[:3]
            ]
        results.append(
            {
                "name": industry_name,
                "regions": sorted(
                    value
                    for value in (aggregate["regions"] if aggregate else set())
                    if value in REGIONS
                ),
                "company_count": company_count,
                "report_count": len(scores),
                "stage": _industry_stage(score),
                "score": score,
                "breadth_pct": round(
                    sum(value >= 5 for value in scores) / company_count * 100,
                    1,
                )
                if company_count
                else 0.0,
                "drivers": [
                    value
                    for value, _ in sorted(
                        (aggregate["drivers"] if aggregate else {}).items(),
                        key=lambda item: (-item[1], item[0].casefold()),
                    )[:4]
                ],
                "risks": [
                    value
                    for value, _ in sorted(
                        (aggregate["risks"] if aggregate else {}).items(),
                        key=lambda item: (-item[1], item[0].casefold()),
                    )[:3]
                ],
                "momentum": {
                    "score_delta": round(score_delta, 2)
                    if score_delta is not None
                    else None,
                    "current_score": current_score,
                    "prior_score": prior_score,
                    "as_of": current_point.get("date") or None,
                    "prior_as_of": prior_point.get("date") if prior_point else None,
                },
                "fundamentals": {
                    "revenue_growth_pct": revenue_growth,
                    "revenue_growth_company_count": revenue_count,
                    "fcf_margin_pct": fcf_margin,
                    "fcf_margin_company_count": fcf_count,
                },
                "driver_claims": driver_claims,
                "risk_claims": risk_claims,
                "deterministic_company_count": (
                    len(aggregate["deterministic_companies"]) if aggregate else 0
                ),
                "deterministic_coverage_pct": round(
                    len(aggregate["deterministic_companies"]) / company_count * 100,
                    1,
                )
                if aggregate and company_count
                else 0.0,
            }
        )
    results.sort(key=lambda item: (-item["score"], item["name"].casefold()))
    return results


def _aggregate_regions(analyses: list[dict]) -> list[dict]:
    configured_counts = configured_region_counts()
    grouped = {region: [] for region in configured_counts}
    for payload in _latest_company_analyses(analyses):
        region = str(payload.get("region") or "").upper()
        if region in grouped:
            grouped[region].append(_analysis_score(payload))
    results = []
    for region, configured_company_count in configured_counts.items():
        scores = grouped[region]
        is_configured = configured_company_count > 0
        score = (
            round(sum(scores) / len(scores), 2)
            if scores and is_configured
            else (0.0 if is_configured else None)
        )
        results.append(
            {
                "code": region,
                "company_count": len(scores),
                "configured_company_count": configured_company_count,
                "coverage_status": (
                    "configured" if is_configured else "not_configured"
                ),
                "score": score,
                "stage": _industry_stage(score) if score is not None else None,
            }
        )
    return results


def _trend_series(analyses: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for payload in analyses:
        raw_date = payload.get("report_date")
        if not raw_date:
            continue
        report_date = str(raw_date)[:10]
        industry = canonicalize_industry(payload.get("industry"))
        bucket = grouped.setdefault(
            (industry, report_date),
            {
                "scores": [],
                "revenue_growth": [],
                "revenue_growth_companies": set(),
                "fcf_margin": [],
                "fcf_margin_companies": set(),
                "companies": set(),
            },
        )
        bucket["scores"].append(_analysis_score(payload))
        company_id = str(
            payload.get("symbol") or payload.get("company") or ""
        ).casefold()
        if company_id:
            bucket["companies"].add(company_id)
        metrics = payload.get("metrics", {})
        if not isinstance(metrics, dict):
            continue
        for metric_name, target, count_target in (
            ("revenue", "revenue_growth", "revenue_growth_companies"),
            ("fcf_margin", "fcf_margin", "fcf_margin_companies"),
        ):
            metric = metrics.get(metric_name, {})
            if not isinstance(metric, dict):
                continue
            field = "change_pct" if metric_name == "revenue" else "value"
            value = _finite_number(metric.get(field))
            if value is not None:
                bucket[target].append(value)
                if company_id:
                    bucket[count_target].add(company_id)

    by_industry: dict[str, list[dict]] = {}
    for (industry, report_date), bucket in grouped.items():
        scores = bucket["scores"]
        revenue_growth = bucket["revenue_growth"]
        fcf_margin = bucket["fcf_margin"]
        by_industry.setdefault(industry, []).append(
            {
                "date": report_date,
                "score": round(sum(scores) / len(scores), 2) if scores else 0.0,
                "company_count": len(bucket["companies"]),
                "revenue_growth_pct": (
                    round(sum(revenue_growth) / len(revenue_growth), 2)
                    if revenue_growth
                    else None
                ),
                "revenue_growth_company_count": len(bucket["revenue_growth_companies"]),
                "fcf_margin_pct": (
                    round(sum(fcf_margin) / len(fcf_margin), 2) if fcf_margin else None
                ),
                "fcf_margin_company_count": len(bucket["fcf_margin_companies"]),
            }
        )
    return [
        {
            "industry": industry,
            "points": sorted(points, key=lambda item: item["date"])[-24:],
        }
        for industry, points in sorted(by_industry.items())
    ]


def _news_monitoring(news_items: list[dict]) -> list[dict]:
    now = datetime.now(UTC).timestamp()
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for item in news_items:
        published = published_timestamp(item)
        if published is None:
            continue
        age_days = max(0.0, (now - published) / 86_400)
        window = "recent" if age_days <= 3 else "prior" if age_days <= 7 else None
        if window is None:
            continue
        labels = [("theme", value) for value in item.get("themes", [])] + [
            ("industry", canonicalize_industry(value))
            for value in item.get("industries", [])
        ]
        for scope, label in labels:
            bucket = buckets.setdefault(
                (scope, label),
                {"recent": 0, "prior": 0},
            )
            bucket[window] += 1
    results = [
        {
            "scope": scope,
            "label": label,
            "recent_count": values["recent"],
            "prior_count": values["prior"],
            "momentum": values["recent"] - values["prior"],
            "stage": (
                "forming"
                if values["prior"] == 0 and values["recent"] > 0
                else "accelerating"
                if values["recent"] >= 3 and values["recent"] > values["prior"]
                else "forming"
                if values["recent"] > values["prior"]
                else "cooling"
                if values["recent"] < values["prior"]
                else "stable"
            ),
        }
        for (scope, label), values in buckets.items()
    ]
    results.sort(
        key=lambda item: (
            -item["momentum"],
            -item["recent_count"],
            item["label"].casefold(),
        )
    )
    return results[:30]


def _peer_metric_value(payload: dict, name: str) -> float | None:
    fundamentals = payload.get("fundamentals")
    fundamentals = fundamentals if isinstance(fundamentals, dict) else {}
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    if name == "revenue_growth_pct":
        metric = metrics.get("revenue")
        value = metric.get("change_pct") if isinstance(metric, dict) else None
    elif name == "revenue_value":
        metric = metrics.get("revenue")
        value = metric.get("value") if isinstance(metric, dict) else None
    elif name == "fcf_margin_pct":
        metric = metrics.get("fcf_margin")
        value = metric.get("value") if isinstance(metric, dict) else None
    else:
        value = fundamentals.get(name)
    return _finite_number(value)


def _peer_currency(payload: dict) -> str | None:
    valuation = payload.get("valuation")
    valuation = valuation if isinstance(valuation, dict) else {}
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    revenue = metrics.get("revenue")
    revenue = revenue if isinstance(revenue, dict) else {}
    return _currency_code(valuation.get("currency_unit") or revenue.get("unit"))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    return (
        ordered[midpoint]
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    )


def _peer_distance(subject: dict, candidate: dict) -> tuple[float, list[str]]:
    components: list[float] = []
    financial_components = 0
    reasons = ["same canonical industry"]
    subject_region = str(subject.get("region") or "").upper()
    candidate_region = str(candidate.get("region") or "").upper()
    if subject_region and candidate_region:
        same_region = subject_region == candidate_region
        components.append(0.0 if same_region else 0.5)
        reasons.append(
            f"{'same' if same_region else 'different'} reporting region "
            f"({candidate_region})"
        )

    for name, floor in (
        ("revenue_growth_pct", 10.0),
        ("fcf_margin_pct", 10.0),
        ("net_margin_pct", 10.0),
        ("return_on_equity_pct", 15.0),
        ("debt_to_equity", 1.0),
        ("capex_to_revenue_pct", 10.0),
    ):
        subject_value = _peer_metric_value(subject, name)
        candidate_value = _peer_metric_value(candidate, name)
        if subject_value is None or candidate_value is None:
            continue
        scale = max(abs(subject_value), abs(candidate_value), floor)
        components.append(min(2.0, abs(subject_value - candidate_value) / scale))
        financial_components += 1
        reasons.append(f"{name} {candidate_value:.2f} vs subject {subject_value:.2f}")

    subject_revenue = _peer_metric_value(subject, "revenue_value")
    candidate_revenue = _peer_metric_value(candidate, "revenue_value")
    subject_currency = _peer_currency(subject)
    candidate_currency = _peer_currency(candidate)
    if (
        subject_revenue is not None
        and subject_revenue > 0
        and candidate_revenue is not None
        and candidate_revenue > 0
        and subject_currency is not None
        and subject_currency == candidate_currency
    ):
        ratio = max(subject_revenue, candidate_revenue) / min(
            subject_revenue, candidate_revenue
        )
        components.append(min(2.0, abs(math.log10(ratio))))
        financial_components += 1
        reasons.append(f"revenue scale {ratio:.2f}x in {subject_currency}")

    if financial_components < 3:
        reasons.append(
            f"limited comparable financial metrics ({financial_components}/7)"
        )
        return 10.0, reasons
    missing_penalty = (7 - financial_components) * 0.15
    if missing_penalty:
        reasons.append(
            f"missing-data penalty for {7 - financial_components} absent metrics"
        )
    return sum(components) / len(components) + missing_penalty, reasons


def _peer_comparisons(analyses: list[dict]) -> dict[str, dict]:
    latest = _latest_company_analyses(analyses)
    grouped: dict[str, list[dict]] = {}
    for payload in latest:
        grouped.setdefault(canonicalize_industry(payload.get("industry")), []).append(
            payload
        )
    metric_names = (
        "revenue_growth_pct",
        "fcf_margin_pct",
        "net_margin_pct",
        "return_on_equity_pct",
        "debt_to_equity",
        "capex_to_revenue_pct",
    )
    output: dict[str, dict] = {}
    for payload in latest:
        company_id = str(
            payload.get("symbol") or payload.get("company") or ""
        ).casefold()
        industry = canonicalize_industry(payload.get("industry"))
        pool = [
            peer
            for peer in grouped.get(industry, [])
            if str(peer.get("symbol") or peer.get("company") or "").casefold()
            != company_id
        ]
        ranked_peers = []
        for peer in pool:
            distance, reasons = _peer_distance(payload, peer)
            ranked_peers.append(
                (
                    distance,
                    str(peer.get("company") or "").casefold(),
                    peer,
                    reasons,
                )
            )
        ranked_peers.sort(key=lambda item: (item[0], item[1]))
        selected = ranked_peers[:8]

        metrics_output = {}
        for name in metric_names:
            values = [
                value
                for _, _, peer, _ in selected
                if (value := _peer_metric_value(peer, name)) is not None
            ]
            sample_count = len(values)
            value = _peer_metric_value(payload, name)
            median = _median(values)
            delta = value - median if value is not None and median is not None else None
            percentile = None
            if value is not None and sample_count >= 2:
                less = sum(peer_value < value for peer_value in values)
                equal = sum(peer_value == value for peer_value in values)
                percentile = round((less + equal * 0.5) / sample_count * 100, 1)
            leave_one_out = (
                [
                    _median(values[:index] + values[index + 1 :])
                    for index in range(sample_count)
                ]
                if sample_count >= 3
                else []
            )
            leave_one_out = [item for item in leave_one_out if item is not None]
            metrics_output[name] = {
                "value": value,
                "median": median,
                "delta": delta,
                "percentile": percentile,
                "sample_count": sample_count,
                "median_leave_one_out_min": min(leave_one_out)
                if leave_one_out
                else None,
                "median_leave_one_out_max": max(leave_one_out)
                if leave_one_out
                else None,
            }
        output[company_id] = {
            "industry": industry,
            "company_count": len(selected),
            "industry_company_count": len(pool),
            "excluded_count": max(0, len(pool) - len(selected)),
            "construction": (
                "Up to eight nearest same-industry issuers ranked by reporting "
                "region, growth, profitability, leverage, capital intensity, "
                "and same-currency revenue scale; the subject is excluded."
            ),
            "members": [
                {
                    "company": peer.get("company"),
                    "symbol": peer.get("symbol"),
                    "region": peer.get("region"),
                    "distance": round(distance, 4),
                    "reasons": reasons,
                }
                for distance, _, peer, reasons in selected
            ],
            "metrics": metrics_output,
        }
    return output


def _attach_peer_comparisons(analyses: list[dict]) -> list[dict]:
    comparisons = _peer_comparisons(analyses)
    latest = _latest_company_analyses(analyses)
    attached = []
    for payload in latest:
        company_id = str(
            payload.get("symbol") or payload.get("company") or ""
        ).casefold()
        attached.append({**payload, "peer_comparison": comparisons.get(company_id)})
    return attached


def _attach_recent_updates(
    annual_analyses: list[dict],
    all_analyses: list[dict],
) -> list[dict]:
    updates: dict[str, list[dict]] = {}
    for item in all_analyses:
        if item.get("document_type") == "annual_report":
            continue
        identity = str(item.get("symbol") or item.get("company") or "").casefold()
        if not identity or len(updates.setdefault(identity, [])) >= 3:
            continue
        updates[identity].append(
            {
                key: item.get(key)
                for key in (
                    "analysis_id",
                    "document_id",
                    "document_type",
                    "report_date",
                    "summary",
                    "thesis",
                    "drivers",
                    "risks",
                    "catalysts",
                    "relationship_facts",
                    "material_relationships",
                    "relationship_reconciliations",
                    "watch_items",
                    "news_context",
                )
            }
        )
    return [
        {
            **item,
            "recent_updates": updates.get(
                str(item.get("symbol") or item.get("company") or "").casefold(),
                [],
            ),
        }
        for item in annual_analyses
    ]


def _valuation_coverage(analyses: list[dict]) -> dict[str, int]:
    counts = {
        "dcf_calculated_count": 0,
        "dcf_enterprise_value_only_count": 0,
        "dcf_unavailable_count": 0,
        "market_price_count": 0,
        "pe_ratio_count": 0,
        "margin_of_safety_count": 0,
    }
    for analysis in analyses:
        valuation = analysis.get("valuation")
        valuation = valuation if isinstance(valuation, dict) else {}
        dcf = valuation.get("dcf")
        dcf = dcf if isinstance(dcf, dict) else {}

        per_share = _finite_number(dcf.get("per_share", valuation.get("dcf_per_share")))
        enterprise_value = _finite_number(dcf.get("enterprise_value"))
        status = str(dcf.get("status") or "").lower()
        if status not in {"calculated", "enterprise_value_only", "unavailable"}:
            status = (
                "calculated"
                if per_share is not None
                else "enterprise_value_only"
                if enterprise_value is not None
                else "unavailable"
            )
        counts[f"dcf_{status}_count"] += 1

        market_price = _finite_number(valuation.get("market_price"))
        if market_price is None:
            metrics = analysis.get("metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            price_metric = metrics.get("market_price")
            if isinstance(price_metric, dict):
                price_metric = price_metric.get("value")
            market_price = _finite_number(price_metric)
        has_market_price = market_price is not None and market_price > 0
        if not has_market_price:
            continue

        counts["market_price_count"] += 1
        pe_ratio = _finite_number(valuation.get("pe", valuation.get("pe_ratio")))
        if pe_ratio is not None:
            counts["pe_ratio_count"] += 1
        margin_of_safety = _finite_number(valuation.get("margin_of_safety"))
        if margin_of_safety is not None and per_share is not None and per_share > 0:
            counts["margin_of_safety_count"] += 1
    return counts


def get_dashboard(config: dict) -> dict:
    with get_session(config) as session:
        document_rows = session.execute(
            text(
                """
                SELECT document_id, company, symbol, region, industry, document_type,
                       report_date, source_url, filename, status, error_message, created_at
                FROM investment_documents
                ORDER BY report_date DESC NULLS LAST, created_at DESC
                LIMIT 100
                """
            )
        ).fetchall()
        company_rows = session.execute(
            text(
                """
                SELECT DISTINCT ON (COALESCE(symbol, LOWER(company)))
                       company, symbol, industry, region
                FROM investment_documents
                ORDER BY COALESCE(symbol, LOWER(company)), created_at DESC
                """
            )
        ).fetchall()
        analysis_rows = session.execute(
            text(
                """
                SELECT a.analysis_id, a.document_id, a.facts, a.analysis, a.model,
                       a.tokens_input, a.tokens_output, a.cost_usd, a.duration_ms,
                       a.created_at, a.updated_at AS analysis_updated_at,
                       d.company, d.symbol, d.region, d.industry,
                       d.document_type, d.report_date, d.source_url,
                       p.timestamp AS public_price_timestamp,
                       p.close AS public_price_close,
                       p.source AS public_price_source,
                       p.metadata AS public_price_metadata,
                       p.created_at AS public_price_created_at
                FROM investment_analyses a
                JOIN investment_documents d ON d.document_id = a.document_id
                LEFT JOIN LATERAL (
                    SELECT timestamp, close, source, metadata, created_at
                    FROM market_data
                    WHERE symbol = d.symbol
                      AND source = 'public_equities'
                      AND timeframe = '1d'
                      AND close > 0
                    ORDER BY timestamp DESC, created_at DESC
                    LIMIT 1
                ) p ON TRUE
                ORDER BY d.report_date DESC NULLS LAST, a.created_at DESC
                """
            )
        ).fetchall()
        observation_rows = session.execute(
            text(
                """
                SELECT source_kind, source_id, observed_at, industry, company,
                       symbol, region, metrics, narrative, themes, score, state,
                       provenance
                FROM investment_research_observations
                ORDER BY observed_at DESC, source_kind, source_id
                LIMIT 5000
                """
            )
        ).fetchall()

    documents = [
        _apply_deterministic_industry(_serialize_row(dict(row._mapping)))
        for row in document_rows
    ]
    analyses = []
    for row in analysis_rows:
        base = _serialize_row(dict(row._mapping))
        facts = _as_json_object(base.pop("facts", {}))
        analysis = _attach_metric_provenance(
            _as_json_object(base.pop("analysis", {})),
            facts,
        )
        enriched = _attach_public_market_data({**base, **analysis}, facts)
        analyses.append(_apply_deterministic_industry(enriched))
    annual_analyses = [
        item for item in analyses if item.get("document_type") == "annual_report"
    ]
    company_universe = [_serialize_row(dict(row._mapping)) for row in company_rows]
    classified_news = load_classified_news(config, company_universe, limit=200)
    news_items = [
        item
        for item in classified_news
        if item.get("companies") or item.get("industries")
    ]
    trend_series = _trend_series(annual_analyses)
    industry_list = _aggregate_industries(annual_analyses, trend_series)
    news_by_industry = Counter(
        canonicalize_industry(industry)
        for item in news_items
        for industry in item.get("industries", [])
    )
    for industry in industry_list:
        industry["news_count"] = news_by_industry.get(industry["name"], 0)
    latest_annual = [
        _attach_analysis_quality(item)
        for item in _attach_peer_comparisons(annual_analyses)[:300]
    ]
    latest_analyses = _attach_recent_updates(latest_annual, analyses)
    total_cost = sum(float(item.get("cost_usd") or 0) for item in annual_analyses)
    durations = [
        int(item["duration_ms"])
        for item in annual_analyses
        if isinstance(item.get("duration_ms"), (int, float))
    ]
    deterministic_count = sum(
        1
        for item in latest_annual
        if isinstance(item.get("extraction"), dict)
        and item["extraction"].get("status") == "success"
    )
    valuation_coverage = _valuation_coverage(latest_annual)
    quality_by_status = dict(
        Counter(
            str(_as_json_object(item.get("quality")).get("status") or "unknown")
            for item in latest_annual
        )
    )
    observations = [dict(row._mapping) for row in observation_rows]
    industry_history = aggregate_industry_history(observations)
    configured_model = (
        config.get("llm", {}).get("models", {}).get("investment_analysis") or MODEL_ID
    )
    return {
        "model": configured_model,
        "regions": _aggregate_regions(annual_analyses),
        "sources": list(FREE_REPORT_SOURCES),
        "industries": industry_list,
        "trend_series": trend_series,
        "industry_history": industry_history,
        "emerging_trends": _news_monitoring(classified_news),
        "news": news_items[:100],
        "research_summary": {
            "company_count": len(latest_annual),
            "analysis_count": len(latest_annual),
            "historical_report_count": len(annual_analyses),
            "update_analysis_count": len(analyses) - len(annual_analyses),
            "deterministic_analysis_count": deterministic_count,
            "history_point_count": sum(
                len(item.get("points", [])) for item in industry_history
            ),
            "missing_deterministic_analysis_count": (
                len(latest_annual) - deterministic_count
            ),
            "llm_cost_usd": round(total_cost, 6),
            "average_duration_ms": (
                round(sum(durations) / len(durations)) if durations else 0
            ),
            "valuation_coverage": valuation_coverage,
            "quality_by_status": quality_by_status,
        },
        "documents": documents,
        "analyses": latest_analyses,
    }
