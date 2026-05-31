"""Compute and render the delta between two lintle run outputs (issue #10).

Reads each run's ``report.jsonl`` and aggregates per-rule counts using only
the *primary* diagnostic — mirroring ``pipeline._record_reject``, which tallies
``stats.reject_counts[primary.rule_id]`` and never the ``related[]`` array. That
shared counting rule is what keeps ``lintle diff``'s numbers in agreement with
each run's own ``report.md``. Output is a deterministic plain-text report: a
corpus-level per-rule delta, then a per-file (per-basename) breakdown.

The per-file breakdown is keyed by ``report.jsonl``'s ``file`` field, which is
a basename (``pipeline.py``: ``os.path.basename``). That key is unambiguous
because ``clean`` accepts only a single positional input, so within any
producible run each basename names exactly one file. A basename present in
only one run, however,
may have been fixed, removed, or renamed — so one-sided files are flagged, not
attributed. Memory is bounded by (distinct files × distinct RuleIDs) — tens of
files × ≤9 rules for a real corpus — never the number of findings. A new leaf
module: depends only on ``diagnostics`` and is imported by ``cli``.
"""

import collections
import dataclasses
import json
import os
import sys

from lintle.diagnostics import RULES, RuleID

_SCHEMA_VERSION = "1"
_FINDINGS_NAME = "report.jsonl"


class DiffError(Exception):
    """A run directory could not be read as a lintle findings set — a missing
    ``report.jsonl``, a malformed line, an unsupported ``schema_version``, or a
    finding with no primary ``rule_id``."""


def iter_findings(run_dir):
    """Yield ``(file, rule_id)`` for every finding in ``<run_dir>/report.jsonl``,
    one per line, in file order. The core reader: a generator that never holds
    more than a single line in memory. Raises :class:`DiffError` on a missing
    file, a malformed JSON line, a ``schema_version`` other than ``"1"``, or a
    line lacking ``rule_id`` — the diff refuses to count what it cannot
    interpret."""
    path = os.path.join(run_dir, _FINDINGS_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, 1):
                line = raw.strip()
                if line:
                    yield _finding_from_line(path, lineno, line)
    except (OSError, UnicodeDecodeError) as exc:
        # OSError: missing file, permission denied, a directory in place of the
        # file. UnicodeDecodeError (a ValueError, not an OSError): foreign bytes
        # in an always-UTF-8 artifact. Both mean "this isn't a lintle findings
        # file" — surface a clean DiffError rather than a raw traceback.
        raise DiffError(f"cannot read {path}: {exc}") from exc


def iter_primary_rule_ids(run_dir):
    """Yield the primary ``rule_id`` of every finding — the file-agnostic view
    of :func:`iter_findings`, for corpus-level aggregation."""
    for _file, rule_id in iter_findings(run_dir):
        yield rule_id


def _finding_from_line(path, lineno, line):
    """Parse one ``report.jsonl`` line and return its ``(file, rule_id)``.
    Raises :class:`DiffError` (citing ``path:lineno``) on malformed JSON, an
    unsupported ``schema_version``, or a missing ``rule_id``."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise DiffError(f"{path}:{lineno}: malformed JSON: {exc}") from exc
    version = record.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise DiffError(
            f"{path}:{lineno}: unsupported schema_version {version!r} "
            f"(this lintle understands {_SCHEMA_VERSION!r})"
        )
    rule_id = record.get("rule_id")
    if rule_id is None:
        raise DiffError(f"{path}:{lineno}: finding has no rule_id")
    return record.get("file"), rule_id


def aggregate(run_dir):
    """Return a :class:`collections.Counter` mapping primary ``rule_id`` →
    number of findings in ``run_dir``. Counts the primary diagnostic only,
    matching the producer's ``stats.reject_counts`` semantics."""
    return collections.Counter(iter_primary_rule_ids(run_dir))


def aggregate_by_file(run_dir):
    """Return ``{basename: Counter(rule_id → count)}`` for ``run_dir``. Keyed by
    the ``report.jsonl`` ``file`` basename, which uniquely names one file within
    a run (``clean`` accepts only a single positional input). Memory is bounded by
    (distinct files × distinct RuleIDs), not the number of findings."""
    by_file = collections.defaultdict(collections.Counter)
    for file, rule_id in iter_findings(run_dir):
        by_file[file][rule_id] += 1
    return dict(by_file)


