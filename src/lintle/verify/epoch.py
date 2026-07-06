"""Parse a TLE line-1 epoch into a chronological sort key. The 2-digit year
pivots at 57 (``57``–``99`` → 1957–1999, ``00``–``56`` → 2000–2056), matching the
space-track convention; a bad key silently mis-orders a satellite's track and
manufactures false residuals downstream, so this is deliberately one tiny,
well-tested unit rather than inlined column slicing (issue: verify §epoch)."""

# Epoch columns on a 69-char TLE line 1 (0-indexed): year ``[18:20]`` (cols
# 19-20), day-of-year integer part ``[20:23]`` (cols 21-23), an assumed decimal
# point at col 24, fractional day ``[24:32]`` (cols 25-32) — the exact slices
# tle.py's semantic check reads, kept in lockstep with it.


def parse_epoch(line1: str) -> tuple[int, float]:
    """Return ``(four_digit_year, day_of_year)`` from a 69-char TLE line 1.
    Raises ``ValueError`` if the epoch columns are not numeric — the caller
    treats that as a re-validation failure, never a silent zero."""
    yy = int(line1[18:20])
    year = 2000 + yy if yy < 57 else 1900 + yy
    day = float(line1[20:23] + "." + line1[24:32])
    return year, day


def epoch_key(line1: str) -> float:
    """A single monotonic float ordering records chronologically: ``year*1000 +
    day_of_year``. Day-of-year is < 367 < 1000, so adjacent years never overlap
    and the float sorts identically to ``(year, day)``."""
    year, day = parse_epoch(line1)
    return year * 1000.0 + day
