"""`lintle extract` — one satellite's complete deduped TLE history as
``<id>.txt`` + ``<id>.json``. A read-only consumer of a prior `dedup` run: the
``05-dedup/import.*`` chunk set holds only validated-perfect records (exactly 140
bytes each) globally sorted by ``(catalog, epoch)``, so each satellite is one
contiguous byte range found by pure binary search — the sorted fixed-width
stream *is* the index. Never imports sgp4; never touches the clean path. Warns
— and, interactively, confirms — before exporting a history with reportable
gaps or upstream-quarantined records."""

import dataclasses
import datetime as _dt
import json
import statistics
from pathlib import Path

from lintle import REPORT_DIRNAME, fsutil, term
from lintle.chunking import ChunkedReader
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
from lintle.verify.checks import element_set
from lintle.verify.epoch import parse_epoch
from lintle.verify.records import catalog_of

# two validated-perfect 69-char lines + two \n — guarded, not assumed
RECORD_BYTES = 140

_README = """\
# 06-extract — per-satellite TLE history

- `<id>.txt` — one satellite's complete deduped TLE history: pure 2-line
  records, epoch-ascending, byte-identical to the source records.
- `<id>.json` — a stats sidecar for that history (record count, epoch span,
  median spacing, reportable gaps, quarantine flag, element-set range).

Regenerate with `lintle extract <id>`.
"""


class ExtractError(RuntimeError):
    """Operational failure (missing/torn dedup tree) — cli maps this to exit 2."""


def _import_chunks(out_dir: str) -> list[Path]:
    """The dedup import chunk set, index-ordered, each verified to hold whole
    140-byte records (a torn chunk must never yield sliced records — correctness
    over recovery)."""
    ddir = Path(out_dir) / DEDUP_DIRNAME
    chunks = ChunkedReader(ddir, IMPORT_STEM, IMPORT_SUFFIX).chunk_paths()
    if not chunks:
        raise ExtractError(
            f"no dedup import set under {ddir}.\n"
            "  run 'lintle dedup' first, or point at its --out-dir."
        )
    for chunk in chunks:
        if chunk.stat().st_size % RECORD_BYTES:
            raise ExtractError(
                f"{chunk} size is not a multiple of {RECORD_BYTES} bytes — "
                "corrupted or foreign import chunk; re-run 'lintle dedup'."
            )
    return chunks


def _quarantined_ids(out_dir: str) -> set[int] | None:
    """NORAD IDs quarantined during clean, from the run report's
    ``broken-noradids.ndjson`` — ``None`` (unknown, not false) when the report
    is absent or unreadable, e.g. a pruned tree."""
    path = Path(out_dir) / REPORT_DIRNAME / "broken-noradids.ndjson"
    if not path.is_file():
        return None
    try:
        return {
            json.loads(line)["noradId"]
            for line in path.read_text(encoding="ascii").splitlines()
            if line
        }
    except json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError, OSError:
        term.warning(f"unreadable {path.name} — quarantine info unavailable")
        return None


def _catalog_at(fh, index: int) -> int:
    """Catalog of record ``index`` in an open chunk (one 140-byte seek+read)."""
    fh.seek(index * RECORD_BYTES)
    line1 = fh.read(RECORD_BYTES)[:69].decode("ascii", errors="replace")
    cat = catalog_of(line1)
    if cat is None:
        raise ExtractError("unparseable catalog in import chunk — corrupted set")
    return cat


def find_spans(out_dir: str, catalog: int) -> list[tuple[Path, int, int]]:
    """Locate ``catalog``'s contiguous run as per-chunk half-open record-index
    ranges ``(chunk_path, lo, hi)`` — ``[]`` if absent. Bisects inside each
    candidate chunk; a run may straddle consecutive chunks (fixed-count rolls
    ignore catalog boundaries)."""
    spans: list[tuple[Path, int, int]] = []
    for chunk in _import_chunks(out_dir):
        n = chunk.stat().st_size // RECORD_BYTES
        if n == 0:
            continue
        with open(chunk, "rb") as fh:
            if _catalog_at(fh, 0) > catalog or _catalog_at(fh, n - 1) < catalog:
                continue
            lo = _bisect(fh, n, lambda c: c >= catalog)
            hi = _bisect(fh, n, lambda c: c > catalog)
        if hi > lo:
            spans.append((chunk, lo, hi))
    return spans


