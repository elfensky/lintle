"""Streaming I/O: read a file, pair lines into records, route them."""

import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from lintle import (
    BROKEN_DIRNAME,
    BROKEN_SUFFIX,
    CLEANED_DIRNAME,
    CLEANED_SUFFIX,
    SHARDS_DIRNAME,
    chunking,
    repair,
    report,
    report_writers,
    stem,
    tle,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.diagnostics import Diagnostic, RuleID, diagnostic


@dataclasses.dataclass(slots=True)
class RecordCandidate:
    """A line-1 / line-2 pair, with their 1-indexed source line numbers."""

    raw_line1: bytes
    raw_line2: bytes
    src1: int
    src2: int


@dataclasses.dataclass(slots=True)
class Orphan:
    """A line that could not be paired into a record. ``diag`` carries the
    rule ID and source line; the raw bytes survive verbatim for the
    quarantine sidecar. (Named ``diag``, not ``diagnostic``, so it does not
    shadow the imported :func:`diagnostic` constructor used in this module.)
    """

    raw_line: bytes
    src: int
    diag: Diagnostic


@dataclasses.dataclass(slots=True, frozen=True)
class FileStarted:
    """Progress event: a worker has begun processing ``name`` (issue #53)."""

    name: str


@dataclasses.dataclass(slots=True, frozen=True)
class FileEnded:
    """Progress event: a worker has finished or failed ``name`` (issue #53).

    Emitted from ``process_file``'s ``finally`` so it fires on both the
    success and the exception path.
    """

    name: str


@dataclasses.dataclass(slots=True, frozen=True)
class FileProgress:
    """Per-file progress delta (issue #53 §6): ``bytes_delta`` is the advance in
    the true file offset and ``records_delta`` the records routed since the last
    message. Across a file's messages the byte deltas sum to its ``st_size`` and
    the record deltas to its routed-record count.
    """

    name: str
    bytes_delta: int
    records_delta: int


# The worker -> display protocol over the progress queue, defined once here so
# producer (``process_file``) and consumer (``cli_progress._ProgressDisplay``)
# share one schema: dispatch is by type and field access is by name, so adding a
# variant or reordering a field is a typed change at both ends rather than a
# silently mis-unpacked tuple.
ProgressMessage = FileStarted | FileEnded | FileProgress


# Maximum bytes read per logical line. A genuine TLE line is 69 bytes plus a
# newline; nothing in the real corpus approaches 4 KB. The cap prevents a file
# with no ``\n`` (or only ``\r`` as line terminators) from materialising as one
# giant bytes object — a 3.2 GB "line" would consume 3.2 GB of RAM and then be
# pickled back across the process-pool boundary. When a chunk of exactly this
# size has no trailing ``\n`` the read loop treats it as the first (and bounded
# excerpt of the) oversized line, drains the remainder cheaply, and emits a
# single ``Orphan`` with ``RuleID.LINE_LENGTH``.
_MAX_LINE_BYTES = 4096


@dataclasses.dataclass(slots=True)
class _ProgressBatcher:
    """Accumulate per-file progress deltas before sending queue messages."""

    progress_queue: object | None
    progress_every: int
    src_name: str
    entries_processed: int = 0
    bytes_flushed: int = 0
    records_since_flush: int = 0
    # Precomputed once so per-record hot path avoids re-evaluating the
    # condition (two attribute reads + a bool() call) on every item_seen call.
    _enabled: bool = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self._enabled = self.progress_queue is not None and bool(self.progress_every)

    def item_seen(self, stats: report.FileStats) -> None:
        """Record one routed candidate and emit a batch when due."""
        self.entries_processed += 1
        if not self._enabled:
            return
        self.records_since_flush += 1
        if self.entries_processed % self.progress_every == 0:
            self._put(stats)

    def flush(self, stats: report.FileStats) -> None:
        """Emit the trailing partial batch so the caller's tally is exact."""
        if not self._enabled:
            return
        byte_delta = stats.bytes_consumed - self.bytes_flushed
        if byte_delta or self.records_since_flush:
            self.progress_queue.put(
                FileProgress(self.src_name, byte_delta, self.records_since_flush)
            )

    def _put(self, stats: report.FileStats) -> None:
        byte_delta = stats.bytes_consumed - self.bytes_flushed
        self.progress_queue.put(
            FileProgress(self.src_name, byte_delta, self.records_since_flush)
        )
        self.bytes_flushed += byte_delta
        self.records_since_flush = 0


def _orphan(raw_line: bytes, src: int, rule_id: RuleID, note: str) -> Orphan:
    """Build an :class:`Orphan` with a pre-constructed :class:`Diagnostic`."""
    return Orphan(raw_line, src, diagnostic(rule_id, source_line_nos=(src,), note=note))


def iter_records(
    path: str, stats: report.FileStats | None = None
) -> Iterator[RecordCandidate | Orphan]:
    """Yield ``RecordCandidate`` / ``Orphan`` items streamed from ``path``.

    The file is read in binary so ``\\r`` and stray bytes are observed
    exactly. Blank, whitespace-only, and CR-only lines are dropped.
    Pairing is prefix-driven and resynchronises on every ``1 `` line, so
    one missing line cannot cascade into a run of mispaired records. The
    prefix is matched on a leading-whitespace-trimmed *view* of the line
    (mirroring what ``repair_line`` will lstrip), so a record indented with
    spaces/tabs still pairs; the **raw** bytes are carried forward unchanged
    so ``repair_line`` performs the trim and tags it ``leading-trim`` (#88).

    Each logical line is read via ``handle.readline(_MAX_LINE_BYTES)`` — a
    single C-level call, so throughput equals the ``for raw in handle``
    iterator on normal lines. A chunk of exactly ``_MAX_LINE_BYTES`` with no
    trailing ``\\n`` is treated as the first (bounded) excerpt of an oversized
    line: the remainder is drained in ``_MAX_LINE_BYTES`` chunks, summing their
    lengths into ``bytes_consumed``, until a ``\\n`` or EOF is seen. One Orphan
    with ``RuleID.LINE_LENGTH`` and a note explaining the truncation is emitted
    for the whole logical line (issue #95). This preserves constant memory even
    for a CR-only or newline-free 3.2 GB file — at most ``_MAX_LINE_BYTES``
    bytes are ever held for the line's raw excerpt.

    When ``stats`` is given, ``stats.input_lines_seen`` is updated to the
    1-indexed lineno of the line just consumed and ``stats.bytes_consumed``
    accumulates each line's raw byte length — both including blanks the
    pairing loop drops and including every discarded byte of oversized lines,
    so ``bytes_consumed`` reaches ``st_size`` at EOF.
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2
    lineno = 0

    with open(path, "rb") as handle:
        while True:
            chunk = handle.readline(_MAX_LINE_BYTES)
            if not chunk:
                break  # EOF

            lineno += 1
            n_bytes = len(chunk)

            if len(chunk) == _MAX_LINE_BYTES and not chunk.endswith(b"\n"):
                # Oversized line: this chunk is the bounded excerpt; drain
                # the remainder without holding it in memory, accumulating
                # every byte into bytes_consumed so the counter reaches st_size.
                excerpt = chunk
                while True:
                    tail = handle.readline(_MAX_LINE_BYTES)
                    if not tail:
                        break
                    n_bytes += len(tail)
                    if tail.endswith(b"\n"):
                        break
                if stats is not None:
                    stats.input_lines_seen = lineno
                    stats.bytes_consumed += n_bytes
                # An oversized (garbage) line breaks pairing: flush any held
                # line-1 as an orphan so it cannot pair with a line-2 across the
                # corruption (mirrors the BAD_PREFIX branch).
                if held is not None:
                    yield _orphan(
                        held[0],
                        held[1],
                        RuleID.ORPHAN_LINE,
                        "orphan line 1: followed by an oversized line",
                    )
                    held = None
                yield _orphan(
                    excerpt,
                    lineno,
                    RuleID.LINE_LENGTH,
                    f"line exceeds {_MAX_LINE_BYTES} bytes; truncated",
                )
                continue

            # Normal line (≤ _MAX_LINE_BYTES, possibly with trailing \n).
            if stats is not None:
                stats.input_lines_seen = lineno
                stats.bytes_consumed += n_bytes
            line = chunk.rstrip(b"\n")
            if line.strip(b" \t\r") == b"":
                continue  # blank, whitespace-only, or CR-only line — dropped

            # Route on a leading-whitespace-trimmed view (repair_line lstrips
            # the same " \t"), but keep ``line`` — the raw bytes — so repair
            # owns the trim and tags it ``leading-trim``. A leading BOM/\r is
            # not trimmed here (nor by repair), so such lines still fall
            # through to BAD_PREFIX, matching repair's behaviour.
            prefix = line.lstrip(b" \t")[:2]
            if prefix == b"1 ":
                if held is not None:
                    yield _orphan(
                        held[0],
                        held[1],
                        RuleID.ORPHAN_LINE,
                        "orphan line 1: followed by another line 1",
                    )
                held = (line, lineno)
            elif prefix == b"2 ":
                if held is not None:
                    yield RecordCandidate(held[0], line, held[1], lineno)
                    held = None
                else:
                    yield _orphan(
                        line,
                        lineno,
                        RuleID.ORPHAN_LINE,
                        "orphan line 2: no preceding line 1",
                    )
            else:
                if held is not None:
                    yield _orphan(
                        held[0],
                        held[1],
                        RuleID.ORPHAN_LINE,
                        "orphan line 1: followed by a non-TLE line",
                    )
                    held = None
                yield _orphan(
                    line,
                    lineno,
                    RuleID.BAD_PREFIX,
                    "line does not start with '1 ' or '2 '",
                )

    if held is not None:
        yield _orphan(
            held[0],
            held[1],
            RuleID.ORPHAN_LINE,
            "orphan line 1 at end of file",
        )


def process_file(
    src_path: str,
    out_dir: str,
    mode: Literal["clean", "validate"],
    progress_queue: object | None = None,
    progress_every: int = 25_000,
    *,
    reconstruct_checksum: bool = False,
    chunk_records: int = CHUNK_RECORDS_DEFAULT,
) -> report.FileStats:
    """Process one source file and return its ``report.FileStats``.

    ``mode`` is ``"clean"`` (writes the ``01-cleaned/<stem>.NNNNN.cleaned.txt``
    and ``02-broken/<stem>.NNNNN.broken.txt`` chunk sets plus the
    ``.shards`` findings shard under ``out_dir``) or ``"validate"``
    (audit only — writes nothing). The production caller
    (``worker_pool.run_workers``) always passes ``"clean"``; ``"validate"`` is
    a test/internal audit surface (e.g. ``test_integration`` re-validates
    cleaned output without writing). Every chunk commits through the durable
    temp-file + atomic-rename path, so an interrupted run never leaves a
    half-written output.

    When ``progress_queue`` is given, a :class:`FileProgress` delta is pushed
    every ``progress_every`` records — and once more when the file ends — so the
    caller can render live byte + record progress; the byte deltas sum to the
    file's size. The queue also receives a :class:`FileStarted` before
    processing begins and a :class:`FileEnded` in a ``finally`` (so failures
    still emit it), letting the caller track which files are currently in
    flight. With no queue (or ``progress_every`` set to 0) no progress is
    reported.

    ``reconstruct_checksum`` (issue #82; default off) is forwarded to the
    repairer to gate the tier-2 missing-checksum reconstruction.
    """
    src_name = Path(src_path).name
    stats = report.FileStats(src_name=src_name)
    # Wall-clock start for the v1 envelope (issue #20). Captured up front
    # so even a file that fails early during open still surfaces a
    # non-zero elapsed_seconds. ``time.monotonic()`` (not ``time.time()``)
    # so NTP jitter mid-run cannot produce a negative duration. The
    # corresponding stop happens in the finally below — covers both
    # success and exception paths. ``stats.bytes`` is captured inside
    # ``_run`` once the file has been successfully opened, so a missing
    # source file raises the original ``OSError`` from ``iter_records``
    # rather than from a separate ``getsize`` probe.
    started_monotonic = time.monotonic()
    progress_enabled = progress_queue is not None and bool(progress_every)
    if progress_enabled:
        progress_queue.put(FileStarted(src_name))

    try:
        return _run(
            src_path,
            out_dir,
            mode,
            stats,
            progress_queue,
            progress_every,
            reconstruct_checksum,
            chunk_records,
        )
    finally:
        # ``finally`` always runs before the return value reaches the
        # caller; ``stats`` is the same object ``_run`` returns, so the
        # assignment lands in the returned instance on the success path
        # and on the exception path alike. Setting elapsed even on
        # exception means a quarantined-by-error file still surfaces a
        # non-zero duration in the envelope when callers retain stats.
        stats.elapsed_seconds = time.monotonic() - started_monotonic
        if progress_enabled:
            progress_queue.put(FileEnded(src_name))


def _record_acceptance(
    stats: report.FileStats,
    cleaned_writer: chunking.ChunkedWriter | None,
    result: repair.Accepted,
) -> None:
    """Tally and optionally write one accepted repaired record.

    The record is written as one chunk unit (:meth:`ChunkedWriter.write_record`),
    so the 2-line record is never split across a chunk boundary and stays
    byte-identical to the pre-chunking single-write output (ASCII, LF-terminated).
    """
    stats.clean_count += 1
    stats.fix_counts.update(result.fixes)
    if cleaned_writer is not None:
        cleaned_writer.write_record(
            result.line1.encode("ascii"), result.line2.encode("ascii")
        )


def _route_candidate(
    candidate: RecordCandidate | Orphan,
    stats: report.FileStats,
    sink: report_writers.QuarantineSink,
    cleaned_writer: chunking.ChunkedWriter | None,
    reconstruct_checksum: bool,
) -> None:
    """Route one paired record or orphan into accepted/quarantined accounting."""
    if isinstance(candidate, Orphan):
        stats.orphan_entries += 1
        _record_quarantine(
            stats,
            sink,
            candidate.diag,
            (),
            [candidate.raw_line],
            [candidate.src],
        )
        return

    stats.paired_records += 1

    try:
        result = repair.repair_record(
            candidate.raw_line1,
            candidate.src1,
            candidate.raw_line2,
            candidate.src2,
            reconstruct_checksum=reconstruct_checksum,
        )
    except Exception as exc:  # one bad record must not kill the run
        _record_quarantine(
            stats,
            sink,
            diagnostic(
                RuleID.INTERNAL_ERROR,
                source_line_nos=(candidate.src1, candidate.src2),
                note=repr(exc),
            ),
            (),
            [candidate.raw_line1, candidate.raw_line2],
            [candidate.src1, candidate.src2],
        )
        return

    if isinstance(result, repair.Accepted):
        _record_acceptance(stats, cleaned_writer, result)
    else:
        _record_quarantine(
            stats,
            sink,
            result.primary,
            result.related,
            result.raw_lines,
            result.source_lines,
        )


@dataclasses.dataclass(slots=True, frozen=True)
class _CleanPaths:
    """Destination paths for one file's clean-mode outputs."""

    cleaned: str
    broken: str
    jsonl: str


def _clean_output_paths(out_dir: str, src_name: str) -> _CleanPaths:
    """Create the cleaned/, broken/, and .shards/ trees under ``out_dir`` and
    return the three per-file output paths. The ``.shards`` findings shard is
    internal staging the cli concatenates into ``report.jsonl`` at end of run
    and then removes (issue #9, spec §4.6). Suffix/dirname constants live in
    ``lintle.__init__`` — the single naming-convention authority."""
    out = Path(out_dir)
    cleaned_dir = out / CLEANED_DIRNAME
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    broken_dir = out / BROKEN_DIRNAME
    broken_dir.mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / SHARDS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return _CleanPaths(
        cleaned=str(cleaned_dir / (stem(src_name) + CLEANED_SUFFIX)),
        broken=str(broken_dir / (stem(src_name) + BROKEN_SUFFIX)),
        jsonl=str(report_writers.shard_path(out_dir, src_name)),
    )


def _run(
    src_path: str,
    out_dir: str,
    mode: Literal["clean", "validate"],
    stats: report.FileStats,
    progress_queue: object | None,
    progress_every: int,
    reconstruct_checksum: bool,
    chunk_records: int = CHUNK_RECORDS_DEFAULT,
) -> report.FileStats:
    """Process one file once start/end progress events are accounted for —
    body of :func:`process_file`. Kept separate so the wrapper above can
    own the queue-event lifecycle without doubling this function's
    indentation.
    """
    src_name = stats.src_name
    cleaned_path = None
    broken_path = None
    jsonl_path = None
    if mode == "clean":
        paths = _clean_output_paths(out_dir, src_name)
        cleaned_path = paths.cleaned
        broken_path = paths.broken
        jsonl_path = paths.jsonl

    # The sink owns the BrokenFileWriter lifecycle in clean mode and the
    # bounded in-memory sample in both modes. Issue #19: cap-enforcement
    # is now a structural property of the sink, not a convention spread
    # across pipeline._record_quarantine. Issue #9: the sink also owns the
    # optional JsonlFindingsWriter, which streams structured findings to
    # the per-file shard alongside the .broken.txt byte-faithful catalog.
    sink = report_writers.QuarantineSink(
        broken_path=broken_path,
        src_name=src_name,
        jsonl_path=jsonl_path,
        chunk_records=chunk_records,
    )

    completed = False
    progress = _ProgressBatcher(progress_queue, progress_every, stats.src_name)
    with sink:
        # Issue #104: the cleaned writer is created INSIDE the `with sink:` block
        # so that a sink.__enter__ failure (e.g. the jsonl writer's open fails)
        # can never leave a chunk temp behind. The inner finally below abandons
        # the whole chunk set on every error path (per-file atomicity).
        cleaned_writer = None
        try:
            if mode == "clean":
                # ChunkedWriter rolls cleaned output into
                # cleaned/<stem>.NNNNN.cleaned.txt every chunk_records records,
                # committing each chunk atomically as it fills. It owns its own
                # temp + durable_replace lifecycle, so no cleaned_tmp here.
                cleaned_writer = chunking.ChunkedWriter(
                    str(Path(cleaned_path).parent),
                    stem(src_name),
                    CLEANED_SUFFIX,
                    chunk_records,
                )
            # Input size for the v1 envelope (issue #20). Captured inside
            # the ``with sink:`` try-block so a missing-source ``OSError``
            # routes through the same cleanup paths as one raised by
            # ``iter_records`` itself — the cleaned chunk set is abandoned
            # by the inner finally and the sink's ``__exit__`` discards
            # the ``.broken.txt`` set. The parent's ``finally`` still
            # emits the lifecycle ``end`` event.
            stats.bytes = Path(src_path).stat().st_size
            for candidate in iter_records(src_path, stats):
                # Flush one ``FileProgress`` message every
                # ``progress_every`` records (issue #53 §6). The byte delta is
                # the advance in ``stats.bytes_consumed`` — the true file offset
                # tracked by ``iter_records``, counting dropped blank lines and
                # exact newline widths — so the deltas sum to st_size exactly.
                progress.item_seen(stats)
                _route_candidate(
                    candidate, stats, sink, cleaned_writer, reconstruct_checksum
                )
            # Push the trailing partial batch so the caller's tally is exact.
            # ``byte_delta`` can be non-zero with zero records when the file
            # ends in dropped blank lines — still flush it so the byte bar
            # reaches st_size.
            progress.flush(stats)
            completed = True
        finally:
            # On any failure, abandon the whole cleaned chunk set — never
            # publish a partial .cleaned.txt set and never leak a chunk temp.
            # The sink's own __exit__ (fires below when `with sink:` ends)
            # handles the .broken.txt set.
            if cleaned_writer is not None and not completed:
                cleaned_writer.discard_all()

        # Still inside `with sink:` — finalize must happen BEFORE __exit__
        # fires, otherwise the writer's exit handler sees _completed=False
        # and abandons the set before finalize can commit it. Two
        # adversarial-review voices caught this bug in the original spec.
        if completed and mode == "clean":
            cleaned_writer.close()
        stats.quarantine_sample = sink.finalize()

    return stats


def _record_quarantine(
    stats: report.FileStats,
    sink: report_writers.QuarantineSink,
    primary: Diagnostic,
    related: tuple[Diagnostic, ...],
    raw_lines: list[bytes],
    source_lines: list[int],
) -> None:
    """Tally one quarantined record; hand it to the sink for sampling + streaming.

    ``primary`` is the headline :class:`Diagnostic`; its ``rule_id`` (string
    value, e.g. ``"TLE-CHK-001"``) is the aggregation key written to
    ``stats.quarantine_counts``. ``related`` carries supporting diagnostics, if
    any, and is rendered as indented continuation lines in ``.broken.txt``.

    :class:`QuarantineSink` owns the bounded-sample insert (cap enforced by
    construction, issue #19) and — in clean mode — the byte-faithful
    sidecar stream via its owned :class:`BrokenFileWriter`. The sample
    surfaces on ``stats.quarantine_sample`` once ``sink.finalize`` runs at
    end of file in :func:`_run`.

    Note: ``stats.quarantine_counts`` and ``stats.quarantined_count`` are
    incremented up front, so on an exception mid-file these counters
    will reflect every quarantine encountered while ``stats.quarantine_sample``
    stays at its empty default (the ``with sink:`` exit discards the
    in-flight sample). That counter/sample divergence is observable
    only on the abnormal-exit path and matches today's behaviour.
    """
    stats.quarantined_count += 1
    # primary.rule_id is a StrEnum — equal to and hashable as its string
    # value, so the dict key is the stable wire token ("TLE-CHK-001") and
    # downstream JSON / sort orders are deterministic.
    stats.quarantine_counts[primary.rule_id] += 1
    # Decode the NORAD ID once, before constructing QuarantineEntry, so the
    # structured ``report.jsonl`` emitter sees the same value the per-NORAD
    # breakdown does (issue #9). Orphan-line-2 and bad-prefix quarantines
    # expose no line-1 catalog field and yield ``None``.
    norad_id = tle.extract_norad_id(raw_lines[0])
    # Construct by keyword at this single production site so field order is
    # decoupled from the call (spec §4.5): a reorder of QuarantineEntry's
    # fields can no longer silently misassign arguments here.
    entry = report.QuarantineEntry(
        raw_lines=raw_lines,
        source_lines=source_lines,
        primary=primary,
        related=related,
        norad_id=norad_id,
    )
    sink.add(entry)  # cap-checked, streamed if writer is open (issue #19)
    # The per-NORAD bucket records which rules the satellite hit, feeding
    # the human-facing per-NORAD breakdown section in report.md; ``record``
    # accrues a +1 to that satellite's per-rule total across all quarantines
    # in this file. Single typed mutation entry point per issue #47 — any
    # future writer that wants to populate the tracker must go through it.
    if norad_id is not None:
        stats.quarantined_norad_ids.record(norad_id, primary.rule_id)
