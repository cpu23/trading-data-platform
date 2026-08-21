from collectors.cboe_options import CboeOptionsCollector
from collectors.central_banks import CentralBanksCollector
from collectors.cftc import CftcCollector
from collectors.company_expectations import CompanyExpectationsCollector
from collectors.forex_factory import ForexFactoryCollector
from collectors.fred import FredCollector
from collectors.issuer_news import IssuerNewsCollector
from collectors.issuer_transcripts import IssuerTranscriptsCollector
from collectors.oanda import OandaCollector
from collectors.official_macro import (
    BoeCollector,
    EcbCollector,
    EiaCollector,
    OecdCollector,
)
from collectors.public_equities import PublicEquitiesCollector
from collectors.public_positioning import (
    FinraShortVolumeCollector,
    SecForm4Collector,
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
    # Free/public keyless collectors: scheduled without credentials.
    "issuer_news": IssuerNewsCollector,
    "issuer_transcripts": IssuerTranscriptsCollector,
    "public_equities": PublicEquitiesCollector,
    "sec_form4": SecForm4Collector,
    "finra_short_volume": FinraShortVolumeCollector,
    "cboe_options": CboeOptionsCollector,
    "company_expectations": CompanyExpectationsCollector,
}

# Standalone snapshot collectors are addressable by the scheduler and CLI but
# excluded from dependency cycles. OANDA's continuous stream owns live quotes;
# this collector persists the configured periodic reaction-window snapshot.
STANDALONE_COLLECTORS: dict[str, type] = {
    "oanda": OandaCollector,
}


def get_collector(source_id: str):
    collector_type = COLLECTORS.get(source_id) or STANDALONE_COLLECTORS.get(source_id)
    if collector_type is None:
        raise ValueError(f"Unknown collector: {source_id}")
    return collector_type()


def get_all_collectors() -> dict:
    return {sid: cls() for sid, cls in COLLECTORS.items()}
