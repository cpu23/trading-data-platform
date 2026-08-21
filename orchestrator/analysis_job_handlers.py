"""Deterministic handlers for durable analysis jobs.

Handlers intentionally query only bounded, allowlisted columns and publish through
``section_snapshots``.  They never include source payloads or error details in a
snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from config_loader import load_config

_MAX_ROWS = 200
_SOURCE_COLUMNS = (
    "source",
    "state",
    "expected_next_at",
    "last_success_at",
    "last_attempt_at",
    "last_material_change_at",
    "lag_seconds",
    "consecutive_failures",
    "updated_at",
)
_MARKET_COLUMNS = (
    "symbol",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _job_value(job: Any, key: str, default: Any = None) -> Any:
    if isinstance(job, Mapping):
        return job.get(key, default)
    return getattr(job, key, default)


def _bound(value: Any, default: int = _MAX_ROWS) -> int:
    try:
        return max(1, min(_MAX_ROWS, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=UTC)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(UTC)
        )
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _timestamp(parsed)
        except ValueError:
            return None
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _raw_rows(result: Any) -> list[dict[str, Any]]:
    try:
        result = result.mappings()
    except AttributeError:
        pass
    rows = []
    for row in result:
        mapping = getattr(row, "_mapping", row)
        rows.append(dict(mapping))
    return rows


def _serialise_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()} for row in rows
    ]


def _job_settings() -> Mapping[str, Any]:
    config = load_config()
    settings = _mapping(_mapping(config).get("event_pipeline")).get("jobs", {})
    return _mapping(settings)


def _config() -> Mapping[str, Any]:
    return _mapping(load_config())


def _source_health_rows(
    session: Any,
) -> tuple[list[dict[str, Any]], int, datetime | None]:
    settings = _job_settings()
    query_settings = _mapping(settings.get("query"))
    limit = _bound(
        query_settings.get("max_source_rows", settings.get("max_source_rows", 100))
    )
    columns = ", ".join(_SOURCE_COLUMNS)
    result = session.execute(
        text(
            f"SELECT {columns} FROM source_freshness_state "
            "ORDER BY updated_at DESC, source ASC LIMIT :limit"
        ),
        {"limit": limit},
    )
    raw_rows = _raw_rows(result)
    freshness = max(
        (_timestamp(row.get("updated_at")) for row in raw_rows),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return _serialise_rows(raw_rows), limit, freshness


def publish_source_health_snapshot(session: Any, job: Any) -> Any:
    """Publish a bounded source-freshness view without private diagnostics."""
    from section_snapshots import publish_section_snapshot

    rows, limit, freshness = _source_health_rows(session)
    return publish_section_snapshot(
        session,
        section_key="source_health",
        scope_key="global",
        payload={"sources": rows},
        render_context={"row_limit": limit},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def _configured_instruments() -> list[str]:
    config = _config()
    oanda = _mapping(_mapping(config.get("collectors")).get("oanda"))
    values: list[str] = []
    for item in oanda.get("instruments", ()) or ():
        instrument = item.get("symbol") or item.get("oanda_instrument")
        if isinstance(instrument, str) and instrument.strip():
            values.append(instrument.strip())
    if not values:
        watchlist = _mapping(config.get("watchlist"))
        trading = watchlist.get("trading", ()) or ()
        for item in trading:
            symbol = item.get("symbol") if isinstance(item, Mapping) else item
            if isinstance(symbol, str) and symbol.strip():
                values.append(symbol.strip())
    return list(dict.fromkeys(values))


def _watchlist_rows(
    session: Any,
) -> tuple[list[dict[str, Any]], int, datetime | None]:
    settings = _job_settings()
    query_settings = _mapping(settings.get("query"))
    limit = _bound(
        query_settings.get(
            "max_watchlist_rows", settings.get("max_watchlist_rows", 100)
        )
    )
    symbols = _configured_instruments()
    if not symbols:
        return [], limit, None
    columns = ", ".join(_MARKET_COLUMNS)
    # One bounded query. DISTINCT ON keeps the latest row per configured instrument.
    result = session.execute(
        text(
            f"SELECT DISTINCT ON (symbol) {columns} FROM market_data "
            "WHERE symbol = ANY(:symbols) ORDER BY symbol, timestamp DESC LIMIT :limit"
        ),
        {"symbols": symbols, "limit": limit},
    )
    raw_rows = _raw_rows(result)
    freshness = max(
        (_timestamp(row.get("timestamp")) for row in raw_rows),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return _serialise_rows(raw_rows), limit, freshness


def publish_watchlist_snapshot(session: Any, job: Any) -> Any:
    """Publish the latest bounded OANDA watchlist rows, including an empty payload."""
    from section_snapshots import publish_section_snapshot

    rows, limit, freshness = _watchlist_rows(session)
    return publish_section_snapshot(
        session,
        section_key="watchlist",
        scope_key="global",
        payload={"instruments": rows},
        render_context={"row_limit": limit},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def _source_event_ids(job: Any) -> tuple[str, ...]:
    source_event_id = _job_value(job, "source_event_id")
    if source_event_id is None:
        return ()
    return (str(source_event_id),)


_PUBLIC_RELEASE_COLUMNS = (
    "id",
    "release_identity",
    "series_id",
    "revision_number",
    "event_name",
    "actual",
    "consensus",
    "previous",
    "revised_previous",
    "absolute_surprise",
    "standardized_surprise",
    "impact",
    "source",
    "observed_at",
    "released_at",
    "revision_at",
    "quality_flags",
    "stage",
    "reaction_summary",
    "created_at",
)


def _public_release_cards(session: Any) -> tuple[list[dict[str, Any]], datetime | None]:
    from macro_releases import list_macro_release_cards

    settings = _job_settings()
    query = _mapping(settings.get("query"))
    limit = _bound(query.get("max_release_cards", 20), default=20)
    rows = list_macro_release_cards(session, limit=limit, current_only=True)
    cards = [
        {
            key: _json_value(row.get(key))
            for key in _PUBLIC_RELEASE_COLUMNS
            if key in row
        }
        for row in rows
    ]
    freshness = max(
        (
            _timestamp(row.get("released_at"))
            or _timestamp(row.get("observed_at"))
            or _timestamp(row.get("created_at"))
            for row in rows
        ),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return cards, freshness


def publish_macro_release_snapshot(session: Any, job: Any) -> Any:
    """Publish a bounded current-card view with no raw source payloads."""
    from section_snapshots import publish_section_snapshot

    cards, freshness = _public_release_cards(session)
    return publish_section_snapshot(
        session,
        section_key="macro_releases",
        scope_key="global",
        payload={"cards": cards},
        render_context={"row_limit": len(cards)},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def update_macro_release_reactions(session: Any, job: Any) -> Any:
    """Backfill eligible windows, advance the card, then republish the section."""
    from macro_releases import advance_macro_release_stage
    from reaction_windows import backfill_reaction_windows, list_event_reactions

    config = _config()
    payload = _mapping(_job_value(job, "payload", {}))
    event_id = payload.get("event_id") or _job_value(job, "source_event_id")
    if event_id is None:
        raise ValueError("macro reaction job requires an event id")
    reaction_settings = _mapping(config.get("reaction_windows"))
    limit = _bound(reaction_settings.get("backfill_limit", 100))
    backfill = backfill_reaction_windows(session, config, limit=limit)
    rows = list_event_reactions(session, event_id, limit=limit)
    public_windows = [
        {
            key: _json_value(row.get(key))
            for key in (
                "instrument_symbol",
                "timeframe",
                "horizon",
                "target_at",
                "observed_at",
                "absolute_move",
                "percentage_move",
                "volatility_adjusted_move",
                "direction_vs_expected",
                "reaction_state",
                "missing_data_reason",
            )
        }
        for row in rows
    ]
    summary = {
        "completed": sum(
            row.get("reaction_state") != "pending"
            and row.get("missing_data_reason") is None
            for row in rows
        ),
        "pending": sum(row.get("reaction_state") == "pending" for row in rows),
        "windows": public_windows,
        "backfill": {
            key: value
            for key, value in backfill.items()
            if key in {"scanned", "completed", "unresolved", "skipped_future"}
        },
    }
    advance_macro_release_stage(
        session,
        event_id,
        str(payload.get("stage") or "developing"),
        reaction_summary=summary,
    )
    return publish_macro_release_snapshot(session, job)


_PUBLIC_STORY_COLUMNS = (
    "id",
    "canonical_key",
    "title",
    "summary",
    "state",
    "lane",
    "first_seen_at",
    "last_seen_at",
    "last_material_change_at",
    "importance",
    "novelty",
    "confidence",
    "entities",
    "markets",
    "source_count",
    "version",
    "change_summary",
)


def _public_story_clusters(
    session: Any,
) -> tuple[list[dict[str, Any]], datetime | None]:
    from stories import list_story_clusters

    settings = _job_settings()
    query = _mapping(settings.get("query"))
    limit = _bound(query.get("max_story_clusters", 50), default=50)
    rows = list_story_clusters(session, limit=limit)
    cluster_ids = [row.get("id") for row in rows if row.get("id") is not None]
    confirmations: dict[str, list[dict[str, Any]]] = {}
    if cluster_ids:
        confirmation_rows = _raw_rows(
            session.execute(
                text(
                    """SELECT cluster_id, market_symbol, observed_at,
                       pre_headline_move, move_5m, move_30m, move_session, flags
                       FROM story_market_confirmations
                       WHERE cluster_id = ANY(:cluster_ids)
                       ORDER BY observed_at DESC, market_symbol
                       LIMIT :limit"""
                ),
                {"cluster_ids": cluster_ids, "limit": _MAX_ROWS},
            )
        )
        for row in confirmation_rows:
            confirmations.setdefault(str(row.get("cluster_id")), []).append(
                {
                    key: _json_value(row.get(key))
                    for key in (
                        "market_symbol",
                        "observed_at",
                        "pre_headline_move",
                        "move_5m",
                        "move_30m",
                        "move_session",
                        "flags",
                    )
                }
            )
    clusters = [
        {
            **{
                key: _json_value(row.get(key))
                for key in _PUBLIC_STORY_COLUMNS
                if key in row
            },
            "market_confirmations": confirmations.get(str(row.get("id")), []),
        }
        for row in rows
    ]
    freshness = max(
        (
            _timestamp(row.get("last_seen_at")) or _timestamp(row.get("updated_at"))
            for row in rows
        ),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return clusters, freshness


def publish_story_clusters_snapshot(session: Any, job: Any) -> Any:
    """Publish bounded canonical stories and descriptive market observations."""
    from section_snapshots import publish_section_snapshot

    clusters, freshness = _public_story_clusters(session)
    return publish_section_snapshot(
        session,
        section_key="news_clusters",
        scope_key="global",
        payload={"clusters": clusters},
        render_context={"row_limit": len(clusters)},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def update_story_market_confirmation(session: Any, job: Any) -> Any:
    """Refresh one story's market observations and republish canonical stories."""
    from story_confirmation import calculate_story_confirmation

    payload = _mapping(_job_value(job, "payload", {}))
    cluster_id = payload.get("cluster_id")
    event_id = payload.get("event_id") or _job_value(job, "source_event_id")
    if cluster_id is None or event_id is None:
        raise ValueError("story confirmation job requires cluster and event ids")
    calculate_story_confirmation(
        session,
        cluster_id,
        event_id,
        _config(),
    )
    return publish_story_clusters_snapshot(session, job)


