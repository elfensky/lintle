"""Tests for the streaming ``SuspectSink`` (#156): peak memory independent of the
suspect count, byte-identical to the list-based writer it replaces."""

from lintle import VERIFY_DIRNAME
from lintle.chunking import ChunkedReader
from lintle.verify import report
from lintle.verify.report import Suspect, SuspectSink, VerifyRule

CHECKED = {
    "files": 3,
    "records": 1000,
    "source_diff": "on",
    "missing_source_files": 0,
    "epoch_reissues": 7,
}

# A scrambled, multi-rule set spanning hard + soft, several catalogs/epochs/files,
# and a tie-key pair (same sort key, different detail) to lock the tie-break order.
SUSPECTS = [
    Suspect(VerifyRule.ORBIT_OUTLIER, 25544, 2020001.5, "tle2020", 9, "resid 250 km"),
    Suspect(VerifyRule.EPOCH_CONFLICT, 5, 2000179.0, "tle01", 2, "clash A"),
    Suspect(VerifyRule.INTERIOR_MUT, 5, 2000179.0, "tle01", 1, "mutated"),
    Suspect(VerifyRule.EPOCH_CONFLICT, 5, 2000179.0, "tle01", 2, "clash B"),  # tie key
    Suspect(VerifyRule.ORIGIN_MISSING, 100, 1999300.0, "tle99", 0, "no origin"),
    Suspect(VerifyRule.REVALIDATE_FAIL, -1, -1.0, "tle07", 42, "garbage"),
    Suspect(VerifyRule.ORBIT_ERROR, 6, 2001001.0, "tle01", 3, "sgp4 error 2"),
    Suspect(VerifyRule.ORBIT_OUTLIER, 5, 2000179.0, "tle01", 4, "resid 900 km"),
]


def _read_suspects(vdir) -> bytes:
    """Concatenate the ``suspects.NNNNN.jsonl`` chunk set in index order — the
    byte-deterministic equivalent of the pre-chunking single file's bytes."""
    reader = ChunkedReader(vdir, "suspects", ".jsonl")
    return b"".join(path.read_bytes() for path in reader.chunk_paths())


def _drain(sink: SuspectSink, out_dir) -> tuple[bytes, bytes, str]:
    sink.write(str(out_dir), checked=CHECKED)
    vdir = out_dir / VERIFY_DIRNAME
    return (
        _read_suspects(vdir),
        (vdir / "summary.json").read_bytes(),
        (vdir / "summary.md").read_text(encoding="utf-8"),
    )


class TestByteEquivalence:
    def test_streaming_output_equals_list_writer(self, tmp_path):
        # chunk_size=3 forces multiple spills -> exercises the k-way merge path.
        sink = SuspectSink(chunk_size=3)
        sink.add_all(SUSPECTS)
        jsonl, summary_json, summary_md = _drain(sink, tmp_path)

        assert jsonl == report.render_suspects_jsonl(SUSPECTS)
        assert summary_json == report.render_summary_json(SUSPECTS, checked=CHECKED)
        assert summary_md == report.render_summary_md(SUSPECTS, checked=CHECKED)

    def test_empty_sink_matches_empty_list(self, tmp_path):
        jsonl, summary_json, summary_md = _drain(SuspectSink(), tmp_path)
        assert jsonl == b"" == report.render_suspects_jsonl([])
        assert summary_json == report.render_summary_json([], checked=CHECKED)
        assert summary_md == report.render_summary_md([], checked=CHECKED)

    def test_no_spill_path_also_matches(self, tmp_path):
        # large chunk -> everything stays in the in-memory tail (no runs on disk)
        sink = SuspectSink(chunk_size=10_000)
        sink.add_all(SUSPECTS)
        assert sink._sorter._runs == []
        jsonl, _, _ = _drain(sink, tmp_path)
        assert jsonl == report.render_suspects_jsonl(SUSPECTS)


class TestTally:
    def test_running_tally_matches_list(self):
        sink = SuspectSink(chunk_size=3)
        sink.add_all(SUSPECTS)
        hard = sum(1 for s in SUSPECTS if s.severity == "hard")
        assert sink.total == len(SUSPECTS)
        assert sink.hard == hard
        assert sink.exit_code == (1 if hard else 0)


class TestConstantMemory:
    def test_peak_buffer_is_bounded_by_chunk(self, tmp_path):
        # 100 suspects, chunk 10 -> spills to disk, buffer never exceeds one
        # chunk. The buffer lives in the delegated ExternalSorter now; the
        # invariant under test is unchanged.
        sink = SuspectSink(chunk_size=10)
        for i in range(100):
            sink.add(Suspect(VerifyRule.ORBIT_OUTLIER, i, float(i), "tle", i, "x"))
            assert len(sink._sorter._buf) <= 10
        # spilled every full chunk -> constant memory
        assert len(sink._sorter._runs) == 10
        sink.write(str(tmp_path), checked=CHECKED)
        lines = _read_suspects(tmp_path / VERIFY_DIRNAME).splitlines()
        assert len(lines) == 100  # nothing lost across the spill/merge
