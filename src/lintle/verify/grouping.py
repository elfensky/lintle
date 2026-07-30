"""Constant-memory external merge sort. Items stream in via
:meth:`ExternalSorter.add`; each full chunk is sorted and spilled to a temp file,
then :meth:`sorted_records` k-way-merges the runs with :func:`heapq.merge`. Peak
memory is one chunk, so a 30 GB corpus's worth of records never lands in RAM at
once. The sorter is parameterised by ``key``/``encode``/``decode``; its two
adapters are :func:`record_sorter` — whose sorted stream groups every satellite's
records together and in epoch order, what the contradiction, dedup and sgp4
passes consume — and ``verify.report``'s suspect sink."""

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


def record_key(rec: CleanedRecord) -> tuple[int, float]:
    """A cleaned record's ``(catalog, epoch_key)`` identity — the external sort
    key, dedup's group key, and the contradiction pass's group key. One
    definition: a divergent second spelling would silently regroup the corpus."""
    return (rec.catalog, rec.epoch_key)


def _encode_record(rec: CleanedRecord) -> str:
    # TLE lines contain no tab or newline, so a tab-delimited row round-trips
    # exactly. repr(epoch_key) preserves the float bit-for-bit.
    return (
        f"{rec.catalog}\t{rec.epoch_key!r}\t{rec.index}\t"
        f"{rec.src_file}\t{rec.line1}\t{rec.line2}\n"
    )


def _decode_record(row: str) -> CleanedRecord:
    catalog, key, index, src_file, line1, line2 = row.rstrip("\n").split("\t")
    return CleanedRecord(int(catalog), float(key), line1, line2, src_file, int(index))


class ExternalSorter[T, K]:
    """Spill-to-disk sorter over any item type: ``add`` items during the
    streaming pass, then iterate ``sorted_records()`` once; temp runs are deleted
    as they drain. ``key`` orders items and ``encode``/``decode`` round-trip one
    item through a temp-file line (the encoding must not contain a bare newline).
    Equal keys come back in **add order** — runs merge in spill order with the
    in-memory tail last, and :func:`heapq.merge` breaks ties by iterable order.

    The three callables are held as *instance* attributes so they stay plain
    functions; as class-level defaults they would bind as methods and swallow
    ``self`` as their first argument."""

    def __init__(
        self,
        *,
        key: Callable[[T], K],
        encode: Callable[[T], str],
        decode: Callable[[str], T],
        chunk_size: int = 200_000,
        prefix: str = "lintle-verify-sort-",
    ) -> None:
        self._key = key
        self._encode = encode
        self._decode = decode
        self._chunk_size = chunk_size
        self._buf: list[T] = []
        self._runs: list[Path] = []
        self._tmpdir = tempfile.TemporaryDirectory(prefix=prefix)

    def add(self, item: T) -> None:
        self._buf.append(item)
        if len(self._buf) >= self._chunk_size:
            self._spill()

    def _spill(self) -> None:
        self._buf.sort(key=self._key)
        path = Path(self._tmpdir.name) / f"run-{len(self._runs):06d}.tsv"
        with path.open("w", encoding="ascii") as fh:
            fh.writelines(self._encode(item) for item in self._buf)
        self._runs.append(path)
        self._buf = []

    def sorted_records(self) -> Iterator[T]:
        """K-way-merge every spilled run plus the in-memory tail, in ``key``
        order. Consumes the sorter; closes the run handles and removes the temp
        directory when the iterator is exhausted *or closed* — wrap a drain that
        may stop early in :func:`contextlib.closing`, so an abandoned iterator
        releases its temp files now rather than whenever it is collected."""
        handles = [p.open(encoding="ascii") for p in self._runs]
        try:
            runs = [(self._decode(row) for row in fh) for fh in handles]
            self._buf.sort(key=self._key)
            yield from heapq.merge(*runs, iter(self._buf), key=self._key)
        finally:
            for fh in handles:
                fh.close()
            self._tmpdir.cleanup()


def record_sorter(
    chunk_size: int = 200_000,
) -> ExternalSorter[CleanedRecord, tuple[int, float]]:
    """An :class:`ExternalSorter` over cleaned records keyed by
    :func:`record_key` — the flavour every cleaned-tree pass wants."""
    return ExternalSorter(
        key=record_key,
        encode=_encode_record,
        decode=_decode_record,
        chunk_size=chunk_size,
    )
