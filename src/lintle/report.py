"""Per-file statistics dataclasses (``FileStats`` et al.), the ``validate``
summary renderers, and the Markdown run-report writer."""

import dataclasses
import datetime
import sys

from lintle import __version__, fsutil
from lintle.categories import FixClass
from lintle.diagnostics import RULES, Diagnostic, RepairTier, RuleID

# How many quarantined records to retain in memory as exemplars per
# ``RuleID`` for the ``validate`` summary. The full byte-faithful catalog
# goes straight to the ``.broken.txt`` sidecar via ``BrokenFileWriter`` —
# this bound only caps the per-rule in-memory display sample, so peak
# memory stays constant even on files where every record is corrupt.
# Total ceiling per file is ``|RuleID| × _PER_RULE_EXEMPLAR_BOUND``. Owned
# here — alongside the :class:`FileSample` dataclass it defaults — because
# the sample shape is report-layer state; ``report_writers.RejectSink`` (the
# canonical cap-enforcement boundary) imports it from here.
_PER_RULE_EXEMPLAR_BOUND = 5


@dataclasses.dataclass
class RejectEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful. ``primary``
    is the headline :class:`Diagnostic`; ``related`` carries any secondary
    diagnostics, rendered on indented continuation lines. ``norad_id``
    (issue #9) carries the 5-digit catalog ID extracted from line 1,
    populated by ``pipeline._record_reject`` — consumed by the structured
    ``report.jsonl`` emitter, not rendered into ``.broken.txt``. ``norad_id``
    MUST stay the trailing field: the pipeline call site constructs this
    dataclass positionally, so any reorder would silently corrupt the
    per-call arguments. This is the report-layer twin of
    :class:`repair.Rejected`; ``pipeline._record_reject`` unpacks a
    ``Rejected`` (or an orphan's fields) and rebuilds it here.
    """

    raw_lines: list
    source_lines: list
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()
    norad_id: int | None = None


@dataclasses.dataclass(frozen=True)
class FileSample:
    """Immutable, per-file bounded sample of quarantined records (issue #19).

    Produced by :meth:`RejectSink.finalize`; consumed by renderers
    (:func:`format_reject_lines`, :func:`write_broken_file`). Frozen so
    post-finalize consumers cannot accidentally mutate the sample — the
    per-rule cap invariant is locked in at construction time. ``cap``
    travels with the sample so renderers can surface truncation against
    the bound that was in force when the sample was built.

    ``dropped_count`` (issue #46) records how many entries the sink
    dropped per rule because the bucket was already at ``cap``. It is
    derivable from ``reject_counts - len(buckets[rule])`` but stored
    explicitly so programmatic consumers (JSON output, aggregators) can
    read it as a first-class field. Missing keys mean zero drops.
    """

    buckets: dict
    cap: int
    dropped_count: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def from_bounded(cls, cap, entries_by_rule, dropped_count=None):
        """Build a FileSample, asserting every bucket honours ``cap``.

        Test-friendly constructor: clones each bucket into a ``tuple`` so
        the result is structurally immutable, and raises ``ValueError``
        (naming the rule and counts) if any bucket exceeds ``cap``.
        Strict by design — silent over-cap inputs would mask test fixture
        mistakes that the sink's cap-enforcement is meant to prevent in
        production. ``dropped_count`` (issue #46) is optional and shallow-
        cloned so caller mutations to the source dict do not leak through.
        """
        for rule_id, entries in entries_by_rule.items():
            if len(entries) > cap:
                raise ValueError(
                    f"bucket {rule_id.name} has {len(entries)} entries; cap is {cap}"
                )
        return cls(
            buckets={rid: tuple(entries) for rid, entries in entries_by_rule.items()},
            cap=cap,
            dropped_count=dict(dropped_count) if dropped_count else {},
        )

    @classmethod
    def empty(cls, cap):
        """Empty sentinel — saves renderer consumers a None-check per file."""
        return cls(buckets={}, cap=cap, dropped_count={})


@dataclasses.dataclass
class NoradTracker:
    """Per-NORAD per-rule quarantine accounting for one source file (issue #47).

    Wraps the previously-raw ``dict[int, dict[RuleID, int]]`` field on
    :class:`FileStats` so all mutations route through :meth:`record` —
    a single named entry point future writers can grep for instead of
    reinventing the ``setdefault``/``+1`` dance. The read surface is the
    public ``counts`` dict by deliberate choice: production consumers
    (``summary_dict``, ``_aggregate_per_norad``, ``aggregate_broken_norad_ids``)
    each want different access shapes (``.items()``, key iteration,
    value-clone) and a proxy-method API would add surface tax with no
    encapsulation gain.

    Bounded by the satellite catalog and the ``RuleID`` enum — never
    "full", no drops, no cap. Mutable through its life; no freeze
    boundary. No ``merge`` method — corpus rollup stays a free
    function in :func:`_aggregate_per_norad` so the per-NORAD data
    shape stays free to evolve (timestamps, provenance) without
    breaking a monoid contract.
    """

    counts: dict = dataclasses.field(default_factory=dict)

    def record(self, norad_id, rule_id):
        """Tally one quarantine for ``norad_id`` against ``rule_id``.

        The only sanctioned mutation entry point. Outer key is the
        catalog-decoded integer NORAD ID; inner key is the
        :class:`RuleID` member, value is a running count. First call
        for a NORAD initialises the bucket; repeated calls accrue.
        """
        per_rule = self.counts.setdefault(norad_id, {})
        per_rule[rule_id] = per_rule.get(rule_id, 0) + 1


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
    """

    src_name: str
    # Per-file timing + size for the v1 run envelope (issue #20). Defaults
    # keep validate-mode fixtures and unit tests that build a bare
    # FileStats() valid; production captures are set by
    # ``pipeline.process_file`` from ``time.monotonic()`` and
    # ``os.path.getsize()`` respectively. ``elapsed_seconds`` is the
    # worker's wall-clock duration on this file — NEVER summed across
    # workers to derive a corpus total (use the parent's wall-clock
    # captured in ``cli.main`` for that).
    elapsed_seconds: float = 0.0
    bytes: int = 0
    paired_records: int = 0
    orphan_entries: int = 0
    input_lines_seen: int = 0
    # Running count of physical bytes read from the source — every line
    # including the blanks the pairing loop drops, so it tracks the true file
    # offset and reaches ``bytes`` (st_size) at EOF. Drives the byte-progress
    # bar (issue #53); updated by ``pipeline.iter_records`` alongside
    # ``input_lines_seen``.
    bytes_consumed: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_counts: dict = dataclasses.field(default_factory=dict)
    reject_sample: FileSample = dataclasses.field(
        default_factory=lambda: FileSample.empty(_PER_RULE_EXEMPLAR_BOUND)
    )
    # Per-NORAD breakdown for records quarantined in this file (issue #47:
    # wrapped behind :class:`NoradTracker` so the single-writer convention
    # is enforced by the type — ``stats.quarantined_norad_ids.record(...)``
    # is the only sanctioned mutation entry point). Outer keys (in
    # ``.counts``) are the 5-digit catalog numbers decoded once at reject
    # time from line-1 columns 3-7; each value is a ``{RuleID: count}``
    # dict tallying which diagnostics that satellite hit in this file.
    # Bounded by the satellite catalog (~tens of thousands of IDs corpus-
    # wide) and the ``RuleID`` enum, so the per-file structure is O(catalog
    # × |RuleID|) — independent of reject count and constant-memory at
    # corpus scale. Field name preserved to keep the ``summary_dict`` JSON
    # output key contract intact; only the type changed.
    quarantined_norad_ids: NoradTracker = dataclasses.field(
        default_factory=NoradTracker
    )


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


# Lower bound on the ``elapsed_seconds`` denominator when computing the
# per-file ``records_per_sec`` field (issue #20, gate R2). Clamping keeps
# the field a stable ``float`` for sub-millisecond runs — typed downstream
# consumers (Go unmarshalers, strict TypeScript) never see ``null``. Files
# faster than this floor report an upper-bound rate; the value is
# documented in ``docs/superpowers/specs/2026-05-25-report-json-envelope.md``
# §5 so consumers can recognise the saturation case.
_RECORDS_PER_SEC_FLOOR = 0.001


def summary_dict(stats):
    """Return a JSON-serialisable summary of one file's stats.

    The ``reject_counts`` map is keyed by stable rule IDs (e.g.
    ``"TLE-CHK-001"``) — the same handles cited in ``report.md`` and the
    ``.broken.txt`` sidecar. The per-NORAD breakdown is shallow-copied per
    ID so caller mutations do not leak back into the live ``FileStats``;
    integer NORAD IDs and ``RuleID`` (``StrEnum``) members both serialise
    natively under ``json.dumps`` — int keys auto-stringify and StrEnum
    keys coerce to their stable wire token. ``elapsed_seconds`` and
    ``bytes`` (issue #20) surface the worker's wall-clock duration and
    the input file size; ``records_per_sec`` is the clamped throughput
    (``paired_records / max(elapsed_seconds, 0.001)``) and is always a
    float — never ``null`` — so the rigid envelope contract holds for
    typed consumers.
    """
    elapsed = max(stats.elapsed_seconds, _RECORDS_PER_SEC_FLOOR)
    return {
        "src_name": stats.src_name,
        "elapsed_seconds": stats.elapsed_seconds,
        "bytes": stats.bytes,
        "records_per_sec": stats.paired_records / elapsed,
        "paired_records": stats.paired_records,
        "orphan_entries": stats.orphan_entries,
        "input_lines_seen": stats.input_lines_seen,
        "clean_count": stats.clean_count,
        "quarantined_count": stats.quarantined_count,
        "fix_counts": dict(stats.fix_counts),
        "reject_counts": dict(stats.reject_counts),
        # Per-rule count of entries the sink had to drop because the bucket
        # was already at cap (issue #46). Always present (empty when no
        # truncation) so programmatic consumers can rely on the field;
        # shallow-copied so caller mutations don't leak into the frozen
        # FileSample. Parallels reject_counts in shape and key vocabulary.
        "dropped_counts": dict(stats.reject_sample.dropped_count),
        "quarantined_norad_ids": {
            nid: dict(rule_counts)
            for nid, rule_counts in stats.quarantined_norad_ids.counts.items()
        },
    }


def stats_from_summary(data):
    """Rebuild a :class:`FileStats` from a JSON-deserialised :func:`summary_dict`.

    The inverse of :func:`summary_dict`, used by a resumed ``clean`` run (issue
    #56) to fold files completed in an earlier session into the final report
    without re-reading them. JSON stringifies every dict key, so the rule, fix,
    and NORAD keys are coerced back to their live types (:class:`RuleID`,
    :class:`FixClass`, ``int``) — a rebuilt instance is then indistinguishable
    from a freshly-produced one. The bytes-bearing ``reject_sample`` exemplars
    are deliberately not persisted (§13.1): the sample reconstructs empty of
    buckets, carrying only the per-rule ``dropped_count`` so the round-trip is
    exact. The derived ``records_per_sec`` field is recomputed by
    :func:`summary_dict`, so it is ignored here.
    """
    return FileStats(
        src_name=data["src_name"],
        elapsed_seconds=data["elapsed_seconds"],
        bytes=data["bytes"],
        paired_records=data["paired_records"],
        orphan_entries=data["orphan_entries"],
        input_lines_seen=data["input_lines_seen"],
        clean_count=data["clean_count"],
        quarantined_count=data["quarantined_count"],
        fix_counts={FixClass(k): v for k, v in data["fix_counts"].items()},
        reject_counts={RuleID(k): v for k, v in data["reject_counts"].items()},
        reject_sample=FileSample.from_bounded(
            cap=_PER_RULE_EXEMPLAR_BOUND,
            entries_by_rule={},
            dropped_count={RuleID(k): v for k, v in data["dropped_counts"].items()},
        ),
        quarantined_norad_ids=NoradTracker(
            counts={
                int(nid): {RuleID(rule): count for rule, count in rule_counts.items()}
                for nid, rule_counts in data["quarantined_norad_ids"].items()
            }
        ),
    )


# Pinned schema version for the ``--report json`` envelope (issue #20).
# Stored as a string so future additive minor revisions can use tags like
# ``"1.1"`` without changing the field's JSON type — adding optional
# fields stays under ``"1"``, renaming or removing fields bumps to ``"2"``.
_ENVELOPE_SCHEMA_VERSION = "1"


def build_run_envelope(all_stats, *, command, started_at, elapsed_seconds):
    """Return the top-level versioned ``--report json`` envelope (issue #20).

    The shape is locked in
    ``docs/superpowers/specs/2026-05-25-report-json-envelope.md``: a single
    object with ``schema_version``, ``run``, ``environment``, ``summary``,
    and ``files``. ``run.elapsed_seconds`` is the parent-process wall-clock
    duration captured by ``cli.main`` — independent of per-file worker
    durations in ``files[i].elapsed_seconds``, which the consumer must NOT
    sum to derive a corpus total. ``environment`` is a strict allowlist
    (tool + Python version only); no env vars, paths, or hostnames leak.
    The per-file shape is exactly ``summary_dict(s)`` for each ``s`` in
    ``all_stats``, preserving order so consumers see deterministic file
    ordering matching ``report.md``.
    """
    totals = _aggregate(all_stats)
    paired = totals.paired
    orphans = totals.orphans
    lines_seen = totals.lines_seen
    clean = totals.clean
    quarantined = totals.quarantined
    fixes = totals.fixes
    rejects = totals.rejects
    return {
        "schema_version": _ENVELOPE_SCHEMA_VERSION,
        "run": {
            "command": command,
            "timestamp": started_at,
            "elapsed_seconds": float(elapsed_seconds),
        },
        "environment": {
            "tool_version": __version__,
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
        },
        "summary": {
            "files_processed": len(all_stats),
            "paired_records": paired,
            "orphan_entries": orphans,
            "input_lines_seen": lines_seen,
            "clean_count": clean,
            "quarantined_count": quarantined,
            "fix_counts": dict(fixes),
            "reject_counts": dict(rejects),
        },
        "files": [summary_dict(s) for s in all_stats],
    }


def _format_diagnostic(diag):
    """Render one :class:`Diagnostic` as a single-line string fragment.

    Format: ``rule: <id>[ (<tier>)][ - col(s) <range>][ observed=...][
    expected=...][ - <note>]``. The bracketed pieces are emitted only when
    their underlying field is set. Shared low-level renderer: the
    ``validate`` summary (:func:`format_reject_lines`) and the
    ``.broken.txt`` sidecar (``report_writers._render_entry``) both consume
    it, so it lives in this leaf and ``report_writers`` imports it — keeping
    the dependency one-way and acyclic.
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


def format_reject_lines(stats):
    """Render grouped reject exemplars for the ``validate`` summary.

    Walks rule IDs in descending order of total occurrences from
    ``stats.reject_counts`` and emits up to N exemplars per rule from
    ``stats.reject_sample.buckets``, each rendered via
    :func:`_format_diagnostic` so column ranges / observed / expected /
    tier survive into the operator view. Related diagnostics fold onto
    indented continuation lines, identical to ``.broken.txt``. A
    trailing ``...and X more`` appears under a rule when its bucket is
    shorter than the rule total. A single noisy rule cannot hide rarer
    defects (issue #21).

    When the sink dropped entries for a rule (issue #46), the heading
    switches from the simple ``(M):`` form to ``(N of M hits, K
    dropped):`` so an operator sees the truncation at a glance, not
    just through the trailing ``...and X more`` hint. Rules that fit
    under cap keep the simple heading — the verbose form is reserved
    for the case where it actually carries new information.
    """
    blocks = []
    for rule_id, total in sorted(
        stats.reject_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        bucket = stats.reject_sample.buckets.get(rule_id, ())
        dropped = stats.reject_sample.dropped_count.get(rule_id, 0)
        if dropped > 0:
            heading = (
                f"  {rule_id} ({len(bucket):,} of {total:,} hits, {dropped:,} dropped):"
            )
        else:
            heading = f"  {rule_id} ({total:,}):"
        lines = [heading]
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


@dataclasses.dataclass(frozen=True)
class _Totals:
    """Corpus-wide totals summed across every file's stats by :func:`_aggregate`.

    Consumed by attribute (not positional unpacking) at the two call sites —
    the ``--report json`` envelope and ``report.md`` — so the field set can
    change without silently misassigning a renumbered tuple.
    """

    paired: int
    orphans: int
    lines_seen: int
    clean: int
    quarantined: int
    fixes: dict
    rejects: dict
    dropped: dict


def _aggregate(all_stats):
    """Sum every file's stats into corpus-wide totals and count dicts.

    Returns a :class:`_Totals`; its ``dropped`` map (issue #46) sums each
    file's ``reject_sample.dropped_count`` so ``report.md`` can show a
    corpus-wide Dropped column alongside the per-rule reject totals.
    """
    fixes = {}
    rejects = {}
    dropped = {}
    for stats in all_stats:
        for key, value in stats.fix_counts.items():
            fixes[key] = fixes.get(key, 0) + value
        for key, value in stats.reject_counts.items():
            rejects[key] = rejects.get(key, 0) + value
        for key, value in stats.reject_sample.dropped_count.items():
            dropped[key] = dropped.get(key, 0) + value
    return _Totals(
        paired=sum(s.paired_records for s in all_stats),
        orphans=sum(s.orphan_entries for s in all_stats),
        lines_seen=sum(s.input_lines_seen for s in all_stats),
        clean=sum(s.clean_count for s in all_stats),
        quarantined=sum(s.quarantined_count for s in all_stats),
        fixes=fixes,
        rejects=rejects,
        dropped=dropped,
    )


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
    ``"total"`` (int), ``"rules"`` (``{RuleID: count}`` summed across files —
    the ``report.md`` column header still reads "Defect categories" for
    readability), and ``"files"`` (set of source filenames where the ID had
    at least one quarantine). Memory is O(unique IDs × (|RuleID| + |source
    files|)) — bounded by the satellite catalog (~tens of thousands) and the
    small fixed number of source files in a corpus run, so the rollup stays
    constant-memory regardless of total reject count.
    """
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


def _format_per_norad_rules(rule_counts):
    """Render a per-NORAD ``{RuleID: count}`` mapping as ``"a (2), b (1)"`` text.

    Sorted by count descending then rule-ID ascending so the order is
    deterministic and the dominant defect surfaces first. ``str(rule)``
    coerces ``RuleID`` enum members via their ``StrEnum`` value, so the
    output matches the stable wire tokens (``"TLE-CHK-001"``, etc.) used
    elsewhere in the report.
    """
    items = sorted(rule_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{rule} ({count})" for rule, count in items)


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
        rules = _format_per_norad_rules(entry["rules"])
        files = _format_per_norad_files(entry["files"])
        lines.append(f"| {nid} | {entry['total']:,} | {rules} | {files} |")
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
    totals = _aggregate(all_stats)
    paired = totals.paired
    orphans = totals.orphans
    lines_seen = totals.lines_seen
    clean = totals.clean
    quarantined = totals.quarantined
    fixes = totals.fixes
    rejects = totals.rejects
    dropped = totals.dropped
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
        # ``Dropped`` (issue #46): per-rule corpus-wide count of entries
        # the sink had to drop because the in-memory bucket was at cap.
        # Most rules read 0 here on healthy runs; non-zero values mean
        # the sample under-represents that rule's true scale — operator
        # should consult the ``.broken.txt`` sidecar for the full catalog.
        lines.append("| Rule | Count | Dropped |")
        lines.append("|------|------:|--------:|")
        for key, value in sorted(rejects.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {value:,} | {dropped.get(key, 0):,} |")
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
    """Write the Markdown run report (``format_run_report``) to ``path``,
    atomically and durably via tmp + :func:`fsutil.durable_replace` (issue #58).
    """
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(format_run_report(all_stats))
    fsutil.durable_replace(tmp, path)
