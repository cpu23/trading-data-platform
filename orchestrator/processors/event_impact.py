import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from budgets import BudgetContext
from db import get_session
from llm_client import LLMStage, LLMValidationError
from logging_config import get_logger

logger = get_logger("processor.event_impact")

_ATOM_CONFIDENCE = {"high": 0.8, "moderate": 0.6, "low": 0.4}


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
        try:
            parsed = self._parse_llm_response(raw_response)
            self._validate_llm_response(parsed)
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
                self._validate_llm_response(parsed)
            except ValueError as retry_exc:
                stage.add_validation_warnings(["final response was not valid JSON"])
                raise LLMValidationError(
                    "LLM response validation failed after retry", stage.telemetry
                ) from retry_exc

        opinion_id = str(uuid4())

        event_names = [e.get("event_name", "") for e in parsed.get("events", [])]
        event_ids = [
            f"{e.get('event_name', '')}-{e.get('scheduled_at', '')}"
            for e in parsed.get("events", [])
        ]

        direction = self._derive_direction(parsed)
        confidence = (
            "high" if len(events) >= 3 else "moderate" if len(events) >= 1 else "low"
        )

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
                stage.telemetry.tokens_input_total + stage.telemetry.tokens_output_total
            ),
            "cost_usd": stage.telemetry.cost_usd_total,
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
            "prompt_text": None,
            "raw_response": None,
            "model_used": llm_result.get("model", model),
            "tokens_input": stage.telemetry.tokens_input_total,
            "tokens_output": stage.telemetry.tokens_output_total,
            "cost_usd": stage.telemetry.cost_usd_total,
        }

        atoms = self._publish_atoms(
            config,
            parsed=parsed,
            events=events,
            regime_opinion_id=self._current_regime_opinion_id(config),
            llm_result=llm_result,
            model=model,
            confidence=confidence,
            correlation_id=correlation_id,
        )

        return {
            "opinion": opinion,
            "extra_records": {},
            "processing_log": processing_log,
            "atoms": atoms,
        }

    def get_prompt_version(self) -> str:
        return "event_impact_v1"

    def get_depends_on(self) -> list[str]:
        return ["forex_factory"]

    def _current_regime_opinion_id(self, config: dict) -> str | None:
        """Return the latest regime classification's opinion id, if any."""
        sql = text("""
            SELECT so.opinion_id
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
                return None
            opinion_id = str(dict(row._mapping).get("opinion_id") or "").strip()
            return opinion_id or None
        except Exception:
            return None

    def _publish_atoms(
        self,
        config: dict,
        *,
        parsed: dict,
        events: list[dict],
        regime_opinion_id: str | None,
        llm_result: dict,
        model: str,
        confidence: str,
        correlation_id: str,
        now: datetime | None = None,
    ) -> dict | None:
        """Publish one atom per parsed event plus one overall atom.

        Atom publication is gated on ``analysis_atoms.enabled`` and always fails
        soft: a validation or persistence problem is logged as a warning and the
        processor's opinion result still succeeds.
        """
        settings = config.get("analysis_atoms", {})
        if not isinstance(settings, Mapping) or not settings.get("enabled", False):
            return None
        try:
            from atoms import publish_atom
            from processors.base import canonical_fingerprint

            try:
                interpretation_hours = max(
                    1,
                    min(168, int(settings.get("event_interpretation_hours", 48))),
                )
            except (TypeError, ValueError, OverflowError):
                interpretation_hours = 48
            current = now or datetime.now(UTC)
            expires_at = current + timedelta(hours=interpretation_hours)
            model_slug = str(llm_result.get("model") or model or "").strip() or None
            prompt_version = self.get_prompt_version()

            event_ids = [
                str(row.get("event_id") or "").strip()
                for row in events
                if str(row.get("event_id") or "").strip()
            ]
            evidence: list[dict] = [
                {
                    "evidence_type": "econ_events",
                    "evidence_id": event_id,
                    "relationship": "context",
                }
                for event_id in event_ids
            ]
            if regime_opinion_id:
                evidence.append(
                    {
                        "evidence_type": "opinion",
                        "evidence_id": regime_opinion_id,
                        "relationship": "context",
                    }
                )

            source_by_name: dict[str, dict] = {}
            for row in events:
                name = str(row.get("event_name") or "").strip()
                if name and name not in source_by_name:
                    source_by_name[name] = row

            published: list[dict] = []
            with get_session(config) as session:
                for event in parsed.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    name = str(event.get("event_name") or "").strip()
                    source_row = source_by_name.get(name)
                    event_id = (
                        str(source_row.get("event_id") or "").strip()
                        if source_row
                        else ""
                    )
                    scheduled_at = str(event.get("scheduled_at") or "").strip()
                    subject_id = event_id or (
                        f"{name}-{scheduled_at}" if name else scheduled_at
                    )
                    if not subject_id:
                        continue
                    fingerprint = canonical_fingerprint(
                        {
                            "subject_type": "econ_event",
                            "subject_id": subject_id,
                            "claim_type": "event_interpretation",
                            "event_ids": event_ids,
                            "regime_opinion_id": regime_opinion_id,
                            "prompt_version": prompt_version,
                            "model": model_slug,
                        }
                    )
                    atom = self._event_interpretation_atom(
                        event,
                        subject_id=subject_id,
                        confidence=confidence,
                        prompt_version=prompt_version,
                        model_slug=model_slug,
                        fingerprint=fingerprint,
                        valid_from=current,
                        expires_at=expires_at,
                    )
                    published.append(publish_atom(session, atom, evidence, now=current))
                overall_fingerprint = canonical_fingerprint(
                    {
                        "subject_type": "econ_event",
                        "subject_id": "overall",
                        "claim_type": "event_interpretation",
                        "event_ids": event_ids,
                        "regime_opinion_id": regime_opinion_id,
                        "prompt_version": prompt_version,
                        "model": model_slug,
                    }
                )
                overall = self._overall_interpretation_atom(
                    parsed,
                    events=events,
                    confidence=confidence,
                    prompt_version=prompt_version,
                    model_slug=model_slug,
                    fingerprint=overall_fingerprint,
                    valid_from=current,
                    expires_at=expires_at,
                )
                published.append(publish_atom(session, overall, evidence, now=current))
            return {
                "published": published,
                "evidence_ids": event_ids,
                "regime_opinion_id": regime_opinion_id,
                "expires_at": expires_at.astimezone(UTC).isoformat(),
            }
        except Exception as exc:
            logger.warning(
                "atom_publish_failed",
                action="publish_atoms",
                processor=self.processor_id,
                error=type(exc).__name__,
                correlation_id=correlation_id,
            )
            return None

    @staticmethod
    def _event_interpretation_atom(
        event: dict,
        *,
        subject_id: str,
        confidence: str,
        prompt_version: str,
        model_slug: str | None,
        fingerprint: str,
        valid_from: datetime,
        expires_at: datetime,
    ) -> dict:
        name = str(event.get("event_name") or "event").strip()
        scheduled_at = str(event.get("scheduled_at") or "").strip()
        consensus = str(event.get("consensus") or "N/A")
        previous = str(event.get("previous") or "N/A")
        context = str(event.get("context") or "").strip()
        observation = (
            f"Scheduled: {scheduled_at or 'N/A'}. Consensus: {consensus}. "
            f"Previous: {previous}."
        )
        if context:
            observation += f" Context: {context}"
        scenario_keys = (
            ("consensus_met_scenario", "Consensus met"),
            ("upside_surprise_scenario", "Upside surprise"),
            ("downside_surprise_scenario", "Downside surprise"),
        )
        scenario_lines: list[str] = []
        directions: list[str] = []
        for key, label in scenario_keys:
            scenario = event.get(key)
            if not isinstance(scenario, dict):
                continue
            direction = str(scenario.get("direction") or "").strip()
            volatility = str(scenario.get("volatility") or "").strip()
            narrative = str(scenario.get("narrative") or "").strip()
            if direction:
                directions.append(direction)
            parts = [label, direction, volatility]
            if narrative:
                parts.append(narrative)
            scenario_lines.append(": ".join(part for part in parts if part))
        market_implications = str(event.get("market_implications") or "").strip()
        if market_implications:
            scenario_lines.append(f"Market implications: {market_implications}")
        assets: list[str] = []
        scenario_assets: list[str] = []
        instruments = event.get("affected_instruments")
        if isinstance(instruments, list):
            for item in instruments:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").strip()
                if symbol and symbol not in assets:
                    assets.append(symbol)
                    reaction = str(item.get("expected_reaction") or "").strip()
                    scenario_assets.append(
                        f"{symbol} ({item.get('sensitivity') or 'unknown'})"
                        + (f": {reaction}" if reaction else "")
                    )
        claim = name
        if market_implications:
            claim = f"{name}: {market_implications}"
        elif directions:
            claim = f"{name}: {'/'.join(dict.fromkeys(directions))} scenarios"
        interpretation = " ".join(line for line in scenario_lines if line).strip()
        return {
            "subject_type": "econ_event",
            "subject_id": subject_id,
            "claim_type": "event_interpretation",
            "claim": claim,
            "observation_text": observation,
            "interpretation_text": interpretation or None,
            "scenario_text": "; ".join(scenario_assets) if scenario_assets else None,
            "unknowns": [],
            "affected_assets": assets,
            "time_horizon": "48h",
            "confidence": _ATOM_CONFIDENCE.get(confidence.lower(), 0.6),
            "confidence_components": {"source": "llm_event_interpretation"},
            "valid_from": valid_from,
            "expires_at": expires_at,
            "carry_forward": False,
            "invalidation_conditions": [
                "event actual released",
                "source data revised",
            ],
            "input_fingerprint": fingerprint,
            "supersedes_atom_id": None,
            "source_event_id": None,
            "prompt_version": prompt_version,
            "model_slug": model_slug,
            "generation_attempt_id": None,
        }

    @staticmethod
    def _overall_interpretation_atom(
        parsed: dict,
        *,
        events: list[dict],
        confidence: str,
        prompt_version: str,
        model_slug: str | None,
        fingerprint: str,
        valid_from: datetime,
        expires_at: datetime,
    ) -> dict:
        names = [
            str(event.get("event_name") or "").strip()
            for event in events
            if isinstance(event, dict)
        ]
        names = [name for name in names if name]
        outlook = str(parsed.get("overall_volatility_outlook") or "").strip()
        catalyst = str(parsed.get("catalyst_summary") or "").strip()
        risk_note = str(parsed.get("risk_management_note") or "").strip()
        interpretation = " ".join(
            part for part in (outlook, catalyst, risk_note) if part
        ).strip()
        assets: list[str] = []
        for event in parsed.get("events", []):
            if not isinstance(event, dict):
                continue
            instruments = event.get("affected_instruments")
            if not isinstance(instruments, list):
                continue
            for item in instruments:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").strip()
                if symbol and symbol not in assets:
                    assets.append(symbol)
        return {
            "subject_type": "econ_event",
            "subject_id": "overall",
            "claim_type": "event_interpretation",
            "claim": f"{len(names)} high-impact event(s) in next 48h"
            + (f": {', '.join(names)}" if names else ""),
            "observation_text": f"Window: next 48h. Events: {', '.join(names) or 'none'}.",
            "interpretation_text": interpretation or None,
            "scenario_text": None,
            "unknowns": [],
            "affected_assets": assets,
            "time_horizon": "48h",
            "confidence": _ATOM_CONFIDENCE.get(confidence.lower(), 0.6),
            "confidence_components": {"source": "llm_event_interpretation"},
            "valid_from": valid_from,
            "expires_at": expires_at,
            "carry_forward": False,
            "invalidation_conditions": [
                "event actual released",
                "source data revised",
            ],
            "input_fingerprint": fingerprint,
            "supersedes_atom_id": None,
            "source_event_id": None,
            "prompt_version": prompt_version,
            "model_slug": model_slug,
            "generation_attempt_id": None,
        }

    def _fetch_upcoming_events(self, config: dict) -> list[dict]:
        sql = text("""
            SELECT event_id, event_name, country, scheduled_at, impact_level, consensus, previous, actual
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

            indicators_str = (
                ", ".join(indicator_parts)
                if indicator_parts
                else "no indicators available"
            )

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
            for scenario_key in [
                "consensus_met_scenario",
                "upside_surprise_scenario",
                "downside_surprise_scenario",
            ]:
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
    def _validate_llm_response(parsed: dict) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("LLM response did not match required schema")
        required_types = {
            "events": list,
            "overall_volatility_outlook": str,
            "risk_management_note": str,
        }
        if any(
            not isinstance(parsed.get(key), expected)
            for key, expected in required_types.items()
        ):
            raise ValueError("LLM response did not match required schema")
        if any(not isinstance(event, dict) for event in parsed["events"]):
            raise ValueError("LLM response did not match required schema")

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
