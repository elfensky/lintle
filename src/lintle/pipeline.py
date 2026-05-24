"""Streaming I/O: read a file, pair lines into records, route them."""

import contextlib
import dataclasses
import os

from lintle import repair, report, stem

# How many quarantined records to retain in memory as exemplars for the
# ``validate`` summary. The full byte-faithful catalog goes straight to the
# ``.broken.txt`` sidecar via ``BrokenFileWriter`` — this bound only caps the
# in-memory display sample, so peak memory stays constant even on files
# where every record is corrupt.
_EXEMPLAR_BOUND = 1000


@dataclasses.dataclass
class RecordCandidate:
    """A line-1 / line-2 pair, with their 1-indexed source line numbers."""

    raw_line1: bytes
    raw_line2: bytes
    src1: int
    src2: int


@dataclasses.dataclass
class Orphan:
    """A line that could not be paired into a record."""

    raw_line: bytes
    src: int
    category: str
    reason: str


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
                    yield Orphan(
                        held[0],
                        held[1],
                        "orphan-line",
                        "orphan line 1: followed by another line 1",
                    )
                held = (line, lineno)
            elif prefix == b"2 ":
                if held is not None:
                    yield RecordCandidate(held[0], line, held[1], lineno)
                    held = None
                else:
                    yield Orphan(
                        line,
                        lineno,
                        "orphan-line",
                        "orphan line 2: no preceding line 1",
                    )
            else:
                if held is not None:
                    yield Orphan(
                        held[0],
                        held[1],
                        "orphan-line",
                        "orphan line 1: followed by a non-TLE line",
                    )
                    held = None
                yield Orphan(
                    line,
                    lineno,
                    "bad-prefix",
                    "line does not start with '1 ' or '2 '",
                )

    if held is not None:
        yield Orphan(held[0], held[1], "orphan-line", "orphan line 1 at end of file")


def process_file(src_path, out_dir, mode, progress_queue=None, progress_every=25_000):
    """Process one source file and return its ``report.FileStats``.

    ``mode`` is ``"validate"`` (audit only — writes nothing) or ``"clean"``
    (also writes ``cleaned/<name>.cleaned.txt`` and
    ``broken/<name>.broken.txt`` under ``out_dir``). The cleaned file is
    written to a temp file and atomically renamed, so an interrupted run
    never leaves a half-written output.

    When ``progress_queue`` is given, the count of newly processed records
    is pushed to it every ``progress_every`` records — and once more when
    the file ends — so the caller can render live progress. With no queue
    (or ``progress_every`` set to 0) no progress is reported.
    """
    src_name = os.path.basename(src_path)
    stats = report.FileStats(src_name=src_name)

    cleaned_handle = None
    cleaned_tmp = None
    cleaned_path = None
    broken_writer = None
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
        # Stream rejects straight to disk so memory stays constant even on
        # reject-heavy files. The writer's own context-manager exit discards
        # its partials when finalize() isn't reached.
        broken_writer = report.BrokenFileWriter(broken_path, src_name)
        broken_writer.__enter__()

    completed = False
    # Tracks paired+orphan yields — the "entries processed" count, used to
    # drive progress reporting. Kept local because the stats counters are
    # split: paired_records and orphan_entries each advance on their own
    # branch below, but progress is an aggregate signal.
    entries_processed = 0
    try:
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
                    broken_writer,
                    candidate.category,
                    candidate.reason,
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
                    broken_writer,
                    "internal-error",
                    f"internal-error: {exc!r}",
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
                    broken_writer,
                    result.category,
                    result.reason,
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
        # half-written .cleaned.txt and never leak the .tmp behind.
        if cleaned_tmp is not None and not completed:
            with contextlib.suppress(OSError):
                os.unlink(cleaned_tmp)
        if broken_writer is not None and not completed:
            # Discard the broken-file partials — never publish a half-written
            # sidecar. The context-manager __exit__ does the cleanup.
            broken_writer.__exit__(None, None, None)

    if mode == "clean":
        os.replace(cleaned_tmp, cleaned_path)
        broken_writer.finalize(stats.paired_records + stats.orphan_entries)
        broken_writer.__exit__(None, None, None)

    return stats


def _record_reject(stats, broken_writer, category, reason, raw_lines, source_lines):
    """Tally one quarantined record; stream its bytes to the broken sidecar.

    The in-memory ``reject_exemplars`` list is capped at ``_EXEMPLAR_BOUND``
    — it only feeds the ``validate`` summary display, never the
    byte-faithful catalog. The full record stream goes straight to the
    ``BrokenFileWriter`` when one is open (``clean`` mode).
    """
    stats.quarantined_count += 1
    stats.reject_categories[category] = stats.reject_categories.get(category, 0) + 1
    entry = report.RejectEntry(raw_lines, source_lines, reason)
    if len(stats.reject_exemplars) < _EXEMPLAR_BOUND:
        stats.reject_exemplars.append(entry)
    if broken_writer is not None:
        broken_writer.write_entry(entry)
