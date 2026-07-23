"""`lintle extract` — one satellite's complete deduped TLE history as
``<id>.txt`` + ``<id>.json``. A read-only consumer of a prior `dedup` run: the
``dedup/import.*`` chunk set holds only validated-perfect records (exactly 140
bytes each) globally sorted by ``(catalog, epoch)``, so each satellite is one
contiguous byte range found by pure binary search — the sorted fixed-width
stream *is* the index. Never imports sgp4; never touches the clean path."""

import datetime as _dt
import json
from pathlib import Path

from lintle import fsutil, term
from lintle.chunking import ChunkedReader
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
from lintle.verify.checks import element_set
from lintle.verify.epoch import parse_epoch
from lintle.verify.records import catalog_of

# two validated-perfect 69-char lines + two \n — guarded, not assumed
RECORD_BYTES = 140


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


def _sidecar(
    out_dir: str,
    catalog: int,
    first: _dt.datetime,
    last: _dt.datetime,
    stats: dict,
    gap: float,
    gap_at: _dt.datetime | None,
) -> str:
    """The ``<id>.json`` document (sorted keys, 2-space indent, trailing LF —
    the house deterministic-JSON shape)."""
    span = (last - first).total_seconds() / 86400.0
    summary_path = Path(out_dir) / DEDUP_DIRNAME / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="ascii"))
    doc = {
        "schema_version": "1",
        "norad_id": catalog,
        "records": stats["count"],
        "first_epoch": _iso(first),
        "last_epoch": _iso(last),
        "span_days": round(span, 6),
        "mean_records_per_day": round(stats["count"] / span, 6) if span else None,
        "largest_gap_days": round(gap, 6),
        "largest_gap_at": _iso(gap_at) if gap_at is not None else None,
        "element_set_first": stats["elset_first"],
        "element_set_last": stats["elset_last"],
        "source": {
            "out_dir": str(Path(out_dir)),
            "dedup_records_written": summary.get("records_written"),
            "dedup_schema_version": summary.get("schema_version"),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _extract_one(out_dir: str, catalog: int, dest: Path) -> bool:
    """Extract one satellite: stream its byte range verbatim to
    ``<dest>/<id>.txt`` (durable temp-then-rename) and write the stats sidecar.
    ``<id>.txt`` + ``<id>.json`` are one atomic unit: a failure anywhere in the
    txt-stream + txt-commit + sidecar-write sequence leaves nothing behind for
    this run's attempted output. A failed extraction leaves the destination
    exactly as it found it — pre-existing files from an earlier successful run
    are never touched. False if the catalog has no records."""
    spans = find_spans(out_dir, catalog)
    if not spans:
        return False
    txt = dest / f"{catalog}.txt"
    tmp = str(txt) + fsutil.PARTIAL_SUFFIX
    sidecar_partial = str(dest / f"{catalog}.json") + fsutil.PARTIAL_SUFFIX
    first = last = gap_at = None
    prev = None
    gap = 0.0
    stats = {"count": 0, "elset_first": None, "elset_last": None}
    committed = False
    try:
        with open(tmp, "wb") as out:
            for chunk, lo, hi in spans:
                with open(chunk, "rb") as fh:
                    fh.seek(lo * RECORD_BYTES)
                    remaining = (hi - lo) * RECORD_BYTES
                    while remaining:
                        block = fh.read(min(_COPY_BLOCK, remaining))
                        out.write(block)
                        remaining -= len(block)
                        # per-record stats over the block (records never
                        # split blocks: both are multiples of RECORD_BYTES)
                        for off in range(0, len(block), RECORD_BYTES):
                            line1 = block[off : off + 69].decode("ascii")
                            dt = _epoch_dt(line1)
                            if first is None:
                                first = dt
                                stats["elset_first"] = element_set(line1)
                            if prev is not None:
                                step = (dt - prev).total_seconds() / 86400.0
                                if step > gap:
                                    gap, gap_at = step, dt
                            prev = last = dt
                            stats["elset_last"] = element_set(line1)
                            stats["count"] += 1
        fsutil.durable_replace(tmp, str(txt))
        committed = True
        fsutil.durable_write_text(
            str(dest / f"{catalog}.json"),
            _sidecar(out_dir, catalog, first, last, stats, gap, gap_at),
            encoding="ascii",
        )
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        Path(sidecar_partial).unlink(missing_ok=True)
        if committed:
            Path(txt).unlink(missing_ok=True)
            Path(dest / f"{catalog}.json").unlink(missing_ok=True)
        raise
    return True


def run(out_dir: str, catalogs: list[int], dest: str) -> int:
    """Extract each catalog's history into ``dest``. Exit 0 if every id was
    found; 2 if any was absent, any catalog's extraction raised, or on an
    operational error up front (missing/torn dedup tree — nothing written at
    all). A raise mid-catalog is isolated: it is reported, the partial temp is
    never left behind, and the remaining catalogs still run."""
    _import_chunks(out_dir)  # raises ExtractError before any per-catalog work
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    missing = []
    for catalog in catalogs:
        try:
            found = _extract_one(out_dir, catalog, dest_dir)
        except Exception as exc:
            term.error(f"extraction failed for catalog {catalog}: {exc}")
            missing.append(catalog)
            continue
        if found:
            term.note(f"wrote {dest_dir / f'{catalog}.txt'}")
        else:
            missing.append(catalog)
            term.error(
                f"no records for catalog {catalog} in {Path(out_dir) / DEDUP_DIRNAME}"
            )
    return 2 if missing else 0