def _bisect(fh, n: int, pred) -> int:
    """First record index whose catalog satisfies ``pred`` (monotone over the
    sorted stream), or ``n`` if none does."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(_catalog_at(fh, mid)):
            hi = mid
        else:
            lo = mid + 1
    return lo


# Largest RECORD_BYTES-multiple <= 1 MiB — constant-memory streaming copy that
# never splits a record across a block boundary (Critical Rule #3 + #4: a
# misaligned block would decode stats from the middle of a record).
_COPY_BLOCK = (1 << 20) // RECORD_BYTES * RECORD_BYTES


def _epoch_dt(line1: str) -> _dt.datetime:
    """Record epoch as an aware UTC datetime — pure arithmetic from
    ``parse_epoch``'s ``(year, day_of_year)``; no wall clock, so sidecar bytes
    stay deterministic."""
    year, day = parse_epoch(line1)
    return _dt.datetime(year, 1, 1, tzinfo=_dt.UTC) + _dt.timedelta(days=day - 1)


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# A gap is reportable when the inter-epoch delta exceeds GAP_FACTOR x the
# satellite's own median spacing; the report keeps the GAPS_CAP largest.
GAP_FACTOR = 10
GAPS_CAP = 10


@dataclasses.dataclass(slots=True, frozen=True)
class Gap:
    """One reportable hole in a satellite's history."""

    start: _dt.datetime
    end: _dt.datetime
    days: float


@dataclasses.dataclass(slots=True, frozen=True)
class HistoryStats:
    """Pass-1 analysis of one satellite's deduped span — everything the
    sidecar and the warn/confirm flow need, computed before a byte is
    exported."""

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


def _analyze(spans: list[tuple[Path, int, int]]) -> HistoryStats:
    """Read ``spans`` (no writing) and compute the history stats. Holds one
    satellite's epoch list in memory — bounded (tens of thousands of records,
    ~hundreds of KB worst case), not a corpus file, so Critical Rule #3's
    streaming mandate is not in play."""
    epochs: list[_dt.datetime] = []
    elset_first = elset_last = None
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                remaining -= len(block)
                for off in range(0, len(block), RECORD_BYTES):
                    line1 = block[off : off + 69].decode("ascii")
                    if not epochs:
                        elset_first = element_set(line1)
                    elset_last = element_set(line1)
                    epochs.append(_epoch_dt(line1))
    deltas = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(epochs, epochs[1:], strict=False)
    ]
    largest = max(deltas, default=0.0)
    largest_at = epochs[deltas.index(largest) + 1] if deltas else None
    median = statistics.median(deltas) if len(deltas) >= 2 else None
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
        elset_first=elset_first,
        elset_last=elset_last,
        largest_gap_days=largest,
        largest_gap_at=largest_at,
        median_spacing_days=median,
        gaps=tuple(sorted(top, key=lambda g: g.start)),
        gap_count=len(reportable),
    )


def _copy_spans(spans: list[tuple[Path, int, int]], out) -> None:
    """Pass 2: verbatim byte copy of ``spans`` into the open binary file
    ``out`` — no decoding, no stats (pass 1 already has them)."""
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                out.write(block)
                remaining -= len(block)


