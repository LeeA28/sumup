"""Reading the /catchup `since` option, and time zone helpers."""
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, available_timezones

EXAMPLE_HINT = "Try 2h, 4:00 PM, or Sep 22 6:23 PM"

# --- Time zones -------------------------------------------------------------

# Real-world zones like "America/Toronto"; skip internal aliases like "Etc/GMT+5"
ALL_ZONES = sorted(
    zone for zone in available_timezones()
    if "/" in zone and not zone.startswith(("Etc/", "SystemV/", "posix/", "right/"))
)
COMMON_ZONES = [
    "America/Toronto", "America/Vancouver", "America/Edmonton", "America/Winnipeg",
    "America/Halifax", "America/St_Johns", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "Europe/London", "Asia/Seoul",
]


def zone_city(zone: str) -> str:
    """'America/St_Johns' -> 'St Johns'"""
    return zone.split("/")[-1].replace("_", " ")


def search_zones(query: str, limit: int = 25) -> list[str]:
    """Zones matching what the user typed, best matches first."""
    if not query.strip():
        return COMMON_ZONES[:limit]
    q = query.strip().lower().replace(" ", "_")
    matches = [zone for zone in ALL_ZONES if q in zone.lower()]
    # Cities that start with the query rank above other matches
    matches.sort(key=lambda zone: not zone.split("/")[-1].lower().startswith(q))
    return matches[:limit]


def format_clock(dt: datetime) -> str:
    """'4:05 PM' (works on Windows, unlike strftime's %-I)."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


# --- Parsing `since` ----------------------------------------------------------

class SinceError(ValueError):
    """The input couldn't be read. The message is shown to the user (max 100 chars)."""


class NeedsTimezone(SinceError):
    """A clock time or date was given, but the user hasn't set a time zone."""


UNITS = r"(d|days?|h|hrs?|hours?|m|mins?|minutes?)"
RELATIVE_RE = re.compile(rf"^(?:\s*\d+\s*{UNITS}\s*,?)+\s*(?:ago)?$")
RELATIVE_PART_RE = re.compile(rf"(\d+)\s*{UNITS}")
NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}\s*[/\-.]\s*\d{1,2}\b")
MONTH_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{4}))?"
)
DAYWORD_RE = re.compile(r"\b(today|yesterday)\b")
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _parse_relative(text: str) -> Optional[timedelta]:
    """'1d 3h' -> timedelta(days=1, hours=3), or None if it isn't a relative time."""
    if not RELATIVE_RE.match(text):
        return None
    total = timedelta()
    for amount, unit in RELATIVE_PART_RE.findall(text):
        n = int(amount)
        if unit.startswith("d"):
            total += timedelta(days=n)
        elif unit.startswith("h"):
            total += timedelta(hours=n)
        else:
            total += timedelta(minutes=n)
    return total


def _hour_options(hour: int, minute: int, meridiem: Optional[str]) -> list[tuple[int, int]]:
    """Every 24-hour time the user could mean. '4:00' could be 4 AM or 4 PM."""
    if meridiem:
        if not 1 <= hour <= 12:
            raise SinceError("With AM/PM, the hour must be 1 to 12.")
        return [(hour % 12 + (12 if meridiem == "pm" else 0), minute)]
    if hour > 23:
        raise SinceError("Hours go up to 23.")
    if hour == 0 or hour > 12:
        return [(hour, minute)]
    if hour == 12:
        return [(12, minute), (0, minute)]
    return [(hour, minute), (hour + 12, minute)]


def parse_since(text: str, now: datetime, tz: Optional[ZoneInfo]) -> datetime:
    """Turn the user's `since` text into a UTC datetime in the past.

    Raises SinceError (or NeedsTimezone) with a message meant for the user.
    """
    raw = " ".join(text.strip().lower().split())
    if not raw:
        raise SinceError(EXAMPLE_HINT)

    # Relative times work without a time zone
    delta = _parse_relative(raw)
    if delta is not None:
        if delta <= timedelta(0):
            raise SinceError("The time needs to be more than 0.")
        return now - delta

    if NUMERIC_DATE_RE.search(raw):
        raise SinceError("Write dates with the month name, like Sep 22 6:23 PM.")
    if tz is None:
        raise NeedsTimezone("Set your time zone with /timezone first, or use a relative time like 2h.")

    s = raw.replace(".", "")  # "p.m." -> "pm", "Sept." -> "sept"
    local_now = now.astimezone(tz)

    month_match = MONTH_RE.search(s)
    if month_match:
        s = s[:month_match.start()] + " " + s[month_match.end():]
    day_match = DAYWORD_RE.search(s)
    if day_match:
        s = s[:day_match.start()] + " " + s[day_match.end():]
    if month_match and day_match:
        raise SinceError("Use either a date or today/yesterday, not both.")

    # Whatever is left should be a time, ignoring filler like "on", "at", ","
    s = " ".join(re.sub(r"\b(?:on|at)\b|,", " ", s).split())
    if s:
        time_match = TIME_RE.fullmatch(s)
        if not time_match:
            raise SinceError(EXAMPLE_HINT)
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if minute > 59:
            raise SinceError("Minutes go up to 59.")
        times = _hour_options(hour, minute, time_match.group(3))
    elif month_match or day_match:
        times = [(0, 0)]  # a date alone means the start of that day
    else:
        raise SinceError(EXAMPLE_HINT)

    # Every date the user could mean
    today = local_now.date()
    if month_match:
        month = MONTHS.index(month_match.group(1)[:3]) + 1
        day = int(month_match.group(2))
        years = [int(month_match.group(3))] if month_match.group(3) else [today.year, today.year - 1]
        dates = []
        for year in years:
            try:
                dates.append(date(year, month, day))
            except ValueError:
                pass  # e.g. Feb 30, or Feb 29 in a non-leap year
        if not dates:
            raise SinceError("That date doesn't exist.")
    elif day_match and day_match.group(1) == "today":
        dates = [today]
    elif day_match:
        dates = [today - timedelta(days=1)]
    else:
        dates = [today, today - timedelta(days=1)]

    # Pick the most recent possibility that's already happened
    candidates = [
        datetime.combine(d, time(h, m), tzinfo=tz)
        for d in dates
        for h, m in times
    ]
    past = [c for c in candidates if c <= local_now]
    if not past:
        raise SinceError("That time is in the future.")
    return max(past).astimezone(timezone.utc)


def describe_since(when: datetime, now: datetime, tz: Optional[ZoneInfo]) -> str:
    """How SumUp read the input, e.g. 'Yesterday at 4:00 PM (Toronto)'."""
    if tz is None:
        minutes = int((now - when).total_seconds() // 60)
        days, rest = divmod(minutes, 24 * 60)
        hours, mins = divmod(rest, 60)
        parts = [f"{n} {unit}{'s' if n != 1 else ''}"
                 for n, unit in ((days, "day"), (hours, "hour"), (mins, "minute")) if n]
        return (" ".join(parts) or "0 minutes") + " ago"

    local = when.astimezone(tz)
    today = now.astimezone(tz).date()
    if local.date() == today:
        day_label = "Today"
    elif local.date() == today - timedelta(days=1):
        day_label = "Yesterday"
    else:
        day_label = f"{local:%b} {local.day}"
        if local.year != today.year:
            day_label += f", {local.year}"
    return f"{day_label} at {format_clock(local)} ({zone_city(tz.key)})"
