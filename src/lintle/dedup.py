"""``lintle dedup`` — emit a de-duplicated 'latest re-issue only' import list.

Space-track republishes the *same* orbit at the *same* epoch with only a bumped
element-set (or revolution) number; the faithful ``cleaned/`` archive keeps every
copy by design. ``dedup`` reads that archive (never mutating it) and writes a
single ingest-ready ``import.txt`` under ``<out-dir>/dedup``: one card per
``(catalog, epoch)``, keeping the latest re-issue (highest element-set number).

Benign re-issues — same parsed orbital state — collapse silently. A *genuine*
contradiction (same satellite and epoch, a different orbit) is never resolved in
silence: the latest is still written, but the group is flagged in ``notes.jsonl``
and the run exits non-zero so a human reviews it. When a ``verify`` run's
``suspects.jsonl`` is present, every hard suspect is excluded from the import list
first. Constant memory: records stream through ``verify``'s external sort and only
one ``(catalog, epoch)`` group is held at a time. Output bytes are deterministic.

``dedup`` shares ``verify.checks.orbital_state`` / ``element_set`` so the two
passes agree, byte-for-byte, on 'same orbit' and 'which is latest'."""

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

from lintle import term
from lintle.verify import checks, grouping, records
from lintle.verify.records import CleanedRecord

DEDUP_DIRNAME = "dedup"
IMPORT_NAME = "import.txt"
NOTES_NAME = "notes.jsonl"
SUMMARY_NAME = "summary.json"
SCHEMA_VERSION = "1"


@dataclasses.dataclass(slots=True, frozen=True)
class Group:
    """One collapsed ``(catalog, epoch)`` group: the ``kept`` card (latest
    re-issue), the ``dropped`` duplicates, and whether the group held more than
    one distinct orbital state (a genuine contradiction, kept-but-flagged)."""

    kept: CleanedRecord
    dropped: list[CleanedRecord]
    conflict: bool


def _elset_or_min(line1: str) -> int:
    """Element-set number as a sort key; an unparseable one sorts below every
    real number so a parseable re-issue always wins the 'latest' pick."""
    es = checks.element_set(line1)
    return es if es is not None else -1


def _collapse(group: list[CleanedRecord]) -> Group:
    """Keep the highest element-set (ties broken by source position for a
    deterministic pick); the rest are dropped. ``conflict`` iff the group carries
    more than one distinct parsed orbital state — same object and instant, yet a
    different orbit. On a wrap (9999 -> 0001) the orbit is identical, so keeping
    the numerically-highest is still safe."""
    kept = max(group, key=lambda r: (_elset_or_min(r.line1), r.src_file, r.index))
    dropped = sorted(
        (r for r in group if r is not kept), key=lambda r: (r.src_file, r.index)
    )
    states = {checks.orbital_state(r.line1, r.line2) for r in group}
    return Group(kept, dropped, len(states) > 1)


def _groups(sorted_records: Iterator[CleanedRecord]) -> Iterator[Group]:
    """Collapse a stream sorted by ``(catalog, epoch_key)`` group by group. Holds
    one group at a time — a handful of re-issues in validated ``cleaned/`` output.
    ponytail: a pathological giant group can't occur in validated cleaned records
    (each has a parseable, unique-ish key); a corrupt tree is ``verify``'s job."""
    group_key: tuple[int, float] | None = None
    buf: list[CleanedRecord] = []
    for rec in sorted_records:
        key = (rec.catalog, rec.epoch_key)
        if key != group_key:
            if buf:
                yield _collapse(buf)
            group_key = key
            buf = []
        buf.append(rec)
    if buf:
        yield _collapse(buf)


