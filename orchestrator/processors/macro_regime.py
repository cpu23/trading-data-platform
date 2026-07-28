import json
import re
from uuid import uuid4

from budgets import BudgetContext
from db import get_session
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger
from processors.base import load_prompt_template
from processors.macro_trends import (
    HISTORY_LIMIT,
    analyze_macro_trends,
    build_macro_synthesis,
    format_macro_synthesis,
    format_trend_signals,
)
from sqlalchemy import text

logger = get_logger("processor.macro_regime")

SERIES_USED = [
    "GDP",
    "GDPC1",
    "CPIAUCSL",
    "PCEPILFE",
    "T5YIE",
    "T10YIE",
    "UNRATE",
    "PAYEMS",
    "ICSA",
    "FEDFUNDS",
    "DGS2",
    "DGS10",
    "T10Y2Y",
    "T10Y3M",
    "BAMLH0A0HYM2",
    "VIXCLS",
    "M2SL",
    "DTWEXBGS",
    "IRLTLT01GBM156N",
    "OECD:CLI_US",
    "OECD:CLI_DE",
    "OECD:CLI_GB",
    "OECD:CLI_JP",
    "ECB:DEPOSIT_RATE",
    "ECB:ESTR",
    "ECB:CISS",
    "ECB:CREDIT_NFC",
    "ECB:GOVT_10Y",
    "BOE:BANK_RATE",
    "BOE:M4",
    "BOE:TOTAL_LENDING_INDIVIDUALS",
    "BOE:MORTGAGE_APPROVALS",
    "DCOILBRENTEU",
    "DCOILWTICO",
    "EIA:CRUDE_STOCKS",
    "EIA:NATGAS_STORAGE",
]

CROSS_INDICATOR_SERIES = {
    "t10y2y": "T10Y2Y",
    "t10y3m": "T10Y3M",
    "hy_spread": "BAMLH0A0HYM2",
    "vix": "VIXCLS",
    "dxy": "DTWEXBGS",
    "t5yie": "T5YIE",
    "t10yie": "T10YIE",
}

DEFAULT_THRESHOLDS = {
    "yield_curve": {
        "deep_inversion": -0.5,
        "inverted": 0,
        "flat": 0.5,
        "normal": 1.5,
    },
    "vix": {
        "very_low": 12,
        "low": 16,
        "moderate": 20,
        "elevated": 25,
        "high": 30,
    },
    "credit_spread": {
        "tight": 3.0,
        "normal": 4.0,
        "widening": 5.0,
    },
}


