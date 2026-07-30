"""Stream cleaned TLE records from a clean-run output tree. Cleaned files are
canonical 2-line ASCII TLEs in source order, one file per input; we pair the
lines back up and tag each record with its catalog id, chronological key, source
stem, and ordinal — the handle every verify pass needs. Streaming only: a 3.2 GB
input's cleaned twin is never held whole."""

import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path

from lintle import CLEANED_DIRNAME, CLEANED_SUFFIX, chunking, epoch, tle

# Distinct-stem parse for the chunked layout: <stem>.NNNNN.cleaned.txt. A
# right-anchored regex on the 5-digit index + literal suffix (not a fixed-length
# tail slice) is unambiguous even for a stem that itself ends in .NNNNN and skips
# any stray non-matching file.
_CLEANED_STEM_RE = re.compile(
    r"^(?P<stem>.+)\.\d{5}" + re.escape(CLEANED_SUFFIX) + r"$"
)


@dataclasses.dataclass(slots=True, frozen=True)
class CleanedRecord:
    """One cleaned 2-line record. ``catalog`` is ``-1`` and ``epoch_key`` is
    ``-1.0`` when line 1 could not be parsed — the re-validate pass turns that
    into a ``VRFY-REVALIDATE-FAIL`` rather than trusting a bogus key."""

    catalog: int
    epoch_key: float
    line1: str
    line2: str
    src_file: str
    index: int


def catalog_of(line1: str) -> int | None:
    """The integer NORAD catalog from line-1 cols 3-7, tolerant of the
    space-padded form space-track uses for low numbers (``'  836'`` -> 836) that
    ``tle.extract_norad_id`` — a strict 5-digit contract for the clean path's
    broken-id output — reports as ``None``. Alpha-5 letter-prefixed ids (catalog
    >= 100000, absent from this 2004-2020 corpus) stay ``None``."""
    if len(line1) < 7 or not line1.startswith("1 "):
        return None
    field = line1[2:7].strip()
    return int(field) if tle.is_ascii_digits(field) else None


def _catalog_and_key(line1: str) -> tuple[int, float]:
    """Best-effort (catalog, epoch_key); (-1, -1.0) if line 1 is unparseable.
    Never raises — a broken cleaned line is a finding, not a crash."""
    try:
        catalog = catalog_of(line1)
        key = epoch.epoch_key(line1)
    except ValueError, IndexError:
        return -1, -1.0
    return (catalog if catalog is not None else -1), key


def cleaned_stems(out_dir: str) -> list[str]:
    """Sorted distinct stems of the cleaned chunk sets under
    ``<out-dir>/01-cleaned`` (``tle2019`` for the ``tle2019.NNNNN.cleaned.txt``
    set); ``[]`` if the directory is absent. Each stem's chunks collapse to one
    entry."""
    cleaned_dir = Path(out_dir) / CLEANED_DIRNAME
    if not cleaned_dir.is_dir():
        return []
    stems = set()
    for p in cleaned_dir.glob("*" + CLEANED_SUFFIX):
        if not p.is_file():
            continue
        m = _CLEANED_STEM_RE.match(p.name)
        if m is not None:
            stems.add(m.group("stem"))
    return sorted(stems)


def cleaned_fingerprint(out_dir: str) -> dict:
    """A cheap structural fingerprint of ``01-cleaned`` — each cleaned stem and
    its total chunk-byte size (``stat`` only, no reads). Lets a downstream run
    (``extract``) detect that ``cleaned/`` changed since a ``dedup`` run
    without re-hashing the ~30 GB corpus; staleness, not bit-rot, is the
    threat this guards against. ``{"stems": []}`` if ``01-cleaned`` is absent."""
    cdir = Path(out_dir) / CLEANED_DIRNAME
    fp = [
        [
            s,
            sum(
                p.stat().st_size
                for p in chunking.ChunkedReader(cdir, s, CLEANED_SUFFIX).chunk_paths()
            ),
        ]
        for s in cleaned_stems(out_dir)
    ]
    return {"stems": sorted(fp)}


def iter_file(out_dir: str, file_stem: str) -> Iterator[CleanedRecord]:
    """Yield the cleaned records of one file's chunk set, in on-disk (source)
    order across the whole set as one logical stream. Streams one chunk at a
    time (constant memory)."""
    reader = chunking.ChunkedReader(
        Path(out_dir) / CLEANED_DIRNAME, file_stem, CLEANED_SUFFIX
    )
    lines = reader.iter_lines()
    index = 0
    for raw1 in lines:
        raw2 = next(lines, None)
        if raw2 is None:
            break  # odd trailing half-line: no pair to check
        line1 = raw1.decode("ascii", errors="replace")
        line2 = raw2.decode("ascii", errors="replace")
        catalog, key = _catalog_and_key(line1)
        yield CleanedRecord(catalog, key, line1, line2, file_stem, index)
        index += 1  # noqa: SIM113 — two lines consumed per record, not one-per-item
