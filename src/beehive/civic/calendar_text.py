"""Full dates in evidence and in generated copy, checked as whole dates.

Generated copy must write a date as YYYY-MM-DD in every locale. A digit-by-digit check sees such
a date as three unrelated numbers: it rejects a correct rendering of "1 April 2027" because 04
and 01 never appear in the evidence, and licensing those parts one by one would instead accept a
date recombined from two different ones. Here a full date in copy that the evidence states whole
is accepted as that date; any other date stays subject to the ordinary number check, so nothing
the digit check accepted before is lost. A month without a day is not a date here; copy names
it in words.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Container, Iterable

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH = "(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ORDINAL = r"(?:st|nd|rd|th)?"
_DAY = rf"(?P<day>[0-3]?\d){_ORDINAL}"
_START = rf"(?:(?P<start>[0-3]?\d){_ORDINAL}\s*(?:-|\u2013|\u2014|to|and|&)\s*)?"
_END = rf"(?:\s*(?:-|\u2013|\u2014|to|and|&)\s*(?P<end>[0-3]?\d){_ORDINAL})?"
_YEAR = r"(?P<year>(?:19|20)\d\d)"

# Evidence: 1 April 2027, the 1st of July 2029, 3-5 September 2026; April 1, 2027;
# September 14-16, 2026. Without a year these state no full date.
_DAY_MONTH = re.compile(
    rf"\b{_START}{_DAY}\s+(?:of\s+)?{_MONTH}\b(?:,?\s+{_YEAR}\b)?", re.IGNORECASE)
_MONTH_DAY = re.compile(
    rf"\b{_MONTH}\s+{_DAY}\b{_END}(?:,?\s+{_YEAR}\b)?", re.IGNORECASE)
_NUMERIC_DATE = re.compile(
    r"\b(?P<day>[0-3]?\d)[/.](?P<month>[01]?\d)[/.](?P<year>(?:19|20)\d\d)\b")
_ISO_DATE = re.compile(r"(?<![\d-])(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?![\d-])")
# Copy: the Chinese base may write a full date as 2026年6月30日.
_ZH_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]")


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def evidence_dates(texts: Iterable[str], *, event_dates: Iterable[date] = ()) -> frozenset[date]:
    """Every whole date the evidence states: written with its year, or the evidence's own event
    date. A day and month without a year state no full date, and a publication date is not a
    date the source states about anything."""
    dates: set[date] = {value for value in event_dates if value is not None}
    for raw in texts:
        text = unicodedata.normalize("NFKC", raw or "")
        for match in _ISO_DATE.finditer(text):
            if (value := _date(int(match["year"]), int(match["month"]), int(match["day"]))):
                dates.add(value)
        for match in _NUMERIC_DATE.finditer(text):
            if (value := _date(int(match["year"]), int(match["month"]), int(match["day"]))):
                dates.add(value)
        for pattern in (_DAY_MONTH, _MONTH_DAY):
            for match in pattern.finditer(text):
                groups = match.groupdict()
                month = _MONTHS[groups["month"].casefold()]
                if groups.get("year") is None:
                    continue
                for key in ("start", "day", "end"):
                    if groups.get(key) is not None and (
                            value := _date(int(groups["year"]), month, int(groups[key]))):
                        dates.add(value)
    return frozenset(dates)


def full_dates(text: str) -> tuple[date, ...]:
    """The full dates generated copy writes, as ISO or as a Chinese 2026年6月30日."""
    text = unicodedata.normalize("NFKC", text)
    dates = []
    for pattern in (_ZH_DATE, _ISO_DATE):
        for match in pattern.finditer(text):
            if (value := _date(int(match["year"]), int(match["month"]), int(match["day"]))):
                dates.append(value)
    return tuple(dates)


def without_dates(text: str, dates: Container[date] | None = None) -> str:
    """`text` without each full date in `dates` (every full date when None).

    A date left in place is still checked digit by digit, exactly as without this module."""
    def remove(match: re.Match) -> str:
        value = _date(int(match["year"]), int(match["month"]), int(match["day"]))
        return " " if value is not None and (dates is None or value in dates) else match[0]

    text = unicodedata.normalize("NFKC", text)
    return _ISO_DATE.sub(remove, _ZH_DATE.sub(remove, text))


__all__ = ["evidence_dates", "full_dates", "without_dates"]
