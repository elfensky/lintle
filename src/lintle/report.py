"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import contextlib
import dataclasses
import datetime
import os
import shutil

from lintle import __version__, stem


@dataclasses.dataclass
class RejectEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful.
    """

    raw_lines: list
    source_lines: list
    reason: str


@dataclasses.dataclass
class FileStats:
    """Accumulated results for one processed source file.

    ``reject_exemplars`` is a *bounded* sample of quarantined records used
    only by the human-facing ``validate`` summary; the byte-faithful full
    catalog is streamed to ``.broken.txt`` during processing. The bound is
    enforced by the pipeline, not by this dataclass, so tests can populate
    it freely.
    """

    src_name: str
    total_records: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_categories: dict = dataclasses.field(default_factory=dict)
    reject_exemplars: list = dataclasses.field(default_factory=list)


def _render_entry(index, entry):
    """Render one ``RejectEntry`` as the bytes it occupies in ``.broken.txt``."""
    if len(entry.source_lines) == 2:
        location = f"source lines {entry.source_lines[0]}-{entry.source_lines[1]}"
    else:
        location = f"source line {entry.source_lines[0]}"
    chunks = [
        f"[{index}] {location} - reason: {entry.reason}\n".encode(
            "ascii", errors="replace"
        )
    ]
    for raw in entry.raw_lines:
        chunks.append(raw)
        chunks.append(b"\n")
    chunks.append(b"\n")
    return b"".join(chunks)


def _render_header(src_name, quarantined, total):
    """Render the three-line ASCII header of a ``.broken.txt`` sidecar."""
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | lintle {__version__}\n"
        f"# {quarantined} records quarantined of {total} total\n\n"
    ).encode("ascii")


class BrokenFileWriter:
    """Streaming writer for the ``.broken.txt`` quarantine sidecar.

    Constant memory: each entry is rendered and flushed to a body temp file
    as ``write_entry`` is called. On ``finalize`` the body is stitched onto
    the now-known header (entry count + corpus total) and atomically renamed
    to the final path. Use as a context manager so an interrupted run never
    leaves a half-written sidecar behind.
    """

    def __init__(self, path, src_name):
        self.path = path
        self.src_name = src_name
        self._body_path = path + ".body.partial"
        self._final_partial = path + ".partial"
        self._handle = None
        self._entry_count = 0
        self._completed = False

    def __enter__(self):
        self._handle = open(self._body_path, "wb")
        return self

    def write_entry(self, entry):
        """Append one ``RejectEntry`` to the sidecar body, byte-faithfully."""
        self._entry_count += 1
        self._handle.write(_render_entry(self._entry_count, entry))

    def finalize(self, total_records):
        """Stitch header + body into the final path; atomic-rename in place."""
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        header = _render_header(self.src_name, self._entry_count, total_records)
        with open(self._final_partial, "wb") as out, open(self._body_path, "rb") as src:
            out.write(header)
            shutil.copyfileobj(src, out, length=65536)
        with contextlib.suppress(OSError):
            os.unlink(self._body_path)
        os.replace(self._final_partial, self.path)
        self._completed = True

    def __exit__(self, exc_type, exc, tb):
        # Always close the body handle; on any non-finalized exit, discard
        # the partials so an interrupted run leaves no debris.
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        if not self._completed:
            for partial in (self._body_path, self._final_partial):
                with contextlib.suppress(OSError):
                    os.unlink(partial)
        return False


def write_broken_file(path, src_name, stats):
    """Write the ``.broken.txt`` sidecar from a populated ``FileStats``.

    Thin wrapper around ``BrokenFileWriter`` that emits whatever is in
    ``stats.reject_exemplars``. Suitable for tests and small-corpus paths
    where the full reject list fits in memory; production cleaning streams
    entries through ``BrokenFileWriter`` directly so memory stays bounded.
    """
    with BrokenFileWriter(path, src_name) as writer:
        for entry in stats.reject_exemplars:
            writer.write_entry(entry)
        writer.finalize(stats.total_records)


def _join_counts(counts):
    """Render a count dict as ``"key value | key value"``, sorted by key."""
    return " | ".join(f"{key} {value:,}" for key, value in sorted(counts.items()))


def format_summary(stats):
    """Return the human-readable multi-line summary block for one file."""
    lines = [
        f"{stats.src_name}   {stats.total_records:,} records   "
        f"{stats.clean_count:,} clean   {stats.quarantined_count:,} quarantined"
    ]
    if stats.fix_counts:
        lines.append(f"  fixes:   {_join_counts(stats.fix_counts)}")
    if stats.reject_categories:
        lines.append(f"  rejects: {_join_counts(stats.reject_categories)}")
    return "\n".join(lines)


def summary_dict(stats):
    """Return a JSON-serialisable summary of one file's stats."""
    return {
        "src_name": stats.src_name,
        "total_records": stats.total_records,
        "clean_count": stats.clean_count,
        "quarantined_count": stats.quarantined_count,
        "fix_counts": dict(stats.fix_counts),
        "reject_categories": dict(stats.reject_categories),
    }


