"""Tests for ``lintle dedup`` — the de-duplicated 'latest re-issue only' import
list. Cleaned output is immutable; dedup only reads it and writes under
``<out-dir>/05-dedup``."""

import json

from lintle import CLEANED_DIRNAME, DEDUP_DIRNAME, VERIFY_DIRNAME, cli, dedup, tle
from lintle.chunking import ChunkedReader
from lintle.verify import epoch
from lintle.verify.records import CleanedRecord

# A canonical known-good record (Vanguard 1, NORAD 00005).
L1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
L2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"


def fix(line: str) -> str:
    """Return ``line`` with a correct column-69 checksum."""
    return line[:68] + str(tle.compute_checksum(line))


def with_elset(line1: str, n: int) -> str:
    """L1 with an explicit element-set number (cols 65-68) and a fixed checksum —
    the only bytes a benign re-issue changes, so orbital state is untouched."""
    return fix(line1[:64] + f"{n:04d}")


def reissued_l2() -> str:
    """L2 with a bumped revolution number (cols 64-68) — same orbital state, the
    other admin-only re-issue shape (issue #142's catalog-16922 case)."""
    return fix(L2[:63] + "99999")


def mutated_l2() -> str:
    """L2 with one inclination digit changed — same catalog+epoch, a genuinely
    different orbit (a real contradiction, not a benign re-issue)."""
    return fix(L2[:13] + "3" + L2[14:])


def other_catalog(n: int) -> str:
    """L1 for a different catalog id (still valid, different (catalog, epoch))."""
    return fix(L1[:2] + f"{n:05d}" + L1[7:])


def build_tree(tmp_path, cleaned_pairs, *, suspects=None, stem="tle01"):
    """Write a minimal clean-run output tree (cleaned/ + optional verify/
    suspects.00001.jsonl chunk); return the out-dir as a string."""
    out = tmp_path / "output"
    (out / CLEANED_DIRNAME).mkdir(parents=True, exist_ok=True)
    (out / CLEANED_DIRNAME / f"{stem}.00001.cleaned.txt").write_text(
        "".join(f"{a}\n{b}\n" for a, b in cleaned_pairs), encoding="ascii"
    )
    if suspects is not None:
        (out / VERIFY_DIRNAME).mkdir(parents=True, exist_ok=True)
        (out / VERIFY_DIRNAME / "suspects.00001.jsonl").write_text(
            "".join(json.dumps(s) + "\n" for s in suspects), encoding="ascii"
        )
    return str(out)


def read_import(out) -> str:
    reader = ChunkedReader(out / DEDUP_DIRNAME, "import", ".txt")
    return "".join(f"{line.decode('ascii')}\n" for line in reader.iter_lines())


def read_notes(out) -> list[dict]:
    reader = ChunkedReader(out / DEDUP_DIRNAME, "notes", ".jsonl")
    return [json.loads(line) for line in reader.iter_lines() if line]


def read_summary(out) -> dict:
    return json.loads(
        (out / DEDUP_DIRNAME / "summary.json").read_text(encoding="ascii")
    )


def read_chunk_bytes(out, stem, suffix) -> bytes:
    """Concatenate a chunk set's committed chunk files in index order — the
    byte-deterministic equivalent of the pre-chunking single file's bytes."""
    reader = ChunkedReader(out / DEDUP_DIRNAME, stem, suffix)
    return b"".join(path.read_bytes() for path in reader.chunk_paths())


def rec(line1=L1, line2=L2, src="tle01", idx=0) -> CleanedRecord:
    from lintle.verify.records import catalog_of

    cat = catalog_of(line1)
    return CleanedRecord(
        cat if cat is not None else -1, epoch.epoch_key(line1), line1, line2, src, idx
    )


