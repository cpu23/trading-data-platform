"""Deterministic, stdlib-only venue calendars for market-event windows.

Reaction windows must resolve baseline (pre-event), target (post-event), and
session-close timestamps on the venue's own trading calendar: 24x5 sessions
for FX/metals, exchange sessions for indices, observed holidays, early closes,
and DST transitions handled through ``zoneinfo``.

Everything here is pure Python (no new dependencies) and deterministic:
holidays are derived from fixed rules (Easter via the Anonymous Gregorian
algorithm, observed-weekday shifts, US/UK/DE conventions), so the same inputs
always produce the same session boundaries.

The ``exchange_calendars`` package was evaluated and rejected as incompatible
with this project's dependency set: it requires pandas/numpy, which are not
dependencies of the orchestrator (shared calendar logic here stays stdlib-only
like the shared outbound-security module). Instead, reliability comes from
explicit encoding: recurring holidays plus exceptional one-off closures
(September 11 2001, presidential National Days of Mourning, Hurricane Sandy,
LSE royal/state mourning days) are encoded through ``CLOSURE_AUDIT_END_YEAR``
(2026). Dates at or before that boundary are fail-closed — known closures are
never treated as sessions; beyond it only recurring rules apply (future
one-off closures are unknowable) and that limitation is exposed in provenance
metadata. Years outside 1990-2101 fail fast with ValueError. Config
``holidays``/``early_closes`` extend the built-in rules per deployment, and
per-instrument policy (venue/exchange_calendar/timezone/session bounds/price
timeframe/target_selection_policy) is resolved as a typed
``InstrumentSessionPolicy`` from ``reaction_windows.calendars.instruments``.

Direction semantics are explicit: ``backward`` searches pre-event ("baseline"),
``forward`` searches post-event ("target"), and ``session_close_after``
resolves the next session close ("session_close").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Calculation version for venue session rules. Bump when built-in rules change
# so persisted calendar_version provenance can distinguish rule sets.
CALENDAR_VERSION = "1"

FX_VENUE = "fx_24x5"
NYSE_VENUE = "nyse"
LSE_VENUE = "lse"
XETRA_VENUE = "xetra"
DEFAULT_VENUE = FX_VENUE

# Direction constants used by reaction-window provenance.
DIRECTION_BASELINE = "baseline"  # pre-event, backward search
DIRECTION_TARGET = "target"  # post-event, forward search
DIRECTION_SESSION_CLOSE = "session_close"  # next session close, forward

_MAX_DAYS = 370  # bound calendar walks; exceeds any weekend/holiday gap


class CalendarBoundError(ValueError):
    """Raised when a calendar walk exhausts its bound without finding a
    session: fail-closed rather than fabricating a timestamp that would
    violate the venue calendar (a target must never be persisted on a day the
    calendar cannot resolve)."""


@dataclass(frozen=True)
class SessionRule:
    """One venue's regular session and closed-day rules.

    ``weekdays`` uses Python convention: 0 = Monday .. 6 = Sunday.
    ``early_closes`` maps a specific date to its early close time (venue tz).
    ``weekly`` models a continuous multi-day session week (e.g. FX Sunday
    17:00 NY through Friday 17:00 NY): the FIRST weekday opens at
    ``open_time``, the LAST weekday closes at ``close_time``, intermediate
    weekdays are full 24-hour sessions, and the open/close times are resolved
    in the venue timezone (so DST shifts the UTC boundaries).
    """

    name: str
    timezone: str
    open_time: time
    close_time: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = MappingProxyType({})
    weekly: bool = False


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (deterministic, no external data)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * offset) // 451
    month = (h + offset - 7 * m + 114) // 31
    day = ((h + offset - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_weekday(holiday: date) -> date:
    """US observed rule: Saturday -> Friday, Sunday -> Monday."""
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _us_equity_holidays(years: range) -> frozenset[date]:
    """NYSE observed holidays for the given years (including year-boundary
    substitutions that land inside the range).

    Era rules: MLK Day is observed only since 1998 (the NYSE traded on MLK
    Day before then), and Juneteenth only since 2022 (NYSE rule filing
    SR-NYSE-2021-56; the NYSE traded on 2021-06-18).
    """
    holidays: set[date] = set()
    for year in years:
        easter = _easter_sunday(year)
        holidays.add(_observed_weekday(date(year, 1, 1)))  # New Year's
        if year >= 1998:
            holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK, 3rd Monday
        holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington, 3rd Monday
        holidays.add(easter - timedelta(days=2))  # Good Friday
        holidays.add(_last_weekday(year, 5, 0))  # Memorial, last Monday
        if year >= 2022:
            holidays.add(_observed_weekday(date(year, 6, 19)))  # Juneteenth
        holidays.add(_observed_weekday(date(year, 7, 4)))  # Independence
        holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor, first Monday
        holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving, 4th Thursday
        holidays.add(_observed_weekday(date(year, 12, 25)))  # Christmas
    return frozenset(holidays)


def _us_equity_early_closes(years: range) -> dict[date, time]:
    """Reliable NYSE early closes (13:00 ET).

    - Day after Thanksgiving: standing rule, all supported years.
    - Pre-Independence: July 3 early close whenever July 3 is an open session
      (July 4 on a weekday). When July 4 falls on Saturday, July 3 is the
      observed full holiday and gets no early close (July 2 stays a regular
      session).
    - Christmas Eve: standing rule through the audit boundary
      (CLOSURE_AUDIT_END_YEAR); later years are NOT claimed (fail-closed).
    """
    closes: dict[date, time] = {}
    for year in years:
        thanksgiving = _nth_weekday(year, 11, 3, 4)
        closes[thanksgiving + timedelta(days=1)] = time(13, 0)
        jul3 = date(year, 7, 3)
        if jul3.weekday() < 5 and _observed_weekday(date(year, 7, 4)) != jul3:
            closes[jul3] = time(13, 0)
        if year <= CLOSURE_AUDIT_END_YEAR:
            eve = date(year, 12, 24)
            if eve.weekday() < 5:
                closes[eve] = time(13, 0)
    return closes


def _lse_may_day(year: int) -> date:
    """LSE early-May bank holiday (first Monday), with anniversary moves."""
    if year == 1995:
        return date(1995, 5, 8)  # VE Day 50th
    if year == 2002:
        return date(2002, 6, 3)  # Golden Jubilee
    if year == 2012:
        return date(2012, 6, 4)  # Diamond Jubilee
    if year == 2020:
        return date(2020, 5, 8)  # VE Day 75
    if year == 2022:
        return date(2022, 6, 2)  # Platinum Jubilee
    return _nth_weekday(year, 5, 0, 1)


def _lse_spring_bank(year: int) -> date:
    """LSE spring bank holiday (last Monday), with jubilee moves."""
    if year == 2002:
        return date(2002, 6, 4)  # Golden Jubilee
    if year == 2012:
        return date(2012, 6, 5)  # Diamond Jubilee
    if year == 2022:
        return date(2022, 6, 3)  # Platinum Jubilee
    return _last_weekday(year, 5, 0)


def _uk_equity_early_closes(years: range) -> dict[date, time]:
    """LSE half-days (12:30 London): Christmas Eve and New Year's Eve."""
    closes: dict[date, time] = {}
    for year in years:
        for month, day in ((12, 24), (12, 31)):
            half_day = date(year, month, day)
            if half_day.weekday() < 5:
                closes[half_day] = time(12, 30)
    return closes


