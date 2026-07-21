"""Fixed-count output chunking: split every record/line stream the pipeline
writes into per-stream chunks of ``units_per_chunk`` units, so no single output
file is ever huge (spec 2026-07-21-output-chunking-design). Stdlib-only leaf —
imports only ``fsutil`` for the per-chunk atomic-durable commit; never ``sgp4``.

The design rides on ``clean``'s one-worker-per-input-file partition and adds no
shared state: each chunk set is counted by its single owning writer, boundaries
fall every N units deterministically, and each chunk is committed atomically via
:func:`fsutil.durable_replace` the instant it fills. Concatenating a set's chunks
in index order is byte-identical to the pre-chunking single file (Critical Rules
#1/#2). Constant memory (#3): the writer holds one open chunk, the reader streams
one chunk at a time."""

import contextlib
import re
from pathlib import Path

from lintle import fsutil

# One chunk ≈ this many records (~140 MB at ~140 B/record). ponytail: tunable
# knob, threaded from the --chunk-records flag; a plain constant is enough until
# repeated use asks for a config key.
CHUNK_RECORDS_DEFAULT = 1_000_000

# 5-digit zero-padded index → 99 999 chunks per stream. Rolling past it is a hard
# error (never a silent .100000 that breaks lexical==numeric order); widen the pad
# here if a future corpus ever needs more.
MAX_CHUNK_INDEX = 99_999


