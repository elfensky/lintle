"""The ``lintle verify`` finding taxonomy and its deterministic output.

Verify has its OWN rule family (``VRFY-*``), distinct from the clean-time
``TLE-*`` RuleIDs, because these defects are only visible ACROSS records or via
physics — a different detection stage. Kept inside ``lintle.verify`` (never in
the clean-path ``diagnostics.py``) so the wall between cleaning and verifying
stays intact. Output is byte-deterministic: suspects sort by a stable key and
serialize as compact ASCII JSON, so two runs over the same output produce
identical bytes."""

import contextlib
import dataclasses
import json
import operator
from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from lintle import VERIFY_DIRNAME, chunking, fsutil
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.verify import grouping

SUSPECTS_STEM = "suspects"
SUSPECTS_SUFFIX = ".jsonl"
SUMMARY_JSON = "summary.json"
SUMMARY_MD = "summary.md"
SCHEMA_VERSION = "2"

_README = """\
# 04-verify — independent audit of 01-cleaned

- `suspects.NNNNN.jsonl` — flagged records, one JSON object per line: `hard`
  means must-fix (the run exits 1), `soft` means inconclusive telemetry that
  never blocks.
- `summary.json` / `summary.md` — audit tallies and the pass/fail verdict.

Regenerate with `lintle verify`.
"""


class VerifyRule(StrEnum):
    """Stable wire tokens for verify findings. ``hard`` rules convict (exit 1);
    the ``soft`` rule is 'worth a look' telemetry that never blocks (exit 0)."""

    REVALIDATE_FAIL = "VRFY-REVALIDATE-FAIL"  # a cleaned record no longer validates
    EPOCH_CONFLICT = "VRFY-EPOCH-CONFLICT"  # same (catalog, epoch), different bytes
    INTERIOR_MUT = "VRFY-INTERIOR-MUT"  # cleaned differs from source off the edges
    ORIGIN_MISSING = "VRFY-ORIGIN-MISSING"  # no source origin found in the window
    ORBIT_ERROR = "VRFY-ORBIT-ERROR"  # sgp4 rejects the elements (parse or physics)
    ORBIT_OUTLIER = "VRFY-ORBIT-OUTLIER"  # residual outlier vs neighbours (soft)


_HARD = frozenset(
    {
        VerifyRule.REVALIDATE_FAIL,
        VerifyRule.EPOCH_CONFLICT,
        VerifyRule.INTERIOR_MUT,
        VerifyRule.ORBIT_ERROR,
    }
)


@dataclasses.dataclass(slots=True, frozen=True)
class Suspect:
    """One verify finding. ``catalog``/``epoch_key`` are the satellite id and
    chronological key (``-1``/``-1.0`` when the record was too broken to parse);
    ``src_file`` is the cleaned-file stem and ``index`` the record's ordinal
    within it — together the stable position address."""

    rule: VerifyRule
    catalog: int
    epoch_key: float
    src_file: str
    index: int
    detail: str

    @property
    def severity(self) -> str:
        return "hard" if self.rule in _HARD else "soft"


def _sort_key(s: Suspect) -> tuple[str, int, float, str, int]:
    return (s.rule.value, s.catalog, s.epoch_key, s.src_file, s.index)


def _suspect_dict(s: Suspect) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rule": s.rule.value,
        "severity": s.severity,
        "catalog": s.catalog,
        "epoch_key": s.epoch_key,
        "src_file": s.src_file,
        "index": s.index,
        "detail": s.detail,
    }


def _suspect_line(s: Suspect) -> str:
    """One suspect as a compact ASCII JSON object (no newline). The single
    serialization used by both the list renderer and the streaming sink, so their
    bytes cannot drift. ``ensure_ascii`` escapes any tab/newline in ``detail``, so
    the result is always a single tab-free ASCII line — safe to spill tab-framed."""
    return json.dumps(_suspect_dict(s), ensure_ascii=True, separators=(",", ":"))


def render_suspects_jsonl(suspects: list[Suspect]) -> bytes:
    """The ``suspects.jsonl`` body: one compact ASCII JSON object per suspect,
    LF-terminated, sorted by the stable key — byte-identical across runs."""
    lines = [_suspect_line(s) for s in sorted(suspects, key=_sort_key)]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("ascii")


