"""Pure history reduction shared by ``extract`` (per-satellite sidecar) and
``dedup`` (the corpus manifest): given a satellite's epoch datetimes and
element-set numbers in stream order, compute its ``HistoryStats`` — count,
span, median spacing, and reportable gaps. No I/O, no ``sgp4``; the single
definition of 'a gap' both callers share so their numbers cannot drift."""

import dataclasses
import datetime as _dt
import statistics

from lintle.verify.epoch import parse_epoch

GAP_FACTOR = 10
GAPS_CAP = 10


def epoch_dt(line1: str) -> _dt.datetime:
    """Record epoch as an aware UTC datetime — pure arithmetic from
    ``parse_epoch``'s ``(year, day_of_year)``, no wall clock."""
    year, day = parse_epoch(line1)
    return _dt.datetime(year, 1, 1, tzinfo=_dt.UTC) + _dt.timedelta(days=day - 1)


def iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


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
    median = statistics.median(deltas) if len(deltas) >= 2 else None
    # `median and ...`: 0 is intentionally excluded here — a zero median
    # would otherwise flag every record as a gap (d > 10*0 is always true
    # for d > 0), and deltas are strictly positive post-dedup anyway.
    reportable = [
        Gap(epochs[i], epochs[i + 1], d)
        for i, d in enumerate(deltas)
        if median and d > GAP_FACTOR * median
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
