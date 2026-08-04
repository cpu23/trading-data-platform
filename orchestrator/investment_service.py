import hashlib
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import warnings
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePath
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from sqlalchemy import text

from budgets import enforce_budget
from db import get_session
from http_client import get_shared_client, make_request
from investment_engine import build_deterministic_analysis
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
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger

logger = get_logger("investment.analysis")


class AnalysisInProgress(RuntimeError):
    pass


MAX_DOCUMENT_BYTES = 20_000_000
MAX_EXTRACTED_CHARS = 1_000_000
MAX_REGULATORY_DOCUMENT_BYTES = 100_000_000
MAX_OCR_PAGES = 500
OCR_WORKERS = 2
MAX_ANALYSIS_CHARS = 120_000
MAX_REDIRECTS = 4
MODEL_ID = "openai/gpt-5.6-luna"
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


def _pdf_reader(content: bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("encrypted PDF is not supported") from exc
    return reader


def _ocr_pdf_page(pdf_path: Path, output_dir: Path, page_number: int, dpi: int) -> str:
    prefix = output_dir / f"page-{page_number}-{dpi}"
    image_path = prefix.with_suffix(".pgm")
    try:
        subprocess.run(
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
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        result = subprocess.run(
            [
                "tesseract",
                str(image_path),
                "stdout",
                "--psm",
                "6",
                "-c",
                "preserve_interword_spaces=1",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        image_path.unlink(missing_ok=True)


def _ocr_pdf(content: bytes, page_count: int) -> str:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    page_count = min(page_count, MAX_OCR_PAGES)
    if page_count <= 220:
        initial_pages = set(range(1, page_count + 1))
    else:
        initial_pages = set(range(1, 71))
        initial_pages.update(range(max(1, page_count - 89), page_count + 1))
        initial_pages.update(range(71, max(72, page_count - 89), 4))
    with tempfile.TemporaryDirectory(prefix="investment-ocr-") as directory:
        output_dir = Path(directory)
        pdf_path = output_dir / "report.pdf"
        pdf_path.write_bytes(content)
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
            first_pass = dict(
                zip(
                    sorted(initial_pages),
                    executor.map(
                        lambda page: _ocr_pdf_page(pdf_path, output_dir, page, 140),
                        sorted(initial_pages),
                    ),
                    strict=False,
                )
            )
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
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
            focused_text = dict(
                zip(
                    sorted(focused_pages),
                    executor.map(
                        lambda page: _ocr_pdf_page(pdf_path, output_dir, page, 180),
                        sorted(focused_pages),
                    ),
                    strict=False,
                )
            )
        first_pass.update(
            {page: value for page, value in focused_text.items() if value}
        )
    return "\n\n".join(
        f"[Page {page}]\n{page_text}"
        for page, page_text in sorted(first_pass.items())
        if page_text
    )


def _extract_pdf(content: bytes) -> str:
    reader = _pdf_reader(content)
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        if page_number > MAX_OCR_PAGES:
            break
        extracted = page.extract_text() or ""
        if extracted.strip():
            page_text = f"\n[Page {page_number}]\n{extracted}"
            pages.append(page_text)
    direct_text = "".join(pages)
    if len(direct_text.strip()) >= 100:
        return direct_text
    return _ocr_pdf(content, len(reader.pages))


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


def _extract_report_package(content: bytes) -> str:
    sections = []
    total_uncompressed = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
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


def _extract_docx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
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


def extract_document_text(
    content: bytes,
    filename: str,
    mime_type: str | None,
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> str:
    if not content:
        raise ValueError("document is empty")
    if len(content) > max_bytes:
        raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")

    content_type = (mime_type or "").split(";", 1)[0].strip().lower()
    suffix = PurePath(filename.lower()).suffix
    if content.startswith(b"%PDF-"):
        extracted = _extract_pdf(content)
    elif content.startswith(b"PK\x03\x04"):
        if (
            content_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or suffix == ".docx"
        ):
            extracted = _extract_docx(content)
        else:
            extracted = _extract_report_package(content)
    elif content_type == "application/pdf" or suffix == ".pdf":
        extracted = _extract_pdf(content)
    elif (
        content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        extracted = _extract_docx(content)
    elif (
        content_type in {"application/zip", "application/x-zip-compressed"}
        or suffix == ".zip"
    ):
        extracted = _extract_report_package(content)
    elif suffix in {".xml", ".xsd"} or content_type in {"application/xml", "text/xml"}:
        extracted = content.decode("utf-8", errors="replace")
    elif content_type in {"text/html", "application/xhtml+xml"} or suffix in {
        ".html",
        ".htm",
    }:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(content, "lxml")
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
        extracted = content.decode("utf-8", errors="replace")
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


def build_analysis_excerpt(document_text: str) -> str:
    if len(document_text) <= MAX_ANALYSIS_CHARS:
        return document_text

    prefix_end = 24_000
    suffix_start = len(document_text) - 28_000
    windows = [(0, prefix_end), (suffix_start, len(document_text))]
    remaining = MAX_ANALYSIS_CHARS - 56_000
    for match in _ANALYSIS_KEYWORDS.finditer(document_text):
        if remaining <= 0:
            break
        start = max(prefix_end, match.start() - 1_600)
        end = min(suffix_start, match.end() + 3_400)
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
    )[:MAX_ANALYSIS_CHARS]


def _validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("source URL must be a public HTTP(S) URL")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ValueError("source URL hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("source URL hostname could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(
                "source URL must not resolve to a private or reserved address"
            )
    return value


def fetch_document_url(url: str) -> tuple[bytes, str, str, str]:
    current = _validate_public_url(url)
    client = get_shared_client()
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream(
            "GET",
            current,
            headers={
                "User-Agent": "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"
            },
            timeout=30.0,
            follow_redirects=False,
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("source URL redirected without a location")
                current = _validate_public_url(urljoin(current, location))
                continue
            response.raise_for_status()
            declared = response.headers.get("content-length")
            try:
                declared_size = int(declared) if declared else None
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > MAX_DOCUMENT_BYTES:
                raise ValueError("remote document exceeds 20 MB")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_DOCUMENT_BYTES:
                    raise ValueError("remote document exceeds 20 MB")
                chunks.append(chunk)
            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            path_name = PurePath(unquote(urlsplit(current).path)).name
            filename = path_name or "remote-report"
            return b"".join(chunks), filename, content_type, current
    raise ValueError("source URL redirected too many times")


def store_document(
    config: dict,
    metadata: dict,
    content: bytes,
    mime_type: str | None,
    *,
    preserve_content: bool = False,
    allow_unextractable: bool = False,
) -> dict:
    normalized = normalize_metadata(metadata)
    try:
        extracted = extract_document_text(
            content,
            normalized["filename"],
            mime_type,
            max_bytes=(
                MAX_REGULATORY_DOCUMENT_BYTES
                if preserve_content
                else MAX_DOCUMENT_BYTES
            ),
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
    digest = hashlib.sha256(content).hexdigest()
    params = {
        **normalized,
        "report_date": normalized["report_date"],
        "mime_type": (mime_type or "application/octet-stream").split(";", 1)[0][:120],
        "content_sha256": digest,
        "extracted_text": extracted,
        "raw_content": content if preserve_content else None,
    }
    statement = text(
        """
        INSERT INTO investment_documents (
            company, symbol, region, industry, document_type, report_date,
            source_url, filing_source, filing_id, filename, mime_type,
            content_sha256, extracted_text, raw_content
        ) VALUES (
            :company, :symbol, :region, :industry, :document_type, :report_date,
            :source_url, :filing_source, :filing_id, :filename, :mime_type,
            :content_sha256, :extracted_text, :raw_content
        )
        ON CONFLICT (content_sha256) DO UPDATE SET
            filing_source = COALESCE(investment_documents.filing_source, EXCLUDED.filing_source),
            filing_id = COALESCE(investment_documents.filing_id, EXCLUDED.filing_id),
            raw_content = COALESCE(investment_documents.raw_content, EXCLUDED.raw_content),
            updated_at = NOW()
        RETURNING document_id, company, symbol, region, industry, document_type,
                  report_date, source_url, filing_source, filing_id, filename,
                  mime_type, status, created_at
        """
    )
    with get_session(config) as session:
        row = session.execute(statement, params).fetchone()
    return _serialize_row(dict(row._mapping))


def store_document_url(config: dict, metadata: dict) -> dict:
    requested_url = _clean_text(metadata.get("url"), limit=2048)
    if not requested_url:
        raise ValueError("url is required")
    content, filename, mime_type, final_url = fetch_document_url(requested_url)
    enriched = {
        **metadata,
        "filename": metadata.get("filename") or filename,
        "source_url": final_url,
    }
    return store_document(config, enriched, content, mime_type)


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
        "risk": {"type": "string"},
        "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
        "impact": {"type": "string", "enum": ["low", "medium", "high"]},
        "mitigation": {"type": "string"},
        "evidence": {"type": "string"},
    }
    catalyst_properties = {
        "catalyst": {"type": "string"},
        "horizon": {"type": "string"},
        "evidence": {"type": "string"},
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
            "drivers": {"type": "array", "items": {"type": "string"}},
            "catalysts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": catalyst_properties,
                    "required": list(catalyst_properties),
                },
            },
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": risk_properties,
                    "required": list(risk_properties),
                },
            },
            "watch_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "classification",
            "qualitative",
            "summary",
            "thesis",
            "drivers",
            "catalysts",
            "risks",
            "watch_items",
        ],
    }
    return {"name": "investment_report_narrative", "strict": True, "schema": schema}


def _load_news_context(config: dict, metadata: dict) -> list[dict]:
    industry = canonicalize_industry(metadata.get("industry"))
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


def _ensure_extracted_text(config: dict, document: dict) -> str:
    existing = str(document.get("extracted_text") or "")
    if len(existing.strip()) >= 100:
        _correct_interim_document_type(config, document, existing)
        return "stored_document"
    raw_content = document.get("raw_content")
    if not isinstance(raw_content, (bytes, bytearray, memoryview)):
        return "missing_report_text"
    try:
        extracted = extract_document_text(
            bytes(raw_content),
            str(document.get("filename") or "report"),
            str(document.get("mime_type") or "application/octet-stream"),
            max_bytes=MAX_REGULATORY_DOCUMENT_BYTES,
        )
    except Exception as exc:
        logger.warning(
            "regulatory_document_extraction_failed",
            document_id=str(document.get("document_id") or ""),
            error_type=type(exc).__name__,
        )
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


def _load_report_excerpt(config: dict, document: dict) -> tuple[str, str]:
    """Use the primary SEC filing for legacy bundles that lost file priority."""
    existing = build_analysis_excerpt(document.get("extracted_text") or "")
    if document.get("filing_source") != "sec_edgar" or document.get("raw_content"):
        return existing, "stored_document"
    source_url = str(document.get("source_url") or "")
    if not source_url.startswith("https://www.sec.gov/Archives/edgar/data/"):
        return existing, "stored_document"
    user_agent = config.get("investment_filings", {}).get(
        "sec_user_agent",
        "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)",
    )
    try:
        index_response = make_request(
            "GET",
            source_url.rstrip("/") + "/index.json",
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            max_retries=2,
            client=get_shared_client(),
        )
        index_response.raise_for_status()
        items = index_response.json().get("directory", {}).get("item", [])
        candidates = []
        for item in items if isinstance(items, list) else []:
            name = PurePath(str(item.get("name") or "")).name
            if not name.lower().endswith((".htm", ".html")):
                continue
            lowered = name.lower()
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
        _, primary_name = max(candidates)
        response = make_request(
            "GET",
            source_url.rstrip("/") + "/" + primary_name,
            headers={"User-Agent": user_agent, "Accept": "text/html"},
            timeout=90.0,
            max_retries=2,
            client=get_shared_client(),
        )
        response.raise_for_status()
        if len(response.content) > MAX_DOCUMENT_BYTES:
            return existing, "stored_document"
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
) -> str:
    news_text = json.dumps(news_items, ensure_ascii=False, sort_keys=True)
    deterministic_text = json.dumps(
        deterministic_metrics,
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"""You are a professional buy-side analyst reviewing report narrative.
Return only the strict JSON response. Use the exact model schema supplied by the caller.

Rules:
- Financial figures are extracted and calculated by deterministic code. Do not return metric objects, infer a missing figure, recalculate a value, or contradict the supplied deterministic facts.
- The filing is authoritative for company statements. Use short verbatim filing evidence for every present qualitative signal, catalyst, and risk.
- Qualitative signals are present only when explicitly supported. AI demand and data-centre demand are distinct.
- Separate company-stated facts from your interpretation. Summary and thesis must identify uncertainty and what would invalidate the thesis.
- Catalysts need a concrete horizon and filing evidence. Each risk needs a practical company, portfolio, or monitoring mitigation; say `No company mitigation stated; monitor ...` when necessary.
- News is separate context. It may inform a catalyst, external risk, or crowding check, but never present news wording as filing evidence.
- When the filing excerpt has no relevant narrative evidence, return an explicit insufficient-evidence summary and empty optional arrays rather than generic investment commentary.

Metadata:
{json.dumps({key: str(value) if value is not None else None for key, value in document.items() if key not in {"extracted_text", "raw_content"}}, sort_keys=True)}

Deterministic filing facts (read-only context):
{deterministic_text}

Related classified news (separate context):
{news_text}

FILING EXCERPT:
{excerpt}
"""


def _parse_llm_json(content: object) -> dict:
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
    for key in ("classification", "qualitative"):
        if not isinstance(parsed.get(key), dict):
            raise ValueError(f"LLM response missing {key}")
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
                item = item.get("label") or item.get("driver") or item.get("risk")
            cleaned = _clean_text(item, limit=400)
            marker = cleaned.casefold()
            if cleaned and marker not in seen:
                seen.add(marker)
                values.append(cleaned)
            if len(values) >= limit:
                return values
    return values


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
                    "rule_version": "1",
                    "structured_response": True,
                    "validation_warnings": (
                        telemetry.validation_warnings if telemetry is not None else []
                    ),
                }
            ),
        },
    )


