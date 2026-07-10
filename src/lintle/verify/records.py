"""Stream cleaned TLE records from a clean-run output tree. Cleaned files are
canonical 2-line ASCII TLEs in source order, one file per input; we pair the
lines back up and tag each record with its catalog id, chronological key, source
stem, and ordinal — the handle every verify pass needs. Streaming only: a 3.2 GB
input's cleaned twin is never held whole."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path

from lintle import CLEANED_DIRNAME, CLEANED_SUFFIX
from lintle.verify import epoch


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
    return int(field) if field.isdigit() else None


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
    """Sorted stems of the cleaned files under ``<out-dir>/cleaned`` (``tle2019``
    for ``tle2019.cleaned.txt``); ``[]`` if the directory is absent."""
    cleaned_dir = Path(out_dir) / CLEANED_DIRNAME
    if not cleaned_dir.is_dir():
        return []
    return sorted(
        p.name[: -len(CLEANED_SUFFIX)]
        for p in cleaned_dir.glob("*" + CLEANED_SUFFIX)
        if p.is_file()
    )


def iter_file(out_dir: str, file_stem: str) -> Iterator[CleanedRecord]:
    """Yield the cleaned records of one file, in on-disk (source) order."""
    path = Path(out_dir) / CLEANED_DIRNAME / (file_stem + CLEANED_SUFFIX)
    with path.open(encoding="ascii", errors="replace") as fh:
        index = 0
        while True:
            line1 = fh.readline()
            line2 = fh.readline()
            if not line2:
                break  # clean odd trailing half-line: no pair to check
            line1 = line1.rstrip("\n")
            line2 = line2.rstrip("\n")
            catalog, key = _catalog_and_key(line1)
            yield CleanedRecord(catalog, key, line1, line2, file_stem, index)
            index += 1
