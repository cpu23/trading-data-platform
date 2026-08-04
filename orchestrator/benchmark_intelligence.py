"""Benchmark market-intelligence model/provider combinations on frozen inputs."""

import argparse
import copy
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text

from config_loader import load_config
from db import get_session
from processors.intelligence import MarketIntelligenceProcessor

V4 = "deepseek/deepseek-v4-flash"
GPT_OSS = "openai/gpt-oss-120b"

PROFILES = {
    "v4_baidu": {
        "model": V4,
        "reasoning_effort": "high",
        "max_tokens": 5000,
        "provider": {"order": ["Baidu"], "allow_fallbacks": False},
    },
    "v4_siliconflow": {
        "model": V4,
        "reasoning_effort": "high",
        "max_tokens": 5000,
        "provider": {"order": ["SiliconFlow"], "allow_fallbacks": False},
    },
    "gpt_wandb": {
        "model": GPT_OSS,
        "reasoning_effort": "medium",
        "max_tokens": 4200,
        "provider": {"order": ["WandB"], "allow_fallbacks": False},
    },
    "gpt_novita": {
        "model": GPT_OSS,
        "reasoning_effort": "medium",
        "max_tokens": 4200,
        "provider": {"order": ["Novita"], "allow_fallbacks": False},
    },
    "gpt_wandb_editor": {
        "model": GPT_OSS,
        "reasoning_effort": "low",
        "max_tokens": 8000,
        "provider": {"order": ["WandB"], "allow_fallbacks": False},
    },
    "gpt_wandb_fast": {
        "model": GPT_OSS,
        "reasoning_effort": "low",
        "max_tokens": 7000,
        "provider": {"order": ["WandB"], "allow_fallbacks": False},
    },
    "gpt_novita_editor": {
        "model": GPT_OSS,
        "reasoning_effort": "low",
        "max_tokens": 8000,
        "provider": {"order": ["Novita"], "allow_fallbacks": False},
    },
    "gpt_wandb_high": {
        "model": GPT_OSS,
        "reasoning_effort": "high",
        "max_tokens": 4800,
        "provider": {"order": ["WandB"], "allow_fallbacks": False},
    },
}

V4_EDITOR_BAIDU = {**PROFILES["v4_baidu"], "max_tokens": 7500}
V4_EDITOR_SILICONFLOW = {**PROFILES["v4_siliconflow"], "max_tokens": 7500}

VARIANTS = {
    "v4_baidu_all": {
        **{role: PROFILES["v4_baidu"] for role in ("analyst", "skeptic", "auditor")},
        "editor": V4_EDITOR_BAIDU,
    },
    "gpt_wandb_all": {
        role: PROFILES["gpt_wandb"]
        for role in ("analyst", "skeptic", "auditor", "editor")
    },
    "gpt_novita_all": {
        role: PROFILES["gpt_novita"]
        for role in ("analyst", "skeptic", "auditor", "editor")
    },
    "gpt_wandb_high_all": {
        role: PROFILES["gpt_wandb_high"]
        for role in ("analyst", "skeptic", "auditor", "editor")
    },
    "gpt_wandb_tiered": {
        "analyst": PROFILES["gpt_wandb_high"],
        "skeptic": PROFILES["gpt_wandb"],
        "auditor": PROFILES["gpt_wandb"],
        "editor": PROFILES["gpt_wandb_high"],
    },
    "gpt_wandb_compact": {
        **{role: PROFILES["gpt_wandb"] for role in ("analyst", "skeptic", "auditor")},
        "editor": PROFILES["gpt_wandb_editor"],
    },
    "gpt_wandb_fast_all": {
        role: PROFILES["gpt_wandb_fast"]
        for role in ("analyst", "skeptic", "auditor", "editor")
    },
    "gpt_wandb_audited_fast": {
        "analyst": PROFILES["gpt_wandb_fast"],
        "skeptic": PROFILES["gpt_wandb_fast"],
        "auditor": PROFILES["gpt_wandb"],
        "editor": PROFILES["gpt_wandb_fast"],
    },
    "gpt_fast_v4_auditor": {
        "analyst": PROFILES["gpt_wandb_fast"],
        "skeptic": PROFILES["gpt_wandb_fast"],
        "auditor": {**PROFILES["v4_siliconflow"], "max_tokens": 6500},
        "editor": PROFILES["gpt_wandb_fast"],
    },
    "gpt_fast_v4_baidu_auditor": {
        "analyst": PROFILES["gpt_wandb_fast"],
        "skeptic": PROFILES["gpt_wandb_fast"],
        "auditor": {**PROFILES["v4_baidu"], "max_tokens": 6500},
        "editor": PROFILES["gpt_wandb_fast"],
    },
    "gpt_novita_compact": {
        **{role: PROFILES["gpt_novita"] for role in ("analyst", "skeptic", "auditor")},
        "editor": PROFILES["gpt_novita_editor"],
    },
    "gpt_roles_v4_editor": {
        **{role: PROFILES["gpt_wandb"] for role in ("analyst", "skeptic", "auditor")},
        "editor": V4_EDITOR_SILICONFLOW,
    },
    "v4_analyst_editor_gpt_checks": {
        "analyst": PROFILES["v4_siliconflow"],
        "skeptic": PROFILES["gpt_wandb"],
        "auditor": PROFILES["gpt_wandb"],
        "editor": V4_EDITOR_SILICONFLOW,
    },
    "v4_roles_gpt_editor": {
        **{
            role: PROFILES["v4_siliconflow"]
            for role in ("analyst", "skeptic", "auditor")
        },
        "editor": PROFILES["gpt_wandb"],
    },
}