class TestCollapse:
    """The pure per-group collapse: kept = highest element-set, dropped = rest,
    conflict = one element-set naming more than one orbital state (verify's #158
    rule — a new element-set with a different orbit is a benign re-issue, #164)."""

    def test_singleton_keeps_the_record_no_conflict(self):
        g = dedup._collapse([rec()])
        assert g.kept.line1 == L1 and g.dropped == [] and g.conflict is False

    def test_benign_reissue_keeps_highest_element_set(self):
        lo = rec(line1=with_elset(L1, 100), idx=0)
        hi = rec(line1=with_elset(L1, 200), idx=1)
        g = dedup._collapse([lo, hi])
        assert g.kept is hi and g.dropped == [lo] and g.conflict is False

    def test_revolution_number_reissue_collapses(self):
        base = rec(idx=0)
        bumped = rec(line2=reissued_l2(), idx=1)
        g = dedup._collapse([base, bumped])
        assert g.conflict is False and len(g.dropped) == 1

    def test_same_elset_different_orbit_is_a_conflict(self):
        # one element-set naming two orbits -> a genuine same-epoch clash (#158)
        base = rec(line1=with_elset(L1, 200), line2=L2, idx=0)
        other = rec(line1=with_elset(L1, 200), line2=mutated_l2(), idx=1)
        g = dedup._collapse([base, other])
        assert g.conflict is True
        assert g.kept is other  # element-set tie -> latest source position kept

    def test_refined_reissue_different_orbit_is_benign(self):
        # a NEW element-set with a refined orbit is a benign re-issue, not a clash
        # (#164: dedup must not flag what verify's #158 counts as a census re-issue)
        base = rec(line1=with_elset(L1, 100), line2=L2, idx=0)
        refined = rec(line1=with_elset(L1, 200), line2=mutated_l2(), idx=1)
        g = dedup._collapse([base, refined])
        assert g.conflict is False
        assert g.kept is refined  # highest element-set still wins