class ChunkedWriter:
    """Streaming writer over *logical units* that rolls to a new chunk file
    every ``units_per_chunk`` units. A unit is one ``write()`` call — one 2-line
    record (:meth:`write_record`) or one JSONL line (:meth:`write_line`) — so the
    record boundary is structural: a record is never split across a chunk.

    Names are ``{stem}.{index:05d}{suffix}`` (1-based, zero-padded), so lexical
    order equals numeric order. Each chunk is written to a ``.partial`` temp and
    :func:`fsutil.durable_replace`-committed on roll/close, so a crash leaves a
    set of complete committed chunks plus at most one discarded temp — never a
    torn file. ``units_per_chunk == 0`` (or ``None``) never rolls (a single
    ``.00001`` chunk). An empty stream still emits one empty ``.00001`` chunk, so
    a stream is always a non-empty set on disk. Use as a context manager: a clean
    exit commits the final chunk, an exception discards the in-progress temp while
    keeping the already-committed chunks."""

    def __init__(
        self,
        directory,
        stem,
        suffix,
        units_per_chunk=CHUNK_RECORDS_DEFAULT,
        *,
        scrub_existing=True,
    ):
        self._dir = Path(directory)
        self._stem = stem
        self._suffix = suffix
        self._limit = units_per_chunk or 0  # 0/None → never roll
        self._scrub_existing = scrub_existing
        self._index = 0  # last opened chunk index
        self._count = 0  # units in the currently open chunk
        self._handle = None
        self._tmp = None
        self._final = None
        self._opened_any = False
        self._closed = False

    def __enter__(self):
        return self

    def _open_next(self):
        """Open the next chunk's ``.partial`` temp for writing."""
        if self._index == 0 and self._scrub_existing:
            # First chunk: scrub any pre-existing set for this stem so a shorter
            # re-run / resume redo never orphans a longer prior run's high-index
            # tail (spec invariant 5). A fresh run's dir was already cleared, so
            # this glob is a cheap no-op there.
            existing = ChunkedReader(self._dir, self._stem, self._suffix)
            for path in existing.chunk_paths():
                with contextlib.suppress(OSError):
                    path.unlink()
        if self._index >= MAX_CHUNK_INDEX:
            raise ValueError(
                f"chunk index would exceed {MAX_CHUNK_INDEX} for stem {self._stem!r}; "
                f"raise --chunk-records or widen the index pad in chunking.py"
            )
        self._index += 1
        self._final = self._dir / f"{self._stem}.{self._index:05d}{self._suffix}"
        self._tmp = str(self._final) + ".partial"
        self._handle = open(self._tmp, "wb")  # noqa: SIM115 — streaming; closed on roll/commit/discard
        self._count = 0
        self._opened_any = True

    def _commit(self):
        """Durably commit the currently open chunk to its final name."""
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
        fsutil.durable_replace(self._tmp, str(self._final))

    def _discard(self):
        """Drop the in-progress chunk's temp without committing (crash/exception
        path); already-committed chunks are durable and stay."""
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None
        if self._tmp is not None:
            with contextlib.suppress(OSError):
                Path(self._tmp).unlink()

    def discard_all(self):
        """Abandon the whole set: drop the in-progress temp AND unlink every
        chunk this writer already committed. Restores per-file atomicity when a
        write must be thrown away mid-stream (a failed input file must leave no
        cleaned output, matching the pre-chunking single-file behaviour)."""
        self._discard()
        for path in ChunkedReader(self._dir, self._stem, self._suffix).chunk_paths():
            with contextlib.suppress(OSError):
                path.unlink()

    def write(self, payload: bytes):
        """Write one unit's raw bytes, rolling to a new chunk first if the
        current one has reached ``units_per_chunk`` (the roll happens *before*
        writing unit N+1, so an exact multiple leaves no trailing empty chunk)."""
        if self._handle is None:
            self._open_next()
        elif self._limit and self._count >= self._limit:
            self._commit()
            self._open_next()
        self._handle.write(payload)
        self._count += 1

    def write_raw(self, payload: bytes):
        """Write raw preamble bytes to the first chunk *without* counting a unit
        — for a file header that must live in ``.00001`` but is not a record, so
        the roll boundary still falls every ``units_per_chunk`` records. Opens
        ``.00001`` if nothing is open yet; intended for use before any units."""
        if self._handle is None:
            self._open_next()
        self._handle.write(payload)

    def write_record(self, line1: bytes, line2: bytes):
        """Write one 2-line TLE record (two ``\\n``-terminated lines), counting 1
        unit — the roll never splits a record across a chunk."""
        self.write(line1 + b"\n" + line2 + b"\n")

    def write_line(self, text: str):
        """Write one ``\\n``-terminated UTF-8 line, counting 1 unit. Binary write
        of explicit ``\\n`` keeps the artifact byte-deterministic across platforms."""
        self.write((text + "\n").encode("utf-8"))

    def close(self):
        """Commit the final in-progress chunk; emit one empty ``.00001`` if the
        stream was empty. Idempotent."""
        if self._closed:
            return
        if not self._opened_any:
            self._open_next()
        self._commit()
        self._closed = True

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self._discard()
        return False


class ChunkedReader:
    """Reads a stem's chunk set as one logical stream. Globs ``{stem}.*{suffix}``,
    parses the 5-digit index, sorts by it, and yields lines across the whole set
    in index order. The right-anchored parse (``^stem\\.(\\d{5})suffix$``) makes
    the set unambiguous even when another stem's name is a prefix (``tle`` vs
    ``tle.00001``) and ignores non-matching files (``summary.json``)."""

    def __init__(self, directory, stem, suffix):
        self._dir = Path(directory)
        self._stem = stem
        self._suffix = suffix
        self._rx = re.compile(rf"^{re.escape(stem)}\.(\d{{5}}){re.escape(suffix)}$")

    def chunk_paths(self):
        """Return the set's chunk paths sorted by index (== lexical order)."""
        matches = []
        for path in self._dir.glob(f"{self._stem}.*{self._suffix}"):
            m = self._rx.match(path.name)
            if m is not None:
                matches.append((int(m.group(1)), path))
        matches.sort()
        return [path for _, path in matches]

    def iter_lines(self):
        """Yield each logical line as ``bytes`` (trailing ``\\n`` stripped),
        streaming one chunk at a time (constant memory)."""
        for path in self.chunk_paths():
            with open(path, "rb") as handle:
                for line in handle:
                    yield line.rstrip(b"\n")