def publish_analysis_atoms_snapshot(session: Any, job: Any) -> Any:
    """Publish bounded current analysis atoms with their evidence links."""
    from atoms import current_atoms
    from section_snapshots import publish_section_snapshot

    settings = _job_settings()
    query = _mapping(settings.get("query"))
    limit = _bound(query.get("max_atoms", 50), default=50)
    rows = current_atoms(session, limit=limit)
    atoms = _serialise_rows(rows)
    freshness = max(
        (_timestamp(row.get("valid_from")) for row in rows),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return publish_section_snapshot(
        session,
        section_key="analysis_atoms",
        scope_key="global",
        payload={"atoms": atoms},
        render_context={"row_limit": limit},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def expire_analysis_atoms(session: Any, job: Any) -> Any:
    """Expire atoms past their horizon, then republish the section snapshot."""
    from atoms import expire_atoms

    expire_atoms(session, _config())
    return publish_analysis_atoms_snapshot(session, job)


def publish_research_snapshot(session: Any, job: Any) -> Any:
    """Publish bounded research themes with their entity and thesis counts."""
    from research import list_themes
    from section_snapshots import publish_section_snapshot

    settings = _job_settings()
    query = _mapping(settings.get("query"))
    limit = _bound(query.get("max_themes", 20), default=20)
    rows = list_themes(session, limit=limit)
    themes = _serialise_rows(rows)
    freshness = max(
        (_timestamp(row.get("updated_at")) for row in rows),
        key=lambda value: value or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    return publish_section_snapshot(
        session,
        section_key="research",
        scope_key="global",
        payload={"themes": themes},
        render_context={"row_limit": limit},
        source_event_ids=_source_event_ids(job),
        data_freshness_at=freshness,
        analysis_freshness_at=datetime.now(UTC),
    )


def run_research_discovery_job(session: Any, job: Any) -> dict[str, Any]:
    """Run hot-market drivers and bounded dynamic case discovery durably."""
    from research_intelligence.service import run_discovery, run_macro_transmission

    config = dict(_config())
    payload = _mapping(_job_value(job, "payload", {}))
    correlation_id = str(_job_value(job, "correlation_id", "") or "") or None
    force = bool(payload.get("force", False))
    errors = 0
    try:
        macro = run_macro_transmission(
            session,
            config,
            correlation_id=correlation_id,
            force=force,
        )
    except Exception as exc:
        macro = {"status": "failed", "error": type(exc).__name__, "driver_count": 0}
        errors += 1
    discovery = run_discovery(
        session,
        config,
        correlation_id=correlation_id,
        force=force,
    )
    errors += len(discovery.get("errors", []))
    raw_cases = discovery.get("cases", [])
    cases = raw_cases if isinstance(raw_cases, list) else []
    case_count = sum(
        1
        for outcome in cases
        if isinstance(outcome, Mapping) and outcome.get("case_id")
    )
    abstention_count = sum(
        1
        for outcome in cases
        if isinstance(outcome, Mapping) and outcome.get("status") == "abstained"
    )
    return {
        "status": "completed" if errors == 0 else "completed_with_errors",
        "case_count": case_count,
        "candidate_count": int(discovery.get("candidate_count") or len(cases)),
        "abstention_count": abstention_count,
        "driver_count": int(macro.get("driver_count") or 0),
        "lifecycle_transition_count": len(discovery.get("lifecycle_transitions", [])),
        "error_count": errors,
        "cost_usd": float(discovery.get("model_cost_usd") or 0)
        + float(macro.get("model_cost_usd") or 0),
    }


def run_research_case_update_job(session: Any, job: Any) -> dict[str, Any]:
    """Run one evidence-linked research-case update through the durable worker."""
    from research_intelligence.service import run_case_update

    payload = _mapping(_job_value(job, "payload", {}))
    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("research case update requires a case id")
    result = run_case_update(
        session,
        dict(_config()),
        case_id,
        correlation_id=str(_job_value(job, "correlation_id", "") or "") or None,
        force=bool(payload.get("force", False)),
    )
    return {
        **result,
        "id": case_id,
        "case_id": case_id,
        "cost_usd": float(result.get("model_cost_usd") or 0),
    }


def run_investment_analysis_job(session: Any, job: Any) -> dict[str, Any]:
    """Analyze one ingested investment document through the durable worker.

    ``analyze_document`` opens its own bounded sessions and enforces the
    daily LLM budget; the durable queue supplies retry/lease semantics and
    deduplication via the ``investment-analysis:{document_id}`` identity.
    """
    from investment_service import (
        MAX_OCR_PAGES,
        OCR_WALL_SECONDS,
        analyze_document,
    )

    payload = _mapping(_job_value(job, "payload", {}))
    document_id = str(payload.get("document_id") or "").strip()
    if not document_id:
        raise ValueError("investment analysis job requires a document_id")
    market_inputs = payload.get("market_inputs")
    if not isinstance(market_inputs, Mapping):
        market_inputs = None
    result = analyze_document(
        dict(_config()),
        document_id,
        market_inputs,
        # The durable worker is not HTTP-bound: allow the full OCR page/wall
        # budgets for scan-heavy regulatory documents.
        ocr_page_budget=MAX_OCR_PAGES,
        ocr_wall_seconds=OCR_WALL_SECONDS,
    )
    analysis_id = result.get("analysis_id") if isinstance(result, Mapping) else None
    return {
        "status": "completed",
        "document_id": document_id,
        "analysis_id": str(analysis_id) if analysis_id else None,
    }


def run_thesis_autonomy_job(session: Any, job: Any) -> dict[str, Any]:
    """Run one bounded autonomous thesis-fusion cycle durably.

    The cycle runs entirely inside the worker's caller-owned transaction;
    the returned result is bounded (status, error count, model cost) and
    safe for ``result_ref`` persistence.
    """
    from thesis_autonomy import run_autonomous_thesis_cycle

    payload = _mapping(_job_value(job, "payload", {}))
    result = run_autonomous_thesis_cycle(
        session,
        dict(_config()),
        correlation_id=str(_job_value(job, "correlation_id", "") or "") or None,
        as_of=payload.get("as_of") or None,
    )
    return {
        "status": str(result.get("status") or "partial"),
        "error_count": int(result.get("error_count") or 0),
        "cost_usd": float(result.get("cost_usd") or 0),
        "promoted_count": int(result.get("promoted_count") or 0),
        "falsification_runs": int(result.get("falsification_runs") or 0),
        "challenge_attempts": int(result.get("challenge_attempts") or 0),
        "challenge_limit": int(result.get("challenge_limit") or 0),
        "challenger_failures": int(result.get("challenger_failures") or 0),
        "role_failures": int(result.get("role_failures") or 0),
        "promotion_gate_rejections": int(result.get("promotion_gate_rejections") or 0),
        "source_gate_rejections": int(result.get("source_gate_rejections") or 0),
        "opposition_gate_rejections": int(
            result.get("opposition_gate_rejections") or 0
        ),
        "semantic_audit_rejections": int(result.get("semantic_audit_rejections") or 0),
    }


_HANDLERS = {
    "publish_source_health_snapshot": publish_source_health_snapshot,
    "publish_watchlist_snapshot": publish_watchlist_snapshot,
    "publish_macro_release_snapshot": publish_macro_release_snapshot,
    "update_macro_release_reactions": update_macro_release_reactions,
    "publish_story_clusters_snapshot": publish_story_clusters_snapshot,
    "update_story_market_confirmation": update_story_market_confirmation,
    "publish_analysis_atoms_snapshot": publish_analysis_atoms_snapshot,
    "expire_analysis_atoms": expire_analysis_atoms,
    "publish_research_snapshot": publish_research_snapshot,
    "research_discovery": run_research_discovery_job,
    "research_case_update": run_research_case_update_job,
    "investment_analysis": run_investment_analysis_job,
    "thesis_autonomy_run": run_thesis_autonomy_job,
}


def route_job(session: Any, job: Any) -> Any:
    """Dispatch a claimed job; unknown types are rejected without leaking payloads."""
    job_type = _job_value(job, "job_type")
    try:
        handler = _HANDLERS[str(job_type)]
    except KeyError as exc:
        raise ValueError("unsupported analysis job type") from exc
    return handler(session, job)


__all__ = [
    "expire_analysis_atoms",
    "publish_analysis_atoms_snapshot",
    "publish_macro_release_snapshot",
    "publish_research_snapshot",
    "publish_source_health_snapshot",
    "publish_watchlist_snapshot",
    "publish_story_clusters_snapshot",
    "route_job",
    "run_investment_analysis_job",
    "run_research_case_update_job",
    "run_research_discovery_job",
    "run_thesis_autonomy_job",
    "update_macro_release_reactions",
    "update_story_market_confirmation",
]