def _uk_equity_holidays(years: range) -> frozenset[date]:
    """LSE holidays: fixed + Easter-based + first/last-Monday rules."""
    holidays: set[date] = set()
    for year in years:
        easter = _easter_sunday(year)
        new_year = date(year, 1, 1)
        if new_year.weekday() == 5:
            new_year += timedelta(days=2)  # substitute Monday
        elif new_year.weekday() == 6:
            new_year += timedelta(days=1)
        holidays.add(new_year)
        holidays.add(easter - timedelta(days=2))  # Good Friday
        holidays.add(easter + timedelta(days=1))  # Easter Monday
        holidays.add(_lse_may_day(year))  # Early May (anniversary moves)
        holidays.add(_lse_spring_bank(year))  # Spring (jubilee moves)
        holidays.add(_last_weekday(year, 8, 0))  # Summer
        christmas = date(year, 12, 25)
        boxing = date(year, 12, 26)
        if christmas.weekday() == 5:
            holidays.add(christmas + timedelta(days=2))  # substitute Monday
        elif christmas.weekday() == 6:
            holidays.add(christmas + timedelta(days=2))  # substitute Tuesday
        else:
            holidays.add(christmas)
        if boxing.weekday() in (5, 6):
            holidays.add(boxing + timedelta(days=2))  # substitute Monday
        else:
            holidays.add(boxing)
    return frozenset(holidays)


def _de_equity_holidays(years: range) -> frozenset[date]:
    """XETRA non-trading days (no weekend substitution).

    XETRA trades on Ascension, Whit Monday, and German Unity (German public
    holidays where the exchange stays open per the Deutsche Boerse trading
    calendar); actual closures are New Year, Good Friday/Easter Monday,
    Labour Day, and Dec 24-26/31.
    """
    holidays: set[date] = set()
    for year in years:
        easter = _easter_sunday(year)
        holidays.add(date(year, 1, 1))
        holidays.add(easter - timedelta(days=2))  # Good Friday
        holidays.add(easter + timedelta(days=1))  # Easter Monday
        holidays.add(date(year, 5, 1))  # Labour Day
        holidays.add(date(year, 12, 24))  # Christmas Eve (XETRA closed)
        holidays.add(date(year, 12, 25))  # Christmas
        holidays.add(date(year, 12, 26))  # Boxing Day
        holidays.add(date(year, 12, 31))  # New Year's Eve (XETRA closed)
    return frozenset(holidays)


