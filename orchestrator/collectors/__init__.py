from collectors.central_banks import CentralBanksCollector
from collectors.cftc import CftcCollector
from collectors.forex_factory import ForexFactoryCollector
from collectors.fred import FredCollector
from collectors.official_macro import (
    BoeCollector,
    EcbCollector,
    EiaCollector,
    OecdCollector,
)

COLLECTORS: dict[str, type] = {
    "fred": FredCollector,
    "forex_factory": ForexFactoryCollector,
    "cftc": CftcCollector,
    "central_banks": CentralBanksCollector,
    "oecd": OecdCollector,
    "ecb": EcbCollector,
    "boe": BoeCollector,
    "eia": EiaCollector,
}


def get_collector(source_id: str):
    if source_id not in COLLECTORS:
        raise ValueError(f"Unknown collector: {source_id}")
    return COLLECTORS[source_id]()


def get_all_collectors() -> dict:
    return {sid: cls() for sid, cls in COLLECTORS.items()}