def _load_hard_positions(out_dir: str) -> set[tuple[str, int]]:
    """The ``(src_file, index)`` of every hard suspect in a prior ``verify`` run's
    ``suspects.jsonl`` — excluded from the import list. Empty set when no verify
    run exists (dedup still collapses re-issues). ponytail: the set is bounded by
    the hard-suspect count, which is the rare exception (~0 on healthy output),
    not the norm."""
    path = Path(out_dir) / "verify" / "suspects.jsonl"
    if not path.is_file():
        return set()
    hard: set[tuple[str, int]] = set()
    with path.open(encoding="ascii") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("severity") == "hard":
                hard.add((row["src_file"], row["index"]))
    return hard


def _card(rec: CleanedRecord) -> dict[str, object]:
    return {
        "src_file": rec.src_file,
        "index": rec.index,
        "element_set": checks.element_set(rec.line1),
    }


def _note_bytes(g: Group) -> bytes:
    """One compact ASCII JSON note for a collapsed group — fixed key order, so
    two runs over the same output produce identical bytes."""
    note = {
        "schema_version": SCHEMA_VERSION,
        "catalog": g.kept.catalog,
        "epoch_key": g.kept.epoch_key,
        "conflict": g.conflict,
        "kept": _card(g.kept),
        "dropped": [_card(r) for r in g.dropped],
    }
    return (json.dumps(note, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def run_dedup(out_dir: str) -> int:
    """De-duplicate a clean run's ``<out-dir>/cleaned`` into
    ``<out-dir>/dedup/import.txt`` (+ ``notes.jsonl`` and ``summary.json``).
    Returns the exit code: ``0`` clean, ``1`` genuine contradiction(s) arbitrated
    (review the notes), ``2`` operational error (no cleaned output)."""
    stems = records.cleaned_stems(out_dir)
    if not stems:
        term.error(
            f"no cleaned output found under {Path(out_dir) / 'cleaned'!s}.\n"
            "  run 'lintle clean' first, or point at its --out-dir."
        )
        return 2

    hard = _load_hard_positions(out_dir)
    sorter = grouping.ExternalSorter()
    n_read = n_excluded = 0
    for stem in stems:
        for rec in records.iter_file(out_dir, stem):
            n_read += 1
            if (rec.src_file, rec.index) in hard:
                n_excluded += 1
                continue
            sorter.add(rec)

    ddir = Path(out_dir) / DEDUP_DIRNAME
    ddir.mkdir(parents=True, exist_ok=True)
    n_written = n_dropped = n_collapsed = n_conflicts = 0
    # Stream both outputs in sorted (catalog, epoch) order — constant memory even
    # when import.txt is corpus-scale.
    with (
        (ddir / IMPORT_NAME).open("w", encoding="ascii", newline="\n") as imp,
        (ddir / NOTES_NAME).open("wb") as notes,
    ):
        for g in _groups(sorter.sorted_records()):
            imp.write(f"{g.kept.line1}\n{g.kept.line2}\n")
            n_written += 1
            if g.dropped:
                notes.write(_note_bytes(g))
                n_collapsed += 1
                n_dropped += len(g.dropped)
                if g.conflict:
                    n_conflicts += 1

    code = 1 if n_conflicts else 0
    summary = {
        "schema_version": SCHEMA_VERSION,
        "cleaned_files": len(stems),
        "records_read": n_read,
        "excluded_hard_suspects": n_excluded,
        "records_written": n_written,
        "records_dropped": n_dropped,
        "groups_collapsed": n_collapsed,
        "conflicts_flagged": n_conflicts,
        "exit_code": code,
    }
    body = json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    (ddir / SUMMARY_NAME).write_bytes(body.encode("ascii"))

    verdict = (
        f"{n_written} records written, {n_dropped} re-issue duplicate(s) collapsed"
    )
    if code:
        term.error(
            f"dedup: {n_conflicts} genuine contradiction(s) arbitrated — review "
            f"{ddir / NOTES_NAME!s}\n  {verdict}"
        )
    else:
        term.note(f"dedup: PASS — {verdict}\n  see {ddir / IMPORT_NAME!s}")
    return code