class MacroRegimeProcessor:
    processor_id = "macro_regime"
    PROCESSOR_SCHEMA_VERSION = "1"

    def process(
        self,
        config: dict,
        correlation_id: str,
        budget_context: BudgetContext | None = None,
    ) -> dict:
        ff_config = config.get("processors", {}).get("macro_regime", {})
        thresholds = ff_config.get("thresholds", DEFAULT_THRESHOLDS)
        prompt_template_path = ff_config.get(
            "prompt_template", "prompts/macro_regime_v2.txt"
        )

        macro_data = self._fetch_macro_data(config)
        if not macro_data:
            raise RuntimeError(
                "No macro data available — run the macro collectors first"
            )

        indicator_table = self._format_indicator_table(macro_data)
        trend_signals = analyze_macro_trends(macro_data, thresholds)
        deterministic_trends = format_trend_signals(trend_signals)
        synthesis = build_macro_synthesis(trend_signals)
        deterministic_synthesis = format_macro_synthesis(synthesis)

        prompt_text = self._build_prompt(
            template_path=prompt_template_path,
            indicator_table=indicator_table,
            deterministic_trends=deterministic_trends,
            deterministic_synthesis=deterministic_synthesis,
        )

        stage = LLMStage(
            config,
            self.processor_id,
            correlation_id=correlation_id,
            budget_context=budget_context,
        )
        llm_result = stage.call(prompt_text)
        raw_response = llm_result["content"]
        try:
            parsed = self._parse_llm_response(raw_response)
        except ValueError as exc:
            stage.add_validation_warnings(["response was not valid JSON"])
            if stage.policy.validation_retries < 1:
                raise LLMValidationError(
                    "LLM response validation failed", stage.telemetry
                ) from exc
            retry_prompt = (
                prompt_text
                + "\n\nIMPORTANT CORRECTION: Return only one valid JSON object matching "
                "the exact schema above. Do not include markdown or commentary."
            )
            llm_result = stage.call(retry_prompt)
            raw_response = llm_result["content"]
            try:
                parsed = self._parse_llm_response(raw_response)
            except ValueError as retry_exc:
                stage.add_validation_warnings(["final response was not valid JSON"])
                raise LLMValidationError(
                    "LLM response validation failed after retry", stage.telemetry
                ) from retry_exc

        model = stage.policy.model

        opinion_id = str(uuid4())
        classification_id = str(uuid4())
        parsed["opinion_id"] = opinion_id
        parsed["classification_id"] = classification_id

        series_ids_used = sorted(macro_data.keys())
        dates = []
        for sid in series_ids_used:
            entry = macro_data[sid]
            if entry.get("latest_date"):
                dates.append(entry["latest_date"])
            if entry.get("previous_date"):
                dates.append(entry["previous_date"])
        date_range = {"from": min(dates), "to": max(dates)} if dates else {}

        opinion = {
            "opinion_id": opinion_id,
            "opinion_type": "macro_regime",
            "scope": "global_macro",
            "direction": parsed.get("direction", "neutral"),
            "confidence": parsed.get("confidence", "low"),
            "timeframe": parsed.get("timeframe", "medium_term"),
            "summary": parsed.get("summary", ""),
            "key_factors": parsed.get("key_factors", []),
            "reasoning": parsed.get("reasoning", ""),
            "data_inputs": {
                "table": "macro_series",
                "series_ids": series_ids_used,
                "date_range": date_range,
                "record_count": len(macro_data),
            },
            "model_used": llm_result.get("model", model),
            "prompt_version": self.get_prompt_version(),
            "tokens_used": (
                stage.telemetry.tokens_input_total + stage.telemetry.tokens_output_total
            ),
            "cost_usd": stage.telemetry.cost_usd_total,
        }

        key_indicators = {}
        for key, series_id in CROSS_INDICATOR_SERIES.items():
            if (
                series_id in macro_data
                and macro_data[series_id].get("latest") is not None
            ):
                key_indicators[key] = macro_data[series_id]["latest"]

        classification = {
            "classification_id": classification_id,
            "scope": "global",
            "regime": parsed.get("regime", "quiet"),
            "sub_regime": parsed.get("sub_regime"),
            "confidence": parsed.get("confidence", "low"),
            "supporting_data": {
                "momentum_implications": parsed.get("momentum_implications", ""),
                "caution_flags": parsed.get("caution_flags", []),
                "key_indicators": key_indicators,
                "deterministic_trends": trend_signals,
                "deterministic_synthesis": synthesis,
            },
            "opinion_id": opinion_id,
        }

        processing_log = {
            "processor": self.processor_id,
            "status": "success",
            "input_summary": {
                "table": "macro_series",
                "series_count": len(macro_data),
                "latest_observation": max(dates) if dates else None,
                "oldest_observation": min(dates) if dates else None,
                **stage.telemetry.as_dict(),
            },
            "output_id": opinion_id,
            "prompt_text": None,
            "raw_response": None,
            "model_used": llm_result.get("model", model),
            "tokens_input": stage.telemetry.tokens_input_total,
            "tokens_output": stage.telemetry.tokens_output_total,
            "cost_usd": stage.telemetry.cost_usd_total,
        }

        return {
            "opinion": opinion,
            "extra_records": {"regime_classifications": [classification]},
            "processing_log": processing_log,
        }

    def get_prompt_version(self) -> str:
        return "macro_regime_v2"

    def get_prompt_identity(self, config: dict) -> dict[str, str]:
        processor_config = config.get("processors", {}).get("macro_regime", {})
        template_path = processor_config.get(
            "prompt_template", "prompts/macro_regime_v2.txt"
        )
        _, identity = load_prompt_template(template_path)
        return identity

    def get_depends_on(self) -> list[str]:
        return ["fred"]

    def get_fingerprint_inputs(self, config: dict) -> dict:
        """Return revision-aware observations for every value consumed by trend rules."""
        series_ids = sorted(set(SERIES_USED) | set(CROSS_INDICATOR_SERIES.values()))
        sql = text("""
            WITH ranked_observations AS (
                SELECT series_id, observed_at, value, updated_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id
                           ORDER BY observed_at DESC
                       ) AS observation_rank
                FROM macro_series
                WHERE series_id = ANY(:ids)
            )
            SELECT series_id, observed_at, value, updated_at
            FROM ranked_observations
            WHERE observation_rank <= :history_limit
            ORDER BY series_id ASC, observed_at ASC
        """)
        with get_session(config) as session:
            rows = [
                dict(row._mapping)
                for row in session.execute(
                    sql, {"ids": series_ids, "history_limit": HISTORY_LIMIT}
                )
            ]
        rows.sort(key=lambda row: (str(row["series_id"]), str(row["observed_at"])))
        thresholds = (
            config.get("processors", {})
            .get("macro_regime", {})
            .get("thresholds", DEFAULT_THRESHOLDS)
        )
        return {
            "series_ids": series_ids,
            "observations": rows,
            "thresholds": thresholds,
        }

    def _fetch_macro_data(self, config: dict) -> dict:
        sql = text("""
            WITH ranked_observations AS (
                SELECT series_id, observed_at, value,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id
                           ORDER BY observed_at DESC
                       ) AS observation_rank
                FROM macro_series
                WHERE series_id = ANY(:ids)
            )
            SELECT series_id, observed_at, value
            FROM ranked_observations
            WHERE observation_rank <= :history_limit
            ORDER BY series_id, observed_at DESC
        """)

        series_ids = list(set(SERIES_USED) | set(CROSS_INDICATOR_SERIES.values()))

        with get_session(config) as session:
            result = session.execute(
                sql, {"ids": series_ids, "history_limit": HISTORY_LIMIT}
            )
            rows = [dict(row._mapping) for row in result]

        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["series_id"], []).append(row)

        macro_data: dict[str, dict] = {}
        for sid, observations in grouped.items():
            latest = observations[0] if observations else None
            previous = observations[1] if len(observations) >= 2 else None
            entry: dict = {"history": observations}
            if latest:
                entry["latest"] = latest["value"]
                entry["latest_date"] = (
                    latest["observed_at"].isoformat()
                    if hasattr(latest["observed_at"], "isoformat")
                    else str(latest["observed_at"])
                )
            if previous:
                entry["previous"] = previous["value"]
                entry["previous_date"] = (
                    previous["observed_at"].isoformat()
                    if hasattr(previous["observed_at"], "isoformat")
                    else str(previous["observed_at"])
                )
            macro_data[sid] = entry

        return macro_data

    def _format_indicator_table(self, data: dict) -> str:
        if not data:
            return "No macro data available."

        lines = [
            "Series ID    | Latest Value | Date       | Previous Value",
            "-------------|------------- |------------|---------------",
        ]
        for sid in sorted(data.keys()):
            entry = data[sid]
            latest = entry.get("latest", "N/A")
            latest_date = entry.get("latest_date", "N/A")
            if isinstance(latest, float):
                latest = f"{latest:,.2f}"
            previous = entry.get("previous", "N/A")
            if isinstance(previous, float):
                previous = f"{previous:,.2f}"
            lines.append(
                f"{sid:<13}| {str(latest):<13}| {str(latest_date)[:10]:<11}| {str(previous)}"
            )
        return "\n".join(lines)

    def _build_prompt(
        self,
        template_path: str,
        indicator_table: str,
        deterministic_trends: str,
        deterministic_synthesis: str,
    ) -> str:
        template, _ = load_prompt_template(template_path)
        result = template.replace("{{indicator_table}}", indicator_table)
        result = result.replace("{{deterministic_trends}}", deterministic_trends)
        return result.replace("{{deterministic_synthesis}}", deterministic_synthesis)

    @staticmethod
    def _parse_llm_response(response_text: str) -> dict:
        text = response_text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        logger.error("llm_response_parse_failed", action="parse_llm_response")
        raise ValueError("Could not parse LLM response as JSON")
