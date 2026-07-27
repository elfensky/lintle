"""`lintle extract` — one satellite's complete deduped TLE history as
``<id>.txt`` + ``<id>.json``. A read-only consumer of a prior `dedup` run: the
``05-dedup/import.*`` chunk set holds only validated-perfect records (exactly 140
bytes each) globally sorted by ``(catalog, epoch)``, so each satellite is one
contiguous byte range found by pure binary search — the sorted fixed-width
stream *is* the index. Never imports sgp4; never touches the clean path. Warns
— and, interactively, confirms — before exporting a history with reportable
gaps or upstream-quarantined records."""

import json
from pathlib import Path

from rich.text import Text

from lintle import CLEANED_DIRNAME, REPORT_DIRNAME, cli_progress, fsutil, summary, term
from lintle.chunking import ChunkedReader
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
from lintle.history import HistoryStats, analyze_epochs
from lintle.history import epoch_dt as _epoch_dt
from lintle.history import iso as _iso
from lintle.verify.checks import element_set
from lintle.verify.records import catalog_of, cleaned_fingerprint

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


def _warn_if_stale(out_dir: str) -> None:
    """Warn — never fail — if ``01-cleaned`` drifted since the ``dedup`` run
    this extraction reads from, by comparing the stat-only structural
    fingerprint ``dedup`` stored in ``summary.json`` against a live recompute.
    No fingerprint stored (older run, or a hand-built dedup tree) means
    nothing to compare against, so it's silently skipped. Matches extract's
    existing warn-and-proceed philosophy (see :func:`_warn_and_confirm`); the
    exit code is untouched."""
    summary_path = Path(out_dir) / DEDUP_DIRNAME / "summary.json"
    if not summary_path.is_file():
        return
    stored = json.loads(summary_path.read_text(encoding="ascii")).get(
        "cleaned_fingerprint"
    )
    if stored is not None and stored != cleaned_fingerprint(out_dir):
        term.warning(
            f"{CLEANED_DIRNAME} changed since the last dedup run — extract results "
            "may be stale; re-run 'lintle dedup'."
        )


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


def _analyze(spans: list[tuple[Path, int, int]]) -> HistoryStats:
    """Read ``spans`` (no writing), decode epochs + element sets, and delegate
    the reduction to ``history.analyze_epochs``. Holds one satellite's lists in
    memory — bounded, not a corpus file (Critical Rule #3 not in play)."""
    epochs: list = []
    elsets: list[int | None] = []
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                remaining -= len(block)
                for off in range(0, len(block), RECORD_BYTES):
                    line1 = block[off : off + 69].decode("ascii")
                    elsets.append(element_set(line1))
                    epochs.append(_epoch_dt(line1))
    return analyze_epochs(epochs, elsets)


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
    are committed as one atomic unit, but the guarantee differs before and
    after the txt commit: a failure *before* ``durable_replace`` (the copy
    itself) leaves the destination exactly as found — pre-existing outputs
    from an earlier run are untouched. A failure *after* the txt commit (e.g.
    the sidecar write) rolls the pair back as a unit: both the just-written
    ``<id>.txt`` and any ``<id>.json`` at that path are removed, so a
    mismatched txt/json pair can never remain — even if that means removing a
    prior run's still-good pair on a failed re-run. Returns "written",
    "declined" (operator declined), or "absent" (no records)."""
    # One spinner over the locate and the read, not two: the bisect and the
    # analysis are the same silent stretch to anyone watching, and two
    # consecutive spinners would only flicker.
    with cli_progress.status(f"analyzing {catalog}…"):
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
        with cli_progress.status(f"writing {catalog}…"):
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


def _render_roster(catalogs: list[int]) -> None:
    """Print the phase-1 roster of requested NORAD ids — the same chrome as
    every other roster, minus a size column, because nothing is known about a
    satellite's history until its span has been located."""
    table = summary.results_table("#", "norad id")
    for index, catalog in enumerate(catalogs, start=1):
        table.add_row(str(index), Text(str(catalog)))
    term.stderr_console.print(table)


def _render_results(outcomes: list[tuple[int, str]], dest_dir: Path) -> None:
    """Print ``extract``'s phase-3 results table: one row per requested id with
    its records, epoch span, gap count, and outcome, then a total row over the
    ids actually written. Numbers come from the ``<id>.json`` sidecar each write
    just committed — a render of the artifact, never a second computation — so a
    skipped, absent, or failed id shows dashes rather than invented figures."""
    if not outcomes:
        return
    console = term.stderr_console
    tier = summary.display_tier(console.width)
    dash = "—" if summary.can_encode(console.encoding, "—") else "-"
    headers = ["#", "norad id", "records"]
    if tier == "wide":
        headers.append("span")
    if tier != "narrow":
        headers.append("gaps")
    headers.append("status")
    table = summary.results_table(*headers, justify={"status": "left"})
    total_records = total_gaps = 0
    for index, (catalog, status) in enumerate(outcomes, start=1):
        doc = _read_sidecar(dest_dir, catalog) if status == "written" else None
        if doc is None:
            cells = [dash] * (len(headers) - 3)
            table.add_row(str(index), Text(str(catalog)), dash, *cells, status)
            continue
        total_records += doc["records"]
        total_gaps += doc["gap_count"]
        cells = [f"{doc['records']:,}"]
        if tier == "wide":
            cells.append(f"{doc['span_days'] / 365.25:.1f}y")
        if tier != "narrow":
            cells.append(f"{doc['gap_count']:,}")
        table.add_row(str(index), Text(str(catalog)), *cells, status)
    table.add_section()
    cells = [f"{total_records:,}"]
    if tier == "wide":
        cells.append("")
    if tier != "narrow":
        cells.append(f"{total_gaps:,}")
    table.add_row("", "total", *cells, "")
    console.print(table)


def _read_sidecar(dest_dir: Path, catalog: int) -> dict | None:
    """Return the just-written ``<id>.json`` as a dict, or ``None`` if it cannot
    be read — the results table is cosmetic and must never fail a good run."""
    try:
        return json.loads((dest_dir / f"{catalog}.json").read_text(encoding="ascii"))
    except OSError, ValueError:
        return None


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
    _warn_if_stale(out_dir)
    # Spinner: reads and parses the whole broken-noradids.ndjson before any
    # per-catalog work, which on a corpus run is far from instant.
    with cli_progress.status("reading quarantine info…"):
        quarantined = _quarantined_ids(out_dir)
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if write_readme:
        fsutil.durable_write_text(
            str(dest_dir / "README.md"), _README, encoding="utf-8"
        )
    # Phase 1 — discovery, only when there is something to orient: a one-row
    # roster above a one-row result table is noise, not orientation.
    if len(catalogs) > 1:
        _render_roster(catalogs)

    missing = []
    outcomes: list[tuple[int, str]] = []
    for catalog in catalogs:
        try:
            outcome = _extract_one(out_dir, catalog, dest_dir, quarantined)
        except Exception as exc:
            term.error(f"extraction failed for catalog {catalog}: {exc}")
            missing.append(catalog)
            outcomes.append((catalog, "failed"))
            continue
        outcomes.append((catalog, outcome))
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
            case _:  # pragma: no cover — outcome set is closed
                raise AssertionError(f"unknown outcome {outcome!r}")
    # Phase 3 — results, rendered from the sidecars just committed rather than
    # recomputed, so the table cannot disagree with the artifacts on disk.
    _render_results(outcomes, dest_dir)
    return 2 if missing else 0
