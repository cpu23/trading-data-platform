import re
from datetime import datetime, timezone
from typing import Any


ALLOWED_BIAS = {"bullish", "bearish", "neutral", "mixed"}
ALLOWED_CONFIDENCE = {"high", "moderate", "low"}
ALLOWED_ASSET_CLASS = {"forex", "index", "metal"}
ALLOWED_TIMEFRAME = {"short_term", "medium_term"}
ALLOWED_REGIME = {
    "expansion",
    "slowdown",
    "contraction",
    "stagflation",
    "transition",
}
ALLOWED_SUB_REGIME = {
    "risk_on",
    "risk_off",
    "tightening",
    "easing",
    "policy_hold",
    "mixed",
    None,
}
ALLOWED_EVENT_DIRECTION = {
    "bullish",
    "bearish",
    "neutral",
    "mixed",
    "bullish_usd",
    "bearish_usd",
}
ALLOWED_VOLATILITY = {"high", "moderate", "low"}
ALLOWED_SENSITIVITY = {"high", "moderate", "low"}

BRIEFING_KEYS = {
    "macro_trend",
    "today",
    "this_week",
    "regime_assessment",
    "watchlist_notes",
}
WATCHLIST_NOTE_KEYS = {
    "symbol",
    "asset_class",
    "bias",
    "confidence",
    "summary",
    "note",
}
MACRO_REGIME_KEYS = {
    "regime",
    "sub_regime",
    "direction",
    "confidence",
    "timeframe",
    "summary",
    "key_factors",
    "reasoning",
    "market_implications",
    "caution_flags",
}
EVENT_IMPACT_KEYS = {
    "events",
    "overall_volatility_outlook",
    "catalyst_summary",
}
EVENT_KEYS = {
    "event_name",
    "scheduled_at",
    "consensus",
    "previous",
    "context",
    "consensus_met_scenario",
    "upside_surprise_scenario",
    "downside_surprise_scenario",
    "affected_instruments",
    "market_implications",
}
SCENARIO_KEYS = {"direction", "volatility", "narrative"}
AFFECTED_INSTRUMENT_KEYS = {
    "symbol",
    "sensitivity",
    "expected_reaction",
}


class OutputPolicyError(ValueError):
    """Raised after an invalid model output has exhausted its one repair attempt."""

    def __init__(self, processor_id: str, issues: list[str]):
        self.processor_id = processor_id
        self.issues = issues
        super().__init__(
            f"{processor_id} output validation_failed after failed repair: "
            + "; ".join(issues)
        )


