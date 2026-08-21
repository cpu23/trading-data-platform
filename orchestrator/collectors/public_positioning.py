"""Public positioning collectors: SEC Form 4 insider activity and FINRA short volume.

Both sources are official, free, automation-permitted public endpoints:

* ``SecForm4Collector`` reads the SEC EDGAR submissions JSON API and the
  ownership-document XML behind each Form 4 (and 4/A amendment) filing for
  operator-configured CIKs, then aggregates open-market insider purchases and
  sales per issuer per transaction date. SEC fair-access policy (max 10
  requests/second) is honored with a configurable request interval.
* ``FinraShortVolumeCollector`` downloads FINRA's Reg SHO daily short-sale
  volume files (``CNMSshvolYYYYMMDD.txt``) from the public CDN and aggregates
  short-sale volume per symbol per trade date.

Both write ``positioning_reports`` rows whose ``metadata.positioning_kind``
explicitly distinguishes the measure:

* ``insider_activity`` — Form 4 open-market transactions (never short sales;
  the sell side is dispositions of shares held),
* ``short_volume`` — FINRA daily short-sale *volume* (a delayed same-day
  proxy/flow measure, never short *interest*),
* ``futures_positioning`` — CFTC Commitments of Traders (see ``cftc.py``).

Every record separates the source timestamp (``report_date`` plus
``metadata.source_time``) from the acquisition timestamp (``acquired_at`` and
``metadata.acquired_at``). Bounded output is valid: an issuer with no Form 4
filings in the window, or a missing FINRA file on a non-trading day, produces
zero records instead of fabricated values, while malformed provider payloads
fail explicitly per issuer/date.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import PurePosixPath

from collectors.base import CollectorNoData, CollectorSetupRequired
from http_client import make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.public_positioning")

# SEC requires a descriptive User-Agent with contact information (fair access
# policy). Kept in sync with investment_filings.SEC_USER_AGENT.
SEC_USER_AGENT = (
    "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"
)
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
FINRA_DAILY_BASE_URL = "https://cdn.finra.org/equity/regsho/daily/"

# SEC permits 10 requests/second; keep a margin below the ceiling.
DEFAULT_SEC_REQUEST_INTERVAL_SECONDS = 0.12
# FINRA's CDN documents no strict limit; a small interval is polite for the
# multi-megabyte daily files.
DEFAULT_FINRA_REQUEST_INTERVAL_SECONDS = 0.05

POSITIONING_KIND_INSIDER_ACTIVITY = "insider_activity"
POSITIONING_KIND_SHORT_VOLUME = "short_volume"
POSITIONING_KIND_FUTURES_POSITIONING = "futures_positioning"
# Short interest is a distinct measure produced by other reports (e.g. the
# bi-monthly FINRA short interest files). These collectors never emit it.
NOT_SHORT_INTEREST_NOTE = "not short interest"

_SEC_PACE_STATE = [0.0]
_SEC_PACE_LOCK = threading.Lock()
_FINRA_PACE_STATE = [0.0]
_FINRA_PACE_LOCK = threading.Lock()


def _pace_requests(state: list[float], lock: threading.Lock, interval_seconds: float):
    """Serialize request starts below the provider's documented rate ceiling."""
    if interval_seconds <= 0:
        return
    with lock:
        now = time.monotonic()
        wait_seconds = state[0] - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            now = time.monotonic()
        state[0] = now + interval_seconds


def _bounded_int(value, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lower, min(parsed, upper))


def _bounded_float(value, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lower, min(parsed, upper))