@dataclasses.dataclass(frozen=True, slots=True)
class RuleDelta:
    """One rule's finding count in run A versus run B. ``delta`` is ``B - A``:
    positive means the defect class grew, negative means it shrank."""

    rule_id: str
    count_a: int
    count_b: int

    @property
    def delta(self):
        return self.count_b - self.count_a


@dataclasses.dataclass(frozen=True, slots=True)
class Delta:
    """The categorized difference between two runs' primary-rule counts.
    ``new`` appeared in B only, ``fixed`` vanished in B, ``changed`` shifted
    count while present in both, ``unchanged`` held steady. Every list is
    sorted by ``rule_id`` so rendered output is byte-for-byte deterministic."""

    new: tuple[RuleDelta, ...]
    fixed: tuple[RuleDelta, ...]
    changed: tuple[RuleDelta, ...]
    unchanged: tuple[RuleDelta, ...]


# How a basename relates to the two runs. ``BOTH`` files get true count deltas;
# one-sided files (``A_ONLY`` / ``B_ONLY``) are flagged but not attributed,
# because a basename in only one run may have been fixed, removed, or renamed.
_BOTH = "both"
_A_ONLY = "a_only"
_B_ONLY = "b_only"


@dataclasses.dataclass(frozen=True, slots=True)
class FileDelta:
    """One basename's findings change between runs. ``presence`` is one of
    ``"both"``, ``"a_only"``, ``"b_only"``. ``rules`` holds only the RuleDeltas
    that changed (rules unchanged within a both-run file are omitted), sorted by
    ``rule_id`` for deterministic output."""

    file: str
    presence: str
    rules: tuple[RuleDelta, ...]


def compute_delta(counts_a, counts_b):
    """Categorize every ``rule_id`` seen in either run into new / fixed /
    changed / unchanged. Pure function over two Counters — deterministic and
    independent of the input dicts' iteration order."""
    new, fixed, changed, unchanged = [], [], [], []
    for rule_id in sorted(set(counts_a) | set(counts_b)):
        a = counts_a.get(rule_id, 0)
        b = counts_b.get(rule_id, 0)
        rd = RuleDelta(rule_id, a, b)
        if a == 0:
            new.append(rd)
        elif b == 0:
            fixed.append(rd)
        elif a != b:
            changed.append(rd)
        else:
            unchanged.append(rd)
    return Delta(tuple(new), tuple(fixed), tuple(changed), tuple(unchanged))


def compute_file_delta(by_file_a, by_file_b):
    """Return a sorted tuple of :class:`FileDelta`, one per basename whose
    findings differ between the runs. Files with identical per-rule counts in
    both runs are omitted — a diff shows what changed. Reuses
    :func:`compute_delta` per file, so each file's rule-level breakdown follows
    exactly the same new/fixed/changed semantics as the corpus-level report."""
    out = []
    for file in sorted(set(by_file_a) | set(by_file_b)):
        a = by_file_a.get(file)
        b = by_file_b.get(file)
        if a is None:
            presence = _B_ONLY
        elif b is None:
            presence = _A_ONLY
        else:
            presence = _BOTH
        per_rule = compute_delta(
            a if a is not None else collections.Counter(),
            b if b is not None else collections.Counter(),
        )
        # Everything except unchanged is a change worth showing; re-sort the
        # union by rule_id so the file's rule lines read in stable order.
        changed = sorted(
            per_rule.new + per_rule.fixed + per_rule.changed,
            key=lambda rd: rd.rule_id,
        )
        if changed:
            out.append(FileDelta(file, presence, tuple(changed)))
    return tuple(out)


def _totals(by_file):
    """Sum a ``{file: Counter}`` map into one corpus-level Counter. Deriving the
    corpus totals from the per-file counts guarantees they always agree with the
    per-file breakdown — they cannot drift, sharing one aggregation pass."""
    total = collections.Counter()
    for counter in by_file.values():
        total.update(counter)
    return total


