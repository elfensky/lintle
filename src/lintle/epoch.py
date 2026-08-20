"""The single definition of a record's moment in time (#199). A TLE line-1
epoch is a 2-digit year (pivot 57: ``57``–``99`` → 1957–1999, ``00``–``56`` →
2000–2056) plus a fractional day-of-year that ``tle.py`` accepts anywhere in
(0, 367) with no leap-year logic — space-track ships real rollover records
like day 366.5 of a non-leap year. This module parses those columns once and
NORMALIZES them (day 366.x of a non-leap year is early January of the next
year; day 0.x is late December of the prior one), so the sort key, the
instant, and every derived bucket agree at year boundaries. Any code needing
a record's epoch must route through these functions; a second definition is
how the year-boundary bug cluster happened."""

import calendar
import datetime as _dt

# Epoch columns on a 69-char TLE line 1 (0-indexed): year ``[18:20]`` (cols
# 19-20), day-of-year integer part ``[20:23]`` (cols 21-23), an assumed decimal
# point at col 23, fractional day ``[24:32]`` (cols 25-32) — the exact slices
# tle.py's semantic check reads.


def _normalize(line1: str) -> tuple[int, float]:
    """``(four_digit_year, day_of_year)`` with year-boundary rolls applied.
    The day is re-formed from the DECIMAL STRING after rolling whole days —
    never float subtraction — so a rolled epoch's value is bit-identical to
    the literal in-range spelling of the same instant (``epoch_key`` is
    serialized via ``repr()`` in the sorter spill and suspect frame), and
    no-roll days are bit-identical to the raw columns. Raises ``ValueError``
    on non-numeric columns — a re-validation failure, never a silent zero."""
    yy = int(line1[18:20])
    year = 2000 + yy if yy < 57 else 1900 + yy
    whole = int(line1[20:23])
    if whole < 1:  # day 0.x — late December of the prior year
        year -= 1
        whole += 365 + calendar.isleap(year)
    elif whole > 365 + calendar.isleap(year):  # 366.x, non-leap — next January
        whole -= 365 + calendar.isleap(year)
        year += 1
    return year, float(f"{whole:03d}.{line1[24:32]}")


def parse_epoch(line1: str) -> tuple[int, float]:
    """Return the NORMALIZED ``(four_digit_year, day_of_year)`` from a 69-char
    TLE line 1 — every function in this module speaks this same truth; callers
    wanting the raw columns can slice the line. Raises ``ValueError`` if the
    epoch columns are not numeric."""
    return _normalize(line1)


def epoch_key(line1: str) -> float:
    """A single monotonic float ordering records chronologically:
    ``year*1000 + day_of_year`` on the normalized pair. Day-of-year is
    < 367 < 1000, so adjacent years never overlap and the float sorts
    identically to ``(year, day)`` — and, post-normalization, identically
    to the instant."""
    year, day = _normalize(line1)
    return year * 1000.0 + day


def epoch_dt(line1: str) -> _dt.datetime:
    """Record epoch as an aware UTC datetime — pure arithmetic from the
    normalized ``(year, day_of_year)``, no wall clock."""
    year, day = _normalize(line1)
    return _dt.datetime(year, 1, 1, tzinfo=_dt.UTC) + _dt.timedelta(days=day - 1)


def iso(dt: _dt.datetime) -> str:
    """The one ISO-8601 spelling of an epoch instant (``…THH:MM:SSZ``) for
    byte-deterministic output. Takes the ``epoch_dt`` result rather than a
    ``line1`` so callers format each parsed instant once, not re-parse per
    rendering — but the format lives here with the parse, one module per #199."""
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