def _round_int(value: Decimal) -> int:
    """Round a (possibly fractional-share) value to an integer, half up."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_string(value: Decimal) -> str:
    return str(value)


def _size_exceeds(response, limit: int) -> bool:
    """Bound a payload before it is consumed; Content-Length is authoritative."""
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            return int(raw_length) > limit
        except (TypeError, ValueError):
            pass
    try:
        return len(getattr(response, "content", b"")) > limit
    except TypeError:  # content not measurable on this response object
        return False


def _normalize_cik(value) -> str:
    if value is None:
        return ""
    digits = "".join(character for character in str(value) if character.isdigit())
    return digits


def _pad_cik(cik: str) -> str:
    """Pad a CIK to the 10-digit form used by data.sec.gov."""
    return cik.zfill(10)


def _strip_cik(cik: str) -> str:
    """Remove leading zeros for www.sec.gov archive paths."""
    return str(int(cik)) if cik else ""


def _configured_symbols(raw_symbols) -> list[tuple[str, str, list]]:
    """Normalize configured symbols to ``(official, upper, assets)`` tuples.

    Accepts plain strings and dicts with ``symbol``/``assets``; comparisons
    are case-insensitive while emitted market ids keep the configured case.
    """
    resolved = []
    seen = set()
    for entry in raw_symbols or []:
        if isinstance(entry, Mapping):
            symbol = str(entry.get("symbol") or "").strip()
            assets = list(entry.get("assets") or [])
        else:
            symbol = str(entry).strip()
            assets = []
        upper = symbol.upper()
        if not symbol or upper in seen:
            continue
        seen.add(upper)
        resolved.append((symbol, upper, assets))
    return resolved


class SecForm4Collector:
    """Daily Form 4 insider buy/sell aggregation for configured CIKs."""

    source_id = "sec_form4"

    # SEC form types ingested: original ownership reports and their amendments.
    FORM4_FORMS = frozenset({"4", "4/A"})
    # Open-market purchase and sale codes from the SEC ownership-document
    # transaction coding. All other codes (G, M, A, F, J, ...) are counted
    # separately and never folded into buy/sell aggregates.
    BUY_CODE = "P"
    SELL_CODE = "S"

    def __init__(self):
        self.last_result_metadata: dict = {}

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        cfg = config["collectors"]["sec_form4"]
        issuers = cfg.get("issuers", [])
        if not issuers:
            raise CollectorSetupRequired(
                "No SEC Form 4 issuers are configured",
                source_id=self.source_id,
            )
        lookback_days = _bounded_int(cfg.get("lookback_days"), 30, 1, 180)
        max_filings = _bounded_int(cfg.get("max_filings_per_issuer"), 100, 1, 500)
        max_concurrency = _bounded_int(cfg.get("max_concurrency"), 4, 1, 8)
        interval = _bounded_float(
            cfg.get("request_interval_seconds"),
            DEFAULT_SEC_REQUEST_INTERVAL_SECONDS,
            0.0,
            60.0,
        )
        max_document_bytes = _bounded_int(
            cfg.get("max_document_bytes"), 5_000_000, 10_000, 50_000_000
        )
        max_submissions_bytes = _bounded_int(
            cfg.get("max_submissions_bytes"), 25_000_000, 100_000, 100_000_000
        )
        user_agent = str(cfg.get("user_agent") or SEC_USER_AGENT).strip()
        if not user_agent:
            user_agent = SEC_USER_AGENT
        submissions_url = validate_configured_origin(
            cfg.get("url") or SEC_SUBMISSIONS_URL,
            cfg,
            label="SEC submissions url",
            canonical={SEC_SUBMISSIONS_URL},
        )
        archive_url = validate_configured_origin(
            cfg.get("archive_url") or SEC_ARCHIVE_URL,
            cfg,
            label="SEC archive url",
            canonical={SEC_ARCHIVE_URL},
        )
        cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
        acquired_at = datetime.now(UTC)

        errors = []
        records = []
        stats_by_issuer = []
        with ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix="sec-form4"
        ) as executor:
            futures = {
                executor.submit(
                    self._collect_issuer,
                    issuer,
                    submissions_url,
                    archive_url,
                    user_agent,
                    cutoff,
                    max_filings,
                    max_document_bytes,
                    max_submissions_bytes,
                    interval,
                    acquired_at,
                    correlation_id,
                ): issuer
                for issuer in issuers
            }
            for future in futures:
                issuer = futures[future]
                cik = _normalize_cik(issuer.get("cik"))
                try:
                    issuer_records, issuer_error, issuer_stats = future.result()
                except Exception as exc:  # isolate one issuer's failure
                    issuer_records = []
                    issuer_stats = {}
                    issuer_error = {
                        "cik": cik,
                        "symbol": str(issuer.get("symbol") or ""),
                        "stage": "collect",
                        "code": "request_failed",
                        "exception_type": type(exc).__name__,
                        "error_class": "transient_source",
                    }
                if issuer_error is not None:
                    errors.append(issuer_error)
                    logger.error(
                        "sec_form4_issuer_failed",
                        source_id=self.source_id,
                        cik=cik,
                        stage=issuer_error.get("stage"),
                        code=issuer_error.get("code"),
                        correlation_id=correlation_id,
                    )
                else:
                    records.extend(issuer_records)
                stats_by_issuer.append(
                    {
                        "cik": cik,
                        "symbol": str(issuer.get("symbol") or ""),
                        **issuer_stats,
                    }
                )

        records.sort(key=lambda record: (record["report_date"], record["market_id"]))
        state = "partial" if errors else "success"
        if not records:
            failed_ciks = {error.get("cik") for error in errors}
            empty_issuers = [
                {"cik": stats["cik"], "symbol": stats.get("symbol", "")}
                for stats in stats_by_issuer
                if stats.get("records", 0) == 0 and stats["cik"] not in failed_ciks
            ]
            raise CollectorNoData(
                "SEC Form 4 produced no observations for configured issuers",
                source_id=self.source_id,
                failed_issuers=errors,
                empty_issuers=empty_issuers,
                issuer_stats=stats_by_issuer,
            )
        self.last_result_metadata = {
            "state": state,
            "source_id": self.source_id,
            "issuers_configured": len(issuers),
            "issuers_failed": errors,
            "issuer_stats": stats_by_issuer,
            "records": len(records),
            "acquired_at": acquired_at.isoformat(),
        }
        logger.info(
            "sec_form4_collection_completed",
            source_id=self.source_id,
            state=state,
            records=len(records),
            issuers_configured=self.last_result_metadata["issuers_configured"],
            issuers_failed=len(errors),
            acquired_at=self.last_result_metadata["acquired_at"],
            correlation_id=correlation_id,
        )
        return records

    def _collect_issuer(
        self,
        issuer: dict,
        submissions_url: str,
        archive_url: str,
        user_agent: str,
        cutoff: date,
        max_filings: int,
        max_document_bytes: int,
        max_submissions_bytes: int,
        interval: float,
        acquired_at: datetime,
        correlation_id: str,
    ):
        cik = _normalize_cik(issuer.get("cik"))
        if not cik:
            return (
                [],
                {
                    "cik": "",
                    "symbol": str(issuer.get("symbol") or ""),
                    "stage": "config",
                    "code": "invalid_source_data",
                    "exception_type": "ValueError",
                    "error_class": "invalid_source_data",
                },
                {},
            )
        symbol = str(issuer.get("symbol") or "").strip() or None
        try:
            _pace_requests(_SEC_PACE_STATE, _SEC_PACE_LOCK, interval)
            response = make_request(
                "GET",
                submissions_url.format(cik=_pad_cik(cik)),
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                correlation_id=correlation_id,
            )
            response.raise_for_status()
            if _size_exceeds(response, max_submissions_bytes):
                raise ValueError("SEC submissions payload exceeds configured bound")
            payload = response.json()
        except Exception as exc:
            return [], self._issuer_error(issuer, cik, "submissions", exc), {}

        filings = self._select_form4_filings(payload, cutoff, max_filings)
        transactions = []
        documents_failed = []
        malformed_transactions = []
        issuer_info = {}
        for filing in filings:
            try:
                content = self._fetch_filing_document(
                    archive_url,
                    cik,
                    filing["accession"],
                    filing["primary_document"],
                    user_agent,
                    max_document_bytes,
                    interval,
                    correlation_id,
                )
                parsed = self._parse_ownership_document(content, filing)
                transactions.extend(parsed["transactions"])
                malformed_transactions.extend(parsed["malformed_transactions"])
                if not issuer_info:
                    issuer_info = {
                        "issuer_cik": parsed["issuer_cik"],
                        "issuer_name": parsed["issuer_name"],
                        "issuer_symbol": parsed["issuer_symbol"],
                    }
            except Exception as exc:
                documents_failed.append(
                    {
                        "accession": filing["accession"],
                        "error": type(exc).__name__,
                    }
                )
                logger.warning(
                    "sec_form4_document_failed",
                    source_id=self.source_id,
                    cik=cik,
                    accession=filing["accession"],
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )

        stats = {
            "filings_selected": len(filings),
            "filings_parsed": len(filings) - len(documents_failed),
            "documents_failed": documents_failed[:20],
            "documents_failed_count": len(documents_failed),
            "malformed_transactions": malformed_transactions[:50],
            "malformed_transactions_count": len(malformed_transactions),
        }
        if not filings:
            # No Form 4 filings in the window: a valid empty outcome, not a
            # failure and never fabricated values.
            stats["records"] = 0
            return [], None, stats
        if not transactions:
            return (
                [],
                self._issuer_error(
                    issuer,
                    cik,
                    "documents",
                    RuntimeError(
                        "no Form 4 transactions could be parsed"
                        + (
                            f" ({len(documents_failed)} document(s) failed)"
                            if documents_failed
                            else ""
                        )
                    ),
                ),
                stats,
            )
        issuer_records, aggregation_stats = self._aggregate_issuer(
            transactions, issuer, cik, symbol, acquired_at, issuer_info
        )
        stats.update(aggregation_stats)
        return issuer_records, None, stats

    @staticmethod
    def _issuer_error(issuer: dict, cik: str, stage: str, exc: Exception) -> dict:
        return {
            "cik": cik,
            "symbol": str(issuer.get("symbol") or ""),
            "stage": stage,
            "code": "invalid_source_data"
            if stage in {"documents", "config"}
            else "request_failed",
            "exception_type": type(exc).__name__,
            "error_class": (
                "invalid_source_data"
                if stage in {"documents", "config"}
                else "transient_source"
            ),
        }

    @staticmethod
    def _select_form4_filings(
        payload: dict, cutoff: date, max_filings: int
    ) -> list[dict]:
        """Pick bounded, in-window Form 4/4/A filings from a submissions payload.

        SEC lists ``filings.recent`` newest first; iterating in order keeps the
        most recent filings when the per-issuer cap is reached.
        """
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        filing_dates = recent.get("filingDate") or []
        primary_documents = recent.get("primaryDocument") or []
        descriptions = recent.get("primaryDocDescription") or []
        cutoff_iso = cutoff.isoformat()
        selected = []
        for index, form in enumerate(forms):
            if form not in SecForm4Collector.FORM4_FORMS:
                continue
            filing_date = filing_dates[index] if index < len(filing_dates) else ""
            if not filing_date or filing_date < cutoff_iso:
                continue
            accession = accessions[index] if index < len(accessions) else ""
            primary_document = (
                primary_documents[index] if index < len(primary_documents) else ""
            )
            if not accession or not primary_document:
                continue
            selected.append(
                {
                    "form": form,
                    "accession": accession,
                    "filing_date": filing_date,
                    "primary_document": primary_document,
                    "description": (
                        descriptions[index] if index < len(descriptions) else ""
                    ),
                }
            )
            if len(selected) >= max_filings:
                break
        return selected

    def _fetch_filing_document(
        self,
        archive_url: str,
        cik: str,
        accession: str,
        primary_document: str,
        user_agent: str,
        max_bytes: int,
        interval: float,
        correlation_id: str,
    ) -> bytes:
        # EDGAR's submissions feed may point at an XSL viewer path such as
        # ``xslF345X06/form4.xml``.  The raw ownership XML lives at the filing
        # root under the same basename; parsing the transformed HTML would
        # reject every current Form 4 document.
        document_name = PurePosixPath(str(primary_document)).name
        if not document_name or document_name in {".", ".."}:
            raise ValueError("SEC primary document name is invalid")
        url = archive_url.format(
            cik=_strip_cik(cik),
            accession=accession.replace("-", ""),
            document=document_name,
        )
        _pace_requests(_SEC_PACE_STATE, _SEC_PACE_LOCK, interval)
        response = make_request(
            "GET",
            url,
            headers={"User-Agent": user_agent, "Accept": "application/xml"},
            correlation_id=correlation_id,
        )
        response.raise_for_status()
        if _size_exceeds(response, max_bytes):
            raise ValueError("SEC document exceeds configured size bound")
        return response.content

    def _parse_ownership_document(self, content: bytes, filing: dict) -> dict:
        """Parse one Form 4 ownership document into normalized transactions.

        Uses namespace-agnostic local-name matching because EDGAR ownership
        documents are served both with and without a default namespace.
        """
        root = ET.fromstring(content)
        document_type = _child_text(root, "documentType") or filing["form"]
        issuer_element = _local_child(root, "issuer")
        issuer_cik = (
            _child_text(issuer_element, "issuerCik")
            if issuer_element is not None
            else ""
        )
        issuer_name = (
            _child_text(issuer_element, "issuerName")
            if issuer_element is not None
            else ""
        )
        issuer_symbol = (
            _child_text(issuer_element, "issuerTradingSymbol")
            if issuer_element is not None
            else ""
        )
        owner = _local_child(root, "reportingOwner")
        owner_id = (
            _local_child(owner, "reportingOwnerId") if owner is not None else None
        )
        owner_cik = _child_text(owner_id, "rptOwnerCik") if owner_id is not None else ""
        owner_name = (
            _child_text(owner_id, "rptOwnerName") if owner_id is not None else ""
        )

        transactions = []
        malformed_transactions = []
        table = _local_child(root, "nonDerivativeTable")
        if table is not None:
            for tx in _local_children(table, "nonDerivativeTransaction"):
                coding = _local_child(tx, "transactionCoding")
                amounts = _local_child(tx, "transactionAmounts")
                transaction_date = _nested_text(tx, "transactionDate", "value")
                code = _child_text(coding, "transactionCode") or ""
                form_type = _child_text(coding, "transactionFormType") or ""
                shares_text = (
                    _nested_text(amounts, "transactionShares", "value")
                    if amounts is not None
                    else ""
                )
                price_text = (
                    _nested_text(amounts, "transactionPricePerShare", "value")
                    if amounts is not None
                    else ""
                )
                disposed = (
                    _nested_text(amounts, "transactionAcquiredDisposedCode", "value")
                    if amounts is not None
                    else ""
                )
                security_title = _nested_text(tx, "securityTitle", "value")
                try:
                    shares = Decimal(shares_text.strip())
                    if shares < 0:
                        raise ValueError("negative share count")
                    transaction_date = date.fromisoformat(transaction_date.strip())
                except (ValueError, TypeError, ArithmeticError):
                    malformed_transactions.append(
                        {
                            "accession": filing["accession"],
                            "error": "malformed_transaction",
                        }
                    )
                    continue
                price = None
                if price_text.strip():
                    try:
                        price = Decimal(price_text.strip())
                    except (ValueError, TypeError, ArithmeticError):
                        price = None
                transactions.append(
                    {
                        "form": form_type or filing["form"],
                        "accession": filing["accession"],
                        "filing_date": filing["filing_date"],
                        "owner_cik": _normalize_cik(owner_cik),
                        "owner_name": (owner_name or "").strip(),
                        "security_title": (security_title or "").strip(),
                        "transaction_date": transaction_date,
                        "code": code.upper(),
                        "disposed": disposed.upper(),
                        "shares": shares,
                        "price": price,
                    }
                )
        return {
            "document_type": document_type,
            "issuer_cik": _normalize_cik(issuer_cik),
            "issuer_name": (issuer_name or "").strip(),
            "issuer_symbol": (issuer_symbol or "").strip(),
            "transactions": transactions,
            "malformed_transactions": malformed_transactions,
        }

    def _aggregate_issuer(
        self,
        transactions: list[dict],
        issuer: dict,
        cik: str,
        symbol,
        acquired_at,
        issuer_info: dict | None = None,
    ):
        """Aggregate transactions per issuer per transaction date.

        Duplicate transactions within one filing form collapse first. Then the
        amendment rule applies: a Form 4/A transaction supersedes every
        remaining Form 4 transaction with the same identity key (owner,
        security, transaction date, code, acquired/disposed). This is
        deterministic and documented; it prevents double counting when an
        amended filing restates a transaction.
        """
        issuer_info = issuer_info or {}
        issuer_cik = issuer_info.get("issuer_cik") or cik
        issuer_name = (
            issuer_info.get("issuer_name") or str(issuer.get("name") or "").strip()
        )
        issuer_symbol = issuer_info.get("issuer_symbol") or ""
        market_id = symbol or issuer_symbol or issuer_cik

        groups = defaultdict(list)
        for tx in transactions:
            key = (
                tx["owner_cik"] or tx["owner_name"] or "",
                tx["security_title"],
                tx["transaction_date"],
                tx["code"],
                tx["disposed"],
            )
            groups[key].append(tx)

        def _dedupe(group: list[dict]) -> list[dict]:
            unique = []
            seen = set()
            for tx in group:
                identity = (
                    tx["form"],
                    tx["owner_cik"],
                    tx["security_title"],
                    tx["transaction_date"],
                    tx["code"],
                    tx["disposed"],
                    str(tx["shares"]),
                    str(tx["price"]),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append(tx)
            return unique

        kept = []
        superseded_transactions = 0
        for _key, group in groups.items():
            amended = _dedupe([tx for tx in group if tx["form"] == "4/A"])
            base = _dedupe([tx for tx in group if tx["form"] != "4/A"])
            if amended:
                superseded_transactions += len(base)
                kept.extend(amended)
            else:
                kept.extend(base)

        by_date = defaultdict(
            lambda: {
                "buy_shares": Decimal("0"),
                "buy_value": Decimal("0"),
                "buy_count": 0,
                "sell_shares": Decimal("0"),
                "sell_value": Decimal("0"),
                "sell_count": 0,
                "other_count": 0,
                "other_shares": Decimal("0"),
                "owners": set(),
                "accessions": set(),
                "amended_accessions": set(),
                "filing_dates": set(),
            }
        )
        for tx in kept:
            bucket = by_date[tx["transaction_date"]]
            bucket["owners"].add(tx["owner_cik"] or tx["owner_name"] or "")
            bucket["accessions"].add(tx["accession"])
            if tx["form"] == "4/A":
                bucket["amended_accessions"].add(tx["accession"])
            bucket["filing_dates"].add(tx["filing_date"])
            if tx["code"] == self.BUY_CODE and tx["disposed"] == "A":
                bucket["buy_shares"] += tx["shares"]
                bucket["buy_count"] += 1
                if tx["price"] is not None:
                    bucket["buy_value"] += tx["shares"] * tx["price"]
            elif tx["code"] == self.SELL_CODE and tx["disposed"] == "D":
                bucket["sell_shares"] += tx["shares"]
                bucket["sell_count"] += 1
                if tx["price"] is not None:
                    bucket["sell_value"] += tx["shares"] * tx["price"]
            else:
                bucket["other_count"] += 1
                bucket["other_shares"] += tx["shares"]

        records = []
        for transaction_date, agg in sorted(by_date.items()):
            buy_shares = _round_int(agg["buy_shares"])
            sell_shares = _round_int(agg["sell_shares"])
            records.append(
                {
                    "source": self.source_id,
                    "market_id": market_id,
                    "report_date": transaction_date,
                    "category": "insider_transactions",
                    "long_positions": buy_shares,
                    "short_positions": sell_shares,
                    "net_position": buy_shares - sell_shares,
                    "open_interest": None,
                    "net_pct_open_interest": None,
                    "acquired_at": acquired_at,
                    "metadata": {
                        "positioning_kind": POSITIONING_KIND_INSIDER_ACTIVITY,
                        "assets": issuer.get("assets") or [],
                        "semantics": (
                            "SEC Form 4 open-market insider purchases (P) and "
                            "sales (S) aggregated by transaction date; sales are "
                            "dispositions of shares held, not short sales; "
                            + NOT_SHORT_INTEREST_NOTE
                        ),
                        "issuer_cik": issuer_cik,
                        "issuer_name": issuer_name,
                        "issuer_trading_symbol": issuer_symbol or None,
                        "buy_value_usd": _decimal_string(agg["buy_value"]),
                        "sell_value_usd": _decimal_string(agg["sell_value"]),
                        "buy_transaction_count": agg["buy_count"],
                        "sell_transaction_count": agg["sell_count"],
                        "other_transaction_count": agg["other_count"],
                        "owner_count": len(agg["owners"]),
                        "accession_numbers": sorted(agg["accessions"]),
                        "amendment_accession_numbers": sorted(
                            agg["amended_accessions"]
                        ),
                        "filing_dates": sorted(agg["filing_dates"]),
                        "source_time": transaction_date.isoformat(),
                        "source_time_kind": "transaction_date",
                        "acquired_at": acquired_at.isoformat(),
                    },
                }
            )
        aggregation_stats = {
            "transactions_parsed": len(transactions),
            "superseded_transactions": superseded_transactions,
            "records": len(records),
        }
        return records, aggregation_stats

    def health_check(self, config: dict) -> dict:
        started = time.monotonic()
        cfg = config["collectors"]["sec_form4"]
        issuers = cfg.get("issuers", [])
        if not issuers:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No SEC Form 4 issuers are configured",
                "latency_ms": 0,
            }
        try:
            cik = _normalize_cik(issuers[0].get("cik"))
            if not cik:
                raise ValueError("first issuer has no CIK")
            user_agent = str(cfg.get("user_agent") or SEC_USER_AGENT).strip()
            submissions_url = validate_configured_origin(
                cfg.get("url") or SEC_SUBMISSIONS_URL,
                cfg,
                label="SEC submissions url",
                canonical={SEC_SUBMISSIONS_URL},
            )
            response = make_request(
                "GET",
                submissions_url.format(cik=_pad_cik(cik)),
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                timeout=15,
            )
            return {
                "healthy": response.status_code == 200,
                "state": "success" if response.status_code == 200 else "failed",
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["sec_form4"]["schedule"]

    def get_target_table(self) -> str:
        return "positioning_reports"

    def get_conflict_columns(self) -> list[str]:
        return ["source", "market_id", "report_date", "category"]


class FinraShortVolumeCollector:
    """Daily short-sale volume aggregation from FINRA Reg SHO public files."""

    source_id = "finra_short_volume"

    FILE_PREFIX = "CNMSshvol"
    FILE_SUFFIX = ".txt"

    def __init__(self):
        self.last_result_metadata: dict = {}

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        cfg = config["collectors"]["finra_short_volume"]
        symbols = _configured_symbols(cfg.get("symbols", []))
        if not symbols:
            raise CollectorSetupRequired(
                "No FINRA short-volume symbols are configured",
                source_id=self.source_id,
            )
        base_url = validate_configured_origin(
            cfg.get("url") or FINRA_DAILY_BASE_URL,
            cfg,
            label="FINRA url",
            canonical={FINRA_DAILY_BASE_URL},
        )
        prefix = str(cfg.get("file_prefix") or self.FILE_PREFIX).strip()
        suffix = str(cfg.get("file_suffix") or self.FILE_SUFFIX).strip()
        lookback_days = _bounded_int(cfg.get("lookback_days"), 7, 1, 30)
        max_file_bytes = _bounded_int(
            cfg.get("max_file_bytes"), 20_000_000, 100_000, 200_000_000
        )
        interval = _bounded_float(
            cfg.get("request_interval_seconds"),
            DEFAULT_FINRA_REQUEST_INTERVAL_SECONDS,
            0.0,
            60.0,
        )
        acquired_at = datetime.now(UTC)

        configured_dates = _parse_configured_dates(cfg.get("dates"))
        if configured_dates:
            dates = sorted(configured_dates)
        else:
            today = datetime.now(UTC).date()
            dates = [today - timedelta(days=offset) for offset in range(lookback_days)]

        wanted = {upper: (official, assets) for official, upper, assets in symbols}

        errors = []
        skipped_dates = []
        fetched_dates = []
        malformed_rows = 0
        non_data_rows = 0
        per_symbol: dict[tuple[date, str], dict] = {}

        for trade_date in sorted(dates, reverse=True):
            file_name = f"{prefix}{trade_date.strftime('%Y%m%d')}{suffix}"
            url = f"{base_url.rstrip('/')}/{file_name}"
            try:
                _pace_requests(_FINRA_PACE_STATE, _FINRA_PACE_LOCK, interval)
                response = make_request("GET", url, correlation_id=correlation_id)
                if response.status_code == 404:
                    # Non-trading days and not-yet-published files carry no
                    # data; absence is a valid empty outcome, not a failure.
                    skipped_dates.append(trade_date.isoformat())
                    continue
                response.raise_for_status()
                if _size_exceeds(response, max_file_bytes):
                    raise ValueError("FINRA file exceeds configured size bound")
                parsed = self._parse_short_volume_file(
                    response.content, trade_date, wanted
                )
                fetched_dates.append(trade_date.isoformat())
                malformed_rows += parsed["malformed_rows"]
                non_data_rows += parsed["non_data_rows"]
                for symbol, agg in parsed["symbols"].items():
                    key = (trade_date, symbol)
                    bucket = per_symbol.setdefault(
                        key,
                        {
                            "short": Decimal("0"),
                            "exempt": Decimal("0"),
                            "total": Decimal("0"),
                            "markets": set(),
                            "row_count": 0,
                            "file_url": url,
                        },
                    )
                    bucket["short"] += agg["short"]
                    bucket["exempt"] += agg["exempt"]
                    bucket["total"] += agg["total"]
                    bucket["markets"].update(agg["markets"])
                    bucket["row_count"] += agg["row_count"]
            except Exception as exc:
                errors.append(
                    {
                        "date": trade_date.isoformat(),
                        "stage": "file",
                        "code": (
                            "invalid_source_data"
                            if isinstance(exc, ValueError)
                            else "request_failed"
                        ),
                        "exception_type": type(exc).__name__,
                        "error_class": (
                            "invalid_source_data"
                            if isinstance(exc, ValueError)
                            else "transient_source"
                        ),
                    }
                )
                logger.error(
                    "finra_short_volume_date_failed",
                    source_id=self.source_id,
                    trade_date=trade_date.isoformat(),
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )

        records = []
        for (trade_date, symbol), agg in per_symbol.items():
            short = agg["short"]
            total = agg["total"]
            records.append(
                {
                    "source": self.source_id,
                    "market_id": symbol,
                    "report_date": trade_date,
                    "category": "short_volume",
                    "long_positions": _round_int(total),
                    "short_positions": _round_int(short),
                    "net_position": _round_int(total - short),
                    "open_interest": None,
                    "net_pct_open_interest": None,
                    "acquired_at": acquired_at,
                    "metadata": {
                        "positioning_kind": POSITIONING_KIND_SHORT_VOLUME,
                        "assets": wanted[symbol.upper()][1],
                        "semantics": (
                            "FINRA Reg SHO daily short sale volume aggregated "
                            "by trade date from consolidated TRF/ADF-reported "
                            "NMS activity; daily short volume is a delayed "
                            "proxy for that day's short-selling flow and "
                            + NOT_SHORT_INTEREST_NOTE
                        ),
                        "short_volume_exact": _decimal_string(short),
                        "short_exempt_volume_exact": _decimal_string(agg["exempt"]),
                        "total_volume_exact": _decimal_string(total),
                        "columns_rounded_to_integer_shares": True,
                        "market_codes": sorted(agg["markets"]),
                        "row_count": agg["row_count"],
                        "file_url": agg["file_url"],
                        "source_time": trade_date.isoformat(),
                        "source_time_kind": "trade_date",
                        "delay_note": (
                            "FINRA publishes the daily file on the evening of "
                            "the trade date; daily short volume is a delayed "
                            "proxy of the day's short-selling flow; it is not "
                            "short interest, which is a separate bi-monthly report"
                        ),
                        "acquired_at": acquired_at.isoformat(),
                    },
                }
            )
        records.sort(key=lambda record: (record["report_date"], record["market_id"]))

        state = "partial" if errors else "success"
        if not records:
            raise CollectorNoData(
                "FINRA returned no short-volume observations for configured symbols",
                source_id=self.source_id,
                failed_dates=errors,
                skipped_dates=skipped_dates,
            )
        self.last_result_metadata = {
            "state": state,
            "source_id": self.source_id,
            "symbols_configured": len(symbols),
            "dates_requested": len(dates),
            "dates_fetched": fetched_dates,
            "dates_skipped": skipped_dates,
            "dates_failed": errors,
            "malformed_rows": malformed_rows,
            "non_data_rows": non_data_rows,
            "records": len(records),
            "acquired_at": acquired_at.isoformat(),
        }
        logger.info(
            "finra_short_volume_collection_completed",
            source_id=self.source_id,
            state=state,
            records=len(records),
            dates_fetched=len(fetched_dates),
            correlation_id=correlation_id,
        )
        return records

    def _parse_short_volume_file(
        self, content: bytes, trade_date: date, wanted: dict
    ) -> dict:
        """Parse one FINRA daily short-volume file.

        The pipe-delimited layout is header-driven (``Date|Symbol|ShortVolume|
        ShortExemptVolume|TotalVolume|Market`` in current files; older files
        omit ``ShortExemptVolume`` and future reorderings are tolerated by
        matching column names). Share counts are whole numbers in files before
        2026-02-23 and may carry up to six decimals afterwards, so volumes are
        parsed as ``Decimal`` and the exact values are preserved in metadata
        while the fixed numeric columns hold rounded integers. Trailer and
        other non-data rows (date field not ``YYYYMMDD``) are skipped
        explicitly, never parsed as data.
        """
        lines = content.decode("utf-8", errors="replace").splitlines()
        if not lines:
            raise ValueError("FINRA file is empty")
        header_fields = [field.strip().lower() for field in lines[0].split("|")]
        try:
            date_index = header_fields.index("date")
            symbol_index = header_fields.index("symbol")
            short_index = header_fields.index("shortvolume")
            total_index = header_fields.index("totalvolume")
            market_index = header_fields.index("market")
        except ValueError as exc:
            raise ValueError("FINRA file missing required header columns") from exc
        exempt_index = (
            header_fields.index("shortexemptvolume")
            if "shortexemptvolume" in header_fields
            else None
        )

        expected_day = trade_date.strftime("%Y%m%d")
        symbols: dict[str, dict] = {}
        malformed_rows = 0
        non_data_rows = 0
        for line in lines[1:]:
            fields = [field.strip() for field in line.split("|")]
            if len(fields) < 4:
                non_data_rows += 1
                continue
            row_date = fields[date_index]
            if not (len(row_date) == 8 and row_date.isdigit()):
                non_data_rows += 1
                continue
            if row_date != expected_day:
                malformed_rows += 1
                continue
            if len(fields) <= market_index:
                # Too few columns to carry the market field: malformed row.
                malformed_rows += 1
                continue
            symbol = fields[symbol_index]
            if not symbol or symbol.upper() not in wanted:
                continue
            try:
                short = Decimal(fields[short_index] or "0")
                total = Decimal(fields[total_index] or "0")
                exempt = (
                    Decimal(fields[exempt_index] or "0")
                    if exempt_index is not None
                    else Decimal("0")
                )
                if min(short, total, exempt) < 0 or short > total:
                    raise ValueError("inconsistent short volume")
            except (ValueError, TypeError, ArithmeticError):
                malformed_rows += 1
                continue
            markets = {
                code
                for code in (field.strip() for field in fields[market_index].split(","))
                if code
            }
            bucket = symbols.setdefault(
                symbol,
                {
                    "short": Decimal("0"),
                    "exempt": Decimal("0"),
                    "total": Decimal("0"),
                    "markets": set(),
                    "row_count": 0,
                },
            )
            bucket["short"] += short
            bucket["exempt"] += exempt
            bucket["total"] += total
            bucket["markets"].update(markets)
            bucket["row_count"] += 1
        return {
            "symbols": symbols,
            "malformed_rows": malformed_rows,
            "non_data_rows": non_data_rows,
        }

    def health_check(self, config: dict) -> dict:
        started = time.monotonic()
        cfg = config["collectors"]["finra_short_volume"]
        if not _configured_symbols(cfg.get("symbols", [])):
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No FINRA short-volume symbols are configured",
                "latency_ms": 0,
            }
        try:
            base_url = validate_configured_origin(
                cfg.get("url") or FINRA_DAILY_BASE_URL,
                cfg,
                label="FINRA url",
                canonical={FINRA_DAILY_BASE_URL},
            )
            prefix = str(cfg.get("file_prefix") or self.FILE_PREFIX).strip()
            suffix = str(cfg.get("file_suffix") or self.FILE_SUFFIX).strip()
            today = datetime.now(UTC).date()
            message = "no recent FINRA short-volume file found"
            for offset in (0, 1):
                file_name = (
                    f"{prefix}{(today - timedelta(days=offset)).strftime('%Y%m%d')}"
                    f"{suffix}"
                )
                response = make_request(
                    "GET",
                    f"{base_url.rstrip('/')}/{file_name}",
                    timeout=15,
                    max_retries=1,
                )
                if response.status_code == 200:
                    return {
                        "healthy": True,
                        "state": "success",
                        "message": f"HTTP 200 for {file_name}",
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    }
                message = f"HTTP {response.status_code} for {file_name}"
            return {
                "healthy": False,
                "state": "failed",
                "message": message,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["finra_short_volume"]["schedule"]

    def get_target_table(self) -> str:
        return "positioning_reports"

    def get_conflict_columns(self) -> list[str]:
        return ["source", "market_id", "report_date", "category"]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _local_children(element, name: str) -> list:
    if element is None:
        return []
    return [child for child in element if _local_name(child.tag) == name]


def _local_child(element, name: str):
    children = _local_children(element, name)
    return children[0] if children else None


def _child_text(element, name: str) -> str:
    child = _local_child(element, name)
    if child is None or child.text is None:
        return ""
    return child.text


def _nested_text(element, *names: str) -> str:
    current = element
    for name in names:
        if current is None:
            return ""
        current = _local_child(current, name)
    if current is None or current.text is None:
        return ""
    return current.text


def _parse_configured_dates(raw_dates) -> list[date]:
    """Optional explicit trade dates (ISO) overriding the lookback window."""
    parsed = []
    for value in raw_dates or []:
        try:
            parsed.append(date.fromisoformat(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return parsed
