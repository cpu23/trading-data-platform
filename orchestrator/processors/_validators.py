ALLOWED_BIAS = {"bullish", "bearish", "neutral", "mixed"}
ALLOWED_CONFIDENCE = {"high", "moderate", "low"}
ALLOWED_ASSET_CLASS = {"forex", "index", "metal"}

REQUIRED_WATCHLIST_KEYS = {"symbol", "asset_class", "bias", "confidence", "summary", "note"}


def validate_briefing_sections(
    sections: dict, watchlist: list[dict]
) -> tuple[bool, list[str]]:
    """Validate the watchlist_notes array shape in briefing sections.

    Returns (is_valid, list_of_warnings). Used to log shape issues
    without failing the run. UI-level handling deals with missing data.
    """
    warnings = []
    is_valid = True

    watchlist_notes = sections.get("watchlist_notes")

    if watchlist_notes is None:
        warnings.append("watchlist_notes is missing from sections")
        return False, warnings

    if isinstance(watchlist_notes, str):
        warnings.append(
            "watchlist_notes is a string (legacy v1 format), not an array"
        )
        return False, warnings

    if not isinstance(watchlist_notes, list):
        warnings.append(
            f"watchlist_notes is type {type(watchlist_notes).__name__}, expected list"
        )
        return False, warnings

    configured_symbols = [w.get("symbol") for w in watchlist if w.get("symbol")]
    configured_symbol_set = set(configured_symbols)
    seen_symbols = []

    for i, note in enumerate(watchlist_notes):
        if not isinstance(note, dict):
            warnings.append(f"watchlist_notes[{i}] is not a dict, got {type(note).__name__}")
            is_valid = False
            continue

        missing_keys = REQUIRED_WATCHLIST_KEYS - set(note.keys())
        if missing_keys:
            warnings.append(
                f"watchlist_notes[{i}] missing keys: {', '.join(sorted(missing_keys))}"
            )
            is_valid = False

        symbol = note.get("symbol", f"<index {i}>")
        if symbol in seen_symbols:
            warnings.append(f"watchlist_notes contains duplicate symbol '{symbol}'")
            is_valid = False
        seen_symbols.append(symbol)

        if symbol not in configured_symbol_set:
            warnings.append(
                f"watchlist_notes[{i}] symbol '{symbol}' is not in configured watchlist"
            )
            is_valid = False

        bias = note.get("bias")
        confidence = note.get("confidence")
        asset_class = note.get("asset_class")

        if bias is not None and bias not in ALLOWED_BIAS:
            warnings.append(
                f"watchlist_notes[{i}] ({symbol}): invalid bias '{bias}'"
            )
            is_valid = False

        if confidence is not None and confidence not in ALLOWED_CONFIDENCE:
            warnings.append(
                f"watchlist_notes[{i}] ({symbol}): invalid confidence '{confidence}'"
            )
            is_valid = False

        if asset_class is not None and asset_class not in ALLOWED_ASSET_CLASS:
            warnings.append(
                f"watchlist_notes[{i}] ({symbol}): invalid asset_class '{asset_class}'"
            )
            is_valid = False

    for symbol in configured_symbols:
        if symbol not in seen_symbols:
            warnings.append(f"Configured symbol '{symbol}' missing from watchlist_notes")
            is_valid = False

    seen_configured = [symbol for symbol in seen_symbols if symbol in configured_symbol_set]
    if seen_configured != configured_symbols:
        warnings.append(
            "watchlist_notes order does not match configured watchlist order"
        )
        is_valid = False

    return is_valid, warnings


def coerce_briefing_fields(sections: dict) -> list[str]:
    """Coerce ambiguous bias/confidence values to allowed enums.

    Returns a list of coercion warnings for logging.
    """
    warnings = []
    watchlist_notes = sections.get("watchlist_notes")

    if not isinstance(watchlist_notes, list):
        return warnings

    BIAS_COERCIONS = {
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

    CONFIDENCE_COERCIONS = {
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

    for i, note in enumerate(watchlist_notes):
        if not isinstance(note, dict):
            continue

        bias = note.get("bias")
        if bias and bias not in ALLOWED_BIAS:
            lower_bias = bias.lower().strip()
            if lower_bias in BIAS_COERCIONS:
                note["bias"] = BIAS_COERCIONS[lower_bias]
                warnings.append(
                    f"watchlist_notes[{i}] ({note.get('symbol', '?')}): "
                    f"coerced bias '{bias}' -> '{note['bias']}'"
                )
            else:
                note["bias"] = "mixed"
                warnings.append(
                    f"watchlist_notes[{i}] ({note.get('symbol', '?')}): "
                    f"could not coerce bias '{bias}', defaulting to 'mixed'"
                )

        confidence = note.get("confidence")
        if confidence and confidence not in ALLOWED_CONFIDENCE:
            lower_conf = confidence.lower().strip()
            if lower_conf in CONFIDENCE_COERCIONS:
                note["confidence"] = CONFIDENCE_COERCIONS[lower_conf]
                warnings.append(
                    f"watchlist_notes[{i}] ({note.get('symbol', '?')}): "
                    f"coerced confidence '{confidence}' -> '{note['confidence']}'"
                )
            else:
                note["confidence"] = "low"
                warnings.append(
                    f"watchlist_notes[{i}] ({note.get('symbol', '?')}): "
                    f"could not coerce confidence '{confidence}', defaulting to 'low'"
                )

    return warnings
