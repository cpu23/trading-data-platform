"""Frozen company-benchmark fixtures split into producer and evaluator halves.

A producer case carries everything the dispatch/finalization seams consume as
of ``as_of``; the evaluator half carries expectations, traps, and later
outcomes. The two halves live in separate YAML files and the evaluator is
reachable only through an explicit ``producer`` argument: producer loading
recursively rejects every evaluator-only key, fails closed on every declared
point-in-time timestamp field (present fields must parse as timezone-aware
timestamps at or before ``as_of``), and forbids structured hindsight claims
dated at or before ``as_of``, so future knowledge can never leak into a
recorded run. Packet values are recursively immutable; production seams
receive explicit plain copies through :func:`plain_copy`. Fingerprints bind
the halves together via ``contracts.canonical_fingerprint``.

Everything here is pure: loaders read their YAML file and nothing else, and
``prepare_company_run`` / ``finalize_recorded_company_run`` call the exact
production ``investment_service`` seams without database, network, or LLM I/O.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace as _dataclass_replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from research_intelligence.contracts import canonical_fingerprint

import investment_service


SCHEMA_VERSION = "company_benchmark_v1"
_FINGERPRINT_RE = re.compile(r"[a-f0-9]{64}")
_CASE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,119}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP_LIKE_KEY_RE = re.compile(r".*(?:_at|_date|_timestamp|_until)$")

_PRODUCER_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "fixture_version",
        "as_of",
        "document",
        "excerpt",
        "deterministic_current",
        "deterministic_prior",
        "market_inputs",
        "prior_facts",
        "previous_state",
        "prior_count",
        "news_items",
        "extraction",
    }
)

# Keys reserved to the evaluator half; a producer payload containing any of
# them (at any depth) would leak answers into the recorded run.
_EVALUATOR_ONLY_KEYS = frozenset(
    {
        "producer_fingerprint",
        "expected_material_observations",
        "deterministic_checks",
        "strongest_counter_thesis",
        "expected_unknowns",
        "known_traps",
        "later_outcomes",
        "required_material_evidence",
        "forbidden_hindsight",
    }
)

# Temporal key classes for point-in-time enforcement. Every timestamp- or
# date-shaped key in a producer payload must belong to exactly one declared
# class below; any undeclared timestamp-like key is treated as an unsigned
# provenance channel and rejected rather than silently bypassed.

# Availability/provenance instants: if any of these (at any depth) carries a
# value, it must parse as a timezone-aware timestamp at or before ``as_of``;
# a present null fails closed.
_PIT_INSTANT_KEYS = frozenset(
    {
        "available_at",
        "published_at",
        "released_at",
        "source_timestamp",
        "target_at",
        "observed_at",
        "checked_at",
        "consensus_availability_checked_at",
        "valid_from",
        "event_ended_at",
        "transcript_created_at",
    }
)

# Historical source dates: ISO dates or timezone-aware datetimes recording
# when a past document was dated, released, announced, or filed. Present
# values must parse and stay at or before ``as_of``.
_PIT_HISTORICAL_DATE_KEYS = frozenset(
    {
        "report_date",
        "release_date",
        "announced_date",
        "filing_date",
    }
)

# Schema-declared optional historical dates: the only temporal keys allowed
# to be null (a quiet-period record genuinely has no report date yet).
_OPTIONAL_HISTORICAL_DATE_KEYS = frozenset({"report_date"})

# Forecast/reference periods: forward-looking boundaries (fiscal period ends,
# guidance target periods, validity windows) that legitimately may lie after
# ``as_of``; present values must still parse as ISO dates/datetimes.
_FORECAST_PERIOD_KEYS = frozenset(
    {
        "valid_to",
        "valid_until",
        "period_end",
        "quarter_end",
        "fiscal_quarter_end",
        "fiscal_year_end",
        "fiscal_period_end",
        "guidance_period_end",
        "guidance_target_period_end",
    }
)

# Every recognized temporal key across all classes.
_PIT_TIMESTAMP_KEYS = (
    _PIT_INSTANT_KEYS | _PIT_HISTORICAL_DATE_KEYS | _FORECAST_PERIOD_KEYS
)

_EVALUATOR_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "fixture_version",
        "producer_fingerprint",
        "expected_material_observations",
        "deterministic_checks",
        "strongest_counter_thesis",
        "expected_unknowns",
        "known_traps",
        "later_outcomes",
        "required_material_evidence",
        "forbidden_hindsight",
    }
)

_FORBIDDEN_CLAIM_KEYS = frozenset(
    {"claim_id", "metric_aliases", "value", "period_aliases", "available_after"}
)
_DOCUMENT_REQUIRED_KEYS = frozenset(
    {"company", "symbol", "document_type", "report_date", "available_at"}
)
_NEWS_TIMESTAMP_KEYS = ("available_at", "published_at")

_MAX_CONTAINER_ITEMS = 512
_MAX_SCAN_DEPTH = 16
_MAX_MAPPING_KEYS = 128
_MAX_NEWS_ITEMS = 100
_MAX_EXCERPT_CHARS = 250_000
_MAX_CONTENT_CHARS = 1_000_000
_NUMERIC_VALUE_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:%|bps|bp)?")

_MAX_ALIAS_ITEMS = 10
_MAX_ALIAS_CHARS = 200
_MAX_CLAIM_ID_CHARS = 120
_MAX_VALUE_CHARS = 64


def plain_copy(value: Any) -> Any:
    """Recursively convert frozen packet structures to plain mutable copies."""
    if isinstance(value, MappingProxyType):
        return {key: plain_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_copy(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    """Return a recursively immutable plain-data structure.

    Mappings become ``MappingProxyType`` over fresh plain copies and lists
    become tuples; scalars, datetimes, and ``None`` pass through. The input
    is never retained: every container is rebuilt from a plain copy of its
    items so later mutation of the caller's object cannot change the packet.
    """
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze(item)
                for key, item in copy.deepcopy(dict(value)).items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ProducerCase:
    """Everything the production dispatch/finalization seams consume."""

    schema_version: str
    case_id: str
    fixture_version: int
    as_of: datetime
    document: Mapping[str, Any]
    excerpt: str
    deterministic_current: Mapping[str, Any]
    deterministic_prior: Mapping[str, Any]
    market_inputs: Mapping[str, Any]
    prior_facts: Mapping[str, Any]
    previous_state: str | None
    prior_count: int
    news_items: tuple[Mapping[str, Any], ...]
    extraction: Mapping[str, Any]
    fingerprint: str
    source_path: str


@dataclass(frozen=True, slots=True)
class EvaluatorCase:
    """Grading expectations; never reachable from a producer case."""

    schema_version: str
    case_id: str
    fixture_version: int
    producer_fingerprint: str
    expected_material_observations: tuple[str, ...]
    deterministic_checks: tuple[Any, ...]
    strongest_counter_thesis: str
    expected_unknowns: tuple[str, ...]
    known_traps: tuple[Any, ...]
    later_outcomes: tuple[Any, ...]
    required_material_evidence: tuple[str, ...]
    forbidden_hindsight: tuple[ForbiddenCompanyClaim, ...]
    source_path: str


@dataclass(frozen=True, slots=True)
class RecordedExecutorOutput:
    """Bounded recorded model response plus flat scalar provenance."""

    content: str
    provenance: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ForbiddenCompanyClaim:
    """One company-specific post-``as_of`` fact the output must not leak.

    Structured instead of descriptive so the deterministic hard gate can catch
    paraphrases: a violation requires one authored string to carry a metric
    alias, the forbidden value, and a period alias together.
    """

    claim_id: str
    metric_aliases: tuple[str, ...]
    value: int | float | str
    period_aliases: tuple[str, ...]
    available_after: datetime


def _text(value: Any, field: str, maximum: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        raise ValueError(f"company benchmark {field} is required")
    if len(cleaned) > maximum:
        raise ValueError(
            f"company benchmark {field} exceeds {maximum} characters"
        )
    return cleaned


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000:
        raise ValueError("company benchmark fixture_version must be 1..1000")
    return value


def _case_id(value: Any) -> str:
    cleaned = _text(value, "case_id", 120).casefold()
    if not _CASE_ID_RE.fullmatch(cleaned):
        raise ValueError("company benchmark case_id has unsupported characters")
    return cleaned


def _aware_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid company benchmark {field}") from None
    else:
        raise ValueError(f"invalid company benchmark {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"company benchmark {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _pit_timestamp(
    value: Any, path: str, *, required: bool = False
) -> datetime | None:
    """Parse an embedded PIT timestamp; ``None`` only when absent-allowed.

    With ``required=True`` a missing (``None``), blank, or unparseable
    value raises naming the path; naive datetimes always raise. With
    ``required=False`` only ``None`` yields ``None`` — any other invalid
    value still raises, so declared producer timestamps fail closed.
    """
    if value is None:
        if required:
            raise ValueError(f"{path} must be a valid timestamp")
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            if required:
                raise ValueError(f"{path} must be a valid timestamp") from None
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return parsed.astimezone(UTC)
    if required:
        raise ValueError(f"{path} must be a valid timestamp")
    return None


def _historical_date(
    value: Any, path: str, *, optional: bool = False
) -> datetime | None:
    """Parse a historical source date; ``None`` only for declared-optional nulls.

    Accepts ISO dates (``YYYY-MM-DD``, normalized to midnight UTC) and
    timezone-aware datetimes. Naive datetimes, unparseable text, and
    non-string scalars raise naming the path; a present ``None`` raises
    unless ``optional``.
    """
    if value is None:
        if optional:
            return None
        raise ValueError(f"{path} must be a valid date")
    if isinstance(value, str):
        if _ISO_DATE_RE.fullmatch(value):
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{path} must be a valid date") from None
            return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{path} must be a valid date") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return parsed.astimezone(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return value.astimezone(UTC)
    raise ValueError(f"{path} must be a valid date")


def _frozen_mapping(value: Any, field: str, *, minimum: int = 0) -> MappingProxyType:
    if (
        not isinstance(value, Mapping)
        or len(value) < minimum
        or len(value) > _MAX_MAPPING_KEYS
    ):
        raise ValueError(
            f"company benchmark {field} must be an object of 1..{_MAX_MAPPING_KEYS} keys"
            if minimum
            else f"company benchmark {field} must be an object of at most {_MAX_MAPPING_KEYS} keys"
        )
    return _freeze(value)


def _scan_producer_value(value: Any, as_of: datetime, *, depth: int, path: str) -> None:
    """Recursively reject evaluator-only keys and invalid or post-``as_of`` stamps.

    Every timestamp- or date-shaped key must belong to a declared temporal
    class; undeclared timestamp-like keys fail closed at any depth.
    """
    if depth > _MAX_SCAN_DEPTH:
        raise ValueError(f"{path}: producer payload nests too deeply")
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{path}: producer object exceeds {_MAX_CONTAINER_ITEMS} keys")
        for key, item in value.items():
            child = f"{path}.{key}"
            key_text = str(key)
            if key_text in _EVALUATOR_ONLY_KEYS:
                raise ValueError(
                    f"{child}: evaluator-only field is not reachable from a producer case"
                )
            if key_text in _PIT_INSTANT_KEYS:
                stamp = _pit_timestamp(item, child)
                if stamp is None:
                    raise ValueError(
                        f"{child}: present point-in-time timestamp field "
                        "must parse as a timezone-aware timestamp"
                    )
                if stamp > as_of:
                    raise ValueError(
                        f"{child}: timestamp is after as_of and cannot appear in a producer case"
                    )
                continue
            if key_text in _PIT_HISTORICAL_DATE_KEYS:
                historical = _historical_date(
                    item,
                    child,
                    optional=key_text in _OPTIONAL_HISTORICAL_DATE_KEYS,
                )
                if historical is not None and historical > as_of:
                    raise ValueError(
                        f"{child}: date is after as_of and cannot appear in a producer case"
                    )
                continue
            if key_text in _FORECAST_PERIOD_KEYS:
                _historical_date(item, child)
                continue
            if _TIMESTAMP_LIKE_KEY_RE.fullmatch(key_text):
                raise ValueError(
                    f"{child}: undeclared timestamp-like field is not recognized; "
                    "declare it in a temporal key class before use"
                )
            _scan_producer_value(item, as_of, depth=depth + 1, path=child)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{path}: producer list exceeds {_MAX_CONTAINER_ITEMS} items")
        for index, item in enumerate(value):
            _scan_producer_value(item, as_of, depth=depth + 1, path=f"{path}[{index}]")


def _news_items(value: Any, *, as_of: datetime) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or len(value) > _MAX_NEWS_ITEMS:
        raise ValueError(f"company benchmark news_items must contain up to {_MAX_NEWS_ITEMS} rows")
    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        frozen = _frozen_mapping(item, "news_items[]")
        for key in _NEWS_TIMESTAMP_KEYS:
            stamp = _pit_timestamp(frozen.get(key), f"news_items[{index}].{key}")
            if stamp is None or stamp > as_of:
                raise ValueError(
                    f"company benchmark news_items[{index}].{key} "
                    "must be a timestamp at or before as_of"
                )
        items.append(frozen)
    return tuple(items)


def _strings(value: Any, field: str, *, limit: int, width: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"company benchmark {field} must be a list of at most {limit} strings")
    return tuple(_text(item, field, width) for item in value)


def _rows(value: Any, field: str, *, limit: int) -> tuple[Any, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"company benchmark {field} must be a list of at most {limit} rows")
    rows: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            rows.append(_frozen_mapping(item, f"{field}[]"))
        else:
            rows.append(_text(item, field, 500))
    return tuple(rows)


def _aliases(value: Any, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= _MAX_ALIAS_ITEMS
    ):
        raise ValueError(
            f"company benchmark {field} must be a list of 1..{_MAX_ALIAS_ITEMS} strings"
        )
    return tuple(_text(item, field, _MAX_ALIAS_CHARS) for item in value)


def _forbidden_value(value: Any) -> int | float | str:
    """Finite number or bounded numeric string; anything else is rejected."""
    if isinstance(value, bool):
        raise ValueError("company benchmark forbidden value must be numeric")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("company benchmark forbidden value must be finite")
        return value
    if (
        isinstance(value, str)
        and 0 < len(value.strip()) <= _MAX_VALUE_CHARS
        and _NUMERIC_VALUE_RE.fullmatch(value.strip())
    ):
        return value
    raise ValueError(
        "company benchmark forbidden value must be a finite number "
        f"or a numeric string of up to {_MAX_VALUE_CHARS} characters"
    )


def _hindsight(value: Any, *, as_of: datetime) -> tuple[ForbiddenCompanyClaim, ...]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("company benchmark forbidden_hindsight must contain up to 50 rows")
    rows: list[ForbiddenCompanyClaim] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _FORBIDDEN_CLAIM_KEYS:
            raise ValueError("forbidden company claim row is invalid")
        claim_id = _text(item.get("claim_id"), "forbidden_hindsight.claim_id", _MAX_CLAIM_ID_CHARS)
        if claim_id in seen:
            raise ValueError(f"duplicate forbidden company claim_id '{claim_id}'")
        seen.add(claim_id)
        rows.append(
            ForbiddenCompanyClaim(
                claim_id=claim_id,
                metric_aliases=_aliases(item.get("metric_aliases"), "metric_aliases"),
                value=_forbidden_value(item.get("value")),
                period_aliases=_aliases(
                    item.get("period_aliases"), "period_aliases"
                ),
                available_after=_aware_datetime(
                    item.get("available_after"), "forbidden_hindsight.available_after"
                ),
            )
        )
        if rows[-1].available_after <= as_of:
            raise ValueError(
                f"forbidden company claim '{claim_id}' must be available strictly after as_of"
            )
    return tuple(rows)


def _producer_case(raw: Any, source_path: str) -> ProducerCase:
    if not isinstance(raw, Mapping) or set(raw) != _PRODUCER_KEYS:
        raise ValueError("company producer case has unexpected or missing fields")
    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported company producer schema_version")
    as_of = _aware_datetime(raw.get("as_of"), "as_of")
    _scan_producer_value(raw, as_of, depth=0, path="producer")
    excerpt_raw = raw.get("excerpt")
    if (
        not isinstance(excerpt_raw, str)
        or not excerpt_raw.strip()
        or len(excerpt_raw) > _MAX_EXCERPT_CHARS
    ):
        raise ValueError("company benchmark excerpt must be nonblank bounded text")
    excerpt_text = excerpt_raw
    prior_count_raw = raw.get("prior_count")
    if (
        not isinstance(prior_count_raw, int)
        or isinstance(prior_count_raw, bool)
        or not 0 <= prior_count_raw <= 1_000
    ):
        raise ValueError("company benchmark prior_count must be an integer of 0..1000")
    prior_count_value = prior_count_raw
    document = _frozen_mapping(raw.get("document"), "document", minimum=1)
    if not _DOCUMENT_REQUIRED_KEYS.issubset(document):
        raise ValueError("company benchmark document is missing required keys")
    for key in ("company", "symbol", "document_type", "region", "industry"):
        if not str(document.get(key) or "").strip():
            raise ValueError(f"company benchmark document.{key} must be nonblank")
    document_available_at = _pit_timestamp(
        document.get("available_at"), "document.available_at", required=True
    )
    if document_available_at > as_of:
        raise ValueError(
            "company benchmark document.available_at must be a timestamp at or before as_of"
        )
    news_items_value = _news_items(raw.get("news_items"), as_of=as_of)
    case = ProducerCase(
        schema_version=schema_version,
        case_id=_case_id(raw.get("case_id")),
        fixture_version=_version(raw.get("fixture_version")),
        as_of=as_of,
        document=document,
        excerpt=excerpt_text,
        deterministic_current=_frozen_mapping(raw.get("deterministic_current"), "deterministic_current"),
        deterministic_prior=_frozen_mapping(raw.get("deterministic_prior"), "deterministic_prior"),
        market_inputs=_frozen_mapping(raw.get("market_inputs"), "market_inputs"),
        prior_facts=_frozen_mapping(raw.get("prior_facts"), "prior_facts"),
        previous_state=(
            None
            if raw.get("previous_state") is None
            else _text(raw.get("previous_state"), "previous_state", 80)
        ),
        prior_count=prior_count_value,
        news_items=news_items_value,
        extraction=_frozen_mapping(raw.get("extraction"), "extraction"),
        fingerprint="",
        source_path=source_path,
    )
    # The fingerprint is derived, never stored-and-trusted: identity is
    # recomputed from the validated producer fields at every boundary.
    return _dataclass_replace(case, fingerprint=canonical_producer_fingerprint(case))


def _identity_as_of(as_of: datetime) -> str:
    """Canonical identity text for ``as_of``: UTC, ``Z``-suffixed ISO-8601.

    The shipped evaluator halves pin fingerprints computed over the raw
    fixture payloads whose timestamps end in ``Z``; the default
    ``datetime.isoformat()`` renders the same instant as ``+00:00``, which
    would silently rebind every case's identity. Normalizing to the ``Z``
    form keeps one canonical serialization for both fixture pairing and
    every later trust boundary.
    """
    stamp = as_of.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return stamp


def canonical_producer_fingerprint_payload(producer: ProducerCase) -> dict[str, Any]:
    """Normalized producer payload whose digest IS the producer identity."""
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": _case_id(producer.case_id),
        "fixture_version": _version(producer.fixture_version),
        "as_of": _identity_as_of(producer.as_of),
        "document": plain_copy(producer.document),
        "excerpt": producer.excerpt,
        "deterministic_current": plain_copy(producer.deterministic_current),
        "deterministic_prior": plain_copy(producer.deterministic_prior),
        "market_inputs": plain_copy(producer.market_inputs),
        "prior_facts": plain_copy(producer.prior_facts),
        "previous_state": producer.previous_state,
        "prior_count": int(producer.prior_count),
        "news_items": [plain_copy(item) for item in producer.news_items],
        "extraction": plain_copy(producer.extraction),
    }


def canonical_producer_fingerprint(producer: ProducerCase) -> str:
    """Canonical producer identity: SHA-256 over normalized fields only."""
    return canonical_fingerprint(canonical_producer_fingerprint_payload(producer))




def _evaluator_case(raw: Any, producer: ProducerCase, source_path: str) -> EvaluatorCase:
    if not isinstance(raw, Mapping) or set(raw) != _EVALUATOR_KEYS:
        raise ValueError("company evaluator case has unexpected or missing fields")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported company evaluator schema_version")
    if _case_id(raw.get("case_id")) != producer.case_id:
        raise ValueError("evaluator case_id does not match the producer case")
    if _version(raw.get("fixture_version")) != producer.fixture_version:
        raise ValueError("evaluator fixture_version does not match the producer case")
    producer_fingerprint = raw.get("producer_fingerprint")
    if not isinstance(producer_fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
        producer_fingerprint
    ):
        raise ValueError("evaluator producer_fingerprint must be SHA-256 hex")
    if producer_fingerprint != producer.fingerprint:
        raise ValueError("evaluator producer_fingerprint does not match the producer case")
    return EvaluatorCase(
        schema_version=raw.get("schema_version"),
        case_id=producer.case_id,
        fixture_version=producer.fixture_version,
        producer_fingerprint=producer_fingerprint,
        expected_material_observations=_strings(
            raw.get("expected_material_observations"),
            "expected_material_observations",
            limit=50,
            width=500,
        ),
        deterministic_checks=_rows(raw.get("deterministic_checks"), "deterministic_checks", limit=100),
        strongest_counter_thesis=_text(
            raw.get("strongest_counter_thesis"), "strongest_counter_thesis", 4_000
        ),
        expected_unknowns=_strings(
            raw.get("expected_unknowns"), "expected_unknowns", limit=50, width=500
        ),
        known_traps=_rows(raw.get("known_traps"), "known_traps", limit=50),
        later_outcomes=_rows(raw.get("later_outcomes"), "later_outcomes", limit=100),
        required_material_evidence=_strings(
            raw.get("required_material_evidence"),
            "required_material_evidence",
            limit=50,
            width=500,
        ),
        forbidden_hindsight=_hindsight(
            raw.get("forbidden_hindsight"), as_of=producer.as_of
        ),
        source_path=source_path,
    )


def load_producer_case(path: "str | Path | Mapping[str, Any]") -> ProducerCase:
    """Load the producer half from a fixture path or a canonical envelope mapping."""
    if isinstance(path, Mapping):
        # Envelope replay from artifact bytes: no source location involved.
        if "fingerprint" in path:
            asserted = path["fingerprint"]
            raw = {key: value for key, value in path.items() if key != "fingerprint"}
            producer = _producer_case(raw, "")
            if (
                not isinstance(asserted, str)
                or not hmac.compare_digest(producer.fingerprint, asserted)
            ):
                raise ValueError(
                    "company run producer envelope fingerprint does not match its contents"
                )
            return producer
        return _producer_case(path, "")
    source = Path(path)
    return _producer_case(
        yaml.safe_load(source.read_text(encoding="utf-8")), str(source)
    )


def load_evaluator_case(
    path: "str | Path | Mapping[str, Any]", *, producer: ProducerCase
) -> EvaluatorCase:
    """Load the evaluator half; it pairs only with the matching producer."""
    if isinstance(path, Mapping):
        return _evaluator_case(path, producer, "")
    source = Path(path)
    return _evaluator_case(
        yaml.safe_load(source.read_text(encoding="utf-8")), producer, str(source)
    )


def prepare_company_run(
    case: ProducerCase,
) -> "investment_service.InvestmentAnalysisRequest":
    """Build the exact production dispatch request from producer fields."""
    return investment_service.build_investment_analysis_request(
        plain_copy(case.document),
        case.excerpt,
        [plain_copy(item) for item in case.news_items],
        plain_copy(case.deterministic_current),
        plain_copy(case.deterministic_prior),
    )


def recorded_executor_output(
    content: object, provenance: Mapping[str, Any] | None = None
) -> RecordedExecutorOutput:
    """Wrap a recorded model response and bounded flat executor provenance."""
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > _MAX_CONTENT_CHARS
    ):
        raise ValueError("recorded executor content must be nonblank bounded text")
    frozen_provenance: Mapping[str, Any] = MappingProxyType({})
    if provenance is not None:
        if not isinstance(provenance, Mapping) or len(provenance) > 32:
            raise ValueError("recorded executor provenance must be an object of at most 32 keys")
        for key, value in provenance.items():
            if not isinstance(key, str) or len(key) > 80:
                raise ValueError("recorded executor provenance keys must be short strings")
            if not isinstance(value, (str, bool, int, float)) and value is not None:
                raise ValueError("recorded executor provenance values must be scalars")
            if isinstance(value, str) and len(value) > 300:
                raise ValueError("recorded executor provenance strings must be bounded")
        frozen_provenance = MappingProxyType(dict(provenance))
    return RecordedExecutorOutput(content=content, provenance=frozen_provenance)


def finalize_recorded_company_run(
    recorded: RecordedExecutorOutput, case: ProducerCase
) -> "investment_service.InvestmentFinalizedAnalysis":
    """Replay validation and pure finalization for one recorded run.

    Calls the production parse/validation/finalization seams on the recorded
    executor output with the producer-case inputs; performs no I/O of its own.
    """
    # Production parse path: recorded output bypasses provider-side
    # enforcement, so the exact repository parser gates the text first, then
    # the canonical re-encode feeds schema/grounding validation byte-stably.
    parsed = investment_service._parse_llm_json(recorded.content)
    canonical = json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    request = prepare_company_run(case)
    deterministic_current = plain_copy(case.deterministic_current)
    deterministic_prior = plain_copy(case.deterministic_prior)
    facts = investment_service._validated_investment_facts(
        canonical,
        excerpt=case.excerpt,
        news_items=[plain_copy(item) for item in case.news_items],
        deterministic_current=deterministic_current,
        deterministic_prior=deterministic_prior,
        document_metadata=plain_copy(case.document),
        relationship_facts=request.relationship_facts,
        material_relationships=request.material_relationships,
    )
    # Production finalization takes deep ownership of mutable case inputs.
    # Relationship inputs remain the exact frozen values prepared once above.
    return investment_service.finalize_investment_analysis(
        facts,
        document=plain_copy(case.document),
        deterministic_current=deterministic_current,
        deterministic_prior=deterministic_prior,
        relationship_facts=request.relationship_facts,
        material_relationships=request.material_relationships,
        market_inputs=plain_copy(case.market_inputs),
        stored_previous_facts=plain_copy(case.prior_facts),
        previous_state=case.previous_state,
        prior_count=case.prior_count,
        news_items=[plain_copy(item) for item in case.news_items],
        extraction=plain_copy(case.extraction),
    )


__all__ = [
    "SCHEMA_VERSION",
    "ForbiddenCompanyClaim",
    "canonical_producer_fingerprint",
    "canonical_producer_fingerprint_payload",
    "plain_copy",
    "RecordedExecutorOutput",
    "finalize_recorded_company_run",
    "load_evaluator_case",
    "load_producer_case",
    "prepare_company_run",
    "recorded_executor_output",
]
