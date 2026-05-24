"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import contextlib
import dataclasses
import datetime
import json
import os
import shutil

from lintle import __version__, stem
from lintle.diagnostics import RULES, Diagnostic, RepairTier


@dataclasses.dataclass
class RejectEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful. ``primary``
    is the headline :class:`Diagnostic` shown on the entry's first line;
    ``related`` carries any secondary diagnostics, rendered on indented
    continuation lines.
    """

    raw_lines: list
    source_lines: list
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()


@dataclasses.dataclass
class FileStats:
    """Accumulated results for one processed source file.

    Three independent counters disambiguate what was previously a single
    ``total_records`` tally (issue #5): ``paired_records`` counts proper
    2-line entries; ``orphan_entries`` counts unpaired single lines surfaced
    as findings; ``input_lines_seen`` counts every physical line read,
    including blanks the pairing loop drops. The invariant
    ``paired_records + orphan_entries == clean_count + quarantined_count``
    holds — orphans still flow through ``_record_reject`` so they are tallied
    in ``quarantined_count`` and ``reject_counts['TLE-PAIR-001']``.

    ``reject_counts`` is keyed by :class:`diagnostics.RuleID` string values
    (e.g. ``"TLE-CHK-001"``) so reports cite stable, citable rule IDs.

    ``reject_exemplars`` is a *bounded* sample of quarantined records used
    only by the human-facing ``validate`` summary; the byte-faithful full
    catalog is streamed to ``.broken.txt`` during processing. The bound is
    enforced by the pipeline, not by this dataclass, so tests can populate
    it freely.
    """

    src_name: str
    paired_records: int = 0
    orphan_entries: int = 0
    input_lines_seen: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_counts: dict = dataclasses.field(default_factory=dict)
    reject_exemplars: list = dataclasses.field(default_factory=list)
    # NORAD IDs of records quarantined in this file, decoded once at
    # reject time from line-1 columns 3-7. Bounded by the satellite
    # catalog (~tens of thousands of IDs corpus-wide), so the in-memory
    # set is independent of reject count and keeps memory constant.
    quarantined_norad_ids: set = dataclasses.field(default_factory=set)


def _format_diagnostic(diag):
    """Render one :class:`Diagnostic` as a single-line string fragment.

    Format: ``rule: <id>[ (<tier>)][ - col(s) <range>][ observed=...][
    expected=...][ - <note>]``. The bracketed pieces are emitted only when
    their underlying field is set.
    """
    parts = [f"rule: {diag.rule_id.value}"]
    if diag.tier_attempted != RepairTier.NONE:
        parts[0] += f" ({diag.tier_attempted.value})"
    if diag.column_range is not None:
        start, end = diag.column_range
        if start == end:
            parts.append(f"col {start}")
        else:
            parts.append(f"cols {start}-{end}")
    if diag.observed is not None:
        parts.append(f"observed={diag.observed!r}")
    if diag.expected is not None:
        parts.append(f"expected={diag.expected!r}")
    head = " ".join(parts)
    if diag.note:
        return f"{head} - {diag.note}"
    return head


def _render_entry(index, entry):
    """Render one :class:`RejectEntry` as the bytes it occupies in ``.broken.txt``.

    Header line cites the primary diagnostic; any related diagnostics fold
    onto indented continuation lines (``    and: ...``). The original raw
    lines follow verbatim — byte-faithful quarantine.
    """
    if len(entry.source_lines) == 2:
        location = f"source lines {entry.source_lines[0]}-{entry.source_lines[1]}"
    else:
        location = f"source line {entry.source_lines[0]}"
    head = f"[{index}] {location} - {_format_diagnostic(entry.primary)}\n"
    chunks = [head.encode("ascii", errors="replace")]
    for extra in entry.related:
        chunks.append(
            f"    and: {_format_diagnostic(extra)}\n".encode("ascii", errors="replace")
        )
    for raw in entry.raw_lines:
        chunks.append(raw)
        chunks.append(b"\n")
    chunks.append(b"\n")
    return b"".join(chunks)


def _render_header(src_name, quarantined, entries):
    """Render the three-line ASCII header of a ``.broken.txt`` sidecar.

    ``entries`` is ``paired_records + orphan_entries`` — the count of things
    that became findings or clean output, which is the meaningful denominator
    for ``quarantined``.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | lintle {__version__}\n"
        f"# {quarantined} quarantined of {entries} entries\n\n"
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

    def finalize(self, entries):
        """Stitch header + body into the final path; atomic-rename in place.

        ``entries`` is the denominator shown in the header — pass
        ``paired_records + orphan_entries`` from the source file's stats.
        """
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        header = _render_header(self.src_name, self._entry_count, entries)
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
        writer.finalize(stats.paired_records + stats.orphan_entries)


def _join_counts(counts):
    """Render a count dict as ``"key value | key value"``, sorted by key."""
    return " | ".join(f"{key} {value:,}" for key, value in sorted(counts.items()))


def format_summary(stats):
    """Return the human-readable multi-line summary block for one file.

    The header line shows ``paired_records`` (true 2-line TLEs), followed by
    clean and quarantined counts, then a parenthetical with the orphan and
    input-line counters — separated so issue #5's conflation never returns.
    """
    lines = [
        f"{stats.src_name}   {stats.paired_records:,} records   "
        f"{stats.clean_count:,} clean   {stats.quarantined_count:,} quarantined   "
        f"({stats.orphan_entries:,} orphan, {stats.input_lines_seen:,} lines)"
    ]
    if stats.fix_counts:
        lines.append(f"  fixes:   {_join_counts(stats.fix_counts)}")
    if stats.reject_counts:
        lines.append(f"  rejects: {_join_counts(stats.reject_counts)}")
    return "\n".join(lines)


def summary_dict(stats):
    """Return a JSON-serialisable summary of one file's stats.

    The ``reject_counts`` map is keyed by stable rule IDs (e.g.
    ``"TLE-CHK-001"``) — the same handles cited in ``report.md`` and the
    ``.broken.txt`` sidecar.
    """
    return {
        "src_name": stats.src_name,
        "paired_records": stats.paired_records,
        "orphan_entries": stats.orphan_entries,
        "input_lines_seen": stats.input_lines_seen,
        "clean_count": stats.clean_count,
        "quarantined_count": stats.quarantined_count,
        "fix_counts": dict(stats.fix_counts),
        "reject_counts": dict(stats.reject_counts),
    }


def format_reject_lines(stats, limit=100):
    """Return a listing of quarantined records' source locations.

    Used by ``validate`` mode. At most ``limit`` entries are shown; the
    remainder are summarised as a trailing count. Reads from the bounded
    ``reject_exemplars`` buffer — full counts live in ``quarantined_count``.
    Related diagnostics fold onto indented continuation lines so a record
    where both lines failed still surfaces both rule IDs.
    """
    lines = []
    for entry in stats.reject_exemplars[:limit]:
        if len(entry.source_lines) == 2:
            location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
        else:
            location = str(entry.source_lines[0])
        lines.append(f"  line {location}: {_format_diagnostic(entry.primary)}")
        for extra in entry.related:
            lines.append(f"    and: {_format_diagnostic(extra)}")
    remaining = stats.quarantined_count - min(len(stats.reject_exemplars), limit)
    if remaining > 0:
        lines.append(f"  ...and {remaining} more")
    return "\n".join(lines)


def _aggregate(all_stats):
    """Sum every file's stats into corpus-wide totals and count dicts."""
    paired = sum(s.paired_records for s in all_stats)
    orphans = sum(s.orphan_entries for s in all_stats)
    lines_seen = sum(s.input_lines_seen for s in all_stats)
    clean = sum(s.clean_count for s in all_stats)
    quarantined = sum(s.quarantined_count for s in all_stats)
    fixes = {}
    rejects = {}
    for stats in all_stats:
        for key, value in stats.fix_counts.items():
            fixes[key] = fixes.get(key, 0) + value
        for key, value in stats.reject_counts.items():
            rejects[key] = rejects.get(key, 0) + value
    return paired, orphans, lines_seen, clean, quarantined, fixes, rejects


def format_run_report(all_stats):
    """Render a Markdown report aggregating every processed file.

    Written to ``<out-dir>/report.md`` after a ``clean`` run: corpus
    totals, the percentage cleaned/quarantined, the corpus-wide fix and
    rule-ID counts, a per-file breakdown table, and a rule reference
    section auto-generated from ``diagnostics.RULES`` for every rule that
    fired in this run. Percentages use ``paired_records + orphan_entries``
    as the denominator — equal to ``clean + quarantined`` by the FileStats
    invariant, so the cleaned and quarantined shares sum to 100 % even
    when orphans are present.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    paired, orphans, lines_seen, clean, quarantined, fixes, rejects = _aggregate(
        all_stats
    )
    denominator = paired + orphans

    def pct(count):
        return f"{100 * count / denominator:.4f}%" if denominator else "n/a"

    lines = [
        "# lintle clean run report",
        "",
        f"- Generated: {timestamp}",
        f"- Tool: lintle {__version__}",
        f"- Files processed: {len(all_stats)}",
        "",
        "## Corpus totals",
        "",
        f"- Records: {paired:,}",
        f"- Orphan lines: {orphans:,}",
        f"- Input lines: {lines_seen:,}",
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

    lines += ["", "## Records quarantined (by rule)", ""]
    if rejects:
        lines.append("| Rule | Count |")
        lines.append("|------|------:|")
        for key, value in sorted(rejects.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {value:,} |")
    else:
        lines.append("_None — every record was clean._")

    lines += [
        "",
        "## Per-file breakdown",
        "",
        "| File | Records | Orphans | Cleaned | Quarantined |",
        "|------|--------:|--------:|--------:|------------:|",
    ]
    for stats in all_stats:
        lines.append(
            f"| {stats.src_name} | {stats.paired_records:,} | "
            f"{stats.orphan_entries:,} | "
            f"{stats.clean_count:,} | {stats.quarantined_count:,} |"
        )

    if rejects:
        lines += ["", "## Rule reference", ""]
        for key in sorted(rejects):
            spec = _spec_for_key(key)
            if spec is None:
                lines.append(f"- `{key}` — (unknown rule)")
            else:
                lines.append(f"- `{key}` — {spec.short_title}")

    lines.append("")
    return "\n".join(lines)


def _spec_for_key(key):
    """Return the :class:`diagnostics.RuleSpec` for a rule-ID key, or ``None``.

    ``key`` may be a :class:`diagnostics.RuleID` or its string value —
    both hash and compare identically (StrEnum extends ``str``), so a
    single dict lookup serves both cases without a linear scan.
    """
    return RULES.get(key)


def write_run_report(path, all_stats):
    """Write the Markdown run report (``format_run_report``) to ``path``."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(format_run_report(all_stats))


def aggregate_broken_norad_ids(all_stats):
    """Return the sorted, deduplicated NORAD IDs quarantined corpus-wide."""
    ids = set()
    for stats in all_stats:
        ids |= stats.quarantined_norad_ids
    return sorted(ids)


def format_broken_noradids_ndjson(all_stats):
    """Render the corpus-wide quarantined-NORAD-ID NDJSON as a string.

    One ``{"noradId": N}`` object per line, deduplicated across every
    processed file and sorted ascending so diffs across runs are
    deterministic. NDJSON has no header; an empty string is returned
    when no records were quarantined. The minimal one-field shape is
    deliberately additive — downstream consumers ignore unknown fields,
    so later releases can extend each record without breaking compat.
    """
    lines = [
        json.dumps({"noradId": nid}, separators=(",", ":"))
        for nid in aggregate_broken_norad_ids(all_stats)
    ]
    return "".join(line + "\n" for line in lines)


def write_broken_noradids_ndjson(path, all_stats):
    """Write the corpus-wide ``broken-noradids.ndjson`` to ``path``.

    Thin wrapper around ``format_broken_noradids_ndjson`` that pins LF
    line endings so the artifact is byte-deterministic across platforms.
    """
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(format_broken_noradids_ndjson(all_stats))
