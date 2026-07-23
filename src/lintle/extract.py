"""`lintle extract` — one satellite's complete deduped TLE history as
``<id>.txt`` + ``<id>.json``. A read-only consumer of a prior `dedup` run: the
``dedup/import.*`` chunk set holds only validated-perfect records (exactly 140
bytes each) globally sorted by ``(catalog, epoch)``, so each satellite is one
contiguous byte range found by pure binary search — the sorted fixed-width
stream *is* the index. Never imports sgp4; never touches the clean path."""

from pathlib import Path

from lintle.chunking import ChunkedReader
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
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
