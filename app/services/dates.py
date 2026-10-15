"""Resolve unambiguous relative rental dates using Gear's local calendar."""

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


GEAR_TIMEZONE = ZoneInfo("Asia/Dubai")
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
MONTH_DAY_RANGE = re.compile(
    r"\b(?:from\s+)?(?P<start>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:to|through|until|[-–])\s*"
    r"(?P<end>\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(?:of\s+)?(?:the\s+)?(?P<month>this|next)\s+m?onth\b",
    re.IGNORECASE,
)
NAMED_MONTH_RANGE = re.compile(
    rf"\b(?:from\s+)?(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*"
    rf"(?:to|through|until|[-–])\s*"
    rf"(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<named_month>{MONTH_NAMES})(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
MONTH_FIRST_RANGE = re.compile(
    rf"\b(?P<named_month>{MONTH_NAMES})\s+"
    rf"(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*"
    rf"(?:to|through|until|[-–])\s*"
    rf"(?P<end>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
BARE_DAY_RANGE = re.compile(
    r"(?<![-:\d])\b(?:from\s+)?(?P<start>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:to|through|until|[-–])\s*"
    r"(?P<end>\d{1,2})(?:st|nd|rd|th)?\b(?![-:\d])",
    re.IGNORECASE,
)
MONTH_PICKUP_DAY = re.compile(
    r"\b(?:from|starting|start(?:ing)?\s+from)\s+"
    r"(?P<start>\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(?:of\s+)?(?:the\s+)?(?P<month>this|next)\s+m?onth\b",
    re.IGNORECASE,
)


def gear_today() -> date:
    return datetime.now(GEAR_TIMEZONE).date()


def resolve_month_day_range(message: str, today: date) -> tuple[date, date | None] | None:
    """Resolve explicit day ranges or a pickup day without making up a year."""
    match = MONTH_DAY_RANGE.search(message)
    named_match = (NAMED_MONTH_RANGE.search(message) or MONTH_FIRST_RANGE.search(message)
                   if match is None else None)
    if named_match is not None:
        month = MONTHS[named_match.group("named_month").lower()]
        supplied_year = named_match.group("year")
        year = int(supplied_year) if supplied_year else today.year
        if supplied_year is None and month < today.month:
            year += 1
        start = date(year, month, int(named_match.group("start")))
        end = date(year, month, int(named_match.group("end")))
        if end < start:
            raise ValueError("Return day must be on or after pickup day")
        return start, end
    pickup_only = match is None
    if pickup_only:
        match = MONTH_PICKUP_DAY.search(message)
    if match is None:
        return None
    month_index = today.year * 12 + today.month - 1
    if match.group("month").lower() == "next":
        month_index += 1
    year, zero_based_month = divmod(month_index, 12)
    start = date(year, zero_based_month + 1, int(match.group("start")))
    if pickup_only:
        return start, None
    end = date(year, zero_based_month + 1, int(match.group("end")))
    if end < start:
        raise ValueError("Return day must be on or after pickup day")
    return start, end


def has_bare_day_range(message: str) -> bool:
    """Return true for a day range whose month/year is still ambiguous."""
    return (BARE_DAY_RANGE.search(message) is not None
            and MONTH_DAY_RANGE.search(message) is None
            and NAMED_MONTH_RANGE.search(message) is None
            and MONTH_FIRST_RANGE.search(message) is None)