class TestEndToEnd:
    def test_singletons_pass_through_sorted(self, tmp_path):
        out = tmp_path / "output"
        out_dir = build_tree(
            tmp_path,
            [(other_catalog(9), L2), (L1, L2)],  # 00009 then 00005
        )
        assert dedup.run(out_dir) == 0
        body = read_import(out)
        # sorted by catalog: 00005 before 00009
        assert body == f"{L1}\n{L2}\n{other_catalog(9)}\n{L2}\n"
        assert read_notes(out) == []

    def test_benign_reissue_collapses_end_to_end(self, tmp_path):
        out = tmp_path / "output"
        pairs = [(with_elset(L1, 100), L2), (with_elset(L1, 200), L2)]
        out_dir = build_tree(tmp_path, pairs)
        assert dedup.run(out_dir) == 0
        # only the highest element-set survives
        assert read_import(out) == f"{with_elset(L1, 200)}\n{L2}\n"
        notes = read_notes(out)
        assert len(notes) == 1
        assert notes[0]["kept"]["element_set"] == 200
        assert notes[0]["dropped"][0]["element_set"] == 100
        assert notes[0]["conflict"] is False
        s = read_summary(out)
        assert s["records_read"] == 2 and s["records_written"] == 1
        assert s["records_dropped"] == 1 and s["conflicts_flagged"] == 0

    def test_genuine_conflict_kept_latest_and_flagged(self, tmp_path):
        out = tmp_path / "output"
        # SAME element-set, two orbits -> a real contradiction (#158)
        pairs = [(with_elset(L1, 200), L2), (with_elset(L1, 200), mutated_l2())]
        out_dir = build_tree(tmp_path, pairs)
        # a real contradiction -> exit 1 (review), but still emit a kept record
        assert dedup.run(out_dir) == 1
        assert read_import(out) == f"{with_elset(L1, 200)}\n{mutated_l2()}\n"
        notes = read_notes(out)
        assert len(notes) == 1 and notes[0]["conflict"] is True
        assert read_summary(out)["conflicts_flagged"] == 1

    def test_refined_reissue_collapses_end_to_end(self, tmp_path):
        # #164: a new element-set carrying a refined orbit collapses benignly
        # (exit 0), never flagged as a contradiction — matches verify's census.
        out = tmp_path / "output"
        pairs = [(with_elset(L1, 100), L2), (with_elset(L1, 200), mutated_l2())]
        out_dir = build_tree(tmp_path, pairs)
        assert dedup.run(out_dir) == 0
        assert read_import(out) == f"{with_elset(L1, 200)}\n{mutated_l2()}\n"
        notes = read_notes(out)
        assert len(notes) == 1 and notes[0]["conflict"] is False
        assert read_summary(out)["conflicts_flagged"] == 0

    def test_hard_suspects_excluded(self, tmp_path):
        out = tmp_path / "output"
        # two distinct satellites; the second is a hard suspect -> excluded
        pairs = [(L1, L2), (other_catalog(9), L2)]
        suspects = [
            {
                "rule": "VRFY-REVALIDATE-FAIL",
                "severity": "hard",
                "src_file": "tle01",
                "index": 1,
                "catalog": 9,
                "epoch_key": 0.0,
                "detail": "x",
            },
        ]
        out_dir = build_tree(tmp_path, pairs, suspects=suspects)
        assert dedup.run(out_dir) == 0
        assert read_import(out) == f"{L1}\n{L2}\n"  # the suspect is gone
        assert read_summary(out)["excluded_hard_suspects"] == 1

    def test_soft_suspects_are_not_excluded(self, tmp_path):
        out = tmp_path / "output"
        pairs = [(L1, L2), (other_catalog(9), L2)]
        suspects = [
            {
                "rule": "VRFY-ORIGIN-MISSING",
                "severity": "soft",
                "src_file": "tle01",
                "index": 1,
                "catalog": 9,
                "epoch_key": 0.0,
                "detail": "x",
            },
        ]
        out_dir = build_tree(tmp_path, pairs, suspects=suspects)
        assert dedup.run(out_dir) == 0
        assert read_summary(out)["excluded_hard_suspects"] == 0
        assert read_import(out).count("\n") == 4  # both records kept

    def test_no_suspects_file_still_dedups(self, tmp_path):
        out = tmp_path / "output"
        pairs = [(with_elset(L1, 100), L2), (with_elset(L1, 200), L2)]
        out_dir = build_tree(tmp_path, pairs)  # no verify/ dir at all
        assert dedup.run(out_dir) == 0
        assert read_import(out) == f"{with_elset(L1, 200)}\n{L2}\n"

    def test_cleaned_tree_is_immutable(self, tmp_path):
        out = tmp_path / "output"
        pairs = [(with_elset(L1, 100), L2), (with_elset(L1, 200), L2)]
        out_dir = build_tree(tmp_path, pairs)
        before = (out / CLEANED_DIRNAME / "tle01.00001.cleaned.txt").read_bytes()
        dedup.run(out_dir)
        after = (out / CLEANED_DIRNAME / "tle01.00001.cleaned.txt").read_bytes()
        assert before == after

    def test_deterministic_bytes(self, tmp_path):
        out = tmp_path / "output"
        pairs = [
            (other_catalog(9), L2),
            (with_elset(L1, 200), L2),
            (with_elset(L1, 100), L2),
        ]
        out_dir = build_tree(tmp_path, pairs)
        dedup.run(out_dir)
        imp1 = read_chunk_bytes(out, "import", ".txt")
        notes1 = read_chunk_bytes(out, "notes", ".jsonl")
        dedup.run(out_dir)
        assert read_chunk_bytes(out, "import", ".txt") == imp1
        assert read_chunk_bytes(out, "notes", ".jsonl") == notes1

    def test_missing_cleaned_dir_is_operational_error(self, tmp_path):
        assert dedup.run(str(tmp_path / "nope")) == 2


class TestCLI:
    def test_dedup_subcommand_dispatches(self, tmp_path):
        out_dir = build_tree(tmp_path, [(L1, L2)])
        assert cli.main(["dedup", out_dir]) == 0
