from collectors.fred import FredCollector
from collectors.forex_factory import ForexFactoryCollector
from collectors.oanda import OandaCollector

COLLECTORS: dict[str, type] = {
    "fred": FredCollector,
    "forex_factory": ForexFactoryCollector,
    "oanda": OandaCollector,
}


def get_collector(source_id: str):
    if source_id not in COLLECTORS:
        raise ValueError(f"Unknown collector: {source_id}")
    return COLLECTORS[source_id]()


def get_all_collectors() -> dict:
    return {sid: cls() for sid, cls in COLLECTORS.items()}
