"""Automated regulatory filing discovery and investment-report ingestion.

SEC filings are ingested from complete accession directories so exhibits are
available to analysis. Companies House statutory accounts are polled by
permanent company number and deduplicated by transaction ID.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import threading
import time
from datetime import date, timedelta
from pathlib import PurePath
from urllib.parse import urlsplit

from sqlalchemy import text

from db import get_session
from http_client import get_shared_client, make_request
from investment_service import (
    analyze_document,
    extract_document_text,
    store_document,
    store_document_url,
)
from investment_universe import top_us_uk_eu_companies
from logging_config import get_logger

logger = get_logger("investment.filings")

# SEC requires a descriptive User-Agent with contact email.
SEC_USER_AGENT = "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_DIRECTORY_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
SEC_FILING_FORMS = frozenset(
    {
        "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
        "10-Q", "10-Q/A", "6-K", "8-K", "8-K/A",
    }
)

COMPANIES_HOUSE_HISTORY_URL = (
    "https://api.company-information.service.gov.uk/company/{company_number}/filing-history"
)
COMPANIES_HOUSE_DOCUMENT_URL = (
    "https://document-api.company-information.service.gov.uk/document/{document_id}/content"
)

EDINET_LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
EDINET_CONTENT_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}/contents.zip"

OPENDART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
OPENDART_REPORT_URL = "https://opendart.fss.or.kr/dsab001/main.do?rcpNo={rcp_no}"

# SEC permits 10 requests/second. Keep a small margin below that ceiling.
REQUEST_DELAY_SECONDS = 0.12
# Companies House permits 600 API requests per five minutes (two per second).
COMPANIES_HOUSE_REQUEST_DELAY_SECONDS = 0.55
MAX_FILINGS_PER_COMPANY = 10
MAX_DIRECTORY_BYTES = 1_000_000_000
MAX_DIRECTORY_FILE_BYTES = 750_000_000
MAX_PARALLEL_SEC_FILE_BYTES = 25_000_000
MAX_COMPANIES_HOUSE_DOCUMENT_BYTES = 100_000_000
MAX_BUNDLE_TEXT_CHARS = 10_000_000
SEC_DIRECTORY_WORKERS = 4
COMPANY_WORKERS = 4
_COMPANIES_HOUSE_RATE_LOCK = threading.Lock()
_companies_house_next_request_at = 0.0
_SEC_RATE_LOCK = threading.Lock()
_sec_next_request_at = 0.0
_SEC_BUNDLE_SEMAPHORE = threading.Semaphore(1)


def _sleep_between_requests() -> None:
    """Serialize SEC request starts below the regulator's 10 req/s ceiling."""
    global _sec_next_request_at
    with _SEC_RATE_LOCK:
        now = time.monotonic()
        wait_seconds = _sec_next_request_at - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            now = time.monotonic()
        _sec_next_request_at = now + REQUEST_DELAY_SECONDS


def _sleep_for_companies_house() -> None:
    """Serialize Companies House starts below 600 requests per five minutes."""
    global _companies_house_next_request_at
    with _COMPANIES_HOUSE_RATE_LOCK:
        now = time.monotonic()
        wait_seconds = _companies_house_next_request_at - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            now = time.monotonic()
        _companies_house_next_request_at = (
            now + COMPANIES_HOUSE_REQUEST_DELAY_SECONDS
        )


