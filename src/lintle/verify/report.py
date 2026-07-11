"""The ``lintle verify`` finding taxonomy and its deterministic output.

Verify has its OWN rule family (``VRFY-*``), distinct from the clean-time
``TLE-*`` RuleIDs, because these defects are only visible ACROSS records or via
physics — a different detection stage. Kept inside ``lintle.verify`` (never in
the clean-path ``diagnostics.py``) so the wall between cleaning and verifying
stays intact. Output is byte-deterministic: suspects sort by a stable key and
serialize as compact ASCII JSON, so two runs over the same output produce
identical bytes."""

import dataclasses
import json
from enum import StrEnum
from pathlib import Path

VERIFY_DIRNAME = "verify"
SUSPECTS_NAME = "suspects.jsonl"
SUMMARY_JSON = "summary.json"
SUMMARY_MD = "summary.md"
SCHEMA_VERSION = "1"


class VrfyRule(StrEnum):
    """Stable wire tokens for verify findings. ``hard`` rules convict (exit 1);
    the ``soft`` rule is 'worth a look' telemetry that never blocks (exit 0)."""

    REVALIDATE_FAIL = "VRFY-REVALIDATE-FAIL"  # a cleaned record no longer validates
    EPOCH_CONFLICT = "VRFY-EPOCH-CONFLICT"  # same (catalog, epoch), different bytes
    INTERIOR_MUT = "VRFY-INTERIOR-MUT"  # cleaned differs from source off the edges
    ORIGIN_MISSING = "VRFY-ORIGIN-MISSING"  # no source origin found in the window


_HARD = frozenset(
    {VrfyRule.REVALIDATE_FAIL, VrfyRule.EPOCH_CONFLICT, VrfyRule.INTERIOR_MUT}
)


@dataclasses.dataclass(slots=True, frozen=True)
class Suspect:
    """One verify finding. ``catalog``/``epoch_key`` are the satellite id and
    chronological key (``-1``/``-1.0`` when the record was too broken to parse);
    ``src_file`` is the cleaned-file stem and ``index`` the record's ordinal
    within it — together the stable position address."""

    rule: VrfyRule
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


def exit_code(suspects: list[Suspect]) -> int:
    """1 if any hard suspect was found, else 0. (Operational errors — a missing
    cleaned tree, etc. — are exit 2, decided by the caller.)"""
    return 1 if any(s.severity == "hard" for s in suspects) else 0


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


def render_suspects_jsonl(suspects: list[Suspect]) -> bytes:
    """The ``suspects.jsonl`` body: one compact ASCII JSON object per suspect,
    LF-terminated, sorted by the stable key — byte-identical across runs."""
    lines = [
        json.dumps(_suspect_dict(s), ensure_ascii=True, separators=(",", ":"))
        for s in sorted(suspects, key=_sort_key)
    ]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("ascii")


def _tally(suspects: list[Suspect]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in sorted(suspects, key=_sort_key):
        counts[s.rule.value] = counts.get(s.rule.value, 0) + 1
    return counts


def render_summary_json(suspects: list[Suspect], *, checked: dict[str, int]) -> bytes:
    """Machine-readable roll-up: schema version, per-rule counts, hard/soft
    totals, and the caller-supplied ``checked`` census (records/files/etc.)."""
    hard = sum(1 for s in suspects if s.severity == "hard")
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "checked": dict(sorted(checked.items())),
        "counts": _tally(suspects),
        "hard": hard,
        "soft": len(suspects) - hard,
        "exit_code": exit_code(suspects),
    }
    return (
        json.dumps(envelope, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")


def render_summary_md(suspects: list[Suspect], *, checked: dict[str, int]) -> str:
    """A short human summary — the census, then a per-rule table, then the
    verdict line."""
    hard = sum(1 for s in suspects if s.severity == "hard")
    out = ["# lintle verify\n"]
    out.append("## Checked\n")
    for k, v in sorted(checked.items()):
        out.append(f"- {k}: {v}")
    out.append("\n## Findings\n")
    counts = _tally(suspects)
    if counts:
        out.append("| Rule | Count | Severity |")
        out.append("| --- | --- | --- |")
        for rule, n in counts.items():
            sev = "hard" if VrfyRule(rule) in _HARD else "soft"
            out.append(f"| {rule} | {n} | {sev} |")
    else:
        out.append("No suspects — all checks passed.")
    verdict = "FAIL" if hard else "PASS"
    out.append(f"\n**Verdict: {verdict}** ({hard} hard, {len(suspects) - hard} soft)\n")
    return "\n".join(out)


def write_reports(
    out_dir: str, suspects: list[Suspect], *, checked: dict[str, int]
) -> Path:
    """Write ``<out-dir>/verify/{suspects.jsonl,summary.json,summary.md}`` and
    return the verify directory. Deterministic bytes; overwrites in place."""
    vdir = Path(out_dir) / VERIFY_DIRNAME
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / SUSPECTS_NAME).write_bytes(render_suspects_jsonl(suspects))
    (vdir / SUMMARY_JSON).write_bytes(render_summary_json(suspects, checked=checked))
    # summary.md is the human-readable file (em-dashes, etc.) — UTF-8, not the
    # ASCII-deterministic structured pair above.
    (vdir / SUMMARY_MD).write_text(
        render_summary_md(suspects, checked=checked), encoding="utf-8"
    )
    return vdir