def _tally(suspects: list[Suspect]) -> Counter[str]:
    counts: Counter[str] = Counter()
    counts.update(s.rule.value for s in suspects)
    return counts


def _summary_json_bytes(
    counts: dict[str, int],
    hard: int,
    total: int,
    *,
    checked: dict[str, int],
    epoch_distribution: dict[str, int],
) -> bytes:
    """The ``summary.json`` bytes from a per-rule tally and the hard/total counts —
    ``sort_keys=True`` makes it independent of ``counts`` insertion order, so the
    list path and the sink's running ``Counter`` render identically.
    ``epoch_distribution`` (the per-month record-density histogram) is a sibling
    of ``checked``, not nested inside it — ``checked`` stays scalar tallies."""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "checked": dict(sorted(checked.items())),
        "epoch_distribution": dict(sorted(epoch_distribution.items())),
        "counts": dict(counts),
        "hard": hard,
        "soft": total - hard,
        "exit_code": 1 if hard else 0,
    }
    return (fsutil.json_document(envelope)).encode("ascii")


def _summary_md_str(
    counts: dict[str, int],
    hard: int,
    total: int,
    *,
    checked: dict[str, int],
    epoch_distribution: dict[str, int],
) -> str:
    """The ``summary.md`` text from a tally + counts. Rules are emitted in
    ``sorted`` order so the sink's Counter and the list path's tally agree.
    The epoch distribution (record density per ``YYYY-MM``, honest naming — this
    is not corpus coverage) gets its own short section, separate from ``Checked``."""
    out = ["# lintle verify\n", "## Checked\n"]
    for k, v in sorted(checked.items()):
        out.append(f"- {k}: {v}")
    if epoch_distribution:
        out.append("\n### Epoch distribution\n")
        for month, n in sorted(epoch_distribution.items()):
            out.append(f"- {month}  {n}")
    out.append("\n## Findings\n")
    if counts:
        out.append("| Rule | Count | Severity |")
        out.append("| --- | --- | --- |")
        for rule, n in sorted(counts.items()):
            sev = "hard" if VerifyRule(rule) in _HARD else "soft"
            out.append(f"| {rule} | {n} | {sev} |")
    else:
        out.append("No suspects — all checks passed.")
    verdict = "FAIL" if hard else "PASS"
    out.append(f"\n**Verdict: {verdict}** ({hard} hard, {total - hard} soft)\n")
    return "\n".join(out)


def render_summary_json(
    suspects: list[Suspect],
    *,
    checked: dict[str, int],
    epoch_distribution: dict[str, int] = {},  # noqa: B006 — read-only, never mutated
) -> bytes:
    """Machine-readable roll-up: schema version, per-rule counts, hard/soft
    totals, the caller-supplied ``checked`` census (records/files/etc.), and the
    ``epoch_distribution`` record-density histogram (a sibling key, empty by
    default)."""
    hard = sum(1 for s in suspects if s.severity == "hard")
    return _summary_json_bytes(
        _tally(suspects),
        hard,
        len(suspects),
        checked=checked,
        epoch_distribution=epoch_distribution,
    )


def render_summary_md(
    suspects: list[Suspect],
    *,
    checked: dict[str, int],
    epoch_distribution: dict[str, int] = {},  # noqa: B006 — read-only, never mutated
) -> str:
    """A short human summary — the census, the epoch distribution (when
    non-empty), then a per-rule table, then the verdict line."""
    hard = sum(1 for s in suspects if s.severity == "hard")
    return _summary_md_str(
        _tally(suspects),
        hard,
        len(suspects),
        checked=checked,
        epoch_distribution=epoch_distribution,
    )


type _SpillItem = tuple[tuple[str, int, float, str, int], str]


def _encode_spill(item: _SpillItem) -> str:
    """A ``(sort_key, suspect_line)`` pair as a tab-framed spill row: the five
    sort-key columns (so a run decodes back to its sort key without re-parsing
    JSON) then the verbatim ``_suspect_line`` — which carries no raw tab, so the
    framing round-trips."""
    (rule, catalog, epoch_key, src_file, index), line = item
    prefix = f"{rule}\t{catalog}\t{epoch_key!r}\t{src_file}\t{index}"
    return f"{prefix}\t{line}\n"


