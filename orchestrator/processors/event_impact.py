import json
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from budgets import BudgetContext
from db import get_session
from llm_client import LLMStage
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("processor.event_impact")


class EventImpactProcessor:
    processor_id = "event_impact"

    def process(
        self,
        config: dict,
        correlation_id: str,
        budget_context: BudgetContext | None = None,
    ) -> dict:
        ff_config = config.get("processors", {}).get("event_impact", {})
        prompt_template_path = ff_config.get(
            "prompt_template", "prompts/event_impact_v1.txt"
        )

        events = self._fetch_upcoming_events(config)
        watchlist = self._format_watchlist(config)
        current_regime = self._get_current_regime(config)

        if not events:
            return self._static_no_events_result(correlation_id)

        events_table = self._format_events_table(events)

        prompt_text = self._build_prompt(
            template_path=prompt_template_path,
            events_table=events_table,
            watchlist=watchlist,
            current_regime=current_regime,
        )

        stage = LLMStage(
            config,
            self.processor_id,
            correlation_id=correlation_id,
            budget_context=budget_context,
        )
        model = stage.policy.model
        llm_result = stage.call(prompt_text)

        raw_response = llm_result["content"]
        parsed = self._parse_llm_response(raw_response)

        opinion_id = str(uuid4())

        event_names = [e.get("event_name", "") for e in parsed.get("events", [])]
        event_ids = [
            f"{e.get('event_name', '')}-{e.get('scheduled_at', '')}"
            for e in parsed.get("events", [])
        ]

        direction = self._derive_direction(parsed)
        confidence = "high" if len(events) >= 3 else "moderate" if len(events) >= 1 else "low"

        opinion = {
            "opinion_id": opinion_id,
            "opinion_type": "event_impact",
            "scope": "upcoming_48h",
            "direction": direction,
            "confidence": confidence,
            "timeframe": "short_term",
            "summary": self._build_summary(parsed),
            "key_factors": event_names,
            "reasoning": f"{parsed.get('overall_volatility_outlook', '')} {parsed.get('risk_management_note', '')}",
            "data_inputs": {
                "table": "econ_events",
                "event_count": len(events),
                "window": "next_48h",
                "events": event_ids,
            },
            "model_used": llm_result.get("model", model),
            "prompt_version": self.get_prompt_version(),
            "tokens_used": (
                llm_result.get("tokens_input", 0) + llm_result.get("tokens_output", 0)
            ),
            "cost_usd": llm_result.get("cost_usd", 0.0),
        }

        processing_log = {
            "processor": self.processor_id,
            "status": "success",
            "input_summary": {
                "table": "econ_events",
                "event_count": len(events),
                "window": "next_48h",
                **stage.telemetry.as_dict(),
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
            "extra_records": {},
            "processing_log": processing_log,
        }

    def get_prompt_version(self) -> str:
        return "event_impact_v1"

    def get_depends_on(self) -> list[str]:
        return ["forex_factory"]

    def _fetch_upcoming_events(self, config: dict) -> list[dict]:
        sql = text("""
            SELECT event_name, country, scheduled_at, impact_level, consensus, previous, actual
            FROM econ_events
            WHERE scheduled_at > NOW()
              AND scheduled_at < NOW() + INTERVAL '48 hours'
              AND impact_level = 'high'
            ORDER BY scheduled_at ASC
        """)

        with get_session(config) as session:
            result = session.execute(sql)
            rows = [dict(row._mapping) for row in result]

        return rows

    def _format_watchlist(self, config: dict) -> str:
        watchlist = config.get("watchlist", {}).get("trading", [])
        if not watchlist:
            return "No watchlist configured."

        items = [f"{w['symbol']} ({w['type']})" for w in watchlist]
        return ", ".join(items)

    def _get_current_regime(self, config: dict) -> str:
        sql = text("""
            SELECT rc.regime, rc.sub_regime, rc.confidence, rc.supporting_data,
                   so.summary, so.key_factors
            FROM regime_classifications rc
            JOIN structured_opinions so ON rc.opinion_id = so.opinion_id
            ORDER BY rc.created_at DESC
            LIMIT 1
        """)

        try:
            with get_session(config) as session:
                result = session.execute(sql)
                row = result.fetchone()

            if row is None:
                return "Current macro regime: Not yet classified"

            r = dict(row._mapping)
            regime = r.get("regime", "unknown").upper()
            sub_regime = r.get("sub_regime", "")
            if sub_regime and sub_regime != "null":
                regime += f" ({sub_regime.replace('_', ' ').title()})"
            confidence = r.get("confidence", "unknown")

            summary = r.get("summary", "")
            key_factors = r.get("key_factors", [])
            if isinstance(key_factors, str):
                try:
                    key_factors = json.loads(key_factors)
                except (json.JSONDecodeError, TypeError):
                    key_factors = []

            supporting = r.get("supporting_data", {})
            if isinstance(supporting, str):
                try:
                    supporting = json.loads(supporting)
                except (json.JSONDecodeError, TypeError):
                    supporting = {}

            key_indicators = supporting.get("key_indicators", {})
            indicator_parts = []
            for k, v in key_indicators.items():
                if v is not None:
                    indicator_parts.append(f"{k}: {v}")

            indicators_str = ", ".join(indicator_parts) if indicator_parts else "no indicators available"

            return (
                f"Current macro regime: {regime}, {confidence} confidence. "
                f"Key: {indicators_str}."
            )
        except Exception:
            return "Current macro regime: Not yet classified"

    def _format_events_table(self, events: list[dict]) -> str:
        lines = [
            "Event Name                | Country | Scheduled At        | Consensus | Previous",
            "--------------------------|---------|---------------------|-----------|---------",
        ]

        for e in events:
            name = e.get("event_name", "Unknown")[:26]
            country = e.get("country", "?")
            scheduled = str(e.get("scheduled_at", ""))[:19]
            consensus = str(e.get("consensus", "N/A"))
            previous = str(e.get("previous", "N/A"))

            lines.append(
                f"{name:<26}| {country:<8}| {scheduled:<20}| {consensus:<10}| {previous}"
            )

        return "\n".join(lines)

    def _build_prompt(
        self,
        template_path: str,
        events_table: str,
        watchlist: str,
        current_regime: str,
    ) -> str:
        if not os.path.isabs(template_path):
            config_dir = os.environ.get("CONFIG_DIR", "/app")
            template_path = os.path.join(config_dir, template_path)

        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Prompt template not found: {template_path}")

        with open(template_path) as f:
            template = f.read()

        result = template
        result = result.replace("{{events_table}}", events_table)
        result = result.replace("{{watchlist}}", watchlist)
        result = result.replace("{{current_regime}}", current_regime)

        return result

    def _static_no_events_result(self, correlation_id: str) -> dict:
        opinion_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        opinion = {
            "opinion_id": opinion_id,
            "opinion_type": "event_impact",
            "scope": "no_upcoming",
            "direction": "neutral",
            "confidence": "high",
            "timeframe": "short_term",
            "summary": "No high-impact economic events scheduled in the next 48 hours. Trading conditions should be driven by technical factors and existing macro regime rather than scheduled catalysts.",
            "key_factors": ["No upcoming high-impact events"],
            "reasoning": "The economic calendar is clear for the next 48 hours with no high-impact releases. This provides a clean environment for trend-following strategies without scheduled catalyst risk. Focus on technical setups and existing macro regime positioning.",
            "data_inputs": {
                "table": "econ_events",
                "event_count": 0,
                "window": "next_48h",
                "events": [],
            },
            "model_used": "none",
            "prompt_version": self.get_prompt_version(),
            "tokens_used": 0,
            "cost_usd": 0.0,
        }

        processing_log = {
            "processor": self.processor_id,
            "status": "success",
            "input_summary": {
                "table": "econ_events",
                "event_count": 0,
                "window": "next_48h",
            },
            "output_id": opinion_id,
            "prompt_text": None,
            "raw_response": None,
            "model_used": "none",
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0.0,
        }

        return {
            "opinion": opinion,
            "extra_records": {},
            "processing_log": processing_log,
        }

    def _derive_direction(self, parsed: dict) -> str:
        events = parsed.get("events", [])
        if not events:
            return "neutral"

        usd_bullish = 0
        usd_bearish = 0
        for e in events:
            for scenario_key in ["consensus_met_scenario", "upside_surprise_scenario", "downside_surprise_scenario"]:
                scenario = e.get(scenario_key, {})
                direction = scenario.get("direction", "")
                if "bullish_usd" in direction:
                    usd_bullish += 1
                elif "bearish_usd" in direction:
                    usd_bearish += 1

        if usd_bullish > usd_bearish:
            return "bullish_usd"
        elif usd_bearish > usd_bullish:
            return "bearish_usd"
        return "mixed"

    def _build_summary(self, parsed: dict) -> str:
        events = parsed.get("events", [])
        if not events:
            return "No high-impact events in the next 48 hours."

        event_names = [e.get("event_name", "Unknown") for e in events]
        outlook = parsed.get("overall_volatility_outlook", "")
        risk_note = parsed.get("risk_management_note", "")

        summary = f"{len(events)} high-impact event(s) in next 48h: {', '.join(event_names)}. "
        if outlook:
            summary += outlook
        if risk_note:
            summary += f" {risk_note}"

        return summary

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

        logger.error(
            "llm_response_parse_failed",
            action="parse_llm_response",
            raw_response=response_text[:2000],
        )
        raise ValueError(
            f"Could not parse LLM response as JSON. Raw text:\n{response_text[:500]}"
        )
