from collections import Counter
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import socket
import warnings
import zipfile
from datetime import date, datetime, timezone
from pathlib import PurePath
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from sqlalchemy import text

from db import get_session
from http_client import get_shared_client
from investment_engine import build_deterministic_analysis
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger

logger = get_logger("investment.analysis")


class AnalysisInProgress(RuntimeError):
    pass

MAX_DOCUMENT_BYTES = 20_000_000
MAX_EXTRACTED_CHARS = 1_000_000
MAX_ANALYSIS_CHARS = 120_000
MAX_REDIRECTS = 4
MODEL_ID = "google/gemini-3.5-flash-lite"
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
)
QUALITATIVE_NAMES = (
    "ai_demand",
    "datacenter_demand",
    "supply_constraints",
    "pricing_power",
    "guidance_up",
    "guidance_down",
)
KEY_INDUSTRIES = (
    "Semiconductors & Memory",
    "AI Infrastructure & Data Centres",
    "Energy, Utilities & Power Grid",
    "Industrials, Automation & Robotics",
    "Banks, Insurance & Capital Markets",
    "Healthcare & Biotechnology",
    "Consumer, Retail & E-commerce",
    "Aerospace & Defence",
)
INDUSTRY_ALIASES = (
    ("Semiconductors & Memory", ("semiconductor", "memory", "dram", "nand", "foundry", "chip")),
    ("AI Infrastructure & Data Centres", ("data centre", "data center", "datacenter", "cloud infrastructure", "ai infrastructure")),
    ("Energy, Utilities & Power Grid", ("energy", "utility", "utilities", "power grid", "electricity", "renewable")),
    ("Industrials, Automation & Robotics", ("industrial", "automation", "robotic", "machinery")),
    ("Banks, Insurance & Capital Markets", ("bank", "insurance", "capital market", "asset management", "fintech")),
    ("Healthcare & Biotechnology", ("healthcare", "biotech", "pharma", "medical device")),
    ("Consumer, Retail & E-commerce", ("consumer", "retail", "e-commerce", "ecommerce")),
    ("Aerospace & Defence", ("aerospace", "defence", "defense")),
)
FREE_REPORT_SOURCES = (
    {"region": "US", "name": "SEC EDGAR", "url": "https://www.sec.gov/edgar/search/"},
    {"region": "EU", "name": "ESEF filings", "url": "https://filings.xbrl.org/"},
    {"region": "ASIA", "name": "Japan EDINET", "url": "https://disclosure2.edinet-fsa.go.jp/"},
    {"region": "ASIA", "name": "HKEXnews", "url": "https://www1.hkexnews.hk/index.htm"},
)
_ANALYSIS_KEYWORDS = re.compile(
    r"revenue|sales|cash flow|capital expenditure|capex|net income|earnings per share|"
    r"gross margin|inventory|backlog|artificial intelligence|\bai\b|data[ -]?cent(?:er|re)|"
    r"supply|capacity|pricing|guidance|outlook|risk|demand",
    re.IGNORECASE,
)


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def canonicalize_industry(value: object) -> str:
    cleaned = _clean_text(value, limit=120)
    normalized = cleaned.casefold()
    for industry, aliases in INDUSTRY_ALIASES:
        if any(alias in normalized for alias in aliases):
            return industry
    return cleaned or "Unclassified"


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


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("encrypted PDF is not supported") from exc
    pages = []
    extracted_chars = 0
    for page_number, page in enumerate(reader.pages, start=1):
        if page_number > 500:
            break
        extracted = page.extract_text() or ""
        if extracted.strip():
            page_text = f"\n[Page {page_number}]\n{extracted}"
            pages.append(page_text)
            extracted_chars += len(page_text)
        if extracted_chars >= MAX_EXTRACTED_CHARS:
            break
    return "".join(pages)


def _extract_docx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCUMENT_BYTES * 2:
            raise ValueError("DOCX document XML is too large")
        root = ElementTree.fromstring(archive.read(info))
    paragraphs = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        words = [
            node.text or ""
            for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ]
        if words:
            paragraphs.append("".join(words))
    return "\n".join(paragraphs)


