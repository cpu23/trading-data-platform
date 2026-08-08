"""Shared display-symbol to event-exposure rules for cockpit components."""

ASSET_EVENT_RULES = {
    "EURUSD": {"currencies": {"EUR", "USD"}},
    "DXY": {"currencies": {"USD"}},
    "USDJPY": {"currencies": {"USD", "JPY"}},
    "AUDJPY": {"currencies": {"AUD", "JPY"}},
    "SP500": {"currencies": {"USD"}, "countries": {"US"}},
    "XAUUSD": {
        "currencies": {"USD"},
        "keywords": {
            "inflation",
            "cpi",
            "ppi",
            "rates",
            "rate",
            "fed",
            "fomc",
            "yield",
            "risk",
            "jobs",
            "payroll",
        },
    },
    "XPTUSD": {
        "currencies": {"USD"},
        "keywords": {
            "inflation",
            "cpi",
            "ppi",
            "industrial",
            "manufacturing",
            "pmi",
            "risk",
            "growth",
            "china",
        },
    },
    "GER40": {
        "currencies": {"EUR"},
        "countries": {"EU", "DE"},
        "keywords": {"germany", "german", "ecb", "eurozone"},
    },
    "UK100": {
        "currencies": {"GBP"},
        "countries": {"GB", "UK"},
        "keywords": {"uk", "britain", "boe", "bank of england"},
    },
}
