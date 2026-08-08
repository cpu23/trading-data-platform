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
    try:
        config = load_config()
    except Exception:
        return {}
    settings = _mapping(_mapping(config).get("event_pipeline")).get("jobs", {})
    return _mapping(settings)


def _config() -> Mapping[str, Any]:
    try:
        return _mapping(load_config())
    except Exception:
        return {}


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
    "update_macro_release_reactions",
    "update_story_market_confirmation",
]
