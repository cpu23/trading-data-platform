import json
import os
import re
from uuid import uuid4

from db import get_session
from llm_client import call_llm, resolve_model
from logging_config import get_logger
from processors._validators import (
    OutputPolicyError,
    coerce_common_enums,
    repair_prompt,
    validate_macro_regime_output,
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

    def process(self, config: dict, correlation_id: str) -> dict:
        ff_config = config.get("processors", {}).get("macro_regime", {})
        thresholds = ff_config.get("thresholds", DEFAULT_THRESHOLDS)
        prompt_template_path = ff_config.get(
            "prompt_template", "prompts/macro_regime_v1.txt"
        )

        macro_data = self._fetch_macro_data(config)
        if not macro_data:
            raise RuntimeError("No macro data available — run FRED collector first")

        indicator_table = self._format_indicator_table(macro_data)
        changes_table = self._format_changes_table(macro_data)
        cross_indicators = self._build_cross_indicators(macro_data, thresholds)

        prompt_text = self._build_prompt(
            template_path=prompt_template_path,
            indicator_table=indicator_table,
            changes_table=changes_table,
            cross_indicators=cross_indicators,
        )

        model = resolve_model(config, processor_id=self.processor_id)

        llm_result = call_llm(
            prompt=prompt_text,
            model=model,
            correlation_id=correlation_id,
            config=config,
        )

        raw_response = llm_result["content"]
        parsed = self._validate_and_repair_output(
            raw_response=raw_response,
            prompt_text=prompt_text,
            llm_result=llm_result,
            model=model,
            config=config,
            correlation_id=correlation_id,
        )
        raw_response = llm_result["content"]

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
                llm_result.get("tokens_input", 0) + llm_result.get("tokens_output", 0)
            ),
            "cost_usd": llm_result.get("cost_usd", 0.0),
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
            "regime": parsed.get("regime", "transition"),
            "sub_regime": parsed.get("sub_regime"),
            "confidence": parsed.get("confidence", "low"),
            "supporting_data": {
                "market_implications": parsed.get("market_implications", ""),
                "momentum_implications": parsed.get("market_implications", ""),
                "caution_flags": parsed.get("caution_flags", []),
                "key_indicators": key_indicators,
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
            },
            "output_id": opinion_id,
            "prompt_text": prompt_text,
            "raw_response": raw_response,
            "model_used": llm_result.get("model", model),
            "tokens_input": llm_result.get("tokens_input", 0),
            "tokens_output": llm_result.get("tokens_output", 0),
            "cost_usd": llm_result.get("cost_usd", 0.0),
        }

        return {
            "opinion": opinion,
            "extra_records": {"regime_classifications": [classification]},
            "processing_log": processing_log,
        }

    def get_prompt_version(self) -> str:
        return "macro_regime_v1"

    def get_depends_on(self) -> list[str]:
        return ["fred"]

    def _fetch_macro_data(self, config: dict) -> dict:
        sql = text("""
            SELECT series_id, observed_at, value FROM (
                SELECT series_id, observed_at, value,
                       ROW_NUMBER() OVER (
                           PARTITION BY series_id ORDER BY observed_at DESC
                       ) AS observation_rank
                FROM macro_series
            ) ranked
            WHERE observation_rank <= 2
            ORDER BY series_id, observed_at DESC
        """)

        with get_session(config) as session:
            result = session.execute(sql)
            rows = [dict(row._mapping) for row in result]

        grouped: dict[str, list] = {}
        for row in rows:
            sid = row["series_id"]
            if sid not in grouped:
                grouped[sid] = []
            grouped[sid].append(row)

        macro_data: dict[str, dict] = {}
        for sid, observations in grouped.items():
            latest = observations[0] if len(observations) >= 1 else None
            previous = observations[1] if len(observations) >= 2 else None

            entry: dict = {}
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
            if (
                latest
                and previous
                and latest["value"] is not None
                and previous["value"] is not None
            ):
                if previous["value"] != 0:
                    entry["change_pct"] = round(
                        ((latest["value"] - previous["value"]) / abs(previous["value"]))
                        * 100,
                        2,
                    )
                else:
                    entry["change_pct"] = None
            macro_data[sid] = entry

        return macro_data

    def _format_indicator_table(self, data: dict) -> str:
        if not data:
            return "No macro data available."

        lines = [
            "Series ID    | Latest Value | Date       | Previous Value | Change %",
            "-------------|------------- |------------|----------------|--------",
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
            change = entry.get("change_pct")
            change_str = f"{change:+.2f}%" if change is not None else "N/A"

            lines.append(
                f"{sid:<13}| {str(latest):<13}| {str(latest_date)[:10]:<11}| {str(previous):<15}| {change_str}"
            )
        return "\n".join(lines)

    def _format_changes_table(self, data: dict) -> str:
        if not data:
            return "No change data available."

        lines = []
        for sid in sorted(data.keys()):
            entry = data[sid]
            change = entry.get("change_pct")
            if change is not None:
                direction = "UP" if change > 0 else "DOWN" if change < 0 else "FLAT"
                lines.append(f"  {sid}: {change:+.2f}% ({direction})")
        return "\n".join(lines) if lines else "No percentage changes available."

    def _build_cross_indicators(self, data: dict, thresholds: dict) -> dict[str, str]:
        result = {}

        yc_thresholds = thresholds.get("yield_curve", DEFAULT_THRESHOLDS["yield_curve"])
        t10y2y_entry = data.get("T10Y2Y", {})
        t10y2y_val = t10y2y_entry.get("latest")
        result["t10y2y_value"] = (
            f"{t10y2y_val:.2f}" if t10y2y_val is not None else "N/A"
        )
        result["t10y2y_interpretation"] = self._interpret_yield_curve(
            t10y2y_val, yc_thresholds
        )

        t10y3m_entry = data.get("T10Y3M", {})
        t10y3m_val = t10y3m_entry.get("latest")
        result["t10y3m_value"] = (
            f"{t10y3m_val:.2f}" if t10y3m_val is not None else "N/A"
        )
        result["t10y3m_interpretation"] = self._interpret_yield_curve(
            t10y3m_val, yc_thresholds
        )

        cs_thresholds = thresholds.get(
            "credit_spread", DEFAULT_THRESHOLDS["credit_spread"]
        )
        hy_entry = data.get("BAMLH0A0HYM2", {})
        hy_val = hy_entry.get("latest")
        result["hy_spread_value"] = f"{hy_val:.2f}" if hy_val is not None else "N/A"
        result["hy_spread_interpretation"] = self._interpret_credit_spread(
            hy_val, cs_thresholds
        )

        vix_thresholds = thresholds.get("vix", DEFAULT_THRESHOLDS["vix"])
        vix_entry = data.get("VIXCLS", {})
        vix_val = vix_entry.get("latest")
        result["vix_value"] = f"{vix_val:.2f}" if vix_val is not None else "N/A"
        result["vix_interpretation"] = self._interpret_vix(vix_val, vix_thresholds)

        dxy_entry = data.get("DTWEXBGS", {})
        dxy_val = dxy_entry.get("latest")
        dxy_prev = dxy_entry.get("previous")
        result["dxy_value"] = f"{dxy_val:.2f}" if dxy_val is not None else "N/A"
        result["dxy_interpretation"] = self._interpret_dxy(dxy_val, dxy_prev)

        t5yie_entry = data.get("T5YIE", {})
        t5yie_val = t5yie_entry.get("latest")
        result["t5yie_value"] = f"{t5yie_val:.2f}" if t5yie_val is not None else "N/A"
        result["t5yie_interpretation"] = self._interpret_breakeven(t5yie_val)

        t10yie_entry = data.get("T10YIE", {})
        t10yie_val = t10yie_entry.get("latest")
        result["t10yie_value"] = (
            f"{t10yie_val:.2f}" if t10yie_val is not None else "N/A"
        )
        result["t10yie_interpretation"] = self._interpret_breakeven(t10yie_val)

        return result

    @staticmethod
    def _interpret_yield_curve(value: float | None, thresholds: dict) -> str:
        if value is None:
            return "data unavailable"
        if value < thresholds.get("deep_inversion", -0.5):
            return "deeply inverted — strong recession signal"
        if value < thresholds.get("inverted", 0):
            return "inverted — recession risk elevated"
        if value < thresholds.get("flat", 0.5):
            return "flat — late cycle, uncertain direction"
        if value < thresholds.get("normal", 1.5):
            return "normal — moderate growth expectations"
        return "steep — strong growth expectations or easing cycle"

    @staticmethod
    def _interpret_vix(value: float | None, thresholds: dict) -> str:
        if value is None:
            return "data unavailable"
        if value < thresholds.get("very_low", 12):
            return "very low — complacency, strong risk-on"
        if value < thresholds.get("low", 16):
            return "low — calm financial conditions"
        if value < thresholds.get("moderate", 20):
            return "moderate — normal range"
        if value < thresholds.get("elevated", 25):
            return "elevated — increased uncertainty"
        if value < thresholds.get("high", 30):
            return "high — fear rising and financial conditions tightening"
        return "very high — severe market stress and risk aversion"

    @staticmethod
    def _interpret_credit_spread(value: float | None, thresholds: dict) -> str:
        if value is None:
            return "data unavailable"
        if value < thresholds.get("tight", 3.0):
            return "tight — risk appetite strong"
        if value < thresholds.get("normal", 4.0):
            return "normal — stable conditions"
        if value < thresholds.get("widening", 5.0):
            return "widening — caution emerging"
        return "wide — stress, risk-off conditions"

    @staticmethod
    def _interpret_dxy(value: float | None, previous: float | None = None) -> str:
        if value is None:
            return "data unavailable"
        if previous is not None:
            change = value - previous
            if change > 1.0:
                return "strengthening — dollar gaining ground"
            if change < -1.0:
                return "weakening — dollar losing ground"
            return "stable — little change"
        return f"current level {value:.1f}"

    @staticmethod
    def _interpret_breakeven(value: float | None) -> str:
        if value is None:
            return "data unavailable"
        if value < 1.5:
            return "very low — deflation concerns"
        if value < 2.0:
            return "below target — subdued inflation expectations"
        if value < 2.5:
            return "near target — stable inflation expectations"
        if value < 3.0:
            return "above target — rising inflation expectations"
        return "elevated — significant inflation concerns"

    def _build_prompt(
        self,
        template_path: str,
        indicator_table: str,
        changes_table: str,
        cross_indicators: dict[str, str],
    ) -> str:
        template_path = template_path
        if not os.path.isabs(template_path):
            config_dir = os.environ.get("CONFIG_DIR", "/app")
            template_path = os.path.join(config_dir, template_path)

        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Prompt template not found: {template_path}")

        with open(template_path) as f:
            template = f.read()

        result = template
        result = result.replace("{{indicator_table}}", indicator_table)
        result = result.replace("{{changes_table}}", changes_table)

        for key, value in cross_indicators.items():
            result = result.replace("{{" + key + "}}", value)

        return result

    def _validate_and_repair_output(
        self,
        raw_response: str,
        prompt_text: str,
        llm_result: dict,
        model: str,
        config: dict,
        correlation_id: str,
    ) -> dict:
        parsed, issues = self._parse_coerce_validate(raw_response)
        if not issues:
            return parsed

        logger.warning(
            "macro_regime_validation_failed_retrying",
            action="validate_macro_regime",
            warnings=issues,
            correlation_id=correlation_id,
        )
        try:
            retry_result = call_llm(
                prompt=repair_prompt(prompt_text, issues),
                model=model,
                correlation_id=correlation_id,
                config=config,
            )
        except Exception as exc:
            repair_issues = [f"repair call failed: {exc}"]
            logger.error(
                "macro_regime_output_quarantined",
                action="repair_macro_regime",
                warnings=repair_issues,
                correlation_id=correlation_id,
            )
            raise OutputPolicyError(self.processor_id, repair_issues) from exc
        repaired, repair_issues = self._parse_coerce_validate(
            retry_result.get("content")
        )
        if repair_issues:
            logger.error(
                "macro_regime_output_quarantined",
                action="validate_macro_regime_retry",
                warnings=repair_issues,
                correlation_id=correlation_id,
            )
            raise OutputPolicyError(self.processor_id, repair_issues)

        self._adopt_repair_result(llm_result, retry_result)
        return repaired

    def _parse_coerce_validate(
        self, raw_response: str
    ) -> tuple[dict | None, list[str]]:
        try:
            parsed = self._parse_llm_response(raw_response)
        except ValueError as exc:
            return None, [f"invalid JSON response: {exc}"]
        if not isinstance(parsed, dict):
            return None, ["top-level JSON value must be an object"]
        coerce_common_enums(parsed)
        valid, issues = validate_macro_regime_output(parsed)
        return parsed, [] if valid else issues

    @staticmethod
    def _adopt_repair_result(initial: dict, repair: dict) -> None:
        initial["content"] = repair["content"]
        initial["model"] = repair.get("model", initial.get("model"))
        initial["tokens_input"] = initial.get("tokens_input", 0) + repair.get(
            "tokens_input", 0
        )
        initial["tokens_output"] = initial.get("tokens_output", 0) + repair.get(
            "tokens_output", 0
        )
        initial["cost_usd"] = initial.get("cost_usd", 0.0) + repair.get(
            "cost_usd", 0.0
        )

    @staticmethod
    def _parse_llm_response(response_text: str) -> dict:
        if not isinstance(response_text, str):
            raise ValueError("LLM response content must be a string")
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

        logger.error(
            "llm_response_parse_failed",
            action="parse_llm_response",
            raw_response=response_text[:2000],
        )
        raise ValueError(
            f"Could not parse LLM response as JSON. Raw text:\n{response_text[:500]}"
        )