PROHIBITED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "trading_instruction",
        re.compile(
            r"\b(?:buy|sell)\s+(?:the|this|at|near|above|below|on|after|before|when|if)\b|"
            r"\b(?:should|could|consider|recommend(?:ed)?)\s+"
            r"(?:buy|sell|go(?:ing)?\s+(?:long|short))\b|"
            r"\b(?:buy|sell)\s+(?:(?-i:[A-Z]{6})|"
            r"(?:(?:eur|usd|gbp|jpy|aud|cad|nzd|chf){2})|"
            r"gold|silver|oil|equities|stocks|bonds)\b|"
            r"\b(?:enter|exit)\s+(?:the|this|at|near|above|below|on|after|before|when|if)\b|"
            r"\b(?:go|be|stay)\s+(?:long|short)\b|"
            r"\b(?:open|close|add to|reduce)\s+(?:a\s+|the\s+)?position\b|"
            r"\btrade\s+(?:this|the|setup|signal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "entry_exit",
        re.compile(
            r"\btrade\s+entr(?:y|ies)\b|\btrade\s+exit(?:s|ing)?\b|"
            r"\bentry\s+(?:level|price|point|trigger)\b|"
            r"\bexit\s+(?:level|price|point|trigger)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "stop_target",
        re.compile(
            r"\bstop[\s-]?loss(?:es)?\b|\btake[\s-]?profit\b|"
            r"\btrailing\s+stop\b|\bprice\s+target\b|\bprofit\s+target\b|"
            r"\btarget\s+(?:price|level)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sizing_allocation",
        re.compile(
            r"\bposition\s+sizing\b|\bsize\s+(?:the|a|your)\s+position\b|"
            r"\bportfolio\s+allocation\b|"
            r"\b(?:increase|decrease|reduce|add|raise|lower)\s+"
            r"(?:your\s+|portfolio\s+)?exposure\b|"
            r"\b(?:overweight|underweight)\s+(?:the\s+)?"
            r"(?:asset|market|instrument|currency|index|metal|equities|stocks|bonds)\b|"
            r"\ballocate\s+(?:capital|funds|portfolio|money)\b|"
            r"\ballocat(?:e|ing)\s+(?:capital|funds|portfolio|money)\s+"
            r"(?:to|into)\b|"
            r"\ballocation\s+(?:to|into)\s+(?:the\s+)?"
            r"(?:asset|market|instrument|currency|index|metal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "technical_analysis",
        re.compile(
            r"\btechnical\s+analysis\b|\btechnical\s+(?:setup|signal|level|factor)s?\b|"
            r"\bchart(?:ing|\s+(?:pattern|setup|signal|level)s?)\b|"
            r"\bprice\s+action\b|\bcandlestick(?:s)?\b|"
            r"\btrend[\s-]?line(?:s)?\b|\bsupport\s+(?:level|zone)s?\b|"
            r"\bresistance\s+(?:level|zone)s?\b|\bmoving\s+average(?:s)?\b|"
            r"\b(?:rsi|macd)\b|\bbollinger\s+bands?\b|\bfibonacci\b|"
            r"\boverbought\b|\boversold\b|\bbreakout(?:s)?\b|\bbreakdown(?:s)?\b|"
            r"\btrend[\s-]?following\b|\bmomentum\s+(?:strategy|signal|setup|indicator)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "risk_execution",
        re.compile(
            r"\brisk\s+management\b|\brisk(?:ing)?\s+\d+(?:\.\d+)?%|"
            r"\breward[\s/-]?risk\b|\brisk[\s/-]?reward\b",
            re.IGNORECASE,
        ),
    ),
)


def _iter_strings(value: Any, path: str = "$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_strings(child, f"{path}[{index}]")


def scan_prohibited_language(value: Any) -> list[str]:
    """Return deterministic path/category findings for prohibited output text."""
    findings = []
    for path, text in _iter_strings(value):
        for category, pattern in PROHIBITED_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(
                    f"{path}: prohibited {category} language '{match.group(0)}'"
                )
    return findings


def _validate_exact_keys(
    value: Any, expected: set[str], path: str, issues: list[str]
) -> bool:
    if not isinstance(value, dict):
        issues.append(f"{path} must be an object")
        return False
    missing = expected - set(value)
    extra = set(value) - expected
    if missing:
        issues.append(f"{path} missing keys: {', '.join(sorted(missing))}")
    if extra:
        issues.append(f"{path} has unexpected keys: {', '.join(sorted(extra))}")
    return not missing and not extra


def _validate_nonempty_string(
    value: Any, path: str, issues: list[str], *, allow_empty: bool = False
) -> None:
    if not isinstance(value, str):
        issues.append(f"{path} must be a string")
    elif not allow_empty and not value.strip():
        issues.append(f"{path} must not be empty")


def _validate_string_list(
    value: Any, path: str, issues: list[str], *, min_items: int = 0
) -> None:
    if not isinstance(value, list):
        issues.append(f"{path} must be an array")
        return
    if len(value) < min_items:
        issues.append(f"{path} must contain at least {min_items} item(s)")
    for index, item in enumerate(value):
        _validate_nonempty_string(item, f"{path}[{index}]", issues)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_briefing_sections(
    sections: dict, watchlist: list[dict]
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not _validate_exact_keys(sections, BRIEFING_KEYS, "$", issues):
        if not isinstance(sections, dict):
            return False, issues

    for key in BRIEFING_KEYS - {"watchlist_notes"}:
        _validate_nonempty_string(sections.get(key), f"$.{key}", issues)

    watchlist_notes = sections.get("watchlist_notes")
    if not isinstance(watchlist_notes, list):
        issues.append("$.watchlist_notes must be an array")
        return False, issues + scan_prohibited_language(sections)

    configured_symbols = [
        item.get("symbol") for item in watchlist if item.get("symbol")
    ]
    configured_types = {
        item.get("symbol"): item.get("type") for item in watchlist if item.get("symbol")
    }
    seen_symbols = []

    for index, note in enumerate(watchlist_notes):
        path = f"$.watchlist_notes[{index}]"
        if not _validate_exact_keys(note, WATCHLIST_NOTE_KEYS, path, issues):
            if not isinstance(note, dict):
                continue

        for key in {"symbol", "asset_class", "bias", "confidence", "summary", "note"}:
            _validate_nonempty_string(note.get(key), f"{path}.{key}", issues)

        symbol = note.get("symbol")
        if symbol in seen_symbols:
            issues.append(f"{path}.symbol duplicates '{symbol}'")
        seen_symbols.append(symbol)

        if symbol not in configured_types:
            issues.append(f"{path}.symbol '{symbol}' is not configured")
        elif note.get("asset_class") != configured_types[symbol]:
            issues.append(
                f"{path}.asset_class must be '{configured_types[symbol]}' for {symbol}"
            )

        if note.get("asset_class") not in ALLOWED_ASSET_CLASS:
            issues.append(f"{path}.asset_class has an invalid value")
        if note.get("bias") not in ALLOWED_BIAS:
            issues.append(f"{path}.bias has an invalid value")
        if note.get("confidence") not in ALLOWED_CONFIDENCE:
            issues.append(f"{path}.confidence has an invalid value")

    if seen_symbols != configured_symbols:
        issues.append(
            "$.watchlist_notes symbols must exactly match configured watchlist order: "
            + ", ".join(configured_symbols)
        )

    issues.extend(scan_prohibited_language(sections))
    return not issues, issues


def validate_macro_regime_output(parsed: dict) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not _validate_exact_keys(parsed, MACRO_REGIME_KEYS, "$", issues):
        if not isinstance(parsed, dict):
            return False, issues

    for key in {"summary", "reasoning", "market_implications"}:
        _validate_nonempty_string(parsed.get(key), f"$.{key}", issues)
    _validate_string_list(parsed.get("key_factors"), "$.key_factors", issues, min_items=1)
    _validate_string_list(parsed.get("caution_flags"), "$.caution_flags", issues)

    if parsed.get("regime") not in ALLOWED_REGIME:
        issues.append("$.regime has an invalid value")
    if parsed.get("sub_regime") not in ALLOWED_SUB_REGIME:
        issues.append("$.sub_regime has an invalid value")
    if parsed.get("direction") not in ALLOWED_BIAS:
        issues.append("$.direction has an invalid value")
    if parsed.get("confidence") not in ALLOWED_CONFIDENCE:
        issues.append("$.confidence has an invalid value")
    if parsed.get("timeframe") not in ALLOWED_TIMEFRAME:
        issues.append("$.timeframe has an invalid value")

    issues.extend(scan_prohibited_language(parsed))
    return not issues, issues


def validate_event_impact_output(
    parsed: dict,
    expected_events: list[dict],
    watchlist: list[dict],
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not _validate_exact_keys(parsed, EVENT_IMPACT_KEYS, "$", issues):
        if not isinstance(parsed, dict):
            return False, issues

    _validate_nonempty_string(
        parsed.get("overall_volatility_outlook"),
        "$.overall_volatility_outlook",
        issues,
    )
    _validate_nonempty_string(
        parsed.get("catalyst_summary"), "$.catalyst_summary", issues
    )

    events = parsed.get("events")
    if not isinstance(events, list):
        issues.append("$.events must be an array")
        return False, issues + scan_prohibited_language(parsed)

    expected_names = [event.get("event_name") for event in expected_events]
    actual_names = []
    configured_symbols = {
        item.get("symbol") for item in watchlist if item.get("symbol")
    }

    for index, event in enumerate(events):
        path = f"$.events[{index}]"
        if not _validate_exact_keys(event, EVENT_KEYS, path, issues):
            if not isinstance(event, dict):
                continue

        for key in {
            "event_name",
            "scheduled_at",
            "consensus",
            "previous",
            "context",
            "market_implications",
        }:
            _validate_nonempty_string(event.get(key), f"{path}.{key}", issues)

        actual_names.append(event.get("event_name"))
        actual_time = _parse_timestamp(event.get("scheduled_at"))
        if actual_time is None:
            issues.append(f"{path}.scheduled_at must be an ISO-8601 timestamp")
        elif index < len(expected_events):
            expected_time = _parse_timestamp(expected_events[index].get("scheduled_at"))
            if expected_time is not None and actual_time != expected_time:
                issues.append(
                    f"{path}.scheduled_at does not match the source event timestamp"
                )

        for scenario_key in {
            "consensus_met_scenario",
            "upside_surprise_scenario",
            "downside_surprise_scenario",
        }:
            scenario_path = f"{path}.{scenario_key}"
            scenario = event.get(scenario_key)
            if not _validate_exact_keys(scenario, SCENARIO_KEYS, scenario_path, issues):
                if not isinstance(scenario, dict):
                    continue
            _validate_nonempty_string(
                scenario.get("narrative"), f"{scenario_path}.narrative", issues
            )
            if scenario.get("direction") not in ALLOWED_EVENT_DIRECTION:
                issues.append(f"{scenario_path}.direction has an invalid value")
            if scenario.get("volatility") not in ALLOWED_VOLATILITY:
                issues.append(f"{scenario_path}.volatility has an invalid value")

        affected = event.get("affected_instruments")
        if not isinstance(affected, list):
            issues.append(f"{path}.affected_instruments must be an array")
            continue
        seen = set()
        for affected_index, instrument in enumerate(affected):
            instrument_path = f"{path}.affected_instruments[{affected_index}]"
            if not _validate_exact_keys(
                instrument, AFFECTED_INSTRUMENT_KEYS, instrument_path, issues
            ):
                if not isinstance(instrument, dict):
                    continue
            symbol = instrument.get("symbol")
            _validate_nonempty_string(symbol, f"{instrument_path}.symbol", issues)
            _validate_nonempty_string(
                instrument.get("expected_reaction"),
                f"{instrument_path}.expected_reaction",
                issues,
            )
            if symbol not in configured_symbols:
                issues.append(f"{instrument_path}.symbol '{symbol}' is not configured")
            if symbol in seen:
                issues.append(f"{instrument_path}.symbol duplicates '{symbol}'")
            seen.add(symbol)
            if instrument.get("sensitivity") not in ALLOWED_SENSITIVITY:
                issues.append(f"{instrument_path}.sensitivity has an invalid value")

    if actual_names != expected_names:
        issues.append(
            "$.events names must exactly match source event order: "
            + ", ".join(str(name) for name in expected_names)
        )

    issues.extend(scan_prohibited_language(parsed))
    return not issues, issues


def coerce_briefing_fields(sections: dict) -> list[str]:
    """Normalize recognized enum variants before strict validation."""
    warnings = []
    watchlist_notes = sections.get("watchlist_notes")
    if not isinstance(watchlist_notes, list):
        return warnings

    bias_coercions = {
        "slightly bullish": "bullish",
        "mildly bullish": "bullish",
        "somewhat bullish": "bullish",
        "slightly bearish": "bearish",
        "mildly bearish": "bearish",
        "somewhat bearish": "bearish",
        "slightly neutral": "neutral",
        "cautiously bullish": "bullish",
        "cautiously bearish": "bearish",
        "lean bullish": "bullish",
        "lean bearish": "bearish",
        "bull": "bullish",
        "bear": "bearish",
        "flat": "neutral",
        "sideways": "neutral",
        "range-bound": "neutral",
        "rangebound": "neutral",
    }
    confidence_coercions = {
        "medium": "moderate",
        "medium confidence": "moderate",
        "very high": "high",
        "very low": "low",
        "fairly high": "high",
        "fairly low": "low",
        "moderate confidence": "moderate",
        "high confidence": "high",
        "low confidence": "low",
    }

    for index, note in enumerate(watchlist_notes):
        if not isinstance(note, dict):
            continue
        for field, allowed, coercions in (
            ("bias", ALLOWED_BIAS, bias_coercions),
            ("confidence", ALLOWED_CONFIDENCE, confidence_coercions),
        ):
            value = note.get(field)
            if not isinstance(value, str):
                continue
            normalized = value.lower().strip()
            replacement = normalized if normalized in allowed else coercions.get(normalized)
            if replacement and replacement != value:
                note[field] = replacement
                warnings.append(
                    f"watchlist_notes[{index}] ({note.get('symbol', '?')}): "
                    f"coerced {field} '{value}' -> '{replacement}'"
                )
    return warnings


def coerce_common_enums(parsed: dict) -> list[str]:
    """Lowercase known top-level/scenario enum strings without inventing defaults."""
    warnings = []

    def normalize(container: dict, field: str, allowed: set, path: str) -> None:
        value = container.get(field)
        if not isinstance(value, str):
            return
        normalized = value.lower().strip().replace(" ", "_")
        if normalized in allowed and normalized != value:
            container[field] = normalized
            warnings.append(f"{path}: coerced '{value}' -> '{normalized}'")

    normalize(parsed, "direction", ALLOWED_BIAS, "$.direction")
    normalize(parsed, "confidence", ALLOWED_CONFIDENCE, "$.confidence")
    normalize(parsed, "timeframe", ALLOWED_TIMEFRAME, "$.timeframe")
    normalize(parsed, "regime", ALLOWED_REGIME, "$.regime")
    normalize(parsed, "sub_regime", ALLOWED_SUB_REGIME - {None}, "$.sub_regime")

    events = parsed.get("events")
    if isinstance(events, list):
        for event_index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            for scenario_key in {
                "consensus_met_scenario",
                "upside_surprise_scenario",
                "downside_surprise_scenario",
            }:
                scenario = event.get(scenario_key)
                if isinstance(scenario, dict):
                    normalize(
                        scenario,
                        "direction",
                        ALLOWED_EVENT_DIRECTION,
                        f"$.events[{event_index}].{scenario_key}.direction",
                    )
                    normalize(
                        scenario,
                        "volatility",
                        ALLOWED_VOLATILITY,
                        f"$.events[{event_index}].{scenario_key}.volatility",
                    )
            affected = event.get("affected_instruments")
            if isinstance(affected, list):
                for affected_index, instrument in enumerate(affected):
                    if isinstance(instrument, dict):
                        normalize(
                            instrument,
                            "sensitivity",
                            ALLOWED_SENSITIVITY,
                            f"$.events[{event_index}].affected_instruments"
                            f"[{affected_index}].sensitivity",
                        )
    return warnings


def repair_prompt(prompt_text: str, issues: list[str]) -> str:
    issue_text = "\n".join(f"- {issue}" for issue in issues)
    return (
        prompt_text
        + "\n\nREPAIR REQUIRED\n"
        + "The previous response was rejected for these reasons:\n"
        + issue_text
        + "\nReturn a complete replacement JSON object only. Follow the exact schema "
        + "and the economics-only policy. Do not repeat prohibited language."
    )
