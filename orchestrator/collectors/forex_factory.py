import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import text

from db import get_session
from http_client import make_request
from http_errors import safe_error_message
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.forex_factory")

CURRENCY_TO_COUNTRY = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
    "AUD": "AU",
    "CAD": "CA",
    "CHF": "CH",
    "NZD": "NZ",
    "CNY": "CN",
}

IMPACT_MAP = {
    "icon--ff-impact-red": "high",
    "icon--ff-impact-ora": "medium",
    "icon--ff-impact-yel": "low",
    "icon--ff-impact-gra": "holiday",
}

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "holiday": 0}

ET = ZoneInfo("America/New_York")
DEFAULT_RELEVANT_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CNY"}
EXCLUDED_EVENT_PATTERNS = (
    "bank holiday",
    "holiday",
)


class ForexFactoryCollector:
    source_id = "forex_factory"

    def __init__(self):
        self.last_result_metadata: dict = {}

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        ff_config = config.get("collectors", {}).get("forex_factory", {})
        source_url = validate_configured_origin(
            ff_config.get("source_url", "https://www.forexfactory.com/calendar"),
            ff_config,
            label="forex_factory source_url",
            canonical={
                "https://www.forexfactory.com/calendar",
                "https://nfs.faireconomy.media",
            },
        )
        export_base_url = validate_configured_origin(
            ff_config.get(
                "weekly_export_base_url", "https://nfs.faireconomy.media"
            ),
            ff_config,
            label="forex_factory weekly_export_base_url",
            canonical={
                "https://www.forexfactory.com/calendar",
                "https://nfs.faireconomy.media",
            },
        )
        user_agent = ff_config.get(
            "user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        min_impact = ff_config.get("min_impact", "medium")
        currencies = set(ff_config.get("currencies", DEFAULT_RELEVANT_CURRENCIES))
        target_week = self._determine_target_week(config)

        cache_entry = self._load_cached_payload(config, target_week, correlation_id)
        payload_source = "cache"
        fetched_at = None

        def _parse_and_check(p: list[dict], source: str) -> list[dict]:
            return self._parse_export_payload(
                payload=p,
                target_week=target_week,
                min_impact=min_impact,
                currencies=currencies,
                payload_source=source,
                correlation_id=correlation_id,
                fetched_at=fetched_at,
            )

        # A successfully fetched payload is immutable for its target week. This
        # deliberately freezes forecasts/revisions after the first weekly fetch.
        if cache_entry is not None:
            payload, fetched_at = self._cache_parts(cache_entry)
            records = _parse_and_check(payload, "cache")
            self._set_result_metadata(
                target_week, "cache", fetched_at, len(payload), len(records)
            )
            self._log_result(correlation_id)
            return records

        payload = None
        if cache_entry is None:
            try:
                payload = self._fetch_export_payload(
                    source_url=source_url,
                    export_base_url=export_base_url,
                    displayed_week=target_week["displayed_week"],
                    user_agent=user_agent,
                    correlation_id=correlation_id,
                )
                fetched_at = datetime.now(UTC)
                self._store_cached_payload(
                    config, target_week, payload, correlation_id, fetched_at=fetched_at
                )
                payload_source = "live"
            except Exception as exc:
                stale_entry = self._load_cached_payload(
                    config, target_week, correlation_id, allow_stale=True
                )
                if stale_entry is None:
                    logger.error(
                        "weekly_export_fetch_failed",
                        action="collect",
                        week=target_week["week_key"],
                        error=safe_error_message(exc, provider="forex_factory"),
                        correlation_id=correlation_id,
                    )
                    raise
                payload, fetched_at = self._cache_parts(stale_entry)
                logger.warning(
                    "weekly_export_fetch_failed_using_cache",
                    action="collect",
                    week=target_week["week_key"],
                    error=safe_error_message(exc, provider="forex_factory"),
                    correlation_id=correlation_id,
                )
                payload_source = "stale_cache"

        records = _parse_and_check(payload, payload_source)
        self._set_result_metadata(
            target_week, payload_source, fetched_at, len(payload), len(records)
        )
        self._log_result(correlation_id)
        return records

    @staticmethod
    def _cache_parts(entry) -> tuple[list[dict], datetime | None]:
        if isinstance(entry, list):
            return entry, None
        return entry["payload"], entry.get("fetched_at")

    def _set_result_metadata(
        self,
        target_week: dict,
        payload_source: str,
        fetched_at: datetime | None,
        payload_records: int,
        events_found: int,
    ) -> None:
        now = datetime.now(UTC)
        if fetched_at and fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        cache_age_hours = (
            max(0.0, (now - fetched_at).total_seconds() / 3600) if fetched_at else None
        )
        self.last_result_metadata = {
            "state": "degraded_cache" if payload_source == "stale_cache" else "success",
            "payload_source": payload_source,
            "target_week": target_week["week_key"],
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "cache_age_hours": round(cache_age_hours, 2)
            if cache_age_hours is not None
            else None,
            "payload_records": payload_records,
            "events_found": events_found,
        }

    def _log_result(self, correlation_id: str) -> None:
        logger.info(
            "weekly_export_parsed",
            action="collect",
            correlation_id=correlation_id,
            **self.last_result_metadata,
        )

    def _determine_target_week(self, config: dict, now: datetime | None = None) -> dict:
        tz_config = config.get("timezone", {}).get("primary", {})
        london = ZoneInfo(tz_config.get("name", "Europe/London"))
        now_london = now.astimezone(london) if now else datetime.now(london)

        displayed_week = "next" if now_london.weekday() >= 5 else "this"
        target_date = now_london.date()
        if displayed_week == "next":
            target_date = target_date + timedelta(days=7 - now_london.weekday())

        monday = target_date - timedelta(days=target_date.weekday())
        friday = monday + timedelta(days=4)
        period_start = datetime.combine(monday, dt_time.min, tzinfo=london)
        period_end = datetime.combine(friday, dt_time.max, tzinfo=london)
        iso_year, iso_week, _ = monday.isocalendar()

        return {
            "week_key": f"{iso_year}-W{iso_week:02d}",
            "cache_key": f"{self.source_id}:{iso_year}-W{iso_week:02d}",
            "displayed_week": displayed_week,
            "period_start": period_start,
            "period_end": period_end,
            "timezone": london.key,
        }

    def _fetch_export_payload(
        self,
        source_url: str,
        export_base_url: str,
        displayed_week: str,
        user_agent: str,
        correlation_id: str,
    ) -> list[dict]:
        export_url = self._find_weekly_export_url(
            source_url=source_url,
            export_base_url=export_base_url,
            displayed_week=displayed_week,
            user_agent=user_agent,
            correlation_id=correlation_id,
        )
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        }
        response = make_request(
            method="GET",
            url=export_url,
            headers=headers,
            timeout=30.0,
            max_retries=2,
            correlation_id=correlation_id,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("ForexFactory weekly JSON export was not a list")
        return payload

    def _find_weekly_export_url(
        self,
        source_url: str,
        export_base_url: str,
        displayed_week: str,
        user_agent: str,
        correlation_id: str,
    ) -> str:
        page_url = f"{source_url}?week={displayed_week}"
        logger.info(
            "scraping_weekly_export_page",
            action="find_weekly_export_url",
            page_url=page_url,
            correlation_id=correlation_id,
        )
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = make_request(
            method="GET",
            url=page_url,
            headers=headers,
            timeout=30.0,
            max_retries=2,
            correlation_id=correlation_id,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        all_links = soup.find_all("a", href=True)
        logger.info(
            "scraped_page_links",
            action="find_weekly_export_url",
            link_count=len(all_links),
            correlation_id=correlation_id,
        )

        for link in all_links:
            href = link["href"]
            href_lower = href.lower()
            text_value = link.get_text(" ", strip=True).lower()
            if ".json" in href_lower or (
                "json" in text_value and "ff_calendar" in href_lower
            ):
                return urljoin(page_url, href)

        suffix = "nextweek" if displayed_week == "next" else "thisweek"
        fallback_url = f"{export_base_url.rstrip('/')}/ff_calendar_{suffix}.json"
        logger.warning(
            "weekly_export_link_not_found_using_fallback",
            action="find_weekly_export_url",
            displayed_week=displayed_week,
            links_searched=len(all_links),
            fallback_url=fallback_url,
            correlation_id=correlation_id,
        )
        return fallback_url

    def _load_cached_payload(
        self,
        config: dict,
        target_week: dict,
        correlation_id: str,
        allow_stale: bool = False,
    ) -> list[dict] | None:
        coverage_clause = ""
        params = {
            "cache_key": target_week["cache_key"],
            "period_start": target_week["period_start"],
            "period_end": target_week["period_end"],
        }
        if not allow_stale:
            coverage_clause = (
                "AND period_start <= :period_start AND period_end >= :period_end"
            )

        sql = text(f"""
            SELECT raw_payload, fetched_at, period_start, period_end
            FROM source_payload_cache
            WHERE cache_key = :cache_key
            {coverage_clause}
            LIMIT 1
        """)
        try:
            with get_session(config) as session:
                row = session.execute(sql, params).fetchone()
            if row is None:
                return None
            mapped = dict(row._mapping)
            payload = mapped["raw_payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(payload, list):
                fetched_at = mapped.get("fetched_at")
                if isinstance(fetched_at, str):
                    fetched_at = datetime.fromisoformat(
                        fetched_at.replace("Z", "+00:00")
                    )
                logger.info(
                    "weekly_export_cache_hit",
                    action="load_cache",
                    week=target_week["week_key"],
                    fetched_at=fetched_at.isoformat() if fetched_at else None,
                    correlation_id=correlation_id,
                )
                return {
                    "payload": payload,
                    "fetched_at": fetched_at,
                    "period_start": mapped.get("period_start"),
                    "period_end": mapped.get("period_end"),
                }
        except Exception as exc:
            logger.warning(
                "weekly_export_cache_read_failed",
                action="load_cache",
                week=target_week["week_key"],
                error=safe_error_message(exc, provider="forex_factory"),
                correlation_id=correlation_id,
            )
        return None

    def _store_cached_payload(
        self,
        config: dict,
        target_week: dict,
        payload: list[dict],
        correlation_id: str,
        fetched_at: datetime | None = None,
    ) -> None:
        raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(raw_json.encode()).hexdigest()
        metadata = {
            "source_url_type": "weekly_export_json",
            "displayed_week": target_week["displayed_week"],
            "timezone": target_week["timezone"],
        }
        sql = text("""
            INSERT INTO source_payload_cache (
                cache_key, source, target_week, raw_payload, payload_hash,
                fetched_at, period_start, period_end, metadata
            )
            VALUES (
                :cache_key, :source, :target_week, CAST(:raw_payload AS JSONB),
                :payload_hash, :fetched_at, :period_start, :period_end,
                CAST(:metadata AS JSONB)
            )
            ON CONFLICT (cache_key) DO NOTHING
        """)
        params = {
            "cache_key": target_week["cache_key"],
            "source": self.source_id,
            "target_week": target_week["week_key"],
            "raw_payload": raw_json,
            "payload_hash": payload_hash,
            "fetched_at": fetched_at or datetime.now(UTC),
            "period_start": target_week["period_start"],
            "period_end": target_week["period_end"],
            "metadata": json.dumps(metadata, sort_keys=True),
        }
        try:
            with get_session(config) as session:
                session.execute(sql, params)
        except Exception as exc:
            logger.error(
                "weekly_export_cache_write_failed",
                action="store_cache",
                week=target_week["week_key"],
                error=safe_error_message(exc, provider="forex_factory"),
                correlation_id=correlation_id,
            )
            raise RuntimeError(
                f"Could not persist immutable Forex Factory cache for "
                f"{target_week['week_key']}"
            ) from exc

    def _parse_export_payload(
        self,
        payload: list[dict],
        target_week: dict,
        min_impact: str,
        currencies: set[str],
        payload_source: str,
        correlation_id: str,
        fetched_at: datetime | None = None,
    ) -> list[dict]:
        records = []
        skipped = 0
        period_start = target_week["period_start"]
        period_end = target_week["period_end"]

        for item in payload:
            try:
                event = self._parse_export_event(
                    item, target_week, payload_source, fetched_at=fetched_at
                )
                if event is None:
                    continue
                scheduled_london = event["scheduled_at"].astimezone(period_start.tzinfo)
                if scheduled_london < period_start or scheduled_london > period_end:
                    continue
                if not self._is_relevant_event(event, min_impact, currencies):
                    continue
                records.append(event)
            except Exception as exc:
                skipped += 1
                logger.warning(
                    "export_event_parse_skipped",
                    action="parse_export_payload",
                    error=safe_error_message(exc, provider="forex_factory"),
                    event=item,
                    correlation_id=correlation_id,
                )

        if skipped:
            logger.info(
                "export_events_skipped",
                action="parse_export_payload",
                skipped=skipped,
                correlation_id=correlation_id,
            )
        return records

    def _parse_export_event(
        self,
        item: dict,
        target_week: dict,
        payload_source: str,
        fetched_at: datetime | None = None,
    ) -> dict | None:
        title = str(item.get("title") or "").strip()
        currency = str(item.get("country") or "").strip().upper()
        impact = self._normalize_impact(item.get("impact"))
        date_value = item.get("date")
        if not title or not currency or not date_value:
            return None

        scheduled_at = datetime.fromisoformat(str(date_value)).astimezone(UTC)
        event = {
            "event_name": title,
            "country": CURRENCY_TO_COUNTRY.get(currency, currency),
            "scheduled_at": scheduled_at,
            "impact_level": impact,
            "consensus": self._clean_value(item.get("forecast")),
            "previous": self._clean_value(item.get("previous")),
            "actual": self._clean_value(item.get("actual")),
            "source": self.source_id,
            "acquired_at": fetched_at or datetime.now(UTC),
            "metadata": {
                "currency": currency,
                "target_week": target_week["week_key"],
                "payload_source": payload_source,
                "payload_fetched_at": fetched_at.isoformat() if fetched_at else None,
                "cache_age_hours": (
                    round(
                        max(
                            0.0,
                            (datetime.now(UTC) - fetched_at).total_seconds() / 3600,
                        ),
                        2,
                    )
                    if fetched_at
                    else None
                ),
                "source_date": date_value,
            },
        }
        event["event_id"] = self._make_event_id(title, scheduled_at)
        return event

    def _is_relevant_event(
        self, event: dict, min_impact: str, currencies: set[str]
    ) -> bool:
        metadata = event.get("metadata") or {}
        currency = metadata.get("currency")
        if currency not in currencies:
            return False
        impact = event.get("impact_level")
        if impact == "holiday":
            return False
        name = event.get("event_name", "").lower()
        if any(pattern in name for pattern in EXCLUDED_EVENT_PATTERNS):
            return False
        return IMPACT_RANK.get(impact, 0) >= IMPACT_RANK.get(min_impact, 2)

    @staticmethod
    def _normalize_impact(value: str | None) -> str:
        impact = str(value or "").strip().lower()
        if impact in {"high", "medium", "low", "holiday"}:
            return impact
        if impact in {"non-economic", "none", "gray", "grey"}:
            return "holiday"
        return impact or "low"

    @staticmethod
    def _clean_value(value) -> str | None:
        if value is None:
            return None
        text_value = str(value).strip()
        if text_value in ("", "—", "–", "N/A"):
            return None
        return text_value

    def _scrape_week(
        self,
        source_url: str,
        week: str,
        user_agent: str,
        min_impact: str,
        countries: list[str] | None,
        correlation_id: str,
    ) -> list[dict]:
        url = f"{source_url}?week={week}"
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Cookie": "fftimezoneoffset=-5; ffdstonaliases=US; ffveession=1",
        }

        response = make_request(
            method="GET",
            url=url,
            headers=headers,
            timeout=30.0,
            max_retries=2,
            correlation_id=correlation_id,
        )
        response.raise_for_status()

        html = response.text

        if "calendar__table" not in html and "calendar__row" not in html:
            logger.error(
                "page_structure_unexpected",
                action="scrape_week",
                week=week,
                html_snippet=html[:2000],
                correlation_id=correlation_id,
            )
            raise RuntimeError(
                f"ForexFactory page structure changed — no calendar table found for week={week}"
            )

        soup = BeautifulSoup(html, "lxml")
        return self._parse_calendar(
            soup=soup,
            min_impact=min_impact,
            countries=countries,
            correlation_id=correlation_id,
        )

    def _parse_calendar(
        self,
        soup: BeautifulSoup,
        min_impact: str,
        countries: list[str] | None,
        correlation_id: str,
    ) -> list[dict]:
        table = soup.find("table", class_="calendar__table")
        if not table:
            logger.error(
                "calendar_table_not_found",
                action="parse_calendar",
                correlation_id=correlation_id,
            )
            return []

        rows = table.find_all("tr", class_="calendar__row")
        if not rows:
            logger.warning(
                "no_calendar_rows",
                action="parse_calendar",
                correlation_id=correlation_id,
            )
            return []

        records: list[dict] = []
        current_date: datetime | None = None
        skipped = 0

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            event_cell = row.find("td", class_="calendar__event")
            if not event_cell:
                continue

            try:
                parsed = self._parse_row(row, current_date, correlation_id)
                if parsed is None:
                    continue

                current_date, event = parsed

                country = event["country"]
                impact = event["impact_level"]

                if countries and country not in countries:
                    continue

                if impact == "holiday":
                    continue

                if IMPACT_RANK.get(impact, 0) < IMPACT_RANK.get(min_impact, 2):
                    continue

                event_id = self._make_event_id(
                    event["event_name"], event["scheduled_at"]
                )
                event["event_id"] = event_id
                records.append(event)

            except Exception as exc:
                skipped += 1
                row_html = str(row)[:500]
                logger.warning(
                    "row_parse_skipped",
                    action="parse_calendar",
                    error=safe_error_message(exc, provider="forex_factory"),
                    row_html=row_html,
                    correlation_id=correlation_id,
                )

        if skipped > 0:
            logger.info(
                "rows_skipped",
                action="parse_calendar",
                skipped=skipped,
                total=len(rows),
                correlation_id=correlation_id,
            )

        return records

    def _parse_row(
        self, row, current_date: datetime | None, correlation_id: str
    ) -> tuple[datetime, dict] | None:
        date_cell = row.find("td", class_="calendar__date")
        if date_cell:
            date_text = date_cell.get_text(strip=True)
            parsed_date = self._parse_date_text(date_text)
            if parsed_date is not None:
                current_date = parsed_date

        if current_date is None:
            return None

        time_cell = row.find("td", class_="calendar__time")
        time_text = time_cell.get_text(strip=True) if time_cell else ""

        currency_cell = row.find("td", class_="calendar__currency")
        currency = currency_cell.get_text(strip=True) if currency_cell else ""

        impact_cell = row.find("td", class_="calendar__impact")
        impact_level = self._parse_impact(impact_cell)

        event_cell = row.find("td", class_="calendar__event")
        if not event_cell:
            return None
        event_name = event_cell.get_text(strip=True)
        if not event_name:
            return None

        actual_cell = row.find("td", class_="calendar__actual")
        forecast_cell = row.find("td", class_="calendar__forecast")
        previous_cell = row.find("td", class_="calendar__previous")

        actual = actual_cell.get_text(strip=True) if actual_cell else None
        forecast = forecast_cell.get_text(strip=True) if forecast_cell else None
        previous = previous_cell.get_text(strip=True) if previous_cell else None

        if actual in ("", "—", "–", "N/A"):
            actual = None
        if forecast in ("", "—", "–", "N/A"):
            forecast = None
        if previous in ("", "—", "–", "N/A"):
            previous = None

        metadata: dict = {}
        scheduled_at = self._parse_scheduled_at(current_date, time_text, metadata)

        if time_text.lower() in ("all day", ""):
            metadata["time_resolution"] = (
                "all_day" if time_text.lower() == "all day" else "tentative"
            )

        country = CURRENCY_TO_COUNTRY.get(currency.strip(), currency.strip())

        return current_date, {
            "event_name": event_name,
            "country": country,
            "scheduled_at": scheduled_at,
            "impact_level": impact_level,
            "consensus": forecast,
            "previous": previous,
            "actual": actual,
            "source": "forexfactory",
            "metadata": metadata if metadata else None,
        }

    def _parse_date_text(self, date_text: str) -> datetime | None:
        date_text = date_text.strip()
        if not date_text:
            return None

        date_text = re.sub(r"\s+", " ", date_text)

        year = datetime.now(ET).year

        cleaned = re.sub(
            r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*",
            r"\2 ",
            date_text,
        )
        if cleaned != date_text:
            date_text = cleaned
        else:
            date_text = re.sub(
                r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*",
                "",
                date_text,
            )

        for fmt in [
            "%b %d",
            "%A %b %d",
            "%a %b %d",
        ]:
            try:
                parsed = datetime.strptime(date_text, fmt)
                parsed = parsed.replace(year=year, tzinfo=ET)
                now_et = datetime.now(ET)
                if parsed > now_et + timedelta(days=60):
                    parsed = parsed.replace(year=year - 1)
                elif parsed < now_et - timedelta(days=300):
                    parsed = parsed.replace(year=year + 1)
                return parsed
            except ValueError:
                continue

        logger.warning("date_parse_failed", action="parse_date", date_text=date_text)
        return None

    def _parse_scheduled_at(
        self, base_date: datetime, time_text: str, metadata: dict
    ) -> datetime:
        time_text = time_text.strip()

        if not time_text or time_text.lower() == "tentative":
            metadata["tentative"] = True if time_text else True
            utc_dt = base_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(UTC)
            return utc_dt

        if time_text.lower() == "all day":
            utc_dt = base_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(UTC)
            return utc_dt

        time_text = time_text.replace(" ", "")
        for fmt in ["%I:%M%p", "%I%p", "%I:%M"]:
            try:
                parsed_time = datetime.strptime(time_text, fmt)
                scheduled = base_date.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute,
                    second=0,
                    microsecond=0,
                )
                return scheduled.astimezone(UTC)
            except ValueError:
                continue

        logger.warning(
            "time_parse_failed",
            action="parse_scheduled_at",
            time_text=time_text,
        )
        metadata["time_resolution"] = "tentative"
        return base_date.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            UTC
        )

    def _parse_impact(self, impact_cell) -> str:
        if impact_cell is None:
            return "low"

        classes = impact_cell.get("class", [])
        if isinstance(classes, str):
            classes = [classes]

        icon = impact_cell.find("span", class_=re.compile(r"icon--ff-impact-"))
        if icon:
            for cls in icon.get("class", []):
                if cls in IMPACT_MAP:
                    return IMPACT_MAP[cls]

        for cls in classes:
            for icon_cls, level in IMPACT_MAP.items():
                if icon_cls in cls:
                    return level

        return "low"

    @staticmethod
    def _make_event_id(event_name: str, scheduled_at: datetime) -> str:
        if isinstance(scheduled_at, datetime):
            scheduled_iso = scheduled_at.isoformat()
        else:
            scheduled_iso = str(scheduled_at)
        raw = f"{event_name}_{scheduled_iso}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def health_check(self, config: dict) -> dict:
        ff_config = config.get("collectors", {}).get("forex_factory", {})
        source_url = validate_configured_origin(
            ff_config.get("source_url", "https://www.forexfactory.com/calendar"),
            ff_config,
            label="forex_factory source_url",
            canonical={"https://www.forexfactory.com/calendar"},
        )
        user_agent = ff_config.get(
            "user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        start_ms = time.monotonic() * 1000
        target_week = self._determine_target_week(config)
        cache_entry = self._load_cached_payload(config, target_week, "health-check")
        if cache_entry is not None:
            payload, fetched_at = self._cache_parts(cache_entry)
            self._set_result_metadata(
                target_week, "cache", fetched_at, len(payload), events_found=0
            )
            return {
                "healthy": True,
                "state": "success",
                "message": "Current-week immutable cache is available",
                "latency_ms": int(time.monotonic() * 1000 - start_ms),
                **self.last_result_metadata,
            }

        try:
            headers = {
                "User-Agent": user_agent,
                "Cookie": "fftimezoneoffset=-5; ffdstonaliases=US; ffveession=1",
            }
            response = make_request(
                method="GET",
                url=source_url,
                headers=headers,
                timeout=10.0,
                max_retries=1,
                correlation_id="health-check",
            )
            latency_ms = int(time.monotonic() * 1000 - start_ms)

            if response.status_code == 200:
                return {
                    "healthy": True,
                    "state": "live_required",
                    "message": "ForexFactory reachable; current week requires first live fetch",
                    "latency_ms": latency_ms,
                    "payload_source": "live",
                    "target_week": target_week["week_key"],
                }
            else:
                return {
                    "healthy": False,
                    "state": "failed",
                    "message": f"ForexFactory returned status {response.status_code}",
                    "latency_ms": latency_ms,
                }
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            return {
                "healthy": False,
                "state": "failed",
                "message": f"ForexFactory unreachable: {safe_error_message(exc, provider='forex_factory')}",
                "latency_ms": latency_ms,
            }

    def get_schedule(self, config: dict) -> str:
        return (
            config.get("collectors", {})
            .get("forex_factory", {})
            .get("schedule", "0 20 * * 0")
        )

    def get_target_table(self) -> str:
        return "econ_events"

    def get_conflict_columns(self) -> list[str]:
        return ["event_id"]