def _decode_spill(row: str) -> _SpillItem:
    """Inverse of :func:`_encode_spill`: ``(sort_key, suspect_json_line)``."""
    rule, catalog, epoch_key, src_file, index, line = row.rstrip("\n").split("\t", 5)
    return (rule, int(catalog), float(epoch_key), src_file, int(index)), line


class SuspectSink:
    """Constant-memory accumulator for verify suspects: `grouping`'s external
    merge-sorter plus a running tally. ``add`` suspects during the checks, then
    ``write`` drains the sorter into a globally-sorted ``suspects.jsonl`` and
    renders ``summary.{json,md}`` from the tally — byte-identical to the
    list-based renderers, but peak memory is one chunk, not the whole suspect set
    (issue #156).

    Suspects are pre-encoded to ``(sort_key, rendered_line)`` on the way in, and
    that pair — not the ``Suspect`` — is what the sorter carries. Deliberate: the
    line is rendered exactly once, so what the merge emits is what was tallied,
    with no second rendering that could drift from the first."""

    def __init__(self, chunk_size: int = 200_000) -> None:
        self._sorter: grouping.ExternalSorter[_SpillItem, tuple] = (
            grouping.ExternalSorter(
                key=operator.itemgetter(0),
                encode=_encode_spill,
                decode=_decode_spill,
                chunk_size=chunk_size,
                prefix="lintle-verify-suspects-",
            )
        )
        self.counts: Counter[str] = Counter()
        # Per-stem tallies for the phase-3 results table. Keyed by the stem each
        # suspect names, so findings raised after the streaming pass (the
        # contradiction and orbit passes) are attributed too, and the columns
        # sum to `hard`/`total`. One entry per cleaned stem — not per record.
        self.hard_by_stem: Counter[str] = Counter()
        self.soft_by_stem: Counter[str] = Counter()
        self.hard = 0
        self.total = 0

    def add(self, s: Suspect) -> None:
        self._sorter.add((_sort_key(s), _suspect_line(s)))
        self.counts[s.rule.value] += 1
        if s.severity == "hard":
            self.hard += 1
            self.hard_by_stem[s.src_file] += 1
        else:
            self.soft_by_stem[s.src_file] += 1
        self.total += 1

    def add_all(self, suspects: Iterable[Suspect]) -> None:
        for s in suspects:
            self.add(s)

    @property
    def exit_code(self) -> int:
        return 1 if self.hard else 0

    def write(
        self,
        out_dir: str,
        *,
        checked: dict[str, int],
        epoch_distribution: dict[str, int] = {},  # noqa: B006 — read-only, never mutated
        chunk_records: int = CHUNK_RECORDS_DEFAULT,
    ) -> Path:
        """Write ``<out-dir>/04-verify/{suspects.NNNNN.jsonl,summary.json,summary.md}``
        and return the verify directory. Consumes the sink (drains the temp runs);
        deterministic bytes, overwrites in place. The suspects stream is chunked
        into a ``suspects.NNNNN.jsonl`` set. ``epoch_distribution`` is the
        per-month record-density histogram (informational, empty by default)."""
        vdir = Path(out_dir) / VERIFY_DIRNAME
        vdir.mkdir(parents=True, exist_ok=True)
        # The sorter merges equal keys in add order, matching what
        # sorted(key=_sort_key) gives the list path. closing() releases the temp
        # runs even if the writer raises part-way through the drain.
        with (
            contextlib.closing(self._sorter.sorted_records()) as merged,
            chunking.ChunkedWriter(
                str(vdir), SUSPECTS_STEM, SUSPECTS_SUFFIX, chunk_records
            ) as out,
        ):
            for _, line in merged:
                out.write((line + "\n").encode("ascii"))
        # Both summaries commit through the one sanctioned durable path.
        fsutil.durable_write_text(
            str(vdir / SUMMARY_JSON),
            _summary_json_bytes(
                self.counts,
                self.hard,
                self.total,
                checked=checked,
                epoch_distribution=epoch_distribution,
            ).decode("ascii"),
            encoding="ascii",
        )
        # summary.md is the human-readable file (em-dashes, etc.) — UTF-8, not the
        # ASCII-deterministic structured pair above.
        fsutil.durable_write_text(
            str(vdir / SUMMARY_MD),
            _summary_md_str(
                self.counts,
                self.hard,
                self.total,
                checked=checked,
                epoch_distribution=epoch_distribution,
            ),
        )
        fsutil.write_step_readme(vdir, _README)
        return vdir
