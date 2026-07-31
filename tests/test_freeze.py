"""The end-to-end output freeze: one sha256 map over a full
``clean -> verify -> dedup -> extract`` run.

Byte-determinism is a hard invariant (Critical Rules #1/#2), but until now it
was asserted piecemeal — each writer had its own test, and nothing checked that
a refactor left the *whole tree* untouched. This is that check: it runs the
real CLI over the ``test_integration`` fixture, canonicalises the handful of
inherently run-specific fields, and compares a sha256 per artifact against a
committed golden map.

It is deliberately blunt. A failure does not mean a bug — it means output bytes
moved, and you must decide whether that was intended. When it was, regenerate
with ``LINTLE_FREEZE_UPDATE=1 uv run pytest tests/test_freeze.py -n0`` and the
diff on ``freeze-e2e.golden.json`` becomes the reviewable record of what moved.

What is canonicalised (and why it must be — these vary per run or per machine,
never per code change): wall-clock timestamps, elapsed/rate timings, the tool
and Python versions, and the absolute tmp paths. ``.clean.lock`` is skipped
outright: it stores the holder's hostname and pid. Everything else — every key
order, separator, sort order, and schema field of every artifact — is frozen.
"""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from lintle import cli, tle

GOLDEN = Path(__file__).parent / "fixtures" / "freeze-e2e.golden.json"

# The holder identity in the lock file is hostname+pid: machine-specific by
# design, and not an output artifact.
SKIP = {".clean.lock"}


def _canonical(text: str, out_dir: str, src_dir: str) -> str:
    """Replace the run-specific fields with fixed placeholders. Each pattern is
    something that changes between two identical runs (or between two machines)
    while the code is unchanged — the freeze is over everything else."""
    text = text.replace(out_dir, "<OUT_DIR>").replace(src_dir, "<SRC_DIR>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "<TIMESTAMP>", text)
    text = re.sub(r'"elapsed_seconds": [0-9.e-]+', '"elapsed_seconds": <T>', text)
    text = re.sub(r'"records_per_sec": [0-9.e-]+', '"records_per_sec": <RATE>', text)
    text = re.sub(r'"tool_version": "[^"]*"', '"tool_version": "<VER>"', text)
    text = re.sub(r'"python_version": "[^"]*"', '"python_version": "<PY>"', text)
    return re.sub(r"lintle \d+\.\d+\.\d+\S*", "lintle <VER>", text)


def _fingerprint(out: Path, src: Path) -> dict[str, str]:
    """``{relative path: sha256 of canonicalised bytes}`` for every file in the
    output tree."""
    digests = {}
    for path in sorted(out.rglob("*")):
        rel = path.relative_to(out).as_posix()
        if not path.is_file() or rel in SKIP:
            continue
        raw = path.read_text(encoding="utf-8", errors="surrogateescape")
        canonical = _canonical(raw, str(out), str(src))
        digests[rel] = hashlib.sha256(
            canonical.encode("utf-8", "surrogateescape")
        ).hexdigest()
    return digests


class TestEndToEndFreeze:
    """One full pipeline run, hashed. See the module docstring before editing
    the golden file."""

    def test_full_pipeline_output_is_frozen(self, tmp_path, line1, line2):
        # The test_integration fixture: one clean record, one repairable
        # (checksumless line 1 with a trailing backslash, checksumless line 2),
        # one quarantined (wrong checksum). Exercises the clean path, the
        # repair tiers, and the quarantine sidecar in a single file.
        #
        # Plus a fourth record: same catalog and epoch as the first but a
        # different inclination — a genuine same-element-set contradiction. Its
        # only job is to make verify emit a hard suspect, so suspects.jsonl has
        # CONTENT to freeze. Without it that file is empty and its writer (and
        # its sort order) sails through this test untested.
        clashed = line2[:13] + "3" + line2[14:]
        clash_l2 = clashed[:68] + str(tle.compute_checksum(clashed))
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes(
            (
                f"{line1}\n{line2}\n"
                f"{line1[:68]}\\\n{line2[:68]}\n"
                f"{line1[:68]}9\n{line2}\n"
                f"{line1}\n{clash_l2}\n"
            ).encode("ascii")
        )
        out = tmp_path / "out"

        # Exit codes are frozen too — a silent policy change is a behaviour
        # change. clean exits 1 on the quarantined record; verify exits 1 on the
        # hard contradiction; dedup then EXCLUDES that hard suspect, so it has
        # nothing left to arbitrate and exits 0 — which is exactly the
        # verify-feeds-dedup handoff, exercised here end to end.
        assert (
            cli.main(
                [
                    "clean",
                    str(src),
                    "--out-dir",
                    str(out),
                    "--jobs",
                    "1",
                    "--reconstruct-checksum",
                ]
            )
            == 1
        )
        assert cli.main(["verify", str(out), "--source", str(src)]) == 1
        assert cli.main(["dedup", str(out)]) == 0
        assert cli.main(["extract", "5", "--out-dir", str(out)]) == 0

        actual = _fingerprint(out, src)

        if os.environ.get("LINTLE_FREEZE_UPDATE"):
            GOLDEN.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
            pytest.skip("golden regenerated (LINTLE_FREEZE_UPDATE)")

        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        # Compare the artifact SET first: a missing or newly-appearing file is a
        # clearer failure than a hash mismatch on a name nobody expected.
        assert sorted(actual) == sorted(expected)
        mismatched = [k for k in sorted(expected) if actual[k] != expected[k]]
        assert not mismatched, (
            "output bytes moved for: "
            + ", ".join(mismatched)
            + " — if intended, regenerate with LINTLE_FREEZE_UPDATE=1"
        )