def _attempt_metrics(config, correlation_id):
    with get_session(config) as session:
        rows = session.execute(
            text(
                """
                SELECT stage, attempt_number, status, tokens_input, tokens_output,
                       cost_usd, duration_ms, request_metadata
                FROM generation_attempts
                WHERE correlation_id = :correlation_id
                ORDER BY created_at
                """
            ),
            {"correlation_id": correlation_id},
        ).fetchall()
    attempts = [dict(row._mapping) for row in rows]
    for attempt in attempts:
        for key in ("cost_usd",):
            if attempt.get(key) is not None:
                attempt[key] = float(attempt[key])
        metadata = attempt.get("request_metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        attempt["provider"] = metadata.get("provider_returned")
        attempt.pop("request_metadata", None)
    return attempts


def _compact_result(result):
    opinions = result["opinions"]
    return {
        "global": next(
            opinion["payload"]
            for opinion in opinions
            if opinion["opinion_type"] == "narrative_memory"
        ),
        "assets": {
            opinion["scope"].split("asset:", 1)[-1]: opinion["payload"]
            for opinion in opinions
            if opinion["opinion_type"] == "asset_panel"
        },
    }


def _deterministic_score(output, attempts):
    assets = output["assets"]
    summaries = [
        output["global"].get("summary", ""),
        *[asset.get("summary", "") for asset in assets.values()],
    ]
    word_counts = [len(str(summary).split()) for summary in summaries]
    repairs = sum(attempt["attempt_number"] > 1 for attempt in attempts)
    validation_failures = sum(
        attempt["status"] == "validation_failed" for attempt in attempts
    )
    evidence_count = sum(
        len(asset.get("summary_evidence", {}).get("evidence_ids", []))
        for asset in assets.values()
    )
    return {
        "assets_present": len(assets),
        "average_summary_words": round(sum(word_counts) / max(1, len(word_counts)), 1),
        "max_summary_words": max(word_counts, default=0),
        "repairs": repairs,
        "validation_failures": validation_failures,
        "asset_summary_evidence_count": evidence_count,
    }


def run_variant(base_config, name, profiles):
    config = copy.deepcopy(base_config)
    config.setdefault("processors", {}).setdefault("market_intelligence", {})[
        "force_inference"
    ] = True
    config["llm"]["intelligence_roles"] = copy.deepcopy(profiles)
    correlation_id = str(uuid4())
    with get_session(config) as session:
        session.execute(
            text(
                """
                INSERT INTO cycle_runs (
                    correlation_id, status, started_at, triggered_by, run_kind,
                    requested_component, publication_status, summary
                ) VALUES (
                    :correlation_id, 'running', NOW(), 'benchmark', 'processor',
                    'market_intelligence', 'pending', CAST(:summary AS JSONB)
                )
                """
            ),
            {
                "correlation_id": correlation_id,
                "summary": json.dumps({"benchmark_variant": name}),
            },
        )
    started = time.monotonic()
    processor = MarketIntelligenceProcessor()
    try:
        result = processor.process(config, correlation_id)
        status = "success"
        error = None
        output = _compact_result(result)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        output = None
    wall_ms = int((time.monotonic() - started) * 1000)
    attempts = _attempt_metrics(config, correlation_id)
    metrics = {
        "wall_ms": wall_ms,
        "tokens_input": sum(item.get("tokens_input") or 0 for item in attempts),
        "tokens_output": sum(item.get("tokens_output") or 0 for item in attempts),
        "cost_usd": round(sum(item.get("cost_usd") or 0 for item in attempts), 8),
        "attempts": attempts,
    }
    if output:
        metrics.update(_deterministic_score(output, attempts))
    with get_session(config) as session:
        session.execute(
            text(
                """
                UPDATE cycle_runs
                SET status = :status,
                    result_status = :result_status,
                    publication_status = 'failed',
                    completed_at = NOW(),
                    error_message = :error
                WHERE correlation_id = :correlation_id
                """
            ),
            {
                "status": "completed" if status == "success" else "failed",
                "result_status": status,
                "error": error,
                "correlation_id": correlation_id,
            },
        )
    return {
        "name": name,
        "status": status,
        "error": error,
        "correlation_id": correlation_id,
        "profiles": profiles,
        "metrics": metrics,
        "output": output,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variants",
        default=",".join(VARIANTS),
        help="Comma-separated variant names",
    )
    parser.add_argument("--output", default="/tmp/intelligence-benchmark.json")
    args = parser.parse_args()
    selected = [item.strip() for item in args.variants.split(",") if item.strip()]
    unknown = [item for item in selected if item not in VARIANTS]
    if unknown:
        raise SystemExit(f"Unknown variants: {', '.join(unknown)}")

    config = load_config()
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "variants": [],
    }
    for name in selected:
        print(f"BENCHMARK_START {name}", flush=True)
        result = run_variant(config, name, VARIANTS[name])
        report["variants"].append(result)
        print(
            "BENCHMARK_DONE "
            + json.dumps(
                {
                    "name": name,
                    "status": result["status"],
                    **result["metrics"],
                    "attempts": len(result["metrics"]["attempts"]),
                },
                default=str,
            ),
            flush=True,
        )
        with open(args.output, "w") as output_file:
            json.dump(report, output_file, indent=2, default=str)
    print(f"BENCHMARK_REPORT {args.output}", flush=True)


if __name__ == "__main__":
    main()
