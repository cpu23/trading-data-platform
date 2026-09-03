"""Deterministic 'what changed since my last view' summary.

The last-seen marker lives in the operator state directory.  The summary is
computed from bounded allowlisted queries only; no LLM call is made.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from config import live_updates_enabled, load_config
from db import query_many

router = APIRouter()

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
MARKER_FILE = STATE_DIR / "last_view.json"
_SUMMARY_LIMIT = 20


_MAX_AGE = timedelta(days=7)


def _parse_iso(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_last_view_marker() -> datetime | None:
    try:
        payload = json.loads(MARKER_FILE.read_text())
    except (OSError, ValueError):
        return None
    marker = (
        _parse_iso(payload.get("last_view_at")) if isinstance(payload, dict) else None
    )
    if marker is None:
        return None
    earliest = datetime.now(UTC) - _MAX_AGE
    return marker if marker >= earliest else earliest


def write_last_view_marker(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = MARKER_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last_view_at": current.isoformat()}))
    temporary.chmod(0o600)
    os.replace(temporary, MARKER_FILE)
    return current


def load_since_last_view(config: dict) -> dict:
    marker = read_last_view_marker()
    if marker is None:
        return {"available": True, "marker": None, "sections": [], "counts": {}}
    sections: list[dict] = []
    counts: dict[str, int] = {}

    def add(key: str, title: str, rows: list[dict]) -> None:
        counts[key] = len(rows)
        if rows:
            sections.append({"key": key, "title": title, "rows": rows})

    try:
        events = query_many(
            """SELECT e.id, e.source, e.event_type, e.observed_at, e.effective_at,
                      e.payload->>'title' AS title
               FROM market_events e
               JOIN event_materiality m ON m.event_id = e.id AND m.decision = 'route'
               WHERE e.observed_at > :marker
               ORDER BY e.observed_at DESC LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "events",
            "Material events",
            [
                {
                    "label": row["title"] or row["event_type"],
                    "detail": row["source"],
                    "at": _iso(row["effective_at"] or row["observed_at"]),
                }
                for row in events
            ],
        )
    except Exception:
        counts["events"] = 0
    try:
        clusters = query_many(
            """SELECT id, title, last_material_change_at
               FROM story_clusters
               WHERE last_material_change_at > :marker
               ORDER BY last_material_change_at DESC LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "stories",
            "Stories materially changed",
            [
                {
                    "label": row["title"],
                    "detail": None,
                    "at": _iso(row["last_material_change_at"]),
                }
                for row in clusters
            ],
        )
    except Exception:
        counts["stories"] = 0
    try:
        atoms = query_many(
            """SELECT claim_type, claim, status, created_at, updated_at
               FROM analysis_atoms
               WHERE created_at > :marker
                  OR (status = 'superseded' AND updated_at > :marker)
               ORDER BY GREATEST(created_at, updated_at) DESC LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "atoms",
            "New or superseded analysis atoms",
            [
                {
                    "label": row["claim"],
                    "detail": f"{row['claim_type']} · {row['status']}",
                    "at": _iso(row["updated_at"] or row["created_at"]),
                }
                for row in atoms
            ],
        )
    except Exception:
        counts["atoms"] = 0
    try:
        sources = query_many(
            """SELECT source, state, updated_at, reason_code
               FROM source_freshness_state
               WHERE updated_at > :marker
               ORDER BY updated_at DESC LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "sources",
            "Source failures or recoveries",
            [
                {
                    "label": row["source"],
                    "detail": row["state"],
                    "at": _iso(row["updated_at"]),
                }
                for row in sources
                if row["state"] in ("stale", "failed", "missing", "ok")
            ],
        )
    except Exception:
        counts["sources"] = 0
    try:
        drivers = query_many(
            """SELECT target, driver_label, direction, strength, horizon, valid_from
               FROM research_market_drivers
               WHERE superseded_at IS NULL
                 AND changed_since_prior = TRUE
                 AND valid_from > :marker
               ORDER BY valid_from DESC, target
               LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "market_drivers",
            "Major-market interpretation changed",
            [
                {
                    "label": f"{row['target']}: {row['driver_label']}",
                    "detail": (
                        f"{row['direction']} · {row['strength']} · {row['horizon']}"
                    ),
                    "at": _iso(row["valid_from"]),
                }
                for row in drivers
            ],
        )
    except Exception:
        counts["market_drivers"] = 0
    try:
        cases = query_many(
            """SELECT c.id, c.title, c.lifecycle_state,
                      s.payload->'deliverable'->'what_changed'->>'text' AS what_changed,
                      c.last_changed_at
               FROM research_cases c
               LEFT JOIN research_case_snapshots s
                 ON s.case_id = c.id AND s.version = c.current_version
               WHERE c.last_changed_at > :marker
                 AND c.lifecycle_state IN ('research_ready', 'mature', 'weakening')
                 AND c.economic_significance = 'high'
               ORDER BY c.last_changed_at DESC, c.id DESC
               LIMIT :limit""",
            params={"marker": marker, "limit": _SUMMARY_LIMIT},
            config=config,
        )
        add(
            "research_cases",
            "Material research developments",
            [
                {
                    "label": row["title"],
                    "detail": row["what_changed"] or row["lifecycle_state"],
                    "at": _iso(row["last_changed_at"]),
                    "href": f"/research/cases/{row['id']}",
                }
                for row in cases
            ],
        )
    except Exception:
        counts["research_cases"] = 0
    return {
        "available": True,
        "marker": marker.isoformat(),
        "sections": sections,
        "counts": counts,
    }


def _iso(value):
    parsed = _parse_iso(value)
    return parsed.isoformat() if parsed else None


@router.get("/partials/dashboard/since-last-view")
def partial_since_last_view(request: Request):
    config = load_config()
    try:
        summary = load_since_last_view(config)
    except Exception:
        summary = {"available": False, "marker": None, "sections": [], "counts": {}}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/since_last_view.html",
        {
            "request": request,
            "since_last_view": summary,
            "live_updates_enabled": live_updates_enabled(config),
        },
    )


@router.post("/api/dashboard/last-view")
async def post_last_view(request: Request):
    marker = await run_in_threadpool(write_last_view_marker)
    return JSONResponse({"status": "ok", "last_view_at": marker.isoformat()})
