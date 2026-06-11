"""Corpus-wide report aggregation helpers."""

import dataclasses
from collections import Counter

from lintle.categories import FixClass
from lintle.diagnostics import RuleID


@dataclasses.dataclass(frozen=True)
class Totals:
    """Corpus-wide totals summed across every file's stats."""

    paired: int
    orphans: int
    lines_seen: int
    clean: int
    quarantined: int
    fixes: dict[FixClass, int]
    quarantines: dict[RuleID, int]
    dropped: dict[RuleID, int]


def aggregate(all_stats):
    """Sum every file's stats into corpus-wide totals and count dicts."""
    fixes = Counter()
    quarantines = Counter()
    dropped = Counter()
    for stats in all_stats:
        fixes.update(stats.fix_counts)
        quarantines.update(stats.quarantine_counts)
        dropped.update(stats.quarantine_sample.dropped_count)
    return Totals(
        paired=sum(s.paired_records for s in all_stats),
        orphans=sum(s.orphan_entries for s in all_stats),
        lines_seen=sum(s.input_lines_seen for s in all_stats),
        clean=sum(s.clean_count for s in all_stats),
        quarantined=sum(s.quarantined_count for s in all_stats),
        # dict() so Totals holds plain dicts (its declared type); Counter
        # preserves first-seen key order, so the JSON envelope's unsorted
        # fix_counts/quarantine_counts stay byte-identical.
        fixes=dict(fixes),
        quarantines=dict(quarantines),
        dropped=dict(dropped),
    )


def aggregate_per_norad(all_stats):
    """Roll the per-file per-NORAD breakdowns up into a corpus-wide view."""
    rollup = {}
    for stats in all_stats:
        for nid, rule_counts in stats.quarantined_norad_ids.counts.items():
            if not rule_counts:
                continue
            entry = rollup.setdefault(nid, {"total": 0, "rules": {}, "files": set()})
            entry["files"].add(stats.src_name)
            for rule, count in rule_counts.items():
                entry["total"] += count
                entry["rules"][rule] = entry["rules"].get(rule, 0) + count
    return rollup


def format_per_norad_rules(rule_counts):
    """Render a per-NORAD ``{RuleID: count}`` mapping."""
    items = sorted(rule_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{rule} ({count})" for rule, count in items)


def format_per_norad_files(files, *, preview_count):
    """Render a bounded, deterministic filename list."""
    ordered = sorted(files)
    if len(ordered) <= preview_count:
        return ", ".join(ordered)
    head = ", ".join(ordered[:preview_count])
    return f"{head}, +{len(ordered) - preview_count} more"


def format_per_norad_section(all_stats, top_n, *, files_preview):
    """Render the ``## Per-NORAD breakdown`` Markdown section as a line list."""
    rollup = aggregate_per_norad(all_stats)
    lines = ["", "## Per-NORAD breakdown", ""]
    if not rollup:
        lines.append("_None — no records quarantined._")
        return lines
    items = sorted(rollup.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    shown = items if top_n is None else items[:top_n]
    lines += [
        "| NORAD ID | Quarantined records | Defect categories | Files |",
        "|---------:|--------------------:|-------------------|-------|",
    ]
    for nid, entry in shown:
        rules = format_per_norad_rules(entry["rules"])
        files = format_per_norad_files(entry["files"], preview_count=files_preview)
        lines.append(f"| {nid} | {entry['total']:,} | {rules} | {files} |")
    if top_n is not None and len(items) > top_n:
        remaining = len(items) - top_n
        lines += [
            "",
            f"_...and {remaining:,} more — "
            "see broken-noradids.ndjson for the full list._",
        ]
    return lines
