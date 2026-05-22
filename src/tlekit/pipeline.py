"""Streaming I/O: read a file, pair lines into records, route them."""

import dataclasses
import os

from tlekit import repair, report, stem


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


def iter_records(path):
    """Yield ``RecordCandidate`` / ``Orphan`` items streamed from ``path``.

    The file is read in binary so ``\\r`` and stray bytes are observed
    exactly. Blank, whitespace-only, and CR-only lines are dropped.
    Pairing is prefix-driven and resynchronises on every ``1 `` line, so
    one missing line cannot cascade into a run of mispaired records.
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2

    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.rstrip(b"\n")
            if line.strip(b" \t\r") == b"":
                continue  # blank, whitespace-only, or CR-only line — dropped

            prefix = line[:2]
            if prefix == b"1 ":
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by another line 1",
                    )
                held = (line, lineno)
            elif prefix == b"2 ":
                if held is not None:
                    yield RecordCandidate(held[0], line, held[1], lineno)
                    held = None
                else:
                    yield Orphan(
                        line, lineno, "orphan-line",
                        "orphan line 2: no preceding line 1",
                    )
            else:
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by a non-TLE line",
                    )
                    held = None
                yield Orphan(
                    line, lineno, "bad-prefix",
                    "line does not start with '1 ' or '2 '",
                )

    if held is not None:
        yield Orphan(
            held[0], held[1], "orphan-line", "orphan line 1 at end of file"
        )


def process_file(src_path, out_dir, mode):
    """Process one source file and return its ``report.FileStats``.

    ``mode`` is ``"validate"`` (audit only — writes nothing) or ``"clean"``
    (also writes ``cleaned/<name>.cleaned.txt`` and
    ``broken/<name>.broken.txt`` under ``out_dir``). The cleaned file is
    written to a temp file and atomically renamed, so an interrupted run
    never leaves a half-written output.
    """
    src_name = os.path.basename(src_path)
    stats = report.FileStats(src_name=src_name)

    cleaned_handle = None
    cleaned_tmp = None
    cleaned_path = None
    if mode == "clean":
        cleaned_dir = os.path.join(out_dir, "cleaned")
        os.makedirs(cleaned_dir, exist_ok=True)
        cleaned_path = os.path.join(cleaned_dir, stem(src_name) + ".cleaned.txt")
        # Deterministic temp name (not tempfile.mkstemp): a killed run leaves
        # at most one .partial per file, which the next run truncates — no
        # random-name debris accumulates. open() also honours the umask
        # (typically 0644), whereas mkstemp would force owner-only 0600.
        cleaned_tmp = cleaned_path + ".partial"
        cleaned_handle = open(cleaned_tmp, "w", encoding="ascii", newline="\n")

    completed = False
    try:
        for candidate in iter_records(src_path):
            stats.total_records += 1

            if isinstance(candidate, Orphan):
                _record_reject(
                    stats, candidate.category, candidate.reason,
                    [candidate.raw_line], [candidate.src],
                )
                continue

            try:
                result = repair.process_record(
                    candidate.raw_line1, candidate.src1,
                    candidate.raw_line2, candidate.src2,
                )
            except Exception as exc:  # one bad record must not kill the run
                _record_reject(
                    stats, "internal-error", f"internal-error: {exc!r}",
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
                    stats, result.category, result.reason,
                    result.raw_lines, result.source_lines,
                )
        completed = True
    finally:
        if cleaned_handle is not None:
            cleaned_handle.close()
        # On any failure, discard the partial temp file — never publish a
        # half-written .cleaned.txt and never leak the .tmp behind.
        if cleaned_tmp is not None and not completed:
            try:
                os.unlink(cleaned_tmp)
            except OSError:
                pass

    if mode == "clean":
        os.replace(cleaned_tmp, cleaned_path)
        broken_dir = os.path.join(out_dir, "broken")
        os.makedirs(broken_dir, exist_ok=True)
        broken_path = os.path.join(broken_dir, stem(src_name) + ".broken.txt")
        report.write_broken_file(broken_path, src_name, stats)

    return stats


def _record_reject(stats, category, reason, raw_lines, source_lines):
    """Tally one quarantined record into ``stats``."""
    stats.quarantined_count += 1
    stats.reject_categories[category] = (
        stats.reject_categories.get(category, 0) + 1
    )
    stats.rejects.append(report.RejectEntry(raw_lines, source_lines, reason))
