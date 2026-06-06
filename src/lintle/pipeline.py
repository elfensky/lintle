"""Streaming I/O: read a file, pair lines into records, route them."""

import contextlib
import dataclasses
import os
import time

from lintle import fsutil, repair, report, report_writers, stem, tle
from lintle.diagnostics import Diagnostic, RuleID, diagnostic


@dataclasses.dataclass
class RecordCandidate:
    """A line-1 / line-2 pair, with their 1-indexed source line numbers."""

    raw_line1: bytes
    raw_line2: bytes
    src1: int
    src2: int


@dataclasses.dataclass
class Orphan:
    """A line that could not be paired into a record. ``diag`` carries the
    rule ID and source line; the raw bytes survive verbatim for the
    quarantine sidecar. (Named ``diag``, not ``diagnostic``, so it does not
    shadow the imported :func:`diagnostic` constructor used in this module.)
    """

    raw_line: bytes
    src: int
    diag: Diagnostic


@dataclasses.dataclass(frozen=True)
class FileStarted:
    """Progress event: a worker has begun processing ``name`` (issue #53)."""

    name: str


@dataclasses.dataclass(frozen=True)
class FileEnded:
    """Progress event: a worker has finished or failed ``name`` (issue #53).

    Emitted from ``process_file``'s ``finally`` so it fires on both the
    success and the exception path.
    """

    name: str


@dataclasses.dataclass(frozen=True)
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


@dataclasses.dataclass
class _ProgressBatcher:
    """Accumulate per-file progress deltas before sending queue messages."""

    progress_queue: object | None
    progress_every: int
    src_name: str
    entries_processed: int = 0
    bytes_flushed: int = 0
    records_since_flush: int = 0

    @property
    def enabled(self):
        return self.progress_queue is not None and bool(self.progress_every)

    def item_seen(self, stats):
        """Record one routed candidate and emit a batch when due."""
        self.entries_processed += 1
        if not self.enabled:
            return
        self.records_since_flush += 1
        if self.entries_processed % self.progress_every == 0:
            self._put(stats)

    def flush(self, stats):
        """Emit the trailing partial batch so the caller's tally is exact."""
        if not self.enabled:
            return
        byte_delta = stats.bytes_consumed - self.bytes_flushed
        if byte_delta or self.records_since_flush:
            self.progress_queue.put(
                FileProgress(self.src_name, byte_delta, self.records_since_flush)
            )

    def _put(self, stats):
        byte_delta = stats.bytes_consumed - self.bytes_flushed
        self.progress_queue.put(
            FileProgress(self.src_name, byte_delta, self.records_since_flush)
        )
        self.bytes_flushed += byte_delta
        self.records_since_flush = 0


def _orphan(raw_line, src, rule_id, note):
    """Build an :class:`Orphan` with a pre-constructed :class:`Diagnostic`."""
    return Orphan(raw_line, src, diagnostic(rule_id, source_line_nos=(src,), note=note))


def iter_records(path, stats=None):
    """Yield ``RecordCandidate`` / ``Orphan`` items streamed from ``path``.

    The file is read in binary so ``\\r`` and stray bytes are observed
    exactly. Blank, whitespace-only, and CR-only lines are dropped.
    Pairing is prefix-driven and resynchronises on every ``1 `` line, so
    one missing line cannot cascade into a run of mispaired records.

    When ``stats`` is given, ``stats.input_lines_seen`` is updated to the
    1-indexed lineno of the line just consumed and ``stats.bytes_consumed``
    accumulates each line's raw byte length — both including blanks the
    pairing loop drops, so the counters reflect every physical line and byte
    read (``bytes_consumed`` reaches ``st_size`` at EOF).
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2

    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            if stats is not None:
                stats.input_lines_seen = lineno
                stats.bytes_consumed += len(raw)
            line = raw.rstrip(b"\n")
            if line.strip(b" \t\r") == b"":
                continue  # blank, whitespace-only, or CR-only line — dropped

            prefix = line[:2]
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


def process_file(src_path, out_dir, mode, progress_queue=None, progress_every=25_000):
    """Process one source file and return its ``report.FileStats``.

    ``mode`` is ``"validate"`` (audit only — writes nothing) or ``"clean"``
    (also writes ``cleaned/<name>.cleaned.txt`` and
    ``broken/<name>.broken.txt`` under ``out_dir``). The cleaned file is
    written to a temp file and atomically renamed, so an interrupted run
    never leaves a half-written output.

    When ``progress_queue`` is given, a :class:`FileProgress` delta is pushed
    every ``progress_every`` records — and once more when the file ends — so the
    caller can render live byte + record progress; the byte deltas sum to the
    file's size. The queue also receives a :class:`FileStarted` before
    processing begins and a :class:`FileEnded` in a ``finally`` (so failures
    still emit it), letting the caller track which files are currently in
    flight. With no queue (or ``progress_every`` set to 0) no progress is
    reported.
    """
    src_name = os.path.basename(src_path)
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
        return _run(src_path, out_dir, mode, stats, progress_queue, progress_every)
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


def _record_acceptance(stats, cleaned_handle, result):
    """Tally and optionally write one accepted repaired record."""
    stats.clean_count += 1
    for fix in result.fixes:
        stats.fix_counts[fix] = stats.fix_counts.get(fix, 0) + 1
    if cleaned_handle is not None:
        cleaned_handle.write(result.line1 + "\n")
        cleaned_handle.write(result.line2 + "\n")


def _route_candidate(candidate, stats, sink, cleaned_handle):
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
        _record_acceptance(stats, cleaned_handle, result)
    else:
        _record_quarantine(
            stats,
            sink,
            result.primary,
            result.related,
            result.raw_lines,
            result.source_lines,
        )


@dataclasses.dataclass(frozen=True)
class _CleanPaths:
    """Destination paths for one file's clean-mode outputs."""

    cleaned: str
    broken: str
    jsonl: str