def _clean(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


# ---------------------------------------------------------------------------
# SEC EDGAR (US)
# ---------------------------------------------------------------------------

def _sec_pad_cik(cik: str) -> str:
    """Pad CIK to 10 digits with leading zeros."""
    digits = re.sub(r"\D", "", cik)
    return digits.zfill(10)


def _sec_strip_cik(cik: str) -> str:
    """Remove leading zeros for archive URLs."""
    return str(int(re.sub(r"\D", "", cik)))


def _fetch_sec_submissions(cik: str, user_agent: str = SEC_USER_AGENT) -> dict | None:
    """Fetch SEC company submissions JSON."""
    padded = _sec_pad_cik(cik)
    url = SEC_SUBMISSIONS_URL.format(cik=padded)
    client = get_shared_client()
    try:
        response = client.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("sec_submissions_failed", cik=cik, error=str(exc))
        return None


def _sec_directory_url(cik: str, accession: str) -> str:
    return SEC_ARCHIVE_DIRECTORY_URL.format(
        cik=_sec_strip_cik(cik),
        accession=accession.replace("-", ""),
    )


def discover_sec_filings(
    company: dict,
    since: date | None = None,
    user_agent: str = SEC_USER_AGENT,
) -> list[dict]:
    """Return supported SEC filing accessions for a company."""
    cik = company.get("cik") or company.get("sec_cik") or ""
    if not cik:
        return []
    data = _fetch_sec_submissions(cik, user_agent)
    if not data:
        return []

    filings = data.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    accessions = filings.get("accessionNumber", [])
    dates = filings.get("filingDate", [])
    primary_docs = filings.get("primaryDocument", [])
    descriptions = filings.get("primaryDocDescription", [])

    results = []
    selected_document_types = set()
    cutoff = since.isoformat() if since else None
    for index, form in enumerate(forms):
        if form not in SEC_FILING_FORMS:
            continue
        filing_date = dates[index] if index < len(dates) else ""
        if cutoff and filing_date < cutoff:
            continue
        accession = accessions[index] if index < len(accessions) else ""
        if not accession:
            continue
        document_type = _sec_form_to_doc_type(form)
        if document_type in selected_document_types:
            continue
        selected_document_types.add(document_type)
        primary_doc = primary_docs[index] if index < len(primary_docs) else ""
        directory_url = _sec_directory_url(cik, accession)
        results.append({
            "source": "sec_edgar",
            "filing_id": accession,
            "company": company.get("company", ""),
            "symbol": company.get("symbol", ""),
            "region": company.get("region", "US"),
            "industry": company.get("industry", ""),
            "document_type": document_type,
            "report_date": filing_date or None,
            "source_url": directory_url,
            "directory_url": directory_url,
            "filename": f"{accession}.txt",
            "primary_document": primary_doc,
            "accession": accession,
            "form": form,
            "description": descriptions[index] if index < len(descriptions) else "",
        })
        if len(results) >= MAX_FILINGS_PER_COMPANY:
            break
    return results


def _sec_form_to_doc_type(form: str) -> str:
    base_form = form.removesuffix("/A")
    if base_form in {"10-K", "20-F", "40-F"}:
        return "annual_report"
    if base_form in {"10-Q"}:
        return "quarterly_report"
    if base_form in {"8-K", "6-K"}:
        return "earnings_release"
    return "regulatory_filing"


def _fetch_sec_directory_bundle(
    filing: dict,
    user_agent: str = SEC_USER_AGENT,
) -> tuple[bytes, str, str]:
    """Fetch every file in an SEC accession directory and build one text bundle."""
    directory_url = filing["directory_url"].rstrip("/") + "/"
    client = get_shared_client()
    _sleep_between_requests()
    index_response = make_request(
        "GET",
        directory_url + "index.json",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=30.0,
        client=client,
    )
    index_response.raise_for_status()
    items = index_response.json().get("directory", {}).get("item", [])
    if not isinstance(items, list) or not items:
        raise ValueError("SEC filing directory was empty")

    file_specs = []
    for item in items:
        raw_name = item.get("name") if isinstance(item, dict) else ""
        filename = PurePath(_clean(raw_name, 240)).name
        if not filename or filename != raw_name or filename in {".", "..", "index.json"}:
            continue
        declared_size = item.get("size")
        try:
            declared_size = int(declared_size)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > MAX_DIRECTORY_FILE_BYTES:
            raise ValueError(f"SEC filing file exceeds limit: {filename}")
        file_specs.append((filename, declared_size))

    if not file_specs:
        raise ValueError("SEC filing directory contained no files")
    declared_total = sum(size or 0 for _, size in file_specs)
    if declared_total > MAX_DIRECTORY_BYTES:
        raise ValueError("SEC filing directory exceeds 1 GB")

    def fetch_file(spec: tuple[str, int | None]) -> tuple[str, bytes, str]:
        filename, _declared_size = spec
        _sleep_between_requests()
        response = make_request(
            "GET",
            directory_url + filename,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            timeout=180.0,
            client=client,
        )
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_DIRECTORY_FILE_BYTES:
            raise ValueError(f"SEC filing file exceeds limit: {filename}")
        content_type = response.headers.get(
            "content-type",
            "application/octet-stream",
        )
        return filename, content, content_type

    total_bytes = 0
    manifest = []
    sections = []
    extracted_chars = 0
    def add_file(fetched: tuple[str, bytes, str]) -> None:
        nonlocal total_bytes, extracted_chars
        filename, content, content_type = fetched
        total_bytes += len(content)
        if total_bytes > MAX_DIRECTORY_BYTES:
            raise ValueError("SEC filing directory exceeds 1 GB")

        digest = hashlib.sha256(content).hexdigest()
        manifest.append({
            "filename": filename,
            "bytes": len(content),
            "content_type": content_type.split(";", 1)[0],
            "sha256": digest,
        })
        is_large_submission = (
            filename == filing.get("filename")
            and len(content) > MAX_PARALLEL_SEC_FILE_BYTES
        )
        if is_large_submission:
            extracted = ""
        else:
            try:
                extracted = extract_document_text(content, filename, content_type)
            except Exception as exc:
                extracted = ""
                logger.info(
                    "sec_directory_file_unextractable",
                    filing_id=filing["filing_id"],
                    filename=filename,
                    error=str(exc),
                )
        if extracted and extracted_chars < MAX_BUNDLE_TEXT_CHARS:
            remaining = MAX_BUNDLE_TEXT_CHARS - extracted_chars
            section = f"\n\n===== {filename} =====\n{extracted[:remaining]}"
            sections.append(section)
            extracted_chars += len(section)

    large_specs = [
        spec
        for spec in file_specs
        if spec[1] is None or spec[1] > MAX_PARALLEL_SEC_FILE_BYTES
    ]
    small_specs = [spec for spec in file_specs if spec not in large_specs]
    for spec in large_specs:
        add_file(fetch_file(spec))
    if small_specs:
        worker_count = min(SEC_DIRECTORY_WORKERS, len(small_specs))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for fetched in executor.map(fetch_file, small_specs):
                add_file(fetched)

    header = json.dumps(
        {
            "source": "sec_edgar",
            "accession": filing["filing_id"],
            "directory_url": directory_url,
            "files": manifest,
        },
        sort_keys=True,
    )
    bundle = (header + "".join(sections)).encode("utf-8")
    return bundle, filing["filename"], "text/plain"


# ---------------------------------------------------------------------------
# Companies House (UK)
# ---------------------------------------------------------------------------

def discover_companies_house_filings(
    company: dict,
    api_key: str,
    since: date | None = None,
) -> list[dict]:
    """Return statutory account filings for a Companies House company number."""
    company_number = _clean(company.get("company_number"), 20).upper()
    if not company_number or not api_key:
        return []
    url = COMPANIES_HOUSE_HISTORY_URL.format(company_number=company_number)
    client = get_shared_client()
    try:
        _sleep_for_companies_house()
        response = client.get(
            url,
            params={"category": "accounts", "items_per_page": 100},
            auth=(api_key, ""),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    except Exception as exc:
        logger.warning(
            "companies_house_history_failed",
            company_number=company_number,
            error=str(exc),
        )
        return []

    cutoff = since.isoformat() if since else None
    results = []
    selected_account_types = set()
    for item in items:
        filing_date = _clean(item.get("date"), 10)
        if cutoff and filing_date < cutoff:
            continue
        description = _clean(item.get("description"), 160) or "accounts"
        account_type = "interim" if "interim" in description else "annual"
        if account_type in selected_account_types:
            continue
        transaction_id = _clean(item.get("transaction_id"), 160)
        metadata_link = _clean(
            (item.get("links") or {}).get("document_metadata"),
            2048,
        )
        document_id = PurePath(urlsplit(metadata_link).path).name
        if (
            not transaction_id
            or not document_id
            or document_id in {".", ".."}
        ):
            continue
        selected_account_types.add(account_type)
        metadata_url = COMPANIES_HOUSE_DOCUMENT_URL.format(
            document_id=document_id
        ).removesuffix("/content")
        results.append({
            "source": "companies_house",
            "filing_id": transaction_id,
            "company_number": company_number,
            "company": company.get("company", ""),
            "symbol": company.get("symbol", ""),
            "region": "EU",
            "industry": company.get("industry", ""),
            "document_type": (
                "quarterly_report" if account_type == "interim" else "annual_report"
            ),
            "report_date": filing_date or None,
            "source_url": metadata_url,
            "document_metadata_url": metadata_url,
            "filename": f"{transaction_id}.pdf",
            "form": description,
        })
        if len(results) >= MAX_FILINGS_PER_COMPANY:
            break
    return results


def _fetch_companies_house_document(
    filing: dict,
    api_key: str,
) -> tuple[bytes, str, str]:
    metadata_url = filing["document_metadata_url"]
    document_id = PurePath(urlsplit(metadata_url).path).name
    if not document_id or document_id in {".", ".."}:
        raise ValueError("Companies House document metadata link was invalid")

    client = get_shared_client()
    _sleep_for_companies_house()
    metadata_response = client.get(
        metadata_url,
        auth=(api_key, ""),
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    metadata_response.raise_for_status()
    resources = metadata_response.json().get("resources", {})
    available_types = resources if isinstance(resources, dict) else {}
    preferred_types = (
        "application/xhtml+xml",
        "application/pdf",
        "text/csv",
        "application/zip",
    )
    content_type = next(
        (item for item in preferred_types if item in available_types),
        "",
    )
    if not content_type:
        raise ValueError("Companies House document had no supported content resource")
    declared_size = available_types.get(content_type, {}).get("content_length")
    try:
        declared_size = int(declared_size)
    except (TypeError, ValueError):
        declared_size = None
    if (
        declared_size is not None
        and declared_size > MAX_COMPANIES_HOUSE_DOCUMENT_BYTES
    ):
        raise ValueError("Companies House document exceeds 100 MB")

    url = COMPANIES_HOUSE_DOCUMENT_URL.format(document_id=document_id)
    _sleep_for_companies_house()
    response = client.get(
        url,
        auth=(api_key, ""),
        headers={"Accept": content_type},
        follow_redirects=True,
        timeout=120.0,
    )
    response.raise_for_status()
    content = response.content
    if len(content) > MAX_COMPANIES_HOUSE_DOCUMENT_BYTES:
        raise ValueError("Companies House document exceeds 100 MB")
    response_type = response.headers.get("content-type", content_type)
    suffix = {
        "application/xhtml+xml": ".html",
        "application/pdf": ".pdf",
        "text/csv": ".csv",
        "application/zip": ".zip",
    }[content_type]
    filename = str(PurePath(filing["filename"]).with_suffix(suffix))
    return content, filename, response_type


# ---------------------------------------------------------------------------
# Japan EDINET
# ---------------------------------------------------------------------------

def _fetch_edinet_documents(api_key: str, target_date: str) -> list[dict]:
    """Fetch EDINET document list for a given date."""
    client = get_shared_client()
    try:
        response = client.get(
            EDINET_LIST_URL,
            params={"date": target_date, "type": "2"},
            headers={"Accept": "application/json", "X-API-KEY": api_key},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])
    except Exception as exc:
        logger.warning("edinet_list_failed", date=target_date, error=str(exc))
        return []


def discover_edinet_filings(
    company: dict, api_key: str, since: date | None = None
) -> list[dict]:
    """Return filing metadata for a Japanese company from EDINET."""
    edinet_code = company.get("edinet_code") or company.get("edinet") or ""
    if not edinet_code or not api_key:
        return []

    # EDINET API returns documents per date; we scan the last N days.
    scan_days = 30
    start = since or (date.today() - timedelta(days=scan_days))
    results = []
    current = start
    today = date.today()
    while current <= today and len(results) < MAX_FILINGS_PER_COMPANY:
        docs = _fetch_edinet_documents(api_key, current.isoformat().replace("-", ""))
        for doc in docs:
            doc_code = doc.get("edinetCode", "")
            if doc_code != edinet_code:
                continue
            doc_type_raw = doc.get("docTypeCode", "")
            # 120 = annual securities report, 130 = quarterly, 140 = extraordinary
            if doc_type_raw not in ("120", "130", "140", "150"):
                continue
            doc_id = doc.get("docID", "")
            if not doc_id:
                continue
            results.append({
                "source": "edinet",
                "company": company.get("company", ""),
                "symbol": company.get("symbol", ""),
                "region": "ASIA",
                "industry": company.get("industry", ""),
                "document_type": "annual_report" if doc_type_raw == "120" else "quarterly_report",
                "report_date": doc.get("docDescription", "").split(" ")[-1] if doc.get("docDescription") else current.isoformat(),
                "source_url": EDINET_CONTENT_URL.format(doc_id=doc_id),
                "filename": f"{edinet_code}_{doc_id}.zip",
                "doc_id": doc_id,
                "doc_type_code": doc_type_raw,
            })
            if len(results) >= MAX_FILINGS_PER_COMPANY:
                break
        _sleep_between_requests()
        current += timedelta(days=1)
        # Don't scan more than 30 days in one pass
        if (current - start).days > scan_days:
            break
    return results


# ---------------------------------------------------------------------------
# Korea OpenDART
# ---------------------------------------------------------------------------

def _fetch_opendart_list(api_key: str, corp_code: str, bgn_de: str) -> list[dict]:
    """Fetch OpenDART filing list."""
    client = get_shared_client()
    try:
        response = client.get(
            OPENDART_LIST_URL,
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "page_count": "100",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "000":
            return []
        return data.get("list", [])
    except Exception as exc:
        logger.warning("opendart_list_failed", corp_code=corp_code, error=str(exc))
        return []


def discover_opendart_filings(
    company: dict, api_key: str, since: date | None = None
) -> list[dict]:
    """Return filing metadata for a Korean company from OpenDART."""
    corp_code = company.get("dart_code") or company.get("corp_code") or ""
    if not corp_code or not api_key:
        return []

    bgn_de = (since or (date.today() - timedelta(days=90))).strftime("%Y%m%d")
    filings = _fetch_opendart_list(api_key, corp_code, bgn_de)
    results = []
    for filing in filings:
        report_code = filing.get("report_code", "")
        # 11011 = annual, 11012 = semi-annual, 11013 = Q1, 11014 = Q3
        if report_code not in ("11011", "11012", "11013", "11014"):
            continue
        rcp_no = filing.get("rcept_no", "")
        if not rcp_no:
            continue
        doc_type = "annual_report" if report_code == "11011" else "quarterly_report"
        results.append({
            "source": "opendart",
            "company": company.get("company", ""),
            "symbol": company.get("symbol", ""),
            "region": "ASIA",
            "industry": company.get("industry", ""),
            "document_type": doc_type,
            "report_date": filing.get("rcept_dt", ""),
            "source_url": OPENDART_REPORT_URL.format(rcp_no=rcp_no),
            "filename": f"{corp_code}_{rcp_no}.html",
            "rcp_no": rcp_no,
            "report_code": report_code,
            "report_name": filing.get("report_nm", ""),
        })
        if len(results) >= MAX_FILINGS_PER_COMPANY:
            break
    return results


# ---------------------------------------------------------------------------
# Orchestration: discover + ingest + analyze
# ---------------------------------------------------------------------------

def _configured_companies(
    filings_config: dict,
    companies: list[dict] | None = None,
) -> list[dict]:
    if companies is not None:
        return [dict(company) for company in companies]
    if filings_config.get("universe") == "top_us_uk_eu_100":
        return top_us_uk_eu_companies()
    return [dict(company) for company in filings_config.get("companies", [])]


def _already_ingested(config: dict, filing: dict) -> bool:
    """Check a source filing ID first, retaining URL fallback for legacy rows."""
    source = _clean(filing.get("source"), 40).lower()
    filing_id = _clean(filing.get("filing_id"), 160)
    source_url = _clean(filing.get("source_url"), 2048)
    with get_session(config) as session:
        row = session.execute(
            text(
                "SELECT 1 FROM investment_documents "
                "WHERE (filing_source = :source AND filing_id = :filing_id) "
                "   OR (:source_url <> '' AND source_url = :source_url) "
                "LIMIT 1"
            ),
            {
                "source": source,
                "filing_id": filing_id,
                "source_url": source_url,
            },
        ).fetchone()
    return row is not None


def _ingest_filing(
    config: dict,
    filing: dict,
    auto_analyze: bool,
    *,
    sec_user_agent: str = SEC_USER_AGENT,
    companies_house_api_key: str = "",
) -> dict:
    """Fetch and store one filing using its source-native content API."""
    source_url = filing.get("source_url", "")
    source = filing.get("source", "")
    filing_id = filing.get("filing_id", "")
    if not source_url or not filing_id:
        return {"status": "skipped", "reason": "missing_filing_identity"}

    if _already_ingested(config, filing):
        return {"status": "skipped", "reason": "already_ingested"}

    metadata = {
        "company": filing.get("company", ""),
        "symbol": filing.get("symbol", ""),
        "region": filing.get("region", "US"),
        "industry": filing.get("industry", ""),
        "document_type": filing.get("document_type", "annual_report"),
        "report_date": filing.get("report_date"),
        "source_url": source_url,
        "filing_source": source,
        "filing_id": filing_id,
        "filename": filing.get("filename", "filing"),
    }
    try:
        if source == "sec_edgar":
            with _SEC_BUNDLE_SEMAPHORE:
                content, filename, mime_type = _fetch_sec_directory_bundle(
                    filing,
                    sec_user_agent,
                )
            metadata["filename"] = filename
            doc = store_document(config, metadata, content, mime_type)
        elif source == "companies_house":
            content, filename, mime_type = _fetch_companies_house_document(
                filing,
                companies_house_api_key,
            )
            metadata["filename"] = filename
            doc = store_document(
                config,
                metadata,
                content,
                mime_type,
                preserve_content=True,
                allow_unextractable=True,
            )
        else:
            doc = store_document_url(config, {**metadata, "url": source_url})

        document_id = doc.get("document_id")
        result = {
            "status": "ingested",
            "document_id": str(document_id),
            "filing_id": filing_id,
        }
        if auto_analyze and document_id:
            try:
                analysis = analyze_document(config, str(document_id))
                result["analysis_id"] = str(analysis.get("analysis_id", ""))
            except Exception as exc:
                result["analysis_error"] = str(exc)
                logger.warning(
                    "filing_auto_analysis_failed",
                    document_id=str(document_id),
                    error=str(exc),
                )
        return result
    except Exception as exc:
        logger.warning(
            "filing_ingest_failed",
            filing_source=source,
            filing_id=filing_id,
            source_url=source_url,
            error=str(exc),
        )
        return {"status": "failed", "filing_id": filing_id, "error": str(exc)}


def run_filing_collection(
    config: dict,
    *,
    correlation_id: str = "",
    auto_analyze: bool = False,
    companies: list[dict] | None = None,
) -> dict:
    """Discover and ingest new filings for the configured company universe."""
    filings_config = config.get("investment_filings", {})
    if not filings_config.get("enabled", False):
        return {"status": "disabled", "ingested": 0, "skipped": 0, "failed": 0}

    sec_user_agent = filings_config.get("sec_user_agent", SEC_USER_AGENT)
    companies_house_key = filings_config.get("companies_house_api_key", "")
    edinet_key = filings_config.get("edinet_api_key", "")
    opendart_key = filings_config.get("opendart_api_key", "")
    lookback_days = int(filings_config.get("lookback_days", 30))
    since = date.today() - timedelta(days=max(lookback_days, 0))

    target_companies = _configured_companies(filings_config, companies)
    if not target_companies:
        return {"status": "no_companies", "ingested": 0, "skipped": 0, "failed": 0}

    def collect_company(company: dict) -> dict:
        region = (company.get("region") or "US").upper()
        company_name = company.get("company", "unknown")
        discovered: list[dict] = []
        company_number = company.get("company_number")
        cik = company.get("cik") or company.get("sec_cik")

        # Query every regulator for which a permanent identifier exists.
        # Dual-listed UK issuers can therefore use SEC coverage when their
        # Companies House filing is unavailable or not machine-readable.
        if cik:
            _sleep_between_requests()
            discovered.extend(
                discover_sec_filings(company, since, sec_user_agent)
            )

        if company_number and companies_house_key:
            discovered.extend(
                discover_companies_house_filings(
                    company,
                    companies_house_key,
                    since,
                )
            )

        if company.get("edinet_code") or company.get("edinet"):
            if edinet_key:
                _sleep_between_requests()
                discovered.extend(discover_edinet_filings(company, edinet_key, since))

        if company.get("dart_code") or company.get("corp_code"):
            if opendart_key:
                _sleep_between_requests()
                discovered.extend(discover_opendart_filings(company, opendart_key, since))

        company_result = {
            "company": company_name,
            "symbol": company.get("symbol", ""),
            "region": region,
            "market": company.get("market", ""),
            "discovered": len(discovered),
            "ingested": 0,
            "skipped": 0,
            "failed": 0,
            "filings": [],
        }

        for filing in discovered:
            result = _ingest_filing(
                config,
                filing,
                auto_analyze,
                sec_user_agent=sec_user_agent,
                companies_house_api_key=companies_house_key,
            )
            company_result["filings"].append({
                "filing_id": filing.get("filing_id", ""),
                "source": filing.get("source", ""),
                "source_url": filing.get("source_url", ""),
                "form": filing.get("form", filing.get("report_name", "")),
                "result": result,
            })
            status = result.get("status", "failed")
            if status == "ingested":
                company_result["ingested"] += 1
            elif status == "skipped":
                company_result["skipped"] += 1
            else:
                company_result["failed"] += 1
        return company_result

    configured_workers = int(filings_config.get("company_workers", COMPANY_WORKERS))
    worker_count = min(max(configured_workers, 1), len(target_companies))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        per_company = list(executor.map(collect_company, target_companies))

    ingested = sum(company["ingested"] for company in per_company)
    skipped = sum(company["skipped"] for company in per_company)
    failed = sum(company["failed"] for company in per_company)

    summary = {
        "status": "completed",
        "correlation_id": correlation_id,
        "companies_scanned": len(target_companies),
        "total_discovered": sum(company["discovered"] for company in per_company),
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "per_company": per_company,
    }
    logger.info(
        "filing_collection_completed",
        correlation_id=correlation_id,
        ingested=ingested,
        skipped=skipped,
        failed=failed,
    )
    return summary


def get_filing_source_status(config: dict) -> dict:
    """Return status of filing sources for the dashboard."""
    filings_config = config.get("investment_filings", {})
    enabled = filings_config.get("enabled", False)
    companies = _configured_companies(filings_config)
    sources = []

    sec_companies = [
        company
        for company in companies
        if (company.get("cik") or company.get("sec_cik"))
        and (
            (company.get("region") or "US").upper() == "US"
            or not company.get("company_number")
        )
    ]
    sources.append({
        "id": "sec_edgar",
        "name": "SEC EDGAR (US and cross-listed)",
        "enabled": enabled and bool(sec_companies),
        "companies": len(sec_companies),
        "api_key_required": False,
        "note": "Complete accession directories, including exhibits.",
        "url": "https://www.sec.gov/edgar/search/",
    })

    companies_house_companies = [
        company for company in companies if company.get("company_number")
    ]
    companies_house_key = filings_config.get("companies_house_api_key", "")
    sources.append({
        "id": "companies_house",
        "name": "Companies House (UK)",
        "enabled": (
            enabled
            and bool(companies_house_companies)
            and bool(companies_house_key)
        ),
        "companies": len(companies_house_companies),
        "api_key_required": True,
        "api_key_configured": bool(companies_house_key),
        "note": "Statutory accounts by permanent company number.",
        "url": "https://find-and-update.company-information.service.gov.uk/",
    })

    eu_companies = [
        company for company in companies if company.get("market") == "EU"
    ]
    sources.append({
        "id": "eu_esef",
        "name": "EU ESEF / national OAMs",
        "enabled": False,
        "companies": len(eu_companies),
        "api_key_required": False,
        "note": (
            "EU issuers with SEC registrations are collected automatically; "
            "remaining ESEF reports are decentralized across national OAMs."
        ),
        "url": "https://www.esma.europa.eu/issuer-disclosure/officially-appointed-mechanisms",
    })

    edinet_companies = [
        company
        for company in companies
        if company.get("edinet_code") or company.get("edinet")
    ]
    edinet_key = filings_config.get("edinet_api_key", "")
    sources.append({
        "id": "edinet",
        "name": "EDINET (Japan)",
        "enabled": enabled and bool(edinet_companies) and bool(edinet_key),
        "companies": len(edinet_companies),
        "api_key_required": True,
        "api_key_configured": bool(edinet_key),
        "url": "https://disclosure2.edinet-fsa.go.jp/",
    })

    dart_companies = [
        company
        for company in companies
        if company.get("dart_code") or company.get("corp_code")
    ]
    opendart_key = filings_config.get("opendart_api_key", "")
    sources.append({
        "id": "opendart",
        "name": "OpenDART (Korea)",
        "enabled": enabled and bool(dart_companies) and bool(opendart_key),
        "companies": len(dart_companies),
        "api_key_required": True,
        "api_key_configured": bool(opendart_key),
        "url": "https://opendart.fss.or.kr/",
    })

    # Last run info
    with get_session(config) as session:
        row = session.execute(
            text(
                "SELECT accepted_at, completed_at, status, result_status, summary "
                "FROM cycle_runs "
                "WHERE run_kind = 'filings' "
                "AND requested_component = 'investment_filings' "
                "ORDER BY accepted_at DESC LIMIT 1"
            )
        ).fetchone()
    last_run = None
    if row:
        mapping = getattr(row, "_mapping", None) or {}
        run_summary = mapping.get("summary") or {}
        accepted_at = mapping.get("accepted_at")
        completed_at = mapping.get("completed_at")
        public_summary = {
            key: run_summary.get(key, 0)
            for key in (
                "companies_scanned",
                "total_discovered",
                "ingested",
                "skipped",
                "failed",
            )
        }
        last_run = {
            "accepted_at": str(accepted_at) if accepted_at else "",
            "completed_at": str(completed_at) if completed_at else "",
            "status": mapping.get("result_status") or mapping.get("status", ""),
            "summary": public_summary,
        }

    return {
        "enabled": enabled,
        "schedule": filings_config.get("schedule", ""),
        "companies_configured": len(companies),
        "sources": sources,
        "last_run": last_run,
    }