def analyze_document(
    config: dict, document_id: str, market_inputs: dict | None = None
) -> dict:
    document = _load_document(config, document_id)
    if document is None:
        raise LookupError("investment document not found")
    enforce_budget(config, "investment_analysis")

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

    correlation_id = str(uuid4())
    started_at = datetime.now(UTC)
    stage = None
    try:
        prepared_text_source = _ensure_extracted_text(config, document)
        news_items = _load_news_context(config, document)
        deterministic_current, deterministic_prior, extraction = (
            load_deterministic_facts(config, document)
        )
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
        stage = LLMStage(
            config,
            "investment_analysis",
            correlation_id=correlation_id,
            response_schema=_response_schema(),
        )
        prompt = _build_prompt(
            document,
            excerpt,
            news_items,
            {
                "metrics": deterministic_current,
                "prior_metrics": deterministic_prior,
            },
        )
        result = stage.call(prompt)
        try:
            facts = _parse_llm_json(result.get("content"))
        except (ValueError, json.JSONDecodeError) as exc:
            stage.add_validation_warnings(["response was not valid investment JSON"])
            if stage.policy.validation_retries < 1:
                raise LLMValidationError(
                    "Investment response validation failed", stage.telemetry
                ) from exc
            result = stage.call(
                prompt
                + "\nCORRECTION: Return one valid JSON object matching the narrative schema exactly. Do not add financial metric fields."
            )
            facts = _parse_llm_json(result.get("content"))
        facts = _merge_metric_facts(
            facts,
            deterministic_current,
            deterministic_prior,
        )

        classification = facts["classification"]
        classified_industry = canonicalize_industry(
            classification.get("industry") or document.get("industry")
        )
        classification["industry"] = classified_industry
        classification["region"] = document["region"]
        classification["document_type"] = document["document_type"]
        document["industry"] = classified_industry

        prior, prior_count = _previous_analysis(config, document)
        previous_facts = prior["facts"] if prior else {}
        if any(
            isinstance(item, dict) and item.get("value") is not None
            for item in facts.get("prior_metrics", {}).values()
        ):
            previous_facts = {
                "metrics": facts["prior_metrics"],
                "qualitative": (
                    previous_facts.get("qualitative", {})
                    if isinstance(previous_facts, dict)
                    else {}
                ),
            }
        previous_facts = previous_facts or None
        previous_analysis = prior["analysis"] if prior else {}
        previous_state = (
            previous_analysis.get("state")
            if isinstance(previous_analysis, dict)
            else None
        )
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
            **deterministic,
            "summary": _clean_text(facts.get("summary"), limit=2400),
            "thesis": _clean_text(facts.get("thesis"), limit=1600),
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
            "watch_items": _dedupe_strings(
                facts.get("watch_items"), deterministic.get("watch_items")
            ),
            "evidence": evidence[:40],
            "news_context": news_items,
            "extraction": extraction,
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
                       d.report_date, d.source_url
                FROM investment_analyses a
                JOIN investment_documents d ON d.document_id = a.document_id
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
    return {**payload, **analysis}


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


