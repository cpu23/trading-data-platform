"""Deterministic filing deltas.

A filing delta is produced before any LLM interpretation: section hashes,
normalized text diffs, and bounded numeric facts per category.  The caller
owns the transaction; helpers never commit or roll back.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

from sqlalchemy import text

from logging_config import get_logger

logger = get_logger("investment.filing_deltas")

CATEGORIES = (
    "guidance",
    "risk_language",
    "segments",
    "margins_cashflow",
    "capex",
    "balance_sheet",
    "capital_allocation",
    "commitments",
    "management_language",
)
_SECTION_PATTERNS = {
    "guidance": r"(?:outlook|guidance|forward[\s-]?looking statements?)",
    "risk_language": r"risk factors?",
    "segments": r"(?:segment|business segment) (?:reporting|information|results)",
    "margins_cashflow": r"(?:liquidity and capital resources|cash flows?|operating results|margin)",
    "capex": r"capital expenditures?",
    "balance_sheet": r"(?:balance sheet|indebtedness|financial position)",
    "capital_allocation": r"(?:capital allocation|dividends?|share repurchases?|buybacks?)",
    "commitments": r"(?:commitments? and contingencies?|material contracts?)",
    "management_language": r"(?:management'?s discussion|md&a|strategic review|operating review)",
}
_TEXT_LIMIT = 200_000
_SECTION_LIMIT = 6_000
_EXCERPT_LIMIT = 500
_PERCENT_LIMIT = 10


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_sections(text_body: str) -> dict[str, str]:
    """Map category -> bounded normalized section text (first match wins)."""
    sections: dict[str, str] = {}
    for category, pattern in _SECTION_PATTERNS.items():
        match = re.search(pattern, text_body, flags=re.IGNORECASE)
        if not match:
            continue
        start = match.end()
        next_match = None
        for other in _SECTION_PATTERNS.values():
            candidate = re.search(other, text_body[start:], flags=re.IGNORECASE)
            if candidate and (next_match is None or candidate.start() < next_match):
                next_match = candidate.start()
        end = start + next_match if next_match is not None else len(text_body)
        sections[category] = _normalize(text_body[start:end])[:_SECTION_LIMIT]
    return sections


def _percent_mentions(section: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?\s?%", section)[:_PERCENT_LIMIT]


def _diff_excerpt(current: str, previous: str) -> str:
    if not previous:
        return current[:_EXCERPT_LIMIT]
    changed = [
        line[2:].strip()
        for line in difflib.unified_diff(
            previous.split(" "), current.split(" "), lineterm="", n=0
        )
        if line.startswith("+ ") or line.startswith("++")
    ]
    joined = " ".join(part for part in changed if part)[:_EXCERPT_LIMIT]
    return joined or current[:_EXCERPT_LIMIT]


def compute_filing_delta(
    config: dict, document_id: str, *, session: Any | None = None
) -> dict[str, Any]:
    """Persist one deterministic delta row per detected category."""
    from db import get_session

    def run(sess: Any) -> dict[str, Any]:
        doc = (
            sess.execute(
                text(
                    """SELECT document_id, company, document_type, report_date,
                              extracted_text, created_at
                       FROM investment_documents
                       WHERE document_id = CAST(:id AS UUID) LIMIT 1"""
                ),
                {"id": document_id},
            )
            .mappings()
            .first()
        )
        if doc is None:
            return {"status": "missing_document", "categories": 0}
        previous = (
            sess.execute(
                text(
                    """SELECT document_id, extracted_text
                       FROM investment_documents
                       WHERE company = :company
                         AND document_type = :document_type
                         AND document_id <> CAST(:document_id AS UUID)
                         AND (
                           (:report_date IS NOT NULL
                            AND report_date IS NOT NULL
                            AND report_date < :report_date)
                           OR (:report_date IS NULL AND created_at < :created_at)
                         )
                       ORDER BY report_date DESC NULLS LAST, created_at DESC,
                                document_id DESC
                       LIMIT 1"""
                ),
                {
                    "company": doc["company"],
                    "document_type": doc["document_type"],
                    "document_id": doc["document_id"],
                    "report_date": doc["report_date"],
                    "created_at": doc["created_at"],
                },
            )
            .mappings()
            .first()
        )
        # Recomputations must not retain categories from a superseded comparison.
        # The caller-owned transaction keeps the delete-and-rebuild atomic.
        sess.execute(
            text(
                """DELETE FROM investment_filing_deltas
                   WHERE document_id = CAST(:document_id AS UUID)"""
            ),
            {"document_id": doc["document_id"]},
        )
        current_sections = _extract_sections(
            (doc["extracted_text"] or "")[:_TEXT_LIMIT]
        )
        previous_sections = (
            _extract_sections((previous["extracted_text"] or "")[:_TEXT_LIMIT])
            if previous
            else {}
        )
        rows = 0
        for category, section in current_sections.items():
            previous_section = previous_sections.get(category)
            section_hash = _hash(section)
            previous_hash = _hash(previous_section) if previous_section else None
            if previous_section is None:
                change_kind = "new"
            elif previous_hash == section_hash or (
                difflib.SequenceMatcher(None, previous_section, section).ratio() >= 0.98
            ):
                change_kind = "unchanged"
            else:
                change_kind = "changed"
            sess.execute(
                text(
                    """INSERT INTO investment_filing_deltas
                       (document_id, previous_document_id, category, change_kind,
                        section_hash, previous_section_hash, excerpt,
                        previous_excerpt, metrics)
                       VALUES (CAST(:document_id AS UUID),
                               CAST(:previous_document_id AS UUID), :category,
                               :change_kind, :section_hash, :previous_section_hash,
                               :excerpt, :previous_excerpt,
                               CAST(:metrics AS JSONB))
                       ON CONFLICT (document_id, category) DO UPDATE SET
                         previous_document_id = EXCLUDED.previous_document_id,
                         change_kind = EXCLUDED.change_kind,
                         section_hash = EXCLUDED.section_hash,
                         previous_section_hash = EXCLUDED.previous_section_hash,
                         excerpt = EXCLUDED.excerpt,
                         previous_excerpt = EXCLUDED.previous_excerpt,
                         metrics = EXCLUDED.metrics"""
                ),
                {
                    "document_id": doc["document_id"],
                    "previous_document_id": previous["document_id"]
                    if previous
                    else None,
                    "category": category,
                    "change_kind": change_kind,
                    "section_hash": section_hash,
                    "previous_section_hash": previous_hash,
                    "excerpt": _diff_excerpt(section, previous_section or ""),
                    "previous_excerpt": (previous_section or "")[:_EXCERPT_LIMIT],
                    "metrics": json.dumps(
                        {"percent_mentions": _percent_mentions(section)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            )
            rows += 1
        for category, previous_section in previous_sections.items():
            if category in current_sections:
                continue
            sess.execute(
                text(
                    """INSERT INTO investment_filing_deltas
                       (document_id, previous_document_id, category, change_kind,
                        section_hash, previous_section_hash, excerpt,
                        previous_excerpt, metrics)
                       VALUES (CAST(:document_id AS UUID),
                               CAST(:previous_document_id AS UUID), :category,
                               'removed', NULL, :previous_section_hash, NULL,
                               :previous_excerpt, CAST('{}' AS JSONB))
                       ON CONFLICT (document_id, category) DO UPDATE SET
                         change_kind = 'removed',
                         previous_section_hash = EXCLUDED.previous_section_hash"""
                ),
                {
                    "document_id": doc["document_id"],
                    "previous_document_id": previous["document_id"]
                    if previous
                    else None,
                    "category": category,
                    "previous_section_hash": _hash(previous_section),
                    "previous_excerpt": previous_section[:_EXCERPT_LIMIT],
                },
            )
            rows += 1
        return {
            "status": "computed",
            "categories": rows,
            "previous_document_id": str(previous["document_id"]) if previous else None,
        }

    if session is not None:
        return run(session)
    with get_session(config) as owned:
        return run(owned)


__all__ = ["CATEGORIES", "compute_filing_delta"]
