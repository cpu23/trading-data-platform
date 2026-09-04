import json

from api_db import query_many, query_one
from fastapi import APIRouter, HTTPException

from config import load_config

router = APIRouter()


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _as_mapping(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


@router.get("/evidence/{opinion_id}")
def get_evidence(opinion_id: str):
    config = load_config()
    opinion = query_one(
        "SELECT * FROM structured_opinions WHERE opinion_id = :opinion_id",
        {"opinion_id": opinion_id},
        config=config,
    )
    if not opinion:
        raise HTTPException(status_code=404, detail="Opinion not found")

    data_inputs = _as_mapping(opinion.get("data_inputs"))
    processing = query_one(
        """
        SELECT log_id, correlation_id, processor, started_at, completed_at, status,
               model_used, tokens_input, tokens_output, cost_usd, prompt_text
        FROM processing_log
        WHERE output_id = CAST(:opinion_id AS UUID)
           OR CAST(:opinion_id AS UUID) = ANY(COALESCE(output_ids, ARRAY[]::UUID[]))
        ORDER BY started_at DESC LIMIT 1
        """,
        {"opinion_id": opinion_id},
        config=config,
    )

    records = {}
    series_ids = data_inputs.get("series_ids") or data_inputs.get("series") or []
    if isinstance(series_ids, list) and series_ids:
        records["macro_series"] = query_many(
            """
            SELECT DISTINCT ON (series_id) series_id, observed_at, value, source
            FROM macro_series WHERE series_id = ANY(:series_ids)
            ORDER BY series_id, observed_at DESC
            """,
            {"series_ids": series_ids},
            config=config,
        )

    event_ids = data_inputs.get("event_ids") or []
    if isinstance(event_ids, list) and event_ids:
        records["econ_events"] = query_many(
            "SELECT event_id, event_name, country, scheduled_at, impact_level, source FROM econ_events WHERE event_id = ANY(:event_ids) ORDER BY scheduled_at",
            {"event_ids": event_ids},
            config=config,
        )

    opinion_ids = (
        data_inputs.get("opinion_ids") or data_inputs.get("opinions_used") or []
    )
    if isinstance(opinion_ids, list) and opinion_ids:
        records["structured_opinions"] = query_many(
            "SELECT opinion_id, created_at, opinion_type, scope, direction, confidence, summary FROM structured_opinions WHERE opinion_id::text = ANY(:opinion_ids)",
            {"opinion_ids": opinion_ids},
            config=config,
        )

    generation_attempts = []
    correlation_id = opinion.get("correlation_id")
    if correlation_id:
        generation_attempts = query_many(
            """
            SELECT attempt_id, stage, attempt_number, status, validation_issues,
                   model_used, tokens_input, tokens_output, cost_usd, duration_ms,
                   created_at
            FROM generation_attempts
            WHERE correlation_id = :correlation_id
            ORDER BY created_at, stage, attempt_number
            """,
            {"correlation_id": correlation_id},
            config=config,
        )

    return _json_value(
        {
            "opinion": opinion,
            "data_inputs": data_inputs,
            "processing": processing,
            "generation_attempts": generation_attempts,
            "records": records,
        }
    )
