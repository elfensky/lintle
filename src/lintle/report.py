"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import dataclasses
import datetime

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
    """Accumulated results for one processed source file."""

    src_name: str
    total_records: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_categories: dict = dataclasses.field(default_factory=dict)
    rejects: list = dataclasses.field(default_factory=list)


def write_broken_file(path, src_name, stats):
    """Write the byte-faithful ``.broken.txt`` quarantine sidecar.

    The header and per-record reason lines are ASCII; the quarantined-line
    payloads are copied as raw bytes, so the file may not be valid UTF-8.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | lintle {__version__}\n"
        f"# {stats.quarantined_count} records quarantined "
        f"of {stats.total_records} total\n\n"
    )
    with open(path, "wb") as handle:
        handle.write(header.encode("ascii"))
        for index, entry in enumerate(stats.rejects, start=1):
            if len(entry.source_lines) == 2:
                location = (
                    f"source lines {entry.source_lines[0]}-{entry.source_lines[1]}"
                )
            else:
                location = f"source line {entry.source_lines[0]}"
            handle.write(
                f"[{index}] {location} - reason: {entry.reason}\n".encode(
                    "ascii", errors="replace"
                )
            )
            for raw in entry.raw_lines:
                handle.write(raw)
                handle.write(b"\n")
            handle.write(b"\n")


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
    remainder are summarised as a trailing count.
    """
    lines = []
    for entry in stats.rejects[:limit]:
        if len(entry.source_lines) == 2:
            location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
        else:
            location = str(entry.source_lines[0])
        lines.append(f"  line {location}: {entry.reason}")
    remaining = len(stats.rejects) - limit
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