def extract_document_text(content: bytes, filename: str, mime_type: str | None) -> str:
    if not content:
        raise ValueError("document is empty")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("document exceeds 20 MB")

    content_type = (mime_type or "").split(";", 1)[0].strip().lower()
    suffix = PurePath(filename.lower()).suffix
    if content.startswith(b"%PDF-") or content_type == "application/pdf" or suffix == ".pdf":
        extracted = _extract_pdf(content)
    elif (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        extracted = _extract_docx(content)
    elif suffix in {".xml", ".xsd"} or content_type in {"application/xml", "text/xml"}:
        extracted = content.decode("utf-8", errors="replace")
    elif content_type in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
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
        raise ValueError("supported formats are PDF, DOCX, HTML, text, Markdown, CSV, JSON, and XML")

    extracted = extracted.replace("\x00", "")
    extracted = re.sub(r"[ \t]+", " ", extracted)
    extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
    if len(extracted) < 100:
        raise ValueError("document did not contain enough extractable text")
    return extracted[:MAX_EXTRACTED_CHARS]


def build_analysis_excerpt(document_text: str) -> str:
    if len(document_text) <= MAX_ANALYSIS_CHARS:
        return document_text

    windows = [(0, 24_000), (max(0, len(document_text) - 28_000), len(document_text))]
    for match in _ANALYSIS_KEYWORDS.finditer(document_text):
        windows.append((max(0, match.start() - 1_600), min(len(document_text), match.end() + 3_400)))

    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 300:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    chunks = []
    used = 0
    for start, end in merged:
        remaining = MAX_ANALYSIS_CHARS - used
        if remaining <= 0:
            break
        chunk = document_text[start:end][:remaining]
        chunks.append(f"[Source characters {start}-{start + len(chunk)}]\n{chunk}")
        used += len(chunk)
    return "\n\n".join(chunks)


def _validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must be a public HTTP(S) URL")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("source URL hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("source URL hostname could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("source URL must not resolve to a private or reserved address")
    return value


def fetch_document_url(url: str) -> tuple[bytes, str, str, str]:
    current = _validate_public_url(url)
    client = get_shared_client()
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream(
            "GET",
            current,
            headers={"User-Agent": "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"},
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
            content_type = response.headers.get("content-type", "application/octet-stream")
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
    enriched = {**metadata, "filename": metadata.get("filename") or filename, "source_url": final_url}
    return store_document(config, enriched, content, mime_type)


def _metric_schema() -> dict:
    properties = {
        "value": {"type": ["number", "null"]},
        "unit": {"type": "string"},
        "period": {"type": "string"},
        "evidence": {"type": "string"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _response_schema() -> dict:
    metric_map = {
        "type": "object",
        "additionalProperties": False,
        "properties": {name: _metric_schema() for name in METRIC_NAMES},
        "required": list(METRIC_NAMES),
    }
    qualitative_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "present": {"type": "boolean"},
            "strength": {"type": "string", "enum": ["none", "weak", "moderate", "strong"]},
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
                    "confidence": {"type": "string", "enum": ["low", "moderate", "high"]},
                },
                "required": ["document_type", "sector", "industry", "region", "confidence"],
            },
            "metrics": metric_map,
            "prior_metrics": metric_map,
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
            "classification", "metrics", "prior_metrics", "qualitative", "summary",
            "thesis", "drivers", "catalysts", "risks", "watch_items",
        ],
    }
    return {"name": "investment_report_analysis", "strict": True, "schema": schema}


def _load_news_context(config: dict, metadata: dict) -> list[dict]:
    output = config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    path = os.path.join(output, "feed.json")
    try:
        if os.path.getsize(path) > 2_000_000:
            return []
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    terms = {
        str(metadata.get("symbol") or "").lower(),
        str(metadata.get("company") or "").lower(),
        str(metadata.get("industry") or "").lower(),
    } - {""}
    selected = []
    for item in payload.get("items", [])[:500] if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            str(item.get(key) or "") for key in ("title", "summary", "symbols", "tags")
        ).lower()
        if not any(term in haystack for term in terms):
            continue
        selected.append(
            {
                "source": _clean_text(item.get("source_label") or item.get("source"), limit=40),
                "title": _clean_text(item.get("title"), limit=240),
                "published": _clean_text(item.get("published"), limit=64),
                "summary": _clean_text(item.get("summary"), limit=500),
                "url": _clean_text(item.get("url"), limit=2048),
            }
        )
        if len(selected) >= 20:
            break
    return selected


def _build_prompt(document: dict, excerpt: str, news_items: list[dict]) -> str:
    news_text = json.dumps(news_items, ensure_ascii=False, sort_keys=True)
    return f"""You are a professional buy-side investment analyst extracting auditable facts.
Return only the strict JSON response. Use the exact model schema supplied by the caller.

Rules:
- The report is the authoritative source. Never invent a number, unit, period, quote, catalyst, or mitigation.
- Every non-null metric must include a short verbatim evidence quote and its period.
- Monetary values must be normalized to the unit stated in `unit`; preserve whether that unit is USDm, EURm, JPYbn, etc. Do not convert currencies.
- Capex is a positive cash outflow. Gross margin is percentage points, not a fraction.
- `metrics` is the latest reported period; `prior_metrics` is the directly comparable prior period in this document. Use null when absent.
- Qualitative signals are present only when explicitly supported. AI demand and data-centre demand are distinct.
- Separate company-stated facts from inference. Summary and thesis must state uncertainty and identify what would invalidate the thesis.
- Catalysts need a time horizon and evidence. Each risk needs a practical company, portfolio, or monitoring mitigation; say `No company mitigation stated; monitor ...` when necessary.
- News context can inform a catalyst or crowding check, but its wording must not be presented as report evidence.

Metadata:
{json.dumps({key: str(value) if value is not None else None for key, value in document.items() if key != 'extracted_text'}, sort_keys=True)}

Related Reuters/Kobeissi feed items:
{news_text}

REPORT EXCERPT:
{excerpt}
"""