def _clean_output_paths(out_dir, src_name):
    """Create the cleaned/, broken/, and .shards/ trees under ``out_dir`` and
    return the three per-file output paths. The ``.shards`` findings shard is
    internal staging the cli concatenates into ``report.jsonl`` at end of run
    and then removes (issue #9, spec §4.6)."""
    cleaned_dir = os.path.join(out_dir, "cleaned")
    os.makedirs(cleaned_dir, exist_ok=True)
    broken_dir = os.path.join(out_dir, "broken")
    os.makedirs(broken_dir, exist_ok=True)
    shard_dir = os.path.join(out_dir, ".shards")
    os.makedirs(shard_dir, exist_ok=True)
    return _CleanPaths(
        cleaned=os.path.join(cleaned_dir, stem(src_name) + ".cleaned.txt"),
        broken=os.path.join(broken_dir, stem(src_name) + ".broken.txt"),
        jsonl=os.path.join(shard_dir, stem(src_name) + ".findings.jsonl"),
    )


def _run(src_path, out_dir, mode, stats, progress_queue, progress_every):
    """Process one file once start/end progress events are accounted for —
    body of :func:`process_file`. Kept separate so the wrapper above can
    own the queue-event lifecycle without doubling this function's
    indentation.
    """
    src_name = stats.src_name
    cleaned_handle = None
    cleaned_tmp = None
    cleaned_path = None
    broken_path = None
    jsonl_path = None
    if mode == "clean":
        paths = _clean_output_paths(out_dir, src_name)
        cleaned_path = paths.cleaned
        broken_path = paths.broken
        jsonl_path = paths.jsonl
        # Deterministic temp name (not tempfile.mkstemp): a killed run leaves
        # at most one .partial per file, which the next run truncates — no
        # random-name debris accumulates. open() also honours the umask
        # (typically 0644), whereas mkstemp would force owner-only 0600.
        cleaned_tmp = cleaned_path + ".partial"
        # SIM115: the handle is long-lived across the record loop and is
        # closed in the `finally` below — a `with` block does not fit.
        cleaned_handle = open(  # noqa: SIM115
            cleaned_tmp, "w", encoding="ascii", newline="\n"
        )

    # The sink owns the BrokenFileWriter lifecycle in clean mode and the
    # bounded in-memory sample in both modes. Issue #19: cap-enforcement
    # is now a structural property of the sink, not a convention spread
    # across pipeline._record_quarantine. Issue #9: the sink also owns the
    # optional JsonlFindingsWriter, which streams structured findings to
    # the per-file shard alongside the .broken.txt byte-faithful catalog.
    sink = report_writers.QuarantineSink(
        broken_path=broken_path, src_name=src_name, jsonl_path=jsonl_path
    )

    completed = False
    progress = _ProgressBatcher(progress_queue, progress_every, stats.src_name)
    with sink:
        try:
            # Input size for the v1 envelope (issue #20). Captured inside
            # the ``with sink:`` try-block so a missing-source ``OSError``
            # routes through the same cleanup paths as one raised by
            # ``iter_records`` itself — the cleaned-temp file is unlinked
            # by the inner finally and the sink's ``__exit__`` discards
            # the ``.broken.txt`` partials. The parent's ``finally`` still
            # emits the lifecycle ``end`` event.
            stats.bytes = os.path.getsize(src_path)
            for candidate in iter_records(src_path, stats):
                # Flush one ``FileProgress`` message every
                # ``progress_every`` records (issue #53 §6). The byte delta is
                # the advance in ``stats.bytes_consumed`` — the true file offset
                # tracked by ``iter_records``, counting dropped blank lines and
                # exact newline widths — so the deltas sum to st_size exactly.
                progress.item_seen(stats)
                _route_candidate(candidate, stats, sink, cleaned_handle)
            # Push the trailing partial batch so the caller's tally is exact.
            # ``byte_delta`` can be non-zero with zero records when the file
            # ends in dropped blank lines — still flush it so the byte bar
            # reaches st_size.
            progress.flush(stats)
            completed = True
        finally:
            if cleaned_handle is not None:
                cleaned_handle.close()
            # On any failure, discard the partial temp file — never publish a
            # half-written .cleaned.txt and never leak the .tmp behind. The
            # sink's own __exit__ (fires below when `with sink:` ends)
            # handles the .broken.txt partials.
            if cleaned_tmp is not None and not completed:
                with contextlib.suppress(OSError):
                    os.unlink(cleaned_tmp)

        # Still inside `with sink:` — finalize must happen BEFORE __exit__
        # fires, otherwise the writer's exit handler sees _completed=False
        # and deletes the body partial before finalize can stitch it. Two
        # adversarial-review voices caught this bug in the original spec.
        if completed and mode == "clean":
            fsutil.durable_replace(cleaned_tmp, cleaned_path)
        stats.quarantine_sample = sink.finalize(
            entries=stats.paired_records + stats.orphan_entries
        )

    return stats


def _record_quarantine(stats, sink, primary, related, raw_lines, source_lines):
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
    stats.quarantine_counts[primary.rule_id] = (
        stats.quarantine_counts.get(primary.rule_id, 0) + 1
    )
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
