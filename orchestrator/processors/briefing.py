import json
import os
import re
from datetime import datetime, time as dt_time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from budgets import BudgetContext
from db import get_session
from llm_client import LLMStage, LLMStageFailure, call_llm
from logging_config import get_logger
from processors._validators import validate_briefing_sections, coerce_briefing_fields
from sqlalchemy import text

logger = get_logger("processor.briefing")

MAX_RETRIES = 1


class DailyBriefingProcessor:
    processor_id = "briefing"

    def process(
        self,
        config: dict,
        correlation_id: str,
        budget_context: BudgetContext | None = None,
    ) -> dict:
        ff_config = config.get("processors", {}).get("briefing", {})
        prompt_template_path = ff_config.get(
            "prompt_template", "prompts/briefing_v3.txt"
        )

        regime_summary = self._get_regime_summary(config)
        calendar_bundle = self._get_calendar_bundle(config)
        watchlist_config = config.get("watchlist", {}).get("trading", [])
        watchlist_str = self._format_watchlist(config)
        london_tz = self._primary_timezone(config)
        current_london = datetime.now(london_tz)
        current_date = current_london.strftime("%A, %B %d, %Y")
        briefing_date = current_london.date()

        missing_context = []
        if regime_summary is None:
            regime_summary = "No macro regime classification available yet. Run the macro_regime processor first."
            missing_context.append("macro regime classification")

        prompt_text = self._build_prompt(
            template_path=prompt_template_path,
            current_date=current_date,
            macro_regime_summary=regime_summary,
            today_events=calendar_bundle["today_prompt"],
            this_week_events=calendar_bundle["week_prompt"],
            watchlist=watchlist_str,
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
        except ValueError as exc:
            stage.add_validation_warnings(["response was not valid JSON"])
            if stage.policy.validation_retries < 1:
                raise LLMStageFailure("LLM response validation failed", stage.telemetry) from exc
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
                raise LLMStageFailure(
                    "LLM response validation failed after retry", stage.telemetry
                ) from retry_exc

        sections = {
            "macro_trend": parsed.get("macro_trend", parsed.get("macro_summary", "")),
            "today": parsed.get("today", ""),
            "this_week": parsed.get("this_week", parsed.get("upcoming_events", "")),
            "regime_assessment": parsed.get("regime_assessment", ""),
            "watchlist_notes": parsed.get("watchlist_notes", []),
        }

        sections = self._validate_and_fix_sections(
            sections=sections,
            watchlist_config=watchlist_config,
            prompt_text=prompt_text,
            raw_response=raw_response,
            llm_result=llm_result,
            model=model,
            config=config,
            correlation_id=correlation_id,
            stage=stage,
        )

        opinion_id = str(uuid4())
        briefing_id = str(uuid4())

        regime_opinion_id = self._get_latest_opinion_id(config, "macro_regime")
        opinion_ids_used = [oid for oid in [regime_opinion_id] if oid]
        opinion_ids_used.append(opinion_id)

        full_briefing_content = self._format_briefing_content(sections, current_date)

        direction = "neutral"
        confidence = "low"
        if regime_summary and regime_summary != "No macro regime classification available yet. Run the macro_regime processor first.":
            regime_data = self._get_latest_regime_raw(config)
            if regime_data:
                direction = regime_data.get("direction", "neutral")
                confidence = regime_data.get("confidence", "low")

        opinion = {
            "opinion_id": opinion_id,
            "opinion_type": "briefing",
            "scope": f"daily_{briefing_date.isoformat()}",
            "direction": direction,
            "confidence": confidence,
            "timeframe": "short_term",
            "summary": sections.get("macro_trend", "")[:500],
            "key_factors": {
                "today_events": calendar_bundle["today_count"],
                "this_week_events": calendar_bundle["week_count"],
                "watchlist": [w.get("symbol") for w in watchlist_config],
            },
            "reasoning": full_briefing_content,
            "data_inputs": {
                "opinions_used": opinion_ids_used,
                "calendar_window": calendar_bundle["window"],
            },
            "model_used": llm_result.get("model", model),
            "prompt_version": self.get_prompt_version(),
            "tokens_used": (
                llm_result.get("tokens_input", 0) + llm_result.get("tokens_output", 0)
            ),
            "cost_usd": llm_result.get("cost_usd", 0.0),
        }

        briefing_record = {
            "briefing_id": briefing_id,
            "briefing_date": briefing_date,
            "content": full_briefing_content,
            "sections": sections,
            "opinion_ids": "{" + ",".join(opinion_ids_used) + "}",
            "model_used": llm_result.get("model", model),
            "prompt_version": self.get_prompt_version(),
        }

        processing_log = {
            "processor": self.processor_id,
            "status": "success",
            "input_summary": {
                "regime_available": regime_summary is not None and "No macro regime classification" not in regime_summary,
                "calendar_events_today": calendar_bundle["today_count"],
                "calendar_events_this_week": calendar_bundle["week_count"],
                "missing_context": missing_context,
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
            "extra_records": {"daily_briefings": [briefing_record]},
            "processing_log": processing_log,
        }

    def get_prompt_version(self) -> str:
        return "briefing_v3"

    def get_depends_on(self) -> list[str]:
        return ["macro_regime"]

    def _validate_and_fix_sections(
        self,
        sections: dict,
        watchlist_config: list[dict],
        prompt_text: str,
        raw_response: str,
        llm_result: dict,
        model: str,
        config: dict,
        correlation_id: str,
        stage: LLMStage | None = None,
    ) -> dict:
        is_valid, warnings = validate_briefing_sections(sections, watchlist_config)

        for warning in warnings:
            logger.warning(
                "briefing_validation_warning",
                action="validate_briefing",
                warning=warning,
                correlation_id=correlation_id,
            )

        coercion_warnings = coerce_briefing_fields(sections)
        for warning in coercion_warnings:
            logger.info(
                "briefing_coercion",
                action="coerce_briefing",
                warning=warning,
                correlation_id=correlation_id,
            )

        if not is_valid:
            if stage is not None:
                stage.add_validation_warnings(warnings)
            logger.warning(
                "briefing_validation_failed_retrying",
                action="validate_briefing",
                warnings=warnings,
                correlation_id=correlation_id,
            )

            expected_symbols = ", ".join(
                w.get("symbol", "") for w in watchlist_config if w.get("symbol")
            )
            retry_prompt = prompt_text + "\n\nIMPORTANT CORRECTION: Your previous response had format issues: " + "; ".join(warnings) + ". Please respond again with the correct JSON format, ensuring watchlist_notes is an array of objects, each with symbol, asset_class, bias, confidence, summary, and note fields. Include each configured symbol exactly once and in this order: " + expected_symbols + ". bias must be one of: bullish, bearish, neutral, mixed. confidence must be one of: high, moderate, low."

            try:
                retry_result = (
                    stage.call(retry_prompt)
                    if stage is not None
                    else call_llm(
                        prompt=retry_prompt,
                        model=model,
                        processor_id=self.processor_id,
                        correlation_id=correlation_id,
                        config=config,
                    )
                )
                retry_parsed = self._parse_llm_response(retry_result["content"])

                retry_sections = {
                    "macro_trend": retry_parsed.get("macro_trend", sections.get("macro_trend", "")),
                    "today": retry_parsed.get("today", sections.get("today", "")),
                    "this_week": retry_parsed.get("this_week", sections.get("this_week", "")),
                    "regime_assessment": retry_parsed.get("regime_assessment", sections.get("regime_assessment", "")),
                    "watchlist_notes": retry_parsed.get("watchlist_notes", sections.get("watchlist_notes", [])),
                }

                retry_valid, retry_warnings = validate_briefing_sections(
                    retry_sections, watchlist_config
                )
                if stage is not None:
                    stage.add_validation_warnings(retry_warnings)
                retry_coercion = coerce_briefing_fields(retry_sections)

                for w in retry_warnings:
                    logger.warning(
                        "briefing_retry_validation_warning",
                        action="validate_briefing_retry",
                        warning=w,
                        correlation_id=correlation_id,
                    )
                for w in retry_coercion:
                    logger.info(
                        "briefing_retry_coercion",
                        action="coerce_briefing_retry",
                        warning=w,
                        correlation_id=correlation_id,
                    )

                if retry_valid:
                    logger.info(
                        "briefing_retry_succeeded",
                        action="validate_briefing",
                        correlation_id=correlation_id,
                    )
                    return retry_sections

                logger.warning(
                    "briefing_retry_still_invalid_using_original",
                    action="validate_briefing",
                    correlation_id=correlation_id,
                )
            except LLMStageFailure:
                raise
            except Exception as exc:
                logger.error(
                    "briefing_retry_failed",
                    action="validate_briefing",
                    error=str(exc),
                    correlation_id=correlation_id,
                )

        return sections

    def _get_regime_summary(self, config: dict) -> str | None:
        sql = text("""
            SELECT rc.regime, rc.sub_regime, rc.confidence, rc.supporting_data,
                   so.summary, so.key_factors, so.reasoning
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

            r = dict(row._mapping)
            regime = r.get("regime", "unknown").upper()
            sub_regime = r.get("sub_regime", "")
            if sub_regime and sub_regime != "null":
                regime += f" ({sub_regime.replace('_', ' ').title()})"
            confidence = r.get("confidence", "unknown")
            summary = r.get("summary", "")
            reasoning = r.get("reasoning", "")

            supporting = r.get("supporting_data", {})
            if isinstance(supporting, str):
                try:
                    supporting = json.loads(supporting)
                except (json.JSONDecodeError, TypeError):
                    supporting = {}

            momentum = supporting.get("momentum_implications", "")
            caution = supporting.get("caution_flags", [])
            key_indicators = supporting.get("key_indicators", {})

            lines = [f"Regime: {regime} | Confidence: {confidence}"]
            if summary:
                lines.append(f"\nSummary: {summary}")
            if reasoning:
                lines.append(f"\nReasoning: {reasoning}")
            if momentum:
                lines.append(f"\nMomentum Implications: {momentum}")
            if caution:
                lines.append(f"\nCaution Flags: {', '.join(caution)}")
            if key_indicators:
                indicator_parts = [f"{k}: {v}" for k, v in key_indicators.items() if v is not None]
                lines.append(f"\nKey Indicators: {', '.join(indicator_parts)}")

            return "\n".join(lines)
        except Exception:
            return None

    def _get_latest_regime_raw(self, config: dict) -> dict | None:
        sql = text("""
            SELECT so.direction, so.confidence
            FROM regime_classifications rc
            JOIN structured_opinions so ON rc.opinion_id = so.opinion_id
            ORDER BY rc.created_at DESC
            LIMIT 1
        """)

        try:
            with get_session(config) as session:
                result = session.execute(sql)
                row = result.fetchone()
            if row:
                return dict(row._mapping)
        except Exception:
            pass
        return None

    def _primary_timezone(self, config: dict) -> ZoneInfo:
        tz_name = (
            config.get("timezone", {})
            .get("primary", {})
            .get("name", "Europe/London")
        )
        return ZoneInfo(tz_name)

    def _secondary_timezone(self, config: dict) -> ZoneInfo:
        tz_name = (
            config.get("timezone", {})
            .get("secondary", {})
            .get("name", "America/New_York")
        )
        return ZoneInfo(tz_name)

    def _calendar_window(self, config: dict) -> dict:
        london = self._primary_timezone(config)
        now_london = datetime.now(london)
        today = now_london.date()

        if now_london.weekday() >= 5:
            monday = today + timedelta(days=7 - now_london.weekday())
        else:
            monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)

        start_date = today if now_london.weekday() < 5 else monday
        period_start = datetime.combine(start_date, dt_time.min, tzinfo=london)
        period_end = datetime.combine(friday, dt_time.max, tzinfo=london)

        return {
            "today": today,
            "monday": monday,
            "friday": friday,
            "period_start": period_start,
            "period_end": period_end,
            "london_tz": london,
            "ny_tz": self._secondary_timezone(config),
            "london_label": config.get("timezone", {})
            .get("primary", {})
            .get("label", "London"),
            "ny_label": config.get("timezone", {})
            .get("secondary", {})
            .get("label", "NY"),
        }

    def _get_calendar_bundle(self, config: dict) -> dict:
        window = self._calendar_window(config)
        sql = text("""
            SELECT event_id, event_name, country, scheduled_at, impact_level,
                   consensus, previous, actual, source, metadata
            FROM econ_events
            WHERE scheduled_at >= :start
              AND scheduled_at <= :end
              AND lower(COALESCE(impact_level, '')) IN ('high', 'medium')
              AND (
                  country IN ('US', 'EU', 'GB', 'JP', 'AU', 'CN')
                  OR metadata ->> 'currency' IN ('USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CNY')
              )
            ORDER BY scheduled_at ASC
        """)

        rows = []
        try:
            with get_session(config) as session:
                result = session.execute(
                    sql,
                    {
                        "start": window["period_start"].astimezone(timezone.utc),
                        "end": window["period_end"].astimezone(timezone.utc),
                    },
                )
                rows = [dict(row._mapping) for row in result]
        except Exception as exc:
            logger.warning(
                "calendar_bundle_query_failed",
                action="get_calendar_bundle",
                error=str(exc),
            )

        today_events = []
        week_events = []
        for row in rows:
            scheduled_at = row.get("scheduled_at")
            if isinstance(scheduled_at, str):
                scheduled_at = datetime.fromisoformat(
                    scheduled_at.replace("Z", "+00:00")
                )
            if scheduled_at is None:
                continue
            row["scheduled_at"] = scheduled_at
            london_date = scheduled_at.astimezone(window["london_tz"]).date()
            if london_date == window["today"]:
                today_events.append(row)
            elif london_date <= window["friday"]:
                week_events.append(row)

        return {
            "today_prompt": self._format_calendar_prompt(today_events, window),
            "week_prompt": self._format_calendar_prompt(week_events, window),
            "today_count": len(today_events),
            "week_count": len(week_events),
            "window": {
                "today": window["today"].isoformat(),
                "period_start": window["period_start"].isoformat(),
                "period_end": window["period_end"].isoformat(),
                "friday": window["friday"].isoformat(),
            },
        }

    def _format_calendar_prompt(self, events: list[dict], window: dict) -> str:
        if not events:
            return "No high- or medium-impact relevant events scheduled."

        lines = []
        for event in events:
            scheduled_at = event["scheduled_at"]
            london_time = scheduled_at.astimezone(window["london_tz"])
            ny_time = scheduled_at.astimezone(window["ny_tz"])
            currency = self._currency_for_event(event)
            impact = (event.get("impact_level") or "").upper()
            expectation = []
            if event.get("consensus"):
                expectation.append(f"exp {event['consensus']}")
            if event.get("previous"):
                expectation.append(f"prev {event['previous']}")
            expectation_text = f" ({', '.join(expectation)})" if expectation else ""
            lines.append(
                f"{london_time.strftime('%a %H:%M')} {window['london_label']} / "
                f"{ny_time.strftime('%H:%M')} {window['ny_label']} | "
                f"{impact} {currency} | {event.get('event_name')}{expectation_text}"
            )
        return "\n".join(lines)

    @staticmethod
    def _currency_for_event(event: dict) -> str:
        metadata = event.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        if metadata.get("currency"):
            return metadata["currency"]
        country_to_currency = {
            "US": "USD",
            "EU": "EUR",
            "GB": "GBP",
            "JP": "JPY",
            "AU": "AUD",
            "CN": "CNY",
        }
        return country_to_currency.get(event.get("country"), event.get("country", ""))

    def _get_latest_opinion_id(self, config: dict, opinion_type: str) -> str | None:
        sql = text("""
            SELECT opinion_id
            FROM structured_opinions
            WHERE opinion_type = :opinion_type
            ORDER BY created_at DESC
            LIMIT 1
        """)

        try:
            with get_session(config) as session:
                result = session.execute(sql, {"opinion_type": opinion_type})
                row = result.fetchone()
            if row:
                return str(dict(row._mapping)["opinion_id"])
        except Exception:
            pass
        return None

    def _format_watchlist(self, config: dict) -> str:
        watchlist = config.get("watchlist", {}).get("trading", [])
        if not watchlist:
            return "No watchlist configured."

        TYPE_TO_ASSET_CLASS = {"forex": "forex", "index": "index", "metal": "metal"}

        lines = []
        for w in watchlist:
            symbol = w["symbol"]
            asset_class = TYPE_TO_ASSET_CLASS.get(w["type"], w["type"])
            lines.append(f"{symbol} ({asset_class})")

        return ", ".join(lines)

    def _build_prompt(
        self,
        template_path: str,
        current_date: str,
        macro_regime_summary: str,
        today_events: str,
        this_week_events: str,
        watchlist: str,
    ) -> str:
        if not os.path.isabs(template_path):
            config_dir = os.environ.get("CONFIG_DIR", "/app")
            template_path = os.path.join(config_dir, template_path)

        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Prompt template not found: {template_path}")

        with open(template_path) as f:
            template = f.read()

        result = template
        result = result.replace("{{current_date}}", current_date)
        result = result.replace("{{macro_regime_summary}}", macro_regime_summary)
        result = result.replace("{{today_events}}", today_events)
        result = result.replace("{{this_week_events}}", this_week_events)
        result = result.replace("{{watchlist}}", watchlist)

        return result

    def _format_briefing_content(self, sections: dict, current_date: str) -> str:
        lines = []

        macro = sections.get("macro_trend", sections.get("macro_summary", ""))
        if macro:
            lines.append("MACRO TREND")
            lines.append("─" * 40)
            lines.append(macro)
            lines.append("")

        today = sections.get("today", "")
        if today:
            lines.append("TODAY")
            lines.append("─" * 40)
            lines.append(today)
            lines.append("")

        this_week = sections.get("this_week", sections.get("upcoming_events", ""))
        if this_week:
            lines.append("THIS WEEK")
            lines.append("─" * 40)
            lines.append(this_week)
            lines.append("")

        regime = sections.get("regime_assessment", "")
        if regime:
            lines.append("REGIME ASSESSMENT")
            lines.append("─" * 40)
            lines.append(regime)
            lines.append("")

        watchlist_notes = sections.get("watchlist_notes", [])
        if watchlist_notes:
            lines.append("WATCHLIST NOTES")
            lines.append("─" * 40)
            if isinstance(watchlist_notes, list):
                for note in watchlist_notes:
                    if isinstance(note, dict):
                        symbol = note.get("symbol", "Unknown")
                        bias = note.get("bias", "—")
                        confidence = note.get("confidence", "—")
                        summary = note.get("summary", "")
                        lines.append(f"{symbol} [{bias}/{confidence}]: {summary}")
                        full_note = note.get("note", "")
                        if full_note:
                            lines.append(f"  {full_note}")
                    else:
                        lines.append(str(note))
            elif isinstance(watchlist_notes, str):
                lines.append(watchlist_notes)
            elif isinstance(watchlist_notes, dict):
                for symbol, note in watchlist_notes.items():
                    lines.append(f"{symbol}: {note}")
            else:
                lines.append("(watchlist notes unavailable)")
            lines.append("")

        return "\n".join(lines)

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