def format_reject_lines(stats, limit=100):
    """Return a listing of quarantined records' source locations.

    Used by ``validate`` mode. At most ``limit`` entries are shown; the
    remainder are summarised as a trailing count. Reads from the bounded
    ``reject_exemplars`` buffer — full counts live in ``quarantined_count``.
    """
    lines = []
    for entry in stats.reject_exemplars[:limit]:
        if len(entry.source_lines) == 2:
            location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
        else:
            location = str(entry.source_lines[0])
        lines.append(f"  line {location}: {entry.reason}")
    remaining = stats.quarantined_count - min(len(stats.reject_exemplars), limit)
    if remaining > 0:
        lines.append(f"  ...and {remaining} more")
    return "\n".join(lines)


def _aggregate(all_stats):
    """Sum every file's stats into corpus-wide totals and count dicts."""
    total = sum(s.total_records for s in all_stats)
    clean = sum(s.clean_count for s in all_stats)
    quarantined = sum(s.quarantined_count for s in all_stats)
    fixes = {}
    rejects = {}
    for stats in all_stats:
        for key, value in stats.fix_counts.items():
            fixes[key] = fixes.get(key, 0) + value
        for key, value in stats.reject_categories.items():
            rejects[key] = rejects.get(key, 0) + value
    return total, clean, quarantined, fixes, rejects


def format_run_report(all_stats):
    """Render a Markdown report aggregating every processed file.

    Written to ``<out-dir>/report.md`` after a ``clean`` run: corpus
    totals, the percentage cleaned/quarantined, the corpus-wide fix and
    defect-category counts, and a per-file breakdown table.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    total, clean, quarantined, fixes, rejects = _aggregate(all_stats)

    def pct(count):
        return f"{100 * count / total:.4f}%" if total else "n/a"

    lines = [
        "# lintle clean run report",
        "",
        f"- Generated: {timestamp}",
        f"- Tool: lintle {__version__}",
        f"- Files processed: {len(all_stats)}",
        "",
        "## Corpus totals",
        "",
        f"- Records: {total:,}",
        f"- Cleaned: {clean:,} ({pct(clean)})",
        f"- Quarantined: {quarantined:,} ({pct(quarantined)})",
        "",
        "## Fixes applied",
        "",
    ]
    if fixes:
        lines.append("| Fix | Count |")
        lines.append("|-----|------:|")
        for key, value in sorted(fixes.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {value:,} |")
    else:
        lines.append("_None._")

    lines += ["", "## Records quarantined (by defect category)", ""]
    if rejects:
        lines.append("| Defect category | Count |")
        lines.append("|-----------------|------:|")
        for key, value in sorted(rejects.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {value:,} |")
    else:
        lines.append("_None — every record was clean._")

    lines += [
        "",
        "## Per-file breakdown",
        "",
        "| File | Records | Cleaned | Quarantined |",
        "|------|--------:|--------:|------------:|",
    ]
    for stats in all_stats:
        lines.append(
            f"| {stats.src_name} | {stats.total_records:,} | "
            f"{stats.clean_count:,} | {stats.quarantined_count:,} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_run_report(path, all_stats):
    """Write the Markdown run report (``format_run_report``) to ``path``."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(format_run_report(all_stats))
