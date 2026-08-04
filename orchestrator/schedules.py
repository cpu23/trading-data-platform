import re
from datetime import UTC

from apscheduler.triggers.cron import CronTrigger

_POSIX_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def normalize_posix_weekdays(day_of_week: str) -> str:
    if not re.search(r"\d", day_of_week):
        return day_of_week

    named_weekdays: list[str] = []
    for item in day_of_week.split(","):
        match = re.fullmatch(r"([0-7])(?:-([0-7]))?", item)
        if not match:
            raise ValueError(
                "Unsupported POSIX numeric weekday expression; use numeric values or "
                "non-wrapping ranges (for example 1-5), or named weekdays such as mon-fri"
            )
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            raise ValueError(
                "Wrapping POSIX numeric weekday ranges are unsupported; use an explicit "
                "comma-separated list of named weekdays"
            )
        for weekday in range(start, end + 1):
            name = _POSIX_WEEKDAYS[weekday]
            if name not in named_weekdays:
                named_weekdays.append(name)
    return ",".join(named_weekdays)


def build_cron_trigger(schedule: str) -> CronTrigger:
    fields = schedule.split()
    if len(fields) == 5:
        fields[4] = normalize_posix_weekdays(fields[4])
        schedule = " ".join(fields)
    return CronTrigger.from_crontab(schedule, timezone=UTC)