def _claim_label(value: object, *, risk: bool = False) -> str:
    if isinstance(value, dict):
        value = value.get("risk" if risk else "label")
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
    grouped = {region: [] for region in ("US", "EU", "ASIA")}
    for payload in _latest_company_analyses(analyses):
        region = str(payload.get("region") or "").upper()
        if region in grouped:
            grouped[region].append(_analysis_score(payload))
    results = []
    for region, scores in grouped.items():
        score = round(sum(scores) / len(scores), 2) if scores else 0.0
        results.append(
            {
                "code": region,
                "company_count": len(scores),
                "score": score,
                "stage": _industry_stage(score),
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
    elif name == "fcf_margin_pct":
        metric = metrics.get("fcf_margin")
        value = metric.get("value") if isinstance(metric, dict) else None
    else:
        value = fundamentals.get(name)
    return _finite_number(value)


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
    )
    output: dict[str, dict] = {}
    for payload in latest:
        company_id = str(
            payload.get("symbol") or payload.get("company") or ""
        ).casefold()
        industry = canonicalize_industry(payload.get("industry"))
        peers = grouped.get(industry, [])
        metrics_output = {}
        for name in metric_names:
            values = sorted(
                value
                for peer in peers
                if (value := _peer_metric_value(peer, name)) is not None
            )
            sample_count = len(values)
            value = _peer_metric_value(payload, name)
            median = (
                (
                    values[sample_count // 2]
                    if sample_count % 2
                    else (values[sample_count // 2 - 1] + values[sample_count // 2]) / 2
                )
                if values
                else None
            )
            delta = value - median if value is not None and median is not None else None
            percentile = None
            if value is not None and sample_count >= 2:
                less = sum(peer_value < value for peer_value in values)
                equal = sum(peer_value == value for peer_value in values)
                percentile = round((less + equal * 0.5) / sample_count * 100, 1)
            metrics_output[name] = {
                "value": value,
                "median": median,
                "delta": delta,
                "percentile": percentile,
                "sample_count": sample_count,
            }
        output[company_id] = {
            "industry": industry,
            "company_count": len(peers),
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
                       a.created_at, d.company, d.symbol, d.region, d.industry,
                       d.document_type, d.report_date, d.source_url
                FROM investment_analyses a
                JOIN investment_documents d ON d.document_id = a.document_id
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

    documents = [_serialize_row(dict(row._mapping)) for row in document_rows]
    analyses = []
    for row in analysis_rows:
        base = _serialize_row(dict(row._mapping))
        facts = _as_json_object(base.pop("facts", {}))
        analysis = _attach_metric_provenance(
            _as_json_object(base.pop("analysis", {})),
            facts,
        )
        analyses.append({**base, **analysis})
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
    latest_annual = _attach_peer_comparisons(annual_analyses)[:300]
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
        },
        "documents": documents,
        "analyses": latest_analyses,
    }
