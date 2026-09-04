"""Investment analysis service, orchestrating ingestion, LLM analysis, and read models."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import PurePath
from types import MappingProxyType
from typing import NamedTuple
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from http_client import get_shared_client, make_request
from investment_engine import (
    build_deterministic_analysis,
    build_material_relationship_contract,
)
from investment_facts import load_deterministic_facts
from investment_ingest import (
    DOCUMENT_TYPES,
    FETCH_DEADLINE_SECONDS,
    FREE_REPORT_SOURCES,
    MAX_ANALYSIS_CHARS,
    MAX_ARCHIVE_ENTRIES,
    MAX_DOCUMENT_BYTES,
    MAX_DOCX_ARCHIVE_ENTRIES,
    MAX_EXTRACTED_CHARS,
    MAX_OCR_PAGES,
    MAX_REDIRECTS,
    MAX_REGULATORY_DOCUMENT_BYTES,
    METRIC_NAMES,
    OCR_SUBPROCESS_TIMEOUT,
    OCR_WALL_SECONDS,
    OCR_WORKERS,
    REGIONS,
    SYNC_OCR_PAGE_BUDGET,
    SYNC_OCR_WALL_SECONDS,
    AnalysisInProgress,
    _clean_text,
    _extract_pdf,
    _file_root,
    _ocr_pdf,
    _ocr_pdf_page,
    _persist_document_file,
    _reject_unsafe_archive,
    _run_ocr_subprocess,
    _serialize_row,
    _serialize_value,
    _sha256_file,
    _validate_public_url,
    _validated_document_content_path,
    build_analysis_excerpt,
    extract_document_text,
    extract_document_text_path,
    fetch_document_url_to_path,
    normalize_metadata,
    store_document,
    store_document_path,
    store_document_url,
)
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
from investment_schemas import (
    INVESTMENT_REPORT_JSON_SCHEMA,
    MATERIALITY_ASSESSMENT_TOPICS,
    NUMERIC_CLAIM_UNITS,
    QUALITATIVE_NAMES,
    VALIDATION_FILING_EVIDENCE,
    VALIDATION_JSON_SCHEMA,
    VALIDATION_PROHIBITED_LANGUAGE,
    InvestmentValidationError,
    filing_content_spans,
    investment_evidence_violations,
    material_numeric_tokens,
    validate_investment_report_payload,
    validate_numeric_claim_rows,
    validate_relationship_reconciliations,
    validate_risk_catalyst_contract_violations,
)
from sqlalchemy import text

from contracts.outbound_security import (
    OutboundSecurityError,
    resolve_redirect_url,
    validate_public_url,
)
from db import get_session

__all__ = [
    "AnalysisInProgress",
    "DOCUMENT_TYPES",
    "FETCH_DEADLINE_SECONDS",
    "FREE_REPORT_SOURCES",
    "INVESTMENT_REPORT_JSON_SCHEMA",
    "InvestmentValidationError",
    "MATERIALITY_ASSESSMENT_TOPICS",
    "MAX_ANALYSIS_CHARS",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_DOCX_ARCHIVE_ENTRIES",
    "MAX_DOCUMENT_BYTES",
    "MAX_EXTRACTED_CHARS",
    "MAX_OCR_PAGES",
    "MAX_REDIRECTS",
    "MAX_REGULATORY_DOCUMENT_BYTES",
    "METRIC_NAMES",
    "NUMERIC_CLAIM_UNITS",
    "OCR_SUBPROCESS_TIMEOUT",
    "OCR_WALL_SECONDS",
    "OCR_WORKERS",
    "OutboundSecurityError",
    "QUALITATIVE_NAMES",
    "REGIONS",
    "SYNC_OCR_PAGE_BUDGET",
    "SYNC_OCR_WALL_SECONDS",
    "VALIDATION_FILING_EVIDENCE",
    "VALIDATION_JSON_SCHEMA",
    "VALIDATION_PROHIBITED_LANGUAGE",
    "_clean_text",
    "_extract_pdf",
    "_file_root",
    "_ocr_pdf",
    "_ocr_pdf_page",
    "_persist_document_file",
    "_reject_unsafe_archive",
    "_run_ocr_subprocess",
    "_serialize_row",
    "_serialize_value",
    "_sha256_file",
    "_validate_public_url",
    "_validated_document_content_path",
    "build_analysis_excerpt",
    "extract_document_text",
    "extract_document_text_path",
    "fetch_document_url_to_path",
    "filing_content_spans",
    "httpx",
    "investment_evidence_violations",
    "material_numeric_tokens",
    "normalize_metadata",
    "os",
    "relationship_reconciliation_problems",
    "resolve_redirect_url",
    "risk_catalyst_contract_violations",
    "shutil",
    "store_document",
    "store_document_path",
    "store_document_url",
    "subprocess",
    "time",
    "validate_numeric_claim_rows",
    "validate_public_url",
]
from investment_universe import configured_region_counts, industry_for
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger

logger = get_logger("investment.analysis")

relationship_reconciliation_problems = validate_relationship_reconciliations
risk_catalyst_contract_violations = validate_risk_catalyst_contract_violations

MODEL_ID = "openai/gpt-5.6-luna"
INVESTMENT_ANALYSIS_RULE_VERSION = "7"

_VALIDATION_WARNING_BY_CATEGORY = {
    VALIDATION_JSON_SCHEMA: "schema_validation_failed",
    VALIDATION_FILING_EVIDENCE: "filing_evidence_unsupported",
    VALIDATION_PROHIBITED_LANGUAGE: "prohibited_language_detected",
}


def _response_schema() -> dict:
    return {
        "name": "investment_report_narrative_v7",
        "strict": True,
        "schema": INVESTMENT_REPORT_JSON_SCHEMA,
    }


def _validated_investment_facts(
    content: object,
    *,
    excerpt: str,
    news_items: object = None,
    deterministic_current: object = None,
    deterministic_prior: object = None,
    document_metadata: object = None,
    relationship_facts: object = None,
    material_relationships: object = None,
) -> dict:
    try:
        facts = _parse_llm_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvestmentValidationError(
            VALIDATION_JSON_SCHEMA,
            ["response was not one JSON object matching the narrative schema"],
        ) from exc
    problems = validate_investment_report_payload(facts, excerpt=excerpt)
    if problems:
        raise InvestmentValidationError(VALIDATION_JSON_SCHEMA, problems)
    if material_relationships:
        rel_problems = validate_relationship_reconciliations(
            facts, material_relationships=material_relationships
        )
        if rel_problems:
            raise InvestmentValidationError(VALIDATION_JSON_SCHEMA, rel_problems)
    return facts


def enqueue_investment_analysis(
    config: dict, document_id: str, *, market_inputs: dict | None = None
) -> dict:
    """Hand an ingested document to the durable job queue.

    Uses ``jobs`` with job type ``investment_analysis``. The worker consumes
    the job; no second queue is introduced. The job
    identity deduplicates on the document id, so repeated ingests or triggers
    do not stack duplicate analysis work.
    """
    from jobs import enqueue_job

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
        "drivers": _dedupe_strings(facts.get("drivers"), deterministic.get("drivers")),
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
        mp = dict(rebuilt["metrics"]["market_price"])
        mp["source"] = market["source"]
        mp["evidence"] = market_inputs["market_price_evidence"]
        stored_metrics["market_price"] = mp
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