def _parse_llm_json(content: object) -> dict:
    if not isinstance(content, str):
        raise ValueError("LLM response was not text")
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not an object")
    for key in ("classification", "metrics", "prior_metrics", "qualitative"):
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
    duration_ms = 0
    if telemetry is not None:
        duration_ms = sum(
            value or 0
            for value in (
                telemetry.first_attempt_duration_ms,
                telemetry.validation_retry_duration_ms,
            )
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
            "tokens_input": telemetry.tokens_input_total if telemetry is not None else 0,
            "tokens_output": telemetry.tokens_output_total if telemetry is not None else 0,
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


def analyze_document(config: dict, document_id: str, market_inputs: dict | None = None) -> dict:
    document = _load_document(config, document_id)
    if document is None:
        raise LookupError("investment document not found")

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
    started_at = datetime.now(timezone.utc)
    stage = None
    try:
        news_items = _load_news_context(config, document)
        excerpt = build_analysis_excerpt(document["extracted_text"])
        stage = LLMStage(
            config,
            "investment_analysis",
            correlation_id=correlation_id,
            response_schema=_response_schema(),
        )
        prompt = _build_prompt(document, excerpt, news_items)
        result = stage.call(prompt)
        try:
            facts = _parse_llm_json(result.get("content"))
        except (ValueError, json.JSONDecodeError) as exc:
            stage.add_validation_warnings(["response was not valid investment JSON"])
            if stage.policy.validation_retries < 1:
                raise LLMValidationError("Investment response validation failed", stage.telemetry) from exc
            result = stage.call(prompt + "\nCORRECTION: Return one valid JSON object only. Fill every required field; use null for missing numbers.")
            facts = _parse_llm_json(result.get("content"))

        classification = facts["classification"]
        classified_industry = canonicalize_industry(
            classification.get("industry") or document.get("industry")
        )
        classification["industry"] = classified_industry
        classification["region"] = document["region"]
        classification["document_type"] = document["document_type"]
        document["industry"] = classified_industry

        prior, prior_count = _previous_analysis(config, document)
        previous_facts = prior["facts"] if prior else None
        if not previous_facts and any(
            isinstance(item, dict) and item.get("value") is not None
            for item in facts.get("prior_metrics", {}).values()
        ):
            previous_facts = {"metrics": facts["prior_metrics"], "qualitative": {}}
        previous_analysis = prior["analysis"] if prior else {}
        previous_state = previous_analysis.get("state") if isinstance(previous_analysis, dict) else None
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
                evidence.append({"source": "report", "metric": metric_name, "quote": _clean_text(item["evidence"], limit=500)})
        for signal_name, item in facts.get("qualitative", {}).items():
            if isinstance(item, dict) and item.get("present") and item.get("evidence"):
                evidence.append({"source": "report", "signal": signal_name, "quote": _clean_text(item["evidence"], limit=500)})

        analysis = {
            **deterministic,
            "summary": _clean_text(facts.get("summary"), limit=2400),
            "thesis": _clean_text(facts.get("thesis"), limit=1600),
            "classification": facts.get("classification", {}),
            "drivers": _dedupe_strings(facts.get("drivers"), deterministic.get("drivers")),
            "catalysts": facts.get("catalysts", [])[:12] if isinstance(facts.get("catalysts"), list) else [],
            "risks": facts.get("risks", [])[:12] if isinstance(facts.get("risks"), list) else [],
            "watch_items": _dedupe_strings(facts.get("watch_items"), deterministic.get("watch_items")),
            "evidence": evidence[:40],
            "news_context": news_items,
            "model": stage.policy.model,
        }
        analysis_id = str(uuid4())
        with get_session(config) as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO investment_analyses (
                        analysis_id, document_id, previous_document_id, facts, analysis,
                        model, tokens_input, tokens_output, cost_usd
                    ) VALUES (
                        :analysis_id, :document_id, :previous_document_id,
                        CAST(:facts AS JSONB), CAST(:analysis AS JSONB), :model,
                        :tokens_input, :tokens_output, :cost_usd
                    )
                    ON CONFLICT (document_id) DO UPDATE SET
                        previous_document_id = EXCLUDED.previous_document_id,
                        facts = EXCLUDED.facts,
                        analysis = EXCLUDED.analysis,
                        model = EXCLUDED.model,
                        tokens_input = EXCLUDED.tokens_input,
                        tokens_output = EXCLUDED.tokens_output,
                        cost_usd = EXCLUDED.cost_usd,
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
                },
            ).fetchone()
            session.execute(
                text(
                    "UPDATE investment_documents "
                    "SET status = 'analyzed', industry = :industry, error_message = NULL, updated_at = NOW() "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": document_id, "industry": classified_industry},
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
                text("UPDATE investment_documents SET status = 'failed', error_message = 'analysis failed', updated_at = NOW() WHERE document_id = :document_id"),
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


def get_analysis(config: dict, analysis_id: str) -> dict | None:
    with get_session(config) as session:
        row = session.execute(
            text(
                """
                SELECT a.analysis_id, a.document_id, a.previous_document_id, a.analysis,
                       a.model, a.created_at, a.updated_at, d.company, d.symbol,
                       d.region, d.industry, d.document_type, d.report_date, d.source_url
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
    analysis = _as_json_object(payload.pop("analysis", {}))
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
    for payload in analyses:
        identity = str(payload.get("symbol") or payload.get("company") or "").casefold()
        if identity:
            latest.setdefault(identity, payload)
    return list(latest.values())


def _aggregate_industries(analyses: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for payload in _latest_company_analyses(analyses):
        industry_name = canonicalize_industry(payload.get("industry"))
        aggregate = grouped.setdefault(
            industry_name,
            {
                "name": industry_name,
                "regions": set(),
                "companies": set(),
                "scores": [],
                "drivers": Counter(),
                "risks": Counter(),
            },
        )
        aggregate["regions"].add(payload.get("region"))
        aggregate["companies"].add(payload.get("symbol") or payload.get("company"))
        aggregate["scores"].append(_analysis_score(payload))
        for driver in payload.get("drivers", []) if isinstance(payload.get("drivers"), list) else []:
            label = _clean_text(driver, limit=160)
            if label:
                aggregate["drivers"][label] += 1
        for risk in payload.get("risks", []) if isinstance(payload.get("risks"), list) else []:
            label = _clean_text(
                risk.get("risk") if isinstance(risk, dict) else risk,
                limit=160,
            )
            if label:
                aggregate["risks"][label] += 1

    results = []
    for industry_name in (*KEY_INDUSTRIES, *grouped):
        if any(item["name"] == industry_name for item in results):
            continue
        aggregate = grouped.get(industry_name)
        if aggregate is None:
            results.append(
                {
                    "name": industry_name,
                    "regions": sorted(REGIONS),
                    "company_count": 0,
                    "report_count": 0,
                    "stage": "monitor",
                    "score": 0.0,
                    "breadth_pct": 0.0,
                    "drivers": [],
                    "risks": [],
                }
            )
            continue
        scores = aggregate["scores"]
        score = round(sum(scores) / len(scores), 2) if scores else 0.0
        results.append(
            {
                "name": industry_name,
                "regions": sorted(value for value in aggregate["regions"] if value),
                "company_count": len(aggregate["companies"]),
                "report_count": len(scores),
                "stage": _industry_stage(score),
                "score": score,
                "breadth_pct": round(
                    sum(value >= 5 for value in scores) / len(scores) * 100,
                    1,
                )
                if scores
                else 0.0,
                "drivers": [
                    value for value, _ in aggregate["drivers"].most_common(4)
                ],
                "risks": [
                    value for value, _ in aggregate["risks"].most_common(3)
                ],
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
        analysis_rows = session.execute(
            text(
                """
                SELECT a.analysis_id, a.document_id, a.analysis, a.model, a.created_at,
                       d.company, d.symbol, d.region, d.industry, d.document_type,
                       d.report_date, d.source_url
                FROM investment_analyses a
                JOIN investment_documents d ON d.document_id = a.document_id
                ORDER BY d.report_date DESC NULLS LAST, a.created_at DESC
                LIMIT 60
                """
            )
        ).fetchall()

    documents = [_serialize_row(dict(row._mapping)) for row in document_rows]
    analyses = []
    for row in analysis_rows:
        base = _serialize_row(dict(row._mapping))
        analysis = _as_json_object(base.pop("analysis", {}))
        analyses.append({**base, **analysis})
    industry_list = _aggregate_industries(analyses)
    return {
        "model": MODEL_ID,
        "regions": _aggregate_regions(analyses),
        "sources": list(FREE_REPORT_SOURCES),
        "industries": industry_list,
        "documents": documents,
        "analyses": analyses,
    }