def _title(rule_id):
    """Return the short human title for ``rule_id``, or ``""`` if the ID is not
    in the current registry (e.g. a retired ID in an older run)."""
    try:
        spec = RULES[RuleID(rule_id)]
    except ValueError, KeyError:
        return ""
    return spec.short_title


def _signed(value):
    """Render a count delta with an explicit sign (``+26``, ``-6``)."""
    return f"+{value}" if value >= 0 else str(value)


def format_text(delta, *, run_a, run_b):
    """Render ``delta`` as a deterministic plain-text report and return it as a
    single string. Returning rather than printing keeps the renderer pure and
    byte-for-byte testable; the CLI prints the result."""
    out = [f"lintle diff: {run_a} -> {run_b}", ""]

    out.append(f"New defect classes in B ({len(delta.new)}):")
    out += _section(delta.new, lambda rd: f"+{rd.count_b}")

    out.append("")
    out.append(f"Fixed defect classes absent in B ({len(delta.fixed)}):")
    out += _section(delta.fixed, lambda rd: f"-{rd.count_a}")

    out.append("")
    out.append(f"Changed counts ({len(delta.changed)}):")
    out += _section(
        delta.changed,
        lambda rd: f"{rd.count_a} -> {rd.count_b} ({_signed(rd.delta)})",
    )

    out.append("")
    out.append(
        f"Summary: {len(delta.new)} new, {len(delta.fixed)} fixed, "
        f"{len(delta.changed)} changed, {len(delta.unchanged)} unchanged."
    )
    return "\n".join(out)


def _section(rule_deltas, count_fragment):
    """Render one report section's body lines. ``count_fragment`` formats the
    per-rule count cell (the new/fixed/changed columns differ). Returns
    ``["  (none)"]`` for an empty section so every section reads uniformly."""
    if not rule_deltas:
        return ["  (none)"]
    return [
        f"  {rd.rule_id}  {count_fragment(rd)}  {_title(rd.rule_id)}".rstrip()
        for rd in rule_deltas
    ]


_PRESENCE_LABEL = {
    _BOTH: "",
    _A_ONLY: " (only in run A — fixed, removed, or renamed)",
    _B_ONLY: " (only in run B — new, added, or renamed)",
}


def format_file_deltas(file_deltas):
    """Render the per-file section as a deterministic string. Both-run files
    show true ``a -> b`` deltas; one-sided files show the count observed on
    their single side (never a ``-> 0`` that would falsely imply the file went
    clean rather than being removed or renamed)."""
    if not file_deltas:
        return "Per-file changes (0):\n  (none)"
    out = [f"Per-file changes ({len(file_deltas)}):"]
    for fd in file_deltas:
        out.append(f"  {fd.file}{_PRESENCE_LABEL[fd.presence]}:")
        for rd in fd.rules:
            out.append("    " + _file_rule_line(fd.presence, rd))
    return "\n".join(out)


def _file_rule_line(presence, rd):
    """Render one rule line within a file block. A both-run file gets a signed
    delta; a one-sided file gets the bare count from the side it appears on."""
    if presence == _A_ONLY:
        cell = str(rd.count_a)
    elif presence == _B_ONLY:
        cell = str(rd.count_b)
    else:
        cell = f"{rd.count_a} -> {rd.count_b} ({_signed(rd.delta)})"
    return f"{rd.rule_id}  {cell}  {_title(rd.rule_id)}".rstrip()


def run(run_a, run_b):
    """Read both runs' ``report.jsonl``, compute the corpus and per-file deltas,
    print them, and return a process exit code: ``0`` on success, ``2`` if
    either run could not be read (operational error, matching the rest of the
    CLI). Reads each run once, deriving corpus totals from the per-file counts
    so the two sections cannot disagree."""
    try:
        by_file_a = aggregate_by_file(run_a)
        by_file_b = aggregate_by_file(run_b)
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    delta = compute_delta(_totals(by_file_a), _totals(by_file_b))
    file_deltas = compute_file_delta(by_file_a, by_file_b)
    print(format_text(delta, run_a=run_a, run_b=run_b))
    print()
    print(format_file_deltas(file_deltas))
    return 0