def _sidecar(
    out_dir: str, catalog: int, hs: HistoryStats, had_quarantined: bool | None
) -> str:
    """The ``<id>.json`` document (sorted keys, 2-space indent, trailing LF —
    the house deterministic-JSON shape). Schema v2 adds the gap-awareness
    fields; ``had_quarantined`` is tri-state (None = clean report absent)."""
    span = (hs.last - hs.first).total_seconds() / 86400.0
    summary_path = Path(out_dir) / DEDUP_DIRNAME / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="ascii"))
    doc = {
        "schema_version": "2",
        "norad_id": catalog,
        "records": hs.count,
        "first_epoch": _iso(hs.first),
        "last_epoch": _iso(hs.last),
        "span_days": round(span, 6),
        "mean_records_per_day": round(hs.count / span, 6) if span else None,
        "largest_gap_days": round(hs.largest_gap_days, 6),
        "largest_gap_at": (
            _iso(hs.largest_gap_at) if hs.largest_gap_at is not None else None
        ),
        "median_spacing_days": (
            round(hs.median_spacing_days, 6)
            if hs.median_spacing_days is not None
            else None
        ),
        "gap_count": hs.gap_count,
        "gaps": [
            {"start": _iso(g.start), "end": _iso(g.end), "days": round(g.days, 6)}
            for g in hs.gaps
        ],
        "had_quarantined_records": had_quarantined,
        "element_set_first": hs.elset_first,
        "element_set_last": hs.elset_last,
        "source": {
            "out_dir": str(Path(out_dir)),
            "dedup_records_written": summary.get("records_written"),
            "dedup_schema_version": summary.get("schema_version"),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _warn_and_confirm(
    catalog: int, hs: HistoryStats, had_quarantined: bool | None
) -> bool:
    """Report the gaps (the GAPS_CAP largest, chronologically) and the
    quarantine flag, then ask to continue. Non-interactive runs warn and
    proceed; Enter and an unusable answer (EOF) both take the default —
    proceed. Only an explicit "n" skips."""
    if hs.gap_count:
        term.warning(
            f"history for {catalog} has {hs.gap_count} gap(s) "
            f"(median spacing {hs.median_spacing_days:.2f} d):"
        )
        for g in hs.gaps:
            term.note(f"  {g.start.date()} → {g.end.date()}  ({g.days:.1f} d)")
        if hs.gap_count > len(hs.gaps):
            term.note(f"  …and {hs.gap_count - len(hs.gaps)} more")
    if had_quarantined:
        term.warning(
            f"records for {catalog} were quarantined during clean — gaps may "
            f"stem from that; see {REPORT_DIRNAME}/report.jsonl"
        )
    if not term.is_interactive():
        return True
    answer = term.prompt_yes_no(f"continue export of {catalog}? [Y/n] ", default=True)
    return answer is not False


def _extract_one(
    out_dir: str, catalog: int, dest: Path, quarantined: set[int] | None
) -> str:
    """Extract one satellite in two passes: analyze the span read-only, then
    stream its byte range verbatim to ``<dest>/<id>.txt`` (durable
    temp-then-rename) and write the stats sidecar. ``<id>.txt`` + ``<id>.json``
    are one atomic unit: a failure anywhere in the txt-stream + txt-commit +
    sidecar-write sequence leaves nothing behind for this run's attempted
    output, and pre-existing files from an earlier successful run are never
    touched. Returns "written", "declined" (operator declined), or "absent"
    (no records)."""
    spans = find_spans(out_dir, catalog)
    if not spans:
        return "absent"
    hs = _analyze(spans)
    had_quarantined = None if quarantined is None else catalog in quarantined
    if (hs.gap_count or had_quarantined) and not _warn_and_confirm(
        catalog, hs, had_quarantined
    ):
        return "declined"
    txt = dest / f"{catalog}.txt"
    tmp = str(txt) + fsutil.PARTIAL_SUFFIX
    sidecar_partial = str(dest / f"{catalog}.json") + fsutil.PARTIAL_SUFFIX
    committed = False
    try:
        with open(tmp, "wb") as out:
            _copy_spans(spans, out)
        fsutil.durable_replace(tmp, str(txt))
        committed = True
        fsutil.durable_write_text(
            str(dest / f"{catalog}.json"),
            _sidecar(out_dir, catalog, hs, had_quarantined),
            encoding="ascii",
        )
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        Path(sidecar_partial).unlink(missing_ok=True)
        if committed:
            Path(txt).unlink(missing_ok=True)
            Path(dest / f"{catalog}.json").unlink(missing_ok=True)
        raise
    return "written"


def run(
    out_dir: str, catalogs: list[int], dest: str, *, write_readme: bool = False
) -> int:
    """Extract each catalog's history into ``dest``. Exit 0 if every id was
    found; 2 if any was absent, any catalog's extraction raised, or on an
    operational error up front (missing/torn dedup tree — nothing written at
    all). A raise mid-catalog is isolated: it is reported, the partial temp is
    never left behind, and the remaining catalogs still run. A user-declined
    skip is not an error (exit stays 0). ``write_readme`` is False by default
    — an explicit ``--dest`` is the user's own directory and is never
    decorated; the cli passes True only when ``dest`` resolved to the default
    ``<out-dir>/06-extract`` (Task 3)."""
    _import_chunks(out_dir)  # raises ExtractError before any per-catalog work
    quarantined = _quarantined_ids(out_dir)
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if write_readme:
        fsutil.durable_write_text(
            str(dest_dir / "README.md"), _README, encoding="utf-8"
        )
    missing = []
    for catalog in catalogs:
        try:
            outcome = _extract_one(out_dir, catalog, dest_dir, quarantined)
        except Exception as exc:
            term.error(f"extraction failed for catalog {catalog}: {exc}")
            missing.append(catalog)
            continue
        match outcome:
            case "written":
                term.note(f"wrote {dest_dir / f'{catalog}.txt'}")
            case "declined":
                term.note(f"skipped {catalog}")
            case "absent":
                missing.append(catalog)
                term.error(
                    f"no records for catalog {catalog} in "
                    f"{Path(out_dir) / DEDUP_DIRNAME}"
                )
    return 2 if missing else 0
