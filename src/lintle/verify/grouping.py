"""Constant-memory external merge sort of cleaned records by ``(catalog,
epoch_key)``. Records stream in via :meth:`ExternalSorter.add`; each full chunk
is sorted and spilled to a temp file, then :meth:`sorted_records` k-way-merges
the runs with :func:`heapq.merge`. Peak memory is one chunk, so a 30 GB corpus's
worth of records never lands in RAM at once. The sorted stream groups every
satellite's records together and in epoch order — what the contradiction check
(and, later, the continuity/sgp4 pass) consumes."""

import heapq
import itertools
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from lintle.verify.records import CleanedRecord


def grouped[K](
    sorted_records: Iterable[CleanedRecord], key: Callable[[CleanedRecord], K]
) -> Iterator[tuple[K, list[CleanedRecord]]]:
    """Yield ``(key_value, records)`` groups from an already-sorted stream — the
    one streaming group-by shape shared by the orbit and dedup passes (each was
    previously a hand-rolled buffer + trailing-flush loop). Holds one group in
    memory at a time, so constant memory w.r.t. the corpus is preserved."""
    for k, group in itertools.groupby(sorted_records, key=key):
        yield k, list(group)


def _key(rec: CleanedRecord) -> tuple[int, float]:
    return (rec.catalog, rec.epoch_key)


def _encode(rec: CleanedRecord) -> str:
    # TLE lines contain no tab or newline, so a tab-delimited row round-trips
    # exactly. repr(epoch_key) preserves the float bit-for-bit.
    return (
        f"{rec.catalog}\t{rec.epoch_key!r}\t{rec.index}\t"
        f"{rec.src_file}\t{rec.line1}\t{rec.line2}\n"
    )


def _decode(row: str) -> CleanedRecord:
    catalog, key, index, src_file, line1, line2 = row.rstrip("\n").split("\t")
    return CleanedRecord(int(catalog), float(key), line1, line2, src_file, int(index))


class ExternalSorter:
    """Spill-to-disk sorter. ``add`` records during the streaming pass, then
    iterate ``sorted_records()`` once; temp runs are deleted as they drain."""

    def __init__(self, chunk_size: int = 200_000) -> None:
        self._chunk_size = chunk_size
        self._buf: list[CleanedRecord] = []
        self._runs: list[Path] = []
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lintle-verify-sort-")

    def add(self, rec: CleanedRecord) -> None:
        self._buf.append(rec)
        if len(self._buf) >= self._chunk_size:
            self._spill()

    def _spill(self) -> None:
        self._buf.sort(key=_key)
        path = Path(self._tmpdir.name) / f"run-{len(self._runs):06d}.tsv"
        with path.open("w", encoding="ascii") as fh:
            fh.writelines(_encode(r) for r in self._buf)
        self._runs.append(path)
        self._buf = []

    def sorted_records(self) -> Iterator[CleanedRecord]:
        """K-way-merge every spilled run plus the in-memory tail, in
        ``(catalog, epoch_key)`` order. Consumes the sorter; cleans up temp
        files when the iterator is exhausted."""
        handles = [p.open(encoding="ascii") for p in self._runs]
        try:
            run_streams = [(_decode(row) for row in fh) for fh in handles]
            self._buf.sort(key=_key)
            yield from heapq.merge(*run_streams, iter(self._buf), key=_key)
        finally:
            for fh in handles:
                fh.close()
            self._tmpdir.cleanup()