# Precompute holiday sets once for a deterministic supported range. Years
# outside this range fail fast (see _bounds_on_date) instead of silently
# treating holidays as sessions. The built-in rule sets are closed: one-off
# exchange closures are never folded in; config ``holidays``/``early_closes``
# extend the built-in rules per deployment.
MIN_SUPPORTED_YEAR = 1990
MAX_SUPPORTED_YEAR = 2101
# Last year for which exceptional (one-off) closures are encoded below. Dates
# at or before this boundary are fail-closed: known closures are never treated
# as sessions. Beyond it, only recurring rules apply (future one-off closures
# are unknowable by definition) and that limitation is exposed in provenance.
CLOSURE_AUDIT_END_YEAR = 2026
_HOLIDAY_YEARS = range(MIN_SUPPORTED_YEAR, MAX_SUPPORTED_YEAR + 1)
_US_HOLIDAYS = _us_equity_holidays(_HOLIDAY_YEARS)
_US_EARLY_CLOSES = _us_equity_early_closes(_HOLIDAY_YEARS)
_UK_HOLIDAYS = _uk_equity_holidays(_HOLIDAY_YEARS)
_UK_EARLY_CLOSES = _uk_equity_early_closes(_HOLIDAY_YEARS)
_DE_HOLIDAYS = _de_equity_holidays(_HOLIDAY_YEARS)

# Exceptional full-day NYSE closures not covered by recurring rules (1990-2026).
_NYSE_ONE_OFF_CLOSURES = frozenset(
    {
        # Nixon funeral.
        date(1994, 4, 27),
        # September 11 attacks (reopened Monday 2001-09-17).
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        # President Reagan National Day of Mourning.
        date(2004, 6, 11),
        # President Ford National Day of Mourning.
        date(2007, 1, 2),
        # Hurricane Sandy (reopened 2012-10-31).
        date(2012, 10, 29),
        date(2012, 10, 30),
        # President George H. W. Bush National Day of Mourning.
        date(2018, 12, 5),
        # President Carter National Day of Mourning.
        date(2025, 1, 9),
    }
)
# Exceptional full-day LSE closures not covered by recurring rules.
_LSE_ONE_OFF_CLOSURES = frozenset(
    {
        # Royal wedding.
        date(2011, 4, 29),
        # Queen Elizabeth II funeral.
        date(2022, 9, 19),
        # Coronation bank holiday.
        date(2023, 5, 8),
    }
)
# XETRA has no encoded one-off closures within the supported range.
_XETRA_ONE_OFF_CLOSURES: frozenset[date] = frozenset()

_INDEX_VENUES = {
    "SP500": NYSE_VENUE,
    "US500": NYSE_VENUE,
    "GER40": XETRA_VENUE,
    "DE40": XETRA_VENUE,
    "UK100": LSE_VENUE,
    "FTSE100": LSE_VENUE,
}


def default_venue_for(symbol: str | None) -> str:
    """Map a symbol to its default venue (explicit indices, else 24x5 FX)."""
    if not isinstance(symbol, str):
        return DEFAULT_VENUE
    return _INDEX_VENUES.get(symbol, DEFAULT_VENUE)


def _parse_time(value: Any, default: time) -> time:
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            pass
    return default


