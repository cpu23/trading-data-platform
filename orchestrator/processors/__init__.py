from processors.briefing import DailyBriefingProcessor
from processors.event_impact import EventImpactProcessor
from processors.intelligence import MarketIntelligenceProcessor
from processors.macro_regime import MacroRegimeProcessor

PROCESSORS: dict[str, type] = {
    "macro_regime": MacroRegimeProcessor,
    "event_impact": EventImpactProcessor,
    "briefing": DailyBriefingProcessor,
    "market_intelligence": MarketIntelligenceProcessor,
}


def get_processor(processor_id: str):
    if processor_id not in PROCESSORS:
        raise ValueError(f"Unknown processor: {processor_id}")
    return PROCESSORS[processor_id]()


def get_all_processors() -> dict:
    return {pid: cls() for pid, cls in PROCESSORS.items()}
