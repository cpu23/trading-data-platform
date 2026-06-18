import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from db import get_session
from llm_client import call_llm, resolve_model
from processors._validators import OutputPolicyError, scan_prohibited_language
from sqlalchemy import text


ROLE_INSTRUCTIONS = {
    "analyst": "Assess rates, inflation, growth, policy, energy, industry, positioning and geopolitics.",
    "skeptic": "Challenge causal claims, confidence, missing evidence and alternative economic explanations.",
    "auditor": "Identify unsupported claims, stale evidence, contradictions and policy-boundary violations.",
}


class MarketIntelligenceProcessor:
    processor_id = "market_intelligence"

    def process(self, config, correlation_id):
        context = self._context(config)
        fingerprint = hashlib.sha256(
            json.dumps(context, sort_keys=True, default=str).encode()
        ).hexdigest()
        previous = self._previous(config)
        if previous and previous.get("payload", {}).get("input_fingerprint") == fingerprint:
            return self._no_change(previous, fingerprint, correlation_id)

        model = resolve_model(config, processor_id=self.processor_id)
        usage = {"tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0}
        roles = {}
        raw = []
        for role, instruction in ROLE_INSTRUCTIONS.items():
            result = call_llm(
                self._role_prompt(role, instruction, context),
                model=model, config=config, correlation_id=correlation_id,
            )
            parsed = self._parse(result["content"])
            self._validate_role(parsed, context["symbols"], role)
            roles[role] = parsed
            raw.append(result["content"])
            self._add_usage(usage, result)

        editor_result = call_llm(
            self._editor_prompt(context, roles),
            model=model, config=config, correlation_id=correlation_id,
        )
        edited = self._parse(editor_result["content"])
        self._validate_editor(edited, context["symbols"])
        raw.append(editor_result["content"])
        self._add_usage(usage, editor_result)

        now = datetime.now(timezone.utc)
        opinions = []
        for asset in edited["assets"]:
            opinions.append(self._opinion(
                "asset_panel", f"asset:{asset['symbol']}", asset,
                asset["bias"], asset["confidence"], asset["summary"],
                fingerprint, correlation_id, model,
            ))
        memory = self._memory(previous, edited)
        opinions.append(self._opinion(
            "narrative_memory", "global:macro", memory, edited["global"]["bias"],
            edited["global"]["confidence"], edited["global"]["summary"],
            fingerprint, correlation_id, model,
        ))
        delta = self._delta(previous, edited)
        opinions.append(self._opinion(
            "cycle_delta", "global:briefing", delta, edited["global"]["bias"],
            edited["global"]["confidence"], delta["headline"],
            fingerprint, correlation_id, model,
        ))
        return {
            "opinions": opinions,
            "processing_log": {
                "status": "success", "output_ids": [o["opinion_id"] for o in opinions],
                "input_summary": {"input_fingerprint": fingerprint, "symbols": context["symbols"]},
                "prompt_text": "Four-role economics-only intelligence chain",
                "raw_response": "\n\n".join(raw), "model_used": model, **usage,
                "request_metadata": {"roles": ["analyst", "skeptic", "auditor", "editor"]},
            },
        }

    def get_depends_on(self): return ["macro_regime"]

    def _context(self, config):
        symbols = [x["symbol"] for x in config.get("watchlist", {}).get("trading", [])]
        with get_session(config) as session:
            regime = session.execute(text(
                "SELECT so.summary, so.reasoning, so.direction, so.confidence "
                "FROM structured_opinions so WHERE so.opinion_type='macro_regime' "
                "ORDER BY so.created_at DESC LIMIT 1"
            )).fetchone()
            events = session.execute(text(
                "SELECT event_name, country, scheduled_at, impact_level, consensus, previous "
                "FROM econ_events WHERE scheduled_at >= NOW() ORDER BY scheduled_at LIMIT 30"
            )).fetchall()
            positioning = session.execute(text(
                "SELECT market_id, report_date, category, net_position, net_pct_open_interest "
                "FROM positioning_reports ORDER BY report_date DESC LIMIT 40"
            )).fetchall()
        return {
            "symbols": symbols,
            "regime": dict(regime._mapping) if regime else {},
            "events": [dict(x._mapping) for x in events],
            "positioning": [dict(x._mapping) for x in positioning],
        }

    def _previous(self, config):
        with get_session(config) as session:
            row = session.execute(text(
                "SELECT payload FROM structured_opinions "
                "WHERE opinion_type='narrative_memory' AND lifecycle_status='published' "
                "ORDER BY published_at DESC LIMIT 1"
            )).fetchone()
        if not row: return None
        payload = row._mapping["payload"]
        return {"payload": json.loads(payload) if isinstance(payload, str) else payload}

    def _role_prompt(self, role, instruction, context):
        return (
            f"You are the {role}. {instruction}\n"
            "Economics-only market assessment. Bias is descriptive, never advice. "
            "Do not discuss technical analysis, entries, exits, stops, targets, sizing or allocation.\n"
            f"Return JSON: {{\"global\":{{\"bias\":\"bullish|bearish|neutral|mixed\","
            "\"confidence\":\"high|moderate|low\",\"claims\":[],\"contradictions\":[]}},"
            "\"assets\":[{\"symbol\":\"...\",\"bias\":\"...\",\"confidence\":\"...\","
            "\"claims\":[],\"contradictions\":[]}]}. Include every symbol once.\n"
            + json.dumps(context, default=str)
        )

    def _editor_prompt(self, context, roles):
        return (
            "Synthesize only claims present in the three role outputs. Economics-only; no trading "
            "instructions or technical analysis. Preserve disagreements. Return JSON with "
            "{\"global\":{\"bias\":\"...\",\"confidence\":\"...\",\"summary\":\"...\","
            "\"drivers\":[],\"contradictions\":[],\"invalidation_conditions\":[]},"
            "\"assets\":[{\"symbol\":\"...\",\"bias\":\"...\",\"confidence\":\"...\","
            "\"summary\":\"...\",\"drivers\":[],\"contradictions\":[],"
            "\"invalidation_conditions\":[],\"disagreements\":[]}]}. Include every symbol once.\n"
            + json.dumps({"symbols": context["symbols"], "roles": roles}, default=str)
        )

    @staticmethod
    def _parse(content):
        text_value = content.strip()
        if text_value.startswith("```"):
            text_value = text_value.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text_value)

    def _validate_role(self, value, symbols, role):
        issues = scan_prohibited_language(value)
        actual = [x.get("symbol") for x in value.get("assets", [])]
        if actual != symbols: issues.append(f"{role} assets do not match watchlist")
        if issues: raise OutputPolicyError(self.processor_id, issues)

    def _validate_editor(self, value, symbols):
        issues = scan_prohibited_language(value)
        if [x.get("symbol") for x in value.get("assets", [])] != symbols:
            issues.append("editor assets do not match watchlist")
        required = {"bias", "confidence", "summary", "drivers", "contradictions", "invalidation_conditions"}
        if not required.issubset(value.get("global", {})): issues.append("editor global schema invalid")
        if issues: raise OutputPolicyError(self.processor_id, issues)

    def _opinion(self, kind, scope, payload, direction, confidence, summary, fingerprint, correlation_id, model):
        return {
            "opinion_id": str(uuid4()), "opinion_type": kind, "scope": scope,
            "schema_version": f"{kind}.v1", "direction": direction, "confidence": confidence,
            "timeframe": "medium_term", "summary": summary, "key_factors": payload.get("drivers", []),
            "reasoning": summary, "payload": {**payload, "input_fingerprint": fingerprint},
            "data_inputs": {"opinions": [], "raw": [], "baseline_opinion_id": None},
            "model_used": model, "prompt_version": "market_intelligence_v1",
            "tokens_used": 0, "cost_usd": 0.0,
        }

    def _memory(self, previous, edited):
        prior = (previous or {}).get("payload", {})
        return {
            "summary": edited["global"]["summary"], "drivers": edited["global"]["drivers"],
            "active_narratives": edited["global"]["drivers"][:8],
            "new_this_cycle": [x for x in edited["global"]["drivers"] if x not in prior.get("active_narratives", [])],
            "strengthened": [], "weakened": [], "invalidated": [],
            "open_questions": edited["global"]["contradictions"],
            "assets": edited["assets"],
        }

    def _delta(self, previous, edited):
        if not previous:
            return {"material_change": True, "headline": "Initial economic context established.", "changed": edited["global"]["drivers"], "asset_deltas": edited["assets"]}
        prior_assets = {x["symbol"]: x for x in previous["payload"].get("assets", [])}
        changes = [
            {"symbol": x["symbol"], "from": prior_assets.get(x["symbol"], {}).get("bias"), "to": x["bias"]}
            for x in edited["assets"] if prior_assets.get(x["symbol"], {}).get("bias") != x["bias"]
        ]
        return {"material_change": bool(changes), "headline": f"{len(changes)} material asset assessment change(s)." if changes else "No material change.", "changed": changes, "asset_deltas": changes}

    def _no_change(self, previous, fingerprint, correlation_id):
        delta = {"material_change": False, "headline": "No material change.", "changed": [], "asset_deltas": []}
        return {"opinions": [self._opinion("cycle_delta", "global:briefing", delta, "neutral", "moderate", delta["headline"], fingerprint, correlation_id, "deterministic")], "processing_log": {"status": "success", "input_summary": {"input_fingerprint": fingerprint, "reused": True}, "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0}}

    @staticmethod
    def _add_usage(total, result):
        total["tokens_input"] += result.get("tokens_input", 0)
        total["tokens_output"] += result.get("tokens_output", 0)
        total["cost_usd"] += result.get("cost_usd") or 0.0
