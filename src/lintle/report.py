"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import contextlib
import dataclasses
import datetime
import json
import os
import shutil

from lintle import __version__, stem
from lintle.diagnostics import RULES, Diagnostic, RepairTier

# How many quarantined records to retain in memory as exemplars per
# ``RuleID`` for the ``validate`` summary. The full byte-faithful catalog
# goes straight to the ``.broken.txt`` sidecar via ``BrokenFileWriter`` —
# this bound only caps the per-rule in-memory display sample, so peak
# memory stays constant even on files where every record is corrupt.
# Total ceiling per file is ``|RuleID| × _PER_RULE_EXEMPLAR_BOUND``. Owned
# here because :class:`RejectSink` is the canonical cap-enforcement
# boundary; ``pipeline.py`` re-imports until the dict-write transition
# completes.
_PER_RULE_EXEMPLAR_BOUND = 5


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


@dataclasses.dataclass(frozen=True)
class FileSample:
    """Immutable, per-file bounded sample of quarantined records (issue #19).

    Produced by :meth:`RejectSink.finalize`; consumed by renderers
    (:func:`format_reject_lines`, :func:`write_broken_file`). Frozen so
    post-finalize consumers cannot accidentally mutate the sample — the
    per-rule cap invariant is locked in at construction time. ``cap``
    travels with the sample so renderers can surface truncation against
    the bound that was in force when the sample was built.
    """

    buckets: dict
    cap: int

    @classmethod
    def from_bounded(cls, cap, entries_by_rule):
        """Build a FileSample, asserting every bucket honours ``cap``.

        Test-friendly constructor: clones each bucket into a ``tuple`` so
        the result is structurally immutable, and raises ``ValueError``
        (naming the rule and counts) if any bucket exceeds ``cap``.
        Strict by design — silent over-cap inputs would mask test fixture
        mistakes that the sink's cap-enforcement is meant to prevent in
        production.
        """
        for rule_id, entries in entries_by_rule.items():
            if len(entries) > cap:
                raise ValueError(
                    f"bucket {rule_id.name} has {len(entries)} entries; cap is {cap}"
                )
        return cls(
            buckets={rid: tuple(entries) for rid, entries in entries_by_rule.items()},
            cap=cap,
        )

    @classmethod
    def empty(cls, cap):
        """Empty sentinel — saves renderer consumers a None-check per file."""
        return cls(buckets={}, cap=cap)


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

    ``reject_sample`` (issue #19) is an immutable :class:`FileSample`
    holding the per-rule bounded sample of quarantined records. The cap
    is enforced structurally by :class:`RejectSink` during processing,
    not by this dataclass. The byte-faithful full catalog is streamed to
    ``.broken.txt`` during processing.

    ``reject_exemplars`` is the legacy mirror retained through the issue
    #19 dual-field migration so renderers (and existing tests) keep
    working while consumers migrate to ``reject_sample.buckets``. It is
    removed in the final cleanup commit of that refactor — new code must
    read ``reject_sample``.
    """

    src_name: str
    paired_records: int = 0
    orphan_entries: int = 0
    input_lines_seen: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_counts: dict = dataclasses.field(default_factory=dict)
    reject_exemplars: dict = dataclasses.field(default_factory=dict)
    reject_sample: FileSample = dataclasses.field(
        default_factory=lambda: FileSample.empty(_PER_RULE_EXEMPLAR_BOUND)
    )
    # Per-NORAD breakdown for records quarantined in this file. Outer keys
    # are the 5-digit catalog numbers decoded once at reject time from
    # line-1 columns 3-7; each value is a ``{RuleID: count}`` dict tallying
    # which diagnostics that satellite hit in this file. Bounded by the
    # satellite catalog (~tens of thousands of IDs corpus-wide) and the
    # ``RuleID`` enum, so the per-file structure is O(catalog × |RuleID|)
    # — independent of reject count and constant-memory at corpus scale.
    quarantined_norad_ids: dict = dataclasses.field(default_factory=dict)


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


class RejectSink:
    """File-scoped reject sink: bounded sample + optional streaming sidecar.

    Single mutation entry point :meth:`add` enforces the per-rule cap by
    construction — there is no other path into the sample. In ``clean``
    mode the sink owns a :class:`BrokenFileWriter` and streams every
    entry byte-faithfully to ``.broken.txt`` as it arrives; in ``validate``
    mode the writer is absent and the sink is purely in-memory. On
    :meth:`finalize` the sink produces an immutable :class:`FileSample`
    and is sealed — any subsequent :meth:`add` raises ``RuntimeError``
    so misuse surfaces loudly.
    """

    def __init__(
        self,
        *,
        cap=_PER_RULE_EXEMPLAR_BOUND,
        broken_path=None,
        src_name=None,
    ):
        self._cap = cap
        self._buckets = {}
        self._writer = None
        self._finalized = False
        self._sample = None
        if broken_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when broken_path is set")
            self._writer = BrokenFileWriter(broken_path, src_name)

    def __enter__(self):
        if self._writer is not None:
            self._writer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._writer is not None:
            self._writer.__exit__(exc_type, exc, tb)
        return False

    def add(self, entry):
        """Record one quarantined entry. Silently drops past cap.

        Drops are not data loss: the operator-visible totals live in
        ``stats.reject_counts`` (incremented in the pipeline before this
        call), and the byte-faithful catalog reaches disk via the writer
        regardless of the in-memory cap. Raises ``RuntimeError`` if the
        sink has already been finalized.
        """
        if self._finalized:
            raise RuntimeError("sink already finalized; cannot add new entries")
        bucket = self._buckets.get(entry.primary.rule_id)
        if bucket is None:
            bucket = []
            self._buckets[entry.primary.rule_id] = bucket
        if len(bucket) < self._cap:
            bucket.append(entry)
        if self._writer is not None:
            self._writer.write_entry(entry)

    def finalize(self, *, entries):
        """Seal the sink and return the immutable :class:`FileSample`.

        ``entries`` is the denominator shown in the sidecar header
        (``paired_records + orphan_entries``). The sink builds the
        ``FileSample`` directly rather than going through
        ``FileSample.from_bounded`` because the sink IS the invariant
        boundary — every bucket here is already capped by construction,
        so re-validation would be busy-work. ``from_bounded`` stays
        strict for test fixtures and any future external construction.
        Idempotent: a second call returns the cached sample.
        """
        if self._finalized:
            return self._sample
        if self._writer is not None:
            self._writer.finalize(entries)
        self._sample = FileSample(
            buckets={rid: tuple(items) for rid, items in self._buckets.items()},
            cap=self._cap,
        )
        self._finalized = True
        return self._sample


def write_broken_file(path, src_name, stats):
    """Write the ``.broken.txt`` sidecar from a populated ``FileStats``.

    Thin wrapper that flattens ``stats.reject_exemplars`` (a per-rule dict)
    and sorts by ``source_lines[0]`` so the rendered sidecar matches
    production encounter order. Suitable for tests and small-corpus paths
    where the sampled set fits in memory; production cleaning streams
    entries through ``BrokenFileWriter`` directly so memory stays bounded.
    """
    with BrokenFileWriter(path, src_name) as writer:
        flattened = [
            entry for bucket in stats.reject_exemplars.values() for entry in bucket
        ]
        flattened.sort(key=lambda e: e.source_lines[0])
        for entry in flattened:
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
    ``.broken.txt`` sidecar. The per-NORAD breakdown is shallow-copied per
    ID so caller mutations do not leak back into the live ``FileStats``;
    integer NORAD IDs and ``RuleID`` (``StrEnum``) members both serialise
    natively under ``json.dumps`` — int keys auto-stringify and StrEnum
    keys coerce to their stable wire token.
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
        "quarantined_norad_ids": {
            nid: dict(cats) for nid, cats in stats.quarantined_norad_ids.items()
        },
    }


def format_reject_lines(stats):
    """Render grouped reject exemplars for the ``validate`` summary.

    Walks rule IDs in descending order of total occurrences from
    ``stats.reject_counts`` and emits up to N exemplars per rule from
    ``stats.reject_exemplars``, each rendered via :func:`_format_diagnostic`
    so column ranges / observed / expected / tier survive into the
    operator view. Related diagnostics fold onto indented continuation
    lines, identical to ``.broken.txt``. A trailing ``...and X more``
    appears under a rule when its bucket is shorter than the rule total.
    A single noisy rule cannot hide rarer defects (issue #21).
    """
    blocks = []
    for rule_id, total in sorted(
        stats.reject_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        bucket = stats.reject_exemplars.get(rule_id, [])
        lines = [f"  {rule_id} ({total:,}):"]
        for entry in bucket:
            if len(entry.source_lines) == 2:
                location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
            else:
                location = str(entry.source_lines[0])
            lines.append(f"    line {location}: {_format_diagnostic(entry.primary)}")
            for extra in entry.related:
                lines.append(f"      and: {_format_diagnostic(extra)}")
        remaining = total - len(bucket)
        if remaining > 0:
            lines.append(f"    ...and {remaining:,} more")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


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


# How many filenames to enumerate before collapsing the trailing tail into
# a "+N more" suffix in the per-NORAD breakdown's Files column. Keeps the
# cell width bounded for satellites quarantined across many source files
# without coupling to any specific filename convention; the full file list
# for a given NORAD can be recovered by grepping the per-file ``.broken.txt``
# sidecars (``broken-noradids.ndjson`` carries only catalog IDs).
_PER_NORAD_FILES_PREVIEW = 5


def _aggregate_per_norad(all_stats):
    """Roll the per-file per-NORAD breakdowns up into a corpus-wide view.

    Returns a ``dict[int, dict]`` keyed by NORAD ID; each value has
    ``"total"`` (int), ``"categories"`` (``{RuleID: count}`` summed across
    files — the dict key is kept named ``categories`` since the column
    header in ``report.md`` reads "Defect categories" to stay readable),
    and ``"files"`` (set of source filenames where the ID had at least one
    quarantine). Memory is O(unique IDs × (|RuleID| + |source files|)) —
    bounded by the satellite catalog (~tens of thousands) and the small
    fixed number of source files in a corpus run, so the rollup stays
    constant-memory regardless of total reject count.
    """
    rollup = {}
    for stats in all_stats:
        for nid, categories in stats.quarantined_norad_ids.items():
            if not categories:
                continue
            entry = rollup.setdefault(
                nid, {"total": 0, "categories": {}, "files": set()}
            )
            entry["files"].add(stats.src_name)
            for cat, count in categories.items():
                entry["total"] += count
                entry["categories"][cat] = entry["categories"].get(cat, 0) + count
    return rollup


def _format_per_norad_categories(categories):
    """Render a per-NORAD ``categories`` mapping as ``"a (2), b (1)"`` text.

    Sorted by count descending then rule-ID ascending so the order is
    deterministic and the dominant defect surfaces first. ``str(cat)``
    coerces ``RuleID`` enum members via their ``StrEnum`` value, so the
    output matches the stable wire tokens (``"TLE-CHK-001"``, etc.) used
    elsewhere in the report.
    """
    items = sorted(categories.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{cat} ({count})" for cat, count in items)


def _format_per_norad_files(files):
    """Render a set of filenames as ``"a, b, c, d, e, +N more"`` text.

    Files are sorted alphabetically — `tleYYYY.txt` corpora sort by year as
    a side effect, but the helper makes no assumption about the naming
    convention. The first ``_PER_NORAD_FILES_PREVIEW`` names are shown
    verbatim; any trailing remainder collapses to ``", +N more"`` so the
    Files column stays bounded for persistent satellites.
    """
    ordered = sorted(files)
    if len(ordered) <= _PER_NORAD_FILES_PREVIEW:
        return ", ".join(ordered)
    head = ", ".join(ordered[:_PER_NORAD_FILES_PREVIEW])
    return f"{head}, +{len(ordered) - _PER_NORAD_FILES_PREVIEW} more"


def _format_per_norad_section(all_stats, top_n):
    """Render the ``## Per-NORAD breakdown`` Markdown section as a line list.

    Rows are sorted by quarantined-record count descending then NORAD ID
    ascending — deterministic so cross-run diffs of ``report.md`` show only
    real changes. When the rollup has more than ``top_n`` rows the table is
    truncated to ``top_n`` and an italicised "...and N more" footer points
    the operator at ``broken-noradids.ndjson`` for the full catalog; pass
    ``top_n=None`` to disable the cap entirely (used by tests asserting the
    long tail). Returns the lines including a leading blank-line separator,
    so the caller can ``lines += _format_per_norad_section(...)`` without
    inserting glue.
    """
    rollup = _aggregate_per_norad(all_stats)
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
        cats = _format_per_norad_categories(entry["categories"])
        files = _format_per_norad_files(entry["files"])
        lines.append(f"| {nid} | {entry['total']:,} | {cats} | {files} |")
    if top_n is not None and len(items) > top_n:
        remaining = len(items) - top_n
        lines += [
            "",
            f"_...and {remaining:,} more — "
            "see broken-noradids.ndjson for the full list._",
        ]
    return lines


def format_run_report(all_stats, top_n=100):
    """Render a Markdown report aggregating every processed file.

    Written to ``<out-dir>/report.md`` after a ``clean`` run: corpus
    totals, the percentage cleaned/quarantined, the corpus-wide fix and
    rule-ID counts, a per-file breakdown table, a rule reference section
    auto-generated from ``diagnostics.RULES`` for every rule that fired in
    this run, and a per-NORAD breakdown table capped at ``top_n`` rows
    (default 100; pass ``None`` to render every quarantined satellite).
    Percentages use ``paired_records + orphan_entries`` as the denominator
    — equal to ``clean + quarantined`` by the FileStats invariant, so the
    cleaned and quarantined shares sum to 100 % even when orphans are
    present.
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

    lines += _format_per_norad_section(all_stats, top_n=top_n)
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
        ids |= set(stats.quarantined_norad_ids)
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
