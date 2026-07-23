"""Per-file statistics dataclasses (``FileStats`` et al.), the ``summary_dict``
and ``build_run_envelope`` JSON shapes, and the Markdown / JSON run-report writers."""

import dataclasses
import datetime
import json
import os
import sys
from collections import Counter

from lintle import __version__, fsutil, report_aggregation
from lintle.categories import FixClass
from lintle.diagnostics import RULES, Diagnostic, RepairTier, RuleID


def utc_stamp() -> str:
    """Return the current UTC time as an ISO 8601 string (``%Y-%m-%dT%H:%M:%SZ``).

    Shared by ``format_run_report`` and ``report_writers._render_header`` so
    the timestamp format is defined once. Resume's compact filename stamp
    (``%Y%m%dT%H%M%SZ``) is intentionally different and lives in resume.py.
    """
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# How many quarantined records to retain in memory as exemplars per
# ``RuleID`` for the ``validate`` summary. The full byte-faithful catalog
# goes straight to the ``.broken.txt`` sidecar via ``BrokenFileWriter`` —
# this bound only caps the per-rule in-memory display sample, so peak
# memory stays constant even on files where every record is corrupt.
# Total ceiling per file is ``|RuleID| × PER_RULE_EXEMPLAR_BOUND``. Owned
# here — alongside the :class:`FileSample` dataclass it defaults — because
# the sample shape is report-layer state; ``report_writers.QuarantineSink`` (the
# canonical cap-enforcement boundary) imports it from here.
PER_RULE_EXEMPLAR_BOUND = 5


