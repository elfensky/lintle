"""Streaming I/O: read a file, pair lines into records, route them."""

import contextlib
import dataclasses
import os
import time

from lintle import repair, report, stem, tle
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
    """A line that could not be paired into a record. The diagnostic carries
    the rule ID and source line; the raw bytes survive verbatim for the
    quarantine sidecar.
    """

    raw_line: bytes
    src: int
    diagnostic: Diagnostic


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
    1-indexed lineno of the line just consumed — including blanks the
    pairing loop drops, so the counter reflects every physical line read.
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2

    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            if stats is not None:
                stats.input_lines_seen = lineno
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

    When ``progress_queue`` is given, the count of newly processed records
    is pushed to it every ``progress_every`` records — and once more when
    the file ends — so the caller can render live progress. The queue also
    receives ``("start", src_name)`` before processing begins and
    ``("end", src_name)`` in a ``finally`` (so failures still emit it),
    letting the caller track which files are currently in flight. With no
    queue (or ``progress_every`` set to 0) no progress is reported.
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
        progress_queue.put(("start", src_name))

    try:
        return _run(src_path, out_dir, mode, stats, progress_queue, progress_every)
    finally:
        # Set elapsed even on exception so a quarantined-by-error file
        # still surfaces non-zero timing in the envelope when callers
        # choose to retain the stats. The worker returns from the
        # try-block in the success path before reaching this finally,
        # but the assignment lands either way.
        stats.elapsed_seconds = time.monotonic() - started_monotonic
        if progress_enabled:
            progress_queue.put(("end", src_name))


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
        cleaned_dir = os.path.join(out_dir, "cleaned")
        os.makedirs(cleaned_dir, exist_ok=True)
        cleaned_path = os.path.join(cleaned_dir, stem(src_name) + ".cleaned.txt")
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
        broken_dir = os.path.join(out_dir, "broken")
        os.makedirs(broken_dir, exist_ok=True)
        broken_path = os.path.join(broken_dir, stem(src_name) + ".broken.txt")
        # Per-file findings shard for the corpus-wide report.jsonl (issue #9).
        # Lives in ``.shards/`` so it's clearly an internal staging directory;
        # the cli concatenates shards into ``report.jsonl`` at end of run and
        # then ``rmtree``s the whole directory. Spec §4.6.
        shard_dir = os.path.join(out_dir, ".shards")
        os.makedirs(shard_dir, exist_ok=True)
        jsonl_path = os.path.join(shard_dir, stem(src_name) + ".findings.jsonl")

    # The sink owns the BrokenFileWriter lifecycle in clean mode and the
    # bounded in-memory sample in both modes. Issue #19: cap-enforcement
    # is now a structural property of the sink, not a convention spread
    # across pipeline._record_reject. Issue #9: the sink also owns the
    # optional JsonlFindingsWriter, which streams structured findings to
    # the per-file shard alongside the .broken.txt byte-faithful catalog.
    sink = report.RejectSink(
        broken_path=broken_path, src_name=src_name, jsonl_path=jsonl_path
    )

    completed = False
    # Tracks paired+orphan yields — the "entries processed" count, used to
    # drive progress reporting. Kept local because the stats counters are
    # split: paired_records and orphan_entries each advance on their own
    # branch below, but progress is an aggregate signal.
    entries_processed = 0
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
                entries_processed += 1

                if (
                    progress_queue is not None
                    and progress_every
                    and entries_processed % progress_every == 0
                ):
                    progress_queue.put(progress_every)

                if isinstance(candidate, Orphan):
                    stats.orphan_entries += 1
                    _record_reject(
                        stats,
                        sink,
                        candidate.diagnostic,
                        (),
                        [candidate.raw_line],
                        [candidate.src],
                    )
                    continue

                stats.paired_records += 1

                try:
                    result = repair.process_record(
                        candidate.raw_line1,
                        candidate.src1,
                        candidate.raw_line2,
                        candidate.src2,
                    )
                except Exception as exc:  # one bad record must not kill the run
                    _record_reject(
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
                    continue

                if isinstance(result, repair.Accepted):
                    stats.clean_count += 1
                    for fix in result.fixes:
                        stats.fix_counts[fix] = stats.fix_counts.get(fix, 0) + 1
                    if cleaned_handle is not None:
                        cleaned_handle.write(result.line1 + "\n")
                        cleaned_handle.write(result.line2 + "\n")
                else:
                    _record_reject(
                        stats,
                        sink,
                        result.primary,
                        result.related,
                        result.raw_lines,
                        result.source_lines,
                    )
            # Push the trailing partial batch so the caller's tally is exact.
            if progress_queue is not None and progress_every:
                remainder = entries_processed % progress_every
                if remainder:
                    progress_queue.put(remainder)
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
            os.replace(cleaned_tmp, cleaned_path)
        stats.reject_sample = sink.finalize(
            entries=stats.paired_records + stats.orphan_entries
        )

    return stats


def _record_reject(stats, sink, primary, related, raw_lines, source_lines):
    """Tally one quarantined record; hand it to the sink for sampling + streaming.

    ``primary`` is the headline :class:`Diagnostic`; its ``rule_id`` (string
    value, e.g. ``"TLE-CHK-001"``) is the aggregation key written to
    ``stats.reject_counts``. ``related`` carries supporting diagnostics, if
    any, and is rendered as indented continuation lines in ``.broken.txt``.

    :class:`RejectSink` owns the bounded-sample insert (cap enforced by
    construction, issue #19) and — in clean mode — the byte-faithful
    sidecar stream via its owned :class:`BrokenFileWriter`. The sample
    surfaces on ``stats.reject_sample`` once ``sink.finalize`` runs at
    end of file in :func:`_run`.

    Note: ``stats.reject_counts`` and ``stats.quarantined_count`` are
    incremented up front, so on an exception mid-file these counters
    will reflect every reject encountered while ``stats.reject_sample``
    stays at its empty default (the ``with sink:`` exit discards the
    in-flight sample). That counter/sample divergence is observable
    only on the abnormal-exit path and matches today's behaviour.
    """
    stats.quarantined_count += 1
    # primary.rule_id is a StrEnum — equal to and hashable as its string
    # value, so the dict key is the stable wire token ("TLE-CHK-001") and
    # downstream JSON / sort orders are deterministic.
    stats.reject_counts[primary.rule_id] = (
        stats.reject_counts.get(primary.rule_id, 0) + 1
    )
    # Decode the NORAD ID once, before constructing RejectEntry, so the
    # structured ``report.jsonl`` emitter sees the same value the per-NORAD
    # breakdown does (issue #9). Orphan-line-2 and bad-prefix rejects
    # expose no line-1 catalog field and yield ``None``.
    norad_id = tle.extract_norad_id(raw_lines[0])
    # Pass norad_id as a kwarg — RejectEntry's positional contract is
    # (raw_lines, source_lines, primary, related), and norad_id is the
    # trailing optional. The kwarg makes the intent explicit at the only
    # production construction site (spec §4.5).
    entry = report.RejectEntry(
        raw_lines, source_lines, primary, related, norad_id=norad_id
    )
    sink.add(entry)  # cap-checked, streamed if writer is open (issue #19)
    # The per-NORAD bucket records which rules the satellite hit, feeding
    # the human-facing per-NORAD breakdown section in report.md; ``record``
    # accrues a +1 to that satellite's per-rule total across all rejects
    # in this file. Single typed mutation entry point per issue #47 — any
    # future writer that wants to populate the tracker must go through it.
    if norad_id is not None:
        stats.quarantined_norad_ids.record(norad_id, primary.rule_id)
