"""Pure history reduction shared by ``extract`` (per-satellite sidecar) and
``dedup`` (the corpus manifest): given a satellite's epoch datetimes and
element-set numbers in stream order, compute its ``HistoryStats`` — count,
span, median spacing, and reportable gaps. No I/O, no ``sgp4``; the single
definition of 'a gap' both callers share so their numbers cannot drift."""

import dataclasses
import datetime as _dt
import statistics

# Re-exported for extract/dedup: the epoch instant and its ISO rendering live
# in lintle.epoch (the single definition, #199); history keeps the names so
# its callers need one import for "reduce this satellite's timeline".
from lintle.epoch import epoch_dt as epoch_dt
from lintle.epoch import iso as iso

GAP_FACTOR = 10
GAPS_CAP = 10
# Gap analysis needs at least this many records: below it there is only one
# delta — no "typical spacing" for GAP_FACTOR to multiply — so the history is
# definitionally gap-silent (median_spacing_days: null tells the reader why,
# and dedup's summary tallies these as gap_silent_satellites).
MIN_GAP_RECORDS = 3


@dataclasses.dataclass(slots=True, frozen=True)
class Gap:
    """One reportable hole in a satellite's history."""

    start: _dt.datetime
    end: _dt.datetime
    days: float


@dataclasses.dataclass(slots=True, frozen=True)
class HistoryStats:
    """Reduction of one satellite's deduped span."""

    count: int
    first: _dt.datetime | None
    last: _dt.datetime | None
    elset_first: int | None
    elset_last: int | None
    largest_gap_days: float
    largest_gap_at: _dt.datetime | None
    median_spacing_days: float | None
    gaps: tuple[Gap, ...]
    gap_count: int


def analyze_epochs(
    epochs: list[_dt.datetime], elsets: list[int | None]
) -> HistoryStats:
    """Reduce one satellite's epoch/element-set lists (stream order) to its
    ``HistoryStats``. A gap is reportable when the inter-epoch delta exceeds
    ``GAP_FACTOR`` x the median spacing; the ``gaps`` tuple keeps the
    ``GAPS_CAP`` largest, chronologically, and ``gap_count`` is the total."""
    deltas = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(epochs, epochs[1:], strict=False)
    ]
    largest = max(deltas, default=0.0)
    largest_at = epochs[deltas.index(largest) + 1] if deltas else None
    enough = len(deltas) >= MIN_GAP_RECORDS - 1
    median = statistics.median(deltas) if enough else None
    # REPORT the interpolated median (byte-stable field) but THRESHOLD on
    # median_low: with exactly 2 deltas the interpolated (a+d)/2 makes
    # d > GAP_FACTOR*median algebraically impossible (#199's n=3 dead zone);
    # the low median keeps the bar at a real observed spacing.
    # `bar and ...`: 0 is intentionally excluded here — a zero bar would
    # otherwise flag every record as a gap (d > 10*0 is always true for
    # d > 0), and deltas are strictly positive post-dedup anyway.
    bar = statistics.median_low(deltas) if enough else None
    reportable = [
        Gap(epochs[i], epochs[i + 1], d)
        for i, d in enumerate(deltas)
        if bar and d > GAP_FACTOR * bar
    ]
    top = sorted(reportable, key=lambda g: g.days, reverse=True)[:GAPS_CAP]
    return HistoryStats(
        count=len(epochs),
        first=epochs[0] if epochs else None,
        last=epochs[-1] if epochs else None,
        elset_first=elsets[0] if elsets else None,
        elset_last=elsets[-1] if elsets else None,
        largest_gap_days=largest,
        largest_gap_at=largest_at,
        median_spacing_days=median,
        gaps=tuple(sorted(top, key=lambda g: g.start)),
        gap_count=len(reportable),
    )