def _parse_weekdays(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    """Parse ISO weekdays (1=Monday .. 7=Sunday) into Python weekday() values
    (0=Monday .. 6=Sunday). Invalid or out-of-range input falls back to the
    venue's default weekday set. The order is preserved so a weekly session
    rule can express its open day first and close day last."""
    if isinstance(value, (list, tuple)):
        days: list[int] = []
        for item in value:
            try:
                day = int(item)
            except (TypeError, ValueError, OverflowError):
                return default
            if day not in (1, 2, 3, 4, 5, 6, 7):  # ISO: 1=Monday .. 7=Sunday
                return default
            days.append(day - 1)  # convert ISO -> Python weekday() (0..6)
        if days:
            return tuple(days)
    return default


def _parse_holidays(value: Any) -> frozenset[date]:
    if not isinstance(value, (list, tuple)):
        return frozenset()
    result: set[date] = set()
    for item in value:
        if isinstance(item, date) and not isinstance(item, datetime):
            result.add(item)
        elif isinstance(item, str):
            try:
                result.add(date.fromisoformat(item))
            except ValueError:
                continue
    return frozenset(result)


def _parse_early_closes(value: Any) -> dict[date, time]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[date, time] = {}
    for key, item in value.items():
        if isinstance(key, date) and not isinstance(key, datetime):
            day = key
        elif isinstance(key, str):
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
        else:
            continue
        parsed = _parse_time(item, time(13, 0))
        result[day] = parsed
    return result


def builtin_rule(venue: str) -> SessionRule:
    """Return the built-in session rule for a venue name.

    The 24x5 FX venue closes at its own session close (built-in default
    21:00 UTC), exactly like every other venue's ``close_time``; there is no
    separate top-level session_close knob. Unknown venue names raise
    (fail-closed) instead of silently defaulting to the FX rule.
    """
    if venue == NYSE_VENUE:
        return SessionRule(
            name=NYSE_VENUE,
            timezone="America/New_York",
            open_time=time(9, 30),
            close_time=time(16, 0),
            holidays=_US_HOLIDAYS | _NYSE_ONE_OFF_CLOSURES,
            early_closes=MappingProxyType(dict(_US_EARLY_CLOSES)),
        )
    if venue == LSE_VENUE:
        return SessionRule(
            name=LSE_VENUE,
            timezone="Europe/London",
            open_time=time(8, 0),
            close_time=time(16, 30),
            holidays=_UK_HOLIDAYS | _LSE_ONE_OFF_CLOSURES,
            early_closes=MappingProxyType(dict(_UK_EARLY_CLOSES)),
        )
    if venue == XETRA_VENUE:
        return SessionRule(
            name=XETRA_VENUE,
            timezone="Europe/Berlin",
            open_time=time(9, 0),
            close_time=time(17, 30),
            holidays=_DE_HOLIDAYS | _XETRA_ONE_OFF_CLOSURES,
        )
    if venue == FX_VENUE:
        # Documented FX market week: opens Sunday 17:00 America/New_York,
        # runs continuously (no daily maintenance modeled), and closes Friday
        # 17:00 America/New_York. DST shifts the UTC boundaries via the
        # America/New_York zone (22:00 UTC in summer, 21:00 UTC in winter).
        return SessionRule(
            name=FX_VENUE,
            timezone="America/New_York",
            open_time=time(17, 0),
            close_time=time(17, 0),
            weekdays=(6, 0, 1, 2, 3, 4),  # Sunday..Friday (Saturday closed)
            weekly=True,
        )
    raise ValueError(
        f"unknown venue: {venue!r} (not a built-in venue; define it under "
        "calendars.venues or use a built-in name)"
    )


BUILTIN_VENUES = frozenset({FX_VENUE, NYSE_VENUE, LSE_VENUE, XETRA_VENUE})


def build_rule(venue: str, spec: Mapping[str, Any] | None) -> SessionRule:
    """Build a session rule from optional config overrides over built-ins.

    ``venue`` must be a built-in name or a ``calendars.venues`` key; anything
    else raises (fail-closed, so a typo never silently gets FX sessions). A
    custom venue key keeps its own identity in ``rule.name`` so provenance
    reflects the configured rule, not the FX fallback base.
    """
    if venue in BUILTIN_VENUES:
        base = builtin_rule(venue)
    elif isinstance(spec, Mapping):
        # Custom venue: defaults mirror VenueCalendarConfig (timezone required
        # by the typed model; open 08:00 / close 17:00 / Mon-Fri otherwise).
        base = SessionRule(
            name=venue,
            timezone="UTC",
            open_time=time(8, 0),
            close_time=time(17, 0),
            weekdays=(0, 1, 2, 3, 4),
        )
    else:
        raise ValueError(
            f"unknown venue: {venue!r} (not a built-in venue and not defined "
            "under calendars.venues)"
        )
    if not isinstance(spec, Mapping):
        return base
    return SessionRule(
        name=venue,
        timezone=_text(spec.get("timezone"), base.timezone),
        open_time=_parse_time(spec.get("open_time"), base.open_time),
        close_time=_parse_time(spec.get("close_time"), base.close_time),
        weekdays=_parse_weekdays(spec.get("weekdays"), base.weekdays),
        holidays=base.holidays | _parse_holidays(spec.get("holidays")),
        early_closes=MappingProxyType(
            {**dict(base.early_closes), **_parse_early_closes(spec.get("early_closes"))}
        ),
        weekly=base.weekly,
    )


def _text(value: Any, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown venue timezone: {timezone_name}") from exc


_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _session_model_metadata(rule: SessionRule) -> dict[str, Any]:
    """Weekly-vs-daily session model metadata persisted into provenance."""
    if rule.weekly:
        return {
            "session_model": "weekly",
            "session_week": {
                "open_day": _WEEKDAY_NAMES[rule.weekdays[0]],
                "open_time": rule.open_time.isoformat(),
                "close_day": _WEEKDAY_NAMES[rule.weekdays[-1]],
                "close_time": rule.close_time.isoformat(),
            },
        }
    return {"session_model": "daily"}


def _closure_policy_metadata() -> dict[str, Any]:
    """Fail-closed closure/early-close scope persisted into provenance."""
    return {
        "one_off_closures_audited_through": CLOSURE_AUDIT_END_YEAR,
        "one_off_closures_policy": (
            "encoded_for_audited_range; recurring rules beyond"
        ),
        "early_closes_policy": (
            "us_black_friday:all_supported_years; "
            "us_pre_independence:july_3_when_open_session; "
            "us_christmas_eve:through_audit_boundary_2026; "
            "uk_dec24_dec31_half_days:all_supported_years"
        ),
    }


class VenueCalendar:
    """Session boundaries for one venue with directional time arithmetic.

    All public methods accept aware datetimes and return aware UTC datetimes.
    """

    def __init__(self, rule: SessionRule, *, version: str = CALENDAR_VERSION):
        self._rule = rule
        self._zone = _zone(rule.timezone)
        self._version = version

    @property
    def name(self) -> str:
        return self._rule.name

    @property
    def version(self) -> str:
        return self._version

    @property
    def timezone_name(self) -> str:
        return self._rule.timezone

    @property
    def rule(self) -> SessionRule:
        return self._rule

    def is_trading_day(self, day: date) -> bool:
        if not MIN_SUPPORTED_YEAR <= day.year <= MAX_SUPPORTED_YEAR:
            raise ValueError(
                f"{self._rule.name} calendar supports years "
                f"{MIN_SUPPORTED_YEAR}-{MAX_SUPPORTED_YEAR}, got {day.isoformat()}"
            )
        return (
            day.weekday() in self._rule.weekdays
            and day not in self._rule.holidays
        )

    def _bounds_on_date(self, day: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(day):
            return None
        rule = self._rule
        if rule.weekly:
            # Continuous weekly session: the first weekday opens at open_time,
            # the last closes at close_time, intermediate days run full 24h.
            days = rule.weekdays
            if day.weekday() == days[0]:
                open_at = datetime.combine(day, rule.open_time, tzinfo=self._zone)
                close_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=self._zone)
            elif day.weekday() == days[-1]:
                open_at = datetime.combine(day, time(0, 0), tzinfo=self._zone)
                close_at = datetime.combine(day, rule.close_time, tzinfo=self._zone)
            else:
                open_at = datetime.combine(day, time(0, 0), tzinfo=self._zone)
                close_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=self._zone)
            return open_at, close_at
        close_time = rule.early_closes.get(day, rule.close_time)
        open_at = datetime.combine(day, rule.open_time, tzinfo=self._zone)
        close_at = datetime.combine(day, close_time, tzinfo=self._zone)
        return open_at, close_at

    def _bounds(self, at: datetime) -> tuple[datetime, datetime] | None:
        local = at.astimezone(self._zone)
        return self._bounds_on_date(local.date())

    def is_open(self, at: datetime) -> bool:
        bounds = self._bounds(at)
        if bounds is None:
            return False
        open_at, close_at = bounds
        local = at.astimezone(self._zone)
        return open_at <= local < close_at

    def _next_open_day(self, day: date) -> datetime:
        for _ in range(_MAX_DAYS):
            day = day + timedelta(days=1)
            candidate = self._bounds_on_date(day)
            if candidate is not None:
                return candidate[0].astimezone(UTC)
        raise CalendarBoundError(
            f"{self._rule.name} calendar found no session within "
            f"{_MAX_DAYS} days of {day.isoformat()}"
        )

    def next_open(self, at: datetime) -> datetime:
        """First open at or after ``at`` (returns ``at`` when already open).

        Raises CalendarBoundError when no session exists within the walk
        bound (fail-closed: never fabricate a timestamp off the calendar).
        """
        local = at.astimezone(self._zone)
        bounds = self._bounds_on_date(local.date())
        if bounds is not None and local < bounds[1]:
            return max(at, bounds[0].astimezone(UTC))
        return self._next_open_day(local.date())

    def previous_close(self, at: datetime) -> datetime:
        """Latest eligible session close at or before ``at``.

        Weekly calendars model a continuous market week as adjacent daily
        slices, so an immediately preceding slice that closes exactly at the
        cursor remains eligible. Daily venues retain strict prior-close
        behavior at a session boundary.
        """
        local = at.astimezone(self._zone)
        bounds = self._bounds_on_date(local.date())
        if bounds is not None and local > bounds[1]:
            return bounds[1].astimezone(UTC)
        day = local.date()
        for _ in range(_MAX_DAYS):
            day = day - timedelta(days=1)
            candidate = self._bounds_on_date(day)
            if candidate is None:
                continue
            candidate_close = candidate[1].astimezone(self._zone)
            if candidate_close < local or (
                self._rule.weekly and candidate_close == local
            ):
                return candidate_close.astimezone(UTC)
        raise CalendarBoundError(
            f"{self._rule.name} calendar found no prior session within "
            f"{_MAX_DAYS} days of {local.isoformat()}"
        )

    def forward(self, at: datetime, minutes: float) -> datetime:
        """Advance ``minutes`` of trading time (direction: target/post-event).

        Closed periods (weekends, holidays, overnight) are skipped; the result
        is the first trading-time boundary at or beyond the requested offset.
        Raises CalendarBoundError when the walk cannot resolve (fail-closed).
        """
        remaining = timedelta(minutes=minutes)
        cursor = at
        for _ in range(_MAX_DAYS):
            if remaining <= timedelta(0):
                return cursor
            bounds = self._bounds(cursor)
            if bounds is None or cursor >= bounds[1]:
                cursor = self.next_open(cursor)
                continue
            open_at, close_at = bounds
            anchor = max(cursor, open_at)
            if anchor + remaining <= close_at:
                return anchor + remaining
            remaining -= close_at - anchor
            cursor = close_at
        raise CalendarBoundError(
            f"{self._rule.name} calendar could not advance {minutes} minutes "
            f"from {at.isoformat()} within the walk bound"
        )

    def backward(self, at: datetime, minutes: float) -> datetime:
        """Rewind ``minutes`` of trading time (direction: baseline/pre-event).

        Closed periods are skipped, so a lookback that crosses a weekend or
        holiday continues from the previous session's close. Raises
        CalendarBoundError when the walk cannot resolve (fail-closed).
        """
        remaining = timedelta(minutes=minutes)
        cursor = at
        for _ in range(_MAX_DAYS):
            if remaining <= timedelta(0):
                return cursor
            bounds = self._bounds(cursor)
            if (
                bounds is not None
                and self._rule.weekly
                and cursor == bounds[0]
            ):
                # A weekly market's midnight slices are contiguous. Rewinding
                # from the exact boundary must consume the preceding slice,
                # not repeatedly resolve the same at-or-before close.
                previous_bounds = self._bounds(cursor - timedelta(microseconds=1))
                if previous_bounds is not None:
                    bounds = previous_bounds
            if bounds is None or cursor <= bounds[0]:
                cursor = self.previous_close(cursor)
                continue
            open_at, close_at = bounds
            anchor = min(cursor, close_at)
            if anchor - remaining >= open_at:
                return anchor - remaining
            remaining -= anchor - open_at
            cursor = open_at
        raise CalendarBoundError(
            f"{self._rule.name} calendar could not rewind {minutes} minutes "
            f"from {at.isoformat()} within the walk bound"
        )

    def session_close_after(self, at: datetime) -> datetime:
        """Next session close at or after ``at`` (direction: session_close).

        For weekly venues the only session close is the last trading day's
        close (e.g. FX Friday 17:00 America/New_York); any other venue
        resolves the containing session's close. Raises CalendarBoundError
        when no session exists within the walk bound (fail-closed).
        """
        local = at.astimezone(self._zone)
        if self._rule.weekly:
            last_day = self._rule.weekdays[-1]
            day = local.date()
            for _ in range(_MAX_DAYS):
                if day.weekday() == last_day:
                    close_at = datetime.combine(
                        day, self._rule.close_time, tzinfo=self._zone
                    )
                    if close_at >= local:
                        return close_at.astimezone(UTC)
                day = day + timedelta(days=1)
            raise CalendarBoundError(
                f"{self._rule.name} weekly calendar found no session close "
                f"within {_MAX_DAYS} days of {local.isoformat()}"
            )
        bounds = self._bounds_on_date(local.date())
        if bounds is not None and local < bounds[1]:
            return bounds[1].astimezone(UTC)
        day = local.date()
        for _ in range(_MAX_DAYS):
            day = day + timedelta(days=1)
            candidate = self._bounds_on_date(day)
            if candidate is not None:
                return candidate[1].astimezone(UTC)
        raise CalendarBoundError(
            f"{self._rule.name} calendar found no session close within "
            f"{_MAX_DAYS} days of {local.isoformat()}"
        )

    def session_open_after(self, at: datetime) -> datetime:
        """Next session open strictly after ``at``.

        Raises CalendarBoundError when no session exists within the walk
        bound (fail-closed).
        """
        local = at.astimezone(self._zone)
        bounds = self._bounds_on_date(local.date())
        if bounds is not None and local < bounds[0]:
            return bounds[0].astimezone(UTC)
        day = local.date()
        for _ in range(_MAX_DAYS):
            day = day + timedelta(days=1)
            candidate = self._bounds_on_date(day)
            if candidate is not None:
                return candidate[0].astimezone(UTC)
        raise CalendarBoundError(
            f"{self._rule.name} calendar found no session open within "
            f"{_MAX_DAYS} days of {local.isoformat()}"
        )

    def session_metadata(self) -> dict[str, Any]:
        """Explicit instrument/session metadata with baseline/pre/post and
        session-close direction semantics (persisted into provenance).

        ``holiday_scope`` documents the deterministic supported years and the
        fail-closed exceptional-closure boundary: recurring rules cover the
        whole range, and one-off closures are encoded through
        ``CLOSURE_AUDIT_END_YEAR`` (dates at or before it are never silently
        treated as sessions). Config ``holidays``/``early_closes`` extend the
        built-in rules per deployment."""
        rule = self._rule
        return {
            "venue": rule.name,
            "timezone": rule.timezone,
            "session_open": rule.open_time.isoformat(),
            "session_close": rule.close_time.isoformat(),
            "trading_days": sorted(rule.weekdays),
            "direction": {
                DIRECTION_BASELINE: "backward",
                DIRECTION_TARGET: "forward",
                DIRECTION_SESSION_CLOSE: "forward",
            },
            "holiday_scope": (
                f"builtin_rules:{MIN_SUPPORTED_YEAR}-{MAX_SUPPORTED_YEAR}"
            ),
            "closure_policy": _closure_policy_metadata(),
            "calendar_version": self._version,
        }


def _calendars_from(config: Mapping[str, Any] | None) -> tuple[Mapping, Mapping, str]:
    if isinstance(config, Mapping):
        reaction = config.get("reaction_windows", {})
        reaction = reaction if isinstance(reaction, Mapping) else {}
        calendars = reaction.get("calendars", {})
        calendars = calendars if isinstance(calendars, Mapping) else {}
        instruments = calendars.get("instruments", {})
        instruments = instruments if isinstance(instruments, Mapping) else {}
        venues = calendars.get("venues", {})
        venues = venues if isinstance(venues, Mapping) else {}
        default_venue = _text(calendars.get("default_venue"), DEFAULT_VENUE)
    else:
        instruments, venues, default_venue = {}, {}, DEFAULT_VENUE
    return instruments, venues, default_venue


def _resolve_rule(
    symbol: str | None,
    config: Mapping[str, Any] | None,
) -> tuple[SessionRule, Mapping[str, Any]]:
    """Resolve the effective session rule for a symbol from config.

    Executable instrument metadata (``calendars.instruments.<symbol>``):
    - ``venue`` (required): base rule name (a built-in venue or a
      ``calendars.venues`` key).
    - ``exchange_calendar`` (optional): rule selector; takes precedence over
      ``venue`` when set.
    - ``timezone`` / ``session_open`` / ``session_close`` (optional):
      per-instrument overrides applied to the rule (persisted via the policy).
    A bare venue-name string is shorthand for ``{"venue": name}``.
    Returns ``(rule, entry_mapping)`` so callers can read policy fields.
    """
    instruments, venues, default_venue = _calendars_from(config)
    entry = instruments.get(symbol) if symbol else None
    spec = entry if isinstance(entry, Mapping) else {}
    venue = _text(spec.get("venue"), "")
    if not venue and not isinstance(entry, Mapping):
        venue = _text(entry, "")
    if not venue:
        venue = default_venue_for(symbol)
        if venue == DEFAULT_VENUE and default_venue != DEFAULT_VENUE:
            venue = default_venue
    rule_name = _text(spec.get("exchange_calendar"), venue) or venue
    rule = build_rule(rule_name, venues.get(rule_name))
    timezone = _text(spec.get("timezone"), rule.timezone)
    session_open = _parse_time(spec.get("session_open"), rule.open_time)
    session_close = _parse_time(spec.get("session_close"), rule.close_time)
    if (timezone, session_open, session_close) != (
        rule.timezone,
        rule.open_time,
        rule.close_time,
    ):
        rule = SessionRule(
            name=rule.name,
            timezone=timezone,
            open_time=session_open,
            close_time=session_close,
            weekdays=rule.weekdays,
            holidays=rule.holidays,
            early_closes=rule.early_closes,
            weekly=rule.weekly,
        )
    return rule, spec


def venue_for_symbol(
    symbol: str | None,
    config: Mapping[str, Any] | None = None,
) -> VenueCalendar:
    """Resolve the venue calendar for a symbol, honoring optional config."""
    rule, _ = _resolve_rule(symbol, config)
    return VenueCalendar(rule)


TARGET_SELECTION_POLICY_FIRST = "first"
_TARGET_SELECTION_POLICIES = {TARGET_SELECTION_POLICY_FIRST}


@dataclass(frozen=True)
class InstrumentSessionPolicy:
    """Typed, executable per-instrument calendar configuration.

    Resolved from ``reaction_windows.calendars.instruments`` (dict entries may
    set ``venue``, ``exchange_calendar``, ``timezone``, ``session_open``,
    ``session_close``, ``price_timeframe``, and ``target_selection_policy``;
    bare-name entries set the venue only). Carries the effective venue,
    timezone, session bounds, the market_data price timeframe used for sample
    queries, the target-selection policy, and closure/version provenance.
    Unknown timezones and unknown target-selection policies raise.
    """

    venue: str
    timezone: str
    session_open: time
    session_close: time
    weekly: bool
    week_open_day: str | None
    week_close_day: str | None
    price_timeframe: str
    target_selection_policy: str
    calendar_version: str
    closure_audit_end_year: int

    def to_metadata(self) -> dict[str, Any]:
        """Executable metadata persisted into reaction-window provenance."""
        return {
            "venue": self.venue,
            "timezone": self.timezone,
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "session_model": "weekly" if self.weekly else "daily",
            "session_week": {
                "open_day": self.week_open_day,
                "open_time": self.session_open.isoformat(),
                "close_day": self.week_close_day,
                "close_time": self.session_close.isoformat(),
            }
            if self.weekly
            else None,
            "price_timeframe": self.price_timeframe,
            "target_selection_policy": self.target_selection_policy,
            "direction": {
                DIRECTION_BASELINE: "backward",
                DIRECTION_TARGET: "forward",
                DIRECTION_SESSION_CLOSE: "forward",
            },
            "holiday_scope": (
                f"builtin_rules:{MIN_SUPPORTED_YEAR}-{MAX_SUPPORTED_YEAR}"
            ),
            "closure_policy": _closure_policy_metadata(),
            "calendar_version": self.calendar_version,
        }


def instrument_policy_for(
    symbol: str | None,
    config: Mapping[str, Any] | None = None,
    *,
    default_timeframe: str = "PRICE",
) -> InstrumentSessionPolicy:
    """Resolve the typed per-instrument session/selection policy.

    ``price_timeframe`` defaults to ``default_timeframe`` (the window's market
    timeframe) unless the instrument entry overrides it. The executed target
    selection policy is ``first``: the target is the first market sample at or
    after the target timestamp (baseline = last pre-event sample, backward;
    end_of_session = next session close). Unknown policy values are rejected.
    """
    rule, spec = _resolve_rule(symbol, config)
    timeframe = _text(spec.get("price_timeframe"), "") or default_timeframe
    policy = _text(
        spec.get("target_selection_policy"), TARGET_SELECTION_POLICY_FIRST
    )
    if policy not in _TARGET_SELECTION_POLICIES:
        raise ValueError(
            f"unsupported target_selection_policy: {policy!r} "
            f"(supported: {sorted(_TARGET_SELECTION_POLICIES)})"
        )
    calendar = VenueCalendar(rule)
    return InstrumentSessionPolicy(
        venue=rule.name,
        timezone=rule.timezone,
        session_open=rule.open_time,
        session_close=rule.close_time,
        weekly=rule.weekly,
        week_open_day=_WEEKDAY_NAMES[rule.weekdays[0]] if rule.weekly else None,
        week_close_day=_WEEKDAY_NAMES[rule.weekdays[-1]] if rule.weekly else None,
        price_timeframe=timeframe,
        target_selection_policy=policy,
        calendar_version=calendar.version,
        closure_audit_end_year=CLOSURE_AUDIT_END_YEAR,
    )


__all__ = [
    "BUILTIN_VENUES",
    "CALENDAR_VERSION",
    "CLOSURE_AUDIT_END_YEAR",
    "DIRECTION_BASELINE",
    "DIRECTION_SESSION_CLOSE",
    "DIRECTION_TARGET",
    "DEFAULT_VENUE",
    "FX_VENUE",
    "InstrumentSessionPolicy",
    "LSE_VENUE",
    "MAX_SUPPORTED_YEAR",
    "MIN_SUPPORTED_YEAR",
    "NYSE_VENUE",
    "TARGET_SELECTION_POLICY_FIRST",
    "SessionRule",
    "VenueCalendar",
    "XETRA_VENUE",
    "build_rule",
    "default_venue_for",
    "instrument_policy_for",
    "venue_for_symbol",
]