@dataclasses.dataclass(slots=True)
class QuarantineEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful. ``primary``
    is the headline :class:`Diagnostic`; ``related`` carries any secondary
    diagnostics, rendered on indented continuation lines. ``norad_id``
    (issue #9) carries the 5-digit catalog ID extracted from line 1,
    populated by ``pipeline._record_quarantine`` — consumed by the structured
    ``report.jsonl`` emitter, not rendered into ``.broken.txt``. The sole
    production construction site (``pipeline._record_quarantine``) builds
    this dataclass by keyword, so field order is not load-bearing — ``norad_id``
    stays last only because it is the one field carrying a default. This is
    the report-layer twin of :class:`repair.Quarantined`;
    ``pipeline._record_quarantine`` unpacks a ``Quarantined`` (or an orphan's
    fields) and rebuilds it here.
    """

    raw_lines: list[bytes]
    source_lines: list[int]
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()
    norad_id: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FileSample:
    """Immutable, per-file bounded sample of quarantined records (issue #19).

    Produced by :meth:`QuarantineSink.finalize`; consumed by the
    ``.broken.txt`` sidecar writer (``report_writers.write_broken_file``). Frozen so
    post-finalize consumers cannot accidentally mutate the sample — the
    per-rule cap invariant is locked in at construction time. ``cap``
    travels with the sample so renderers can surface truncation against
    the bound that was in force when the sample was built.

    ``dropped_count`` (issue #46) records how many entries the sink
    dropped per rule because the bucket was already at ``cap``. It is
    derivable from ``quarantine_counts - len(buckets[rule])`` but stored
    explicitly so programmatic consumers (JSON output, aggregators) can
    read it as a first-class field. Missing keys mean zero drops.
    """

    buckets: dict[RuleID, tuple[QuarantineEntry, ...]]
    cap: int
    dropped_count: dict[RuleID, int] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_bounded(
        cls,
        cap: int,
        entries_by_rule: dict[RuleID, tuple[QuarantineEntry, ...]],
        dropped_count: dict[RuleID, int] | None = None,
    ) -> FileSample:
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
    def empty(cls, cap: int) -> FileSample:
        """Empty sentinel — saves renderer consumers a None-check per file."""
        return cls(buckets={}, cap=cap, dropped_count={})


@dataclasses.dataclass(slots=True)
class NoradTracker:
    """Per-NORAD per-rule quarantine accounting for one source file (issue #47).

    Wraps the previously-raw ``dict[int, dict[RuleID, int]]`` field on
    :class:`FileStats` so all mutations route through :meth:`record` —
    a single named entry point future writers can grep for instead of
    reinventing the ``setdefault``/``+1`` dance. The read surface is the
    public ``counts`` dict by deliberate choice: production consumers
    (``summary_dict``, ``report_aggregation.aggregate_per_norad``,
    ``aggregate_broken_norad_ids``) each want different access shapes
    (``.items()``, key iteration, value-clone) and a proxy-method API would
    add surface tax with no encapsulation gain.

    Bounded by the satellite catalog and the ``RuleID`` enum — never
    "full", no drops, no cap. Mutable through its life; no freeze
    boundary. No ``merge`` method — corpus rollup stays a free function in
    :func:`report_aggregation.aggregate_per_norad` so the per-NORAD data shape
    stays free to evolve (timestamps, provenance) without breaking a monoid
    contract.
    """

    counts: dict[int, Counter[RuleID]] = dataclasses.field(default_factory=dict)

    def record(self, norad_id: int, rule_id: RuleID) -> None:
        """Tally one quarantine for ``norad_id`` against ``rule_id``.

        The only sanctioned mutation entry point. Outer key is the
        catalog-decoded integer NORAD ID; inner key is the
        :class:`RuleID` member, value is a running count. First call
        for a NORAD initialises the bucket; repeated calls accrue.
        """
        self.counts.setdefault(norad_id, Counter())[rule_id] += 1


@dataclasses.dataclass(slots=True)
class FileStats:
    """Accumulated results for one processed source file.

    Three independent counters disambiguate what was previously a single
    ``total_records`` tally (issue #5): ``paired_records`` counts proper
    2-line entries; ``orphan_entries`` counts unpaired single lines surfaced
    as findings; ``input_lines_seen`` counts every physical line read,
    including blanks the pairing loop drops. The invariant
    ``paired_records + orphan_entries == clean_count + quarantined_count``
    holds — orphans still flow through ``_record_quarantine`` so they are tallied
    in ``quarantined_count`` and ``quarantine_counts['TLE-PAIR-001']``.

    ``quarantine_counts`` is keyed by :class:`diagnostics.RuleID` string values
    (e.g. ``"TLE-CHK-001"``) so reports cite stable, citable rule IDs.

    ``quarantine_sample`` (issue #19) is an immutable :class:`FileSample`
    holding the per-rule bounded sample of quarantined records. The cap
    is enforced structurally by :class:`QuarantineSink` during processing,
    not by this dataclass. The byte-faithful full catalog is streamed to
    ``.broken.txt`` during processing.
    """

    src_name: str
    # Per-file timing + size for the v1 run envelope (issue #20). Defaults
    # keep validate-mode fixtures and unit tests that build a bare
    # FileStats() valid; production captures are set by
    # ``pipeline.process_file`` from ``time.monotonic()`` and
    # ``Path(src_path).stat().st_size`` respectively. ``elapsed_seconds`` is the
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
    fix_counts: Counter[FixClass] = dataclasses.field(default_factory=Counter)
    quarantine_counts: Counter[RuleID] = dataclasses.field(default_factory=Counter)
    quarantine_sample: FileSample = dataclasses.field(
        default_factory=lambda: FileSample.empty(PER_RULE_EXEMPLAR_BOUND)
    )
    # Per-NORAD breakdown for records quarantined in this file (issue #47:
    # wrapped behind :class:`NoradTracker` so the single-writer convention
    # is enforced by the type — ``stats.quarantined_norad_ids.record(...)``
    # is the only sanctioned mutation entry point). Outer keys (in
    # ``.counts``) are the 5-digit catalog numbers decoded once at quarantine
    # time from line-1 columns 3-7; each value is a ``{RuleID: count}``
    # dict tallying which diagnostics that satellite hit in this file.
    # Bounded by the satellite catalog (~tens of thousands of IDs corpus-
    # wide) and the ``RuleID`` enum, so the per-file structure is O(catalog
    # × |RuleID|) — independent of quarantine count and constant-memory at
    # corpus scale. Field name preserved to keep the ``summary_dict`` JSON
    # output key contract intact; only the type changed.
    quarantined_norad_ids: NoradTracker = dataclasses.field(
        default_factory=NoradTracker
    )


# Lower bound on the ``elapsed_seconds`` denominator when computing the
# per-file ``records_per_sec`` field (issue #20, gate R2). Clamping keeps
# the field a stable ``float`` for sub-millisecond runs — typed downstream
# consumers (Go unmarshalers, strict TypeScript) never see ``null``. Files
# faster than this floor report an upper-bound rate; the value is
# documented in ``ARCHITECTURE.md`` §6 (Outputs & machine-readable contracts)
# so consumers can recognise the saturation case.
_RECORDS_PER_SEC_FLOOR = 0.001


def summary_dict(stats: FileStats) -> dict[str, object]:
    """Return a JSON-serialisable summary of one file's stats.

    The ``quarantine_counts`` map is keyed by stable rule IDs (e.g.
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
        "quarantine_counts": dict(stats.quarantine_counts),
        # Per-rule count of entries the sink had to drop because the bucket
        # was already at cap (issue #46). Always present (empty when no
        # truncation) so programmatic consumers can rely on the field;
        # shallow-copied so caller mutations don't leak into the frozen
        # FileSample. Parallels quarantine_counts in shape and key vocabulary.
        "dropped_counts": dict(stats.quarantine_sample.dropped_count),
        "quarantined_norad_ids": {
            nid: dict(rule_counts)
            for nid, rule_counts in stats.quarantined_norad_ids.counts.items()
        },
    }


def stats_from_summary(data: dict[str, object]) -> FileStats:
    """Rebuild a :class:`FileStats` from a JSON-deserialised :func:`summary_dict`.

    The inverse of :func:`summary_dict`, used by a resumed ``clean`` run (issue
    #56) to fold files completed in an earlier session into the final report
    without re-reading them. JSON stringifies every dict key, so the rule, fix,
    and NORAD keys are coerced back to their live types (:class:`RuleID`,
    :class:`FixClass`, ``int``) — a rebuilt instance is then indistinguishable
    from a freshly-produced one. The bytes-bearing ``quarantine_sample`` exemplars
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
        fix_counts=Counter({FixClass(k): v for k, v in data["fix_counts"].items()}),
        quarantine_counts=Counter(
            {RuleID(k): v for k, v in data["quarantine_counts"].items()}
        ),
        quarantine_sample=FileSample.from_bounded(
            cap=PER_RULE_EXEMPLAR_BOUND,
            entries_by_rule={},
            dropped_count={RuleID(k): v for k, v in data["dropped_counts"].items()},
        ),
        quarantined_norad_ids=NoradTracker(
            counts={
                int(nid): Counter(
                    {RuleID(rule): count for rule, count in rule_counts.items()}
                )
                for nid, rule_counts in data["quarantined_norad_ids"].items()
            }
        ),
    )


# Pinned schema version for the ``--report json`` envelope (issue #20).
# Stored as a string so future additive minor revisions can use tags like
# ``"3.1"`` without changing the field's JSON type — adding optional
# fields stays under ``"3"``, renaming or removing fields bumps it again.
# Bumped "1" -> "2" when the per-rule counts key became ``quarantine_counts``.
# Bumped "2" -> "3" when ``run.failed_files`` and ``summary.failed_count``
# were added (issue #83) — both fields are always present (``[]`` / ``0``
# on a fully successful run) so the shape is stable for typed consumers.
_ENVELOPE_SCHEMA_VERSION = "3"


def build_run_envelope(
    all_stats: list[FileStats],
    *,
    command: str,
    started_at: str,
    elapsed_seconds: float,
    failed_files: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Return the top-level versioned ``--report json`` envelope (issue #20).

    The shape is locked in ``ARCHITECTURE.md`` §6 (Outputs & machine-readable
    contracts): a single object with ``schema_version``, ``run``,
    ``environment``, ``summary``, and ``files``. ``run.elapsed_seconds`` is the
    parent-process wall-clock duration captured by ``cli.main`` — independent
    of per-file worker durations in ``files[i].elapsed_seconds``, which the
    consumer must NOT sum to derive a corpus total. ``environment`` is a strict
    allowlist (tool + Python version only); no env vars, paths, or hostnames
    leak. The per-file shape is exactly ``summary_dict(s)`` for each ``s`` in
    ``all_stats``, preserving order so consumers see deterministic file ordering
    matching ``report.md``.

    ``failed_files`` is the ``list[tuple[path, error_str]]`` returned by
    ``worker_pool.run_workers`` (issue #83). It is serialised into
    ``run.failed_files`` as ``[{"file": basename, "error": str}, ...]`` sorted
    by ``file`` for byte-determinism, and its length is mirrored into
    ``summary.failed_count``. Both fields are always present (``[]`` / ``0``
    on a fully successful run) so the envelope shape is stable.
    """
    if failed_files is None:
        failed_files = []
    totals = report_aggregation.aggregate(all_stats)
    # Serialise failed_files sorted by basename for byte-determinism.
    serialised_failures = sorted(
        [{"file": os.path.basename(p), "error": err} for p, err in failed_files],
        key=lambda e: e["file"],
    )
    return {
        "schema_version": _ENVELOPE_SCHEMA_VERSION,
        "run": {
            "command": command,
            "timestamp": started_at,
            "elapsed_seconds": float(elapsed_seconds),
            "failed_files": serialised_failures,
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
            "paired_records": totals.paired,
            "orphan_entries": totals.orphans,
            "input_lines_seen": totals.lines_seen,
            "clean_count": totals.clean,
            "quarantined_count": totals.quarantined,
            "failed_count": len(failed_files),
            "fix_counts": dict(totals.fixes),
            "quarantine_counts": dict(totals.quarantines),
        },
        "files": [summary_dict(s) for s in all_stats],
    }


def format_diagnostic(diag: Diagnostic) -> str:
    """Render one :class:`Diagnostic` as a single-line string fragment.

    Format: ``rule: <id>[ (<tier>)][ - col(s) <range>][ observed=...][
    expected=...][ - <note>]``. The bracketed pieces are emitted only when
    their underlying field is set. Shared low-level renderer: the
    ``.broken.txt`` sidecar (``report_writers._render_entry``) consumes it,
    so it is a public name on this leaf module and ``report_writers`` imports
    it — keeping the dependency one-way, acyclic, and on an intentional public
    surface.
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


# How many filenames to enumerate before collapsing the trailing tail into
# a "+N more" suffix in the per-NORAD breakdown's Files column. Keeps the
# cell width bounded for satellites quarantined across many source files
# without coupling to any specific filename convention; the full file list
# for a given NORAD can be recovered by grepping the per-file ``.broken.txt``
# sidecars (``broken-noradids.ndjson`` carries only catalog IDs).
_PER_NORAD_FILES_PREVIEW = 5


def format_run_report(
    all_stats: list[FileStats],
    top_n: int | None = 100,
    failed_files: list[tuple[str, str]] | None = None,
) -> str:
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
    present. When ``failed_files`` is non-empty, a ``## Failures`` section
    lists each file that could not be processed alongside its error string —
    omitted entirely on a clean run so a zero-failure report.md is unchanged.
    """
    if failed_files is None:
        failed_files = []
    timestamp = utc_stamp()
    totals = report_aggregation.aggregate(all_stats)
    paired = totals.paired
    orphans = totals.orphans
    lines_seen = totals.lines_seen
    clean = totals.clean
    quarantined = totals.quarantined
    fixes = totals.fixes
    quarantines = totals.quarantines
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
    if quarantines:
        # ``Dropped`` (issue #46): per-rule corpus-wide count of entries
        # the sink had to drop because the in-memory bucket was at cap.
        # Most rules read 0 here on healthy runs; non-zero values mean
        # the sample under-represents that rule's true scale — operator
        # should consult the ``.broken.txt`` sidecar for the full catalog.
        lines.append("| Rule | Count | Dropped |")
        lines.append("|------|------:|--------:|")
        for key, value in sorted(quarantines.items(), key=lambda kv: -kv[1]):
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

    if quarantines:
        lines += ["", "## Rule reference", ""]
        for key in sorted(quarantines):
            # RULES.get() accepts both RuleID (StrEnum) and its string value;
            # both hash identically, so a single dict lookup covers both cases.
            spec = RULES.get(key)
            if spec is None:
                lines.append(f"- `{key}` — (unknown rule)")
            else:
                lines.append(f"- `{key}` — {spec.short_title}")

    lines += report_aggregation.format_per_norad_section(
        all_stats, top_n, files_preview=_PER_NORAD_FILES_PREVIEW
    )

    if failed_files:
        lines += ["", "## Failures", ""]
        lines.append("| File | Error |")
        lines.append("|------|-------|")
        for path, err in sorted(failed_files, key=lambda fe: os.path.basename(fe[0])):
            # Escape ``|`` and collapse newlines so a path/error containing
            # either can never break the Markdown table row.
            name = os.path.basename(path).replace("|", r"\|")
            msg = err.replace("\n", " ").replace("\r", " ").replace("|", r"\|")
            lines.append(f"| {name} | {msg} |")

    lines.append("")
    return "\n".join(lines)


def write_run_report(
    path: str,
    all_stats: list[FileStats],
    failed_files: list[tuple[str, str]] | None = None,
) -> None:
    """Write the Markdown run report (``format_run_report``) to ``path``,
    atomically and durably via tmp + :func:`fsutil.durable_replace` (issue #58).
    ``failed_files`` is forwarded to :func:`format_run_report` so the
    ``## Failures`` section appears when any input files could not be processed.
    """
    fsutil.durable_write_text(
        path, format_run_report(all_stats, failed_files=failed_files)
    )


def write_run_json(path, envelope):
    """Write the run ``envelope`` (the exact object ``--report json`` prints) to
    ``path``, atomically and durably via tmp + :func:`fsutil.durable_replace`.
    Serialised byte-for-byte like the ``--report json`` stdout path: ``indent=2``,
    insertion order, a trailing newline, UTF-8 — so the persisted ``report.json``
    is a byte-identical twin of the stdout envelope (Critical Rules #1/#2)."""
    fsutil.durable_write_text(path, json.dumps(envelope, indent=2) + "\n")
