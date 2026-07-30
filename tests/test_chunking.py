"""Tests for chunking.py — the ChunkedWriter/ChunkedReader primitive that
splits every record/line stream into fixed-count chunks (spec
2026-07-21-output-chunking-design). Covers roll boundaries, the empty-stream
and exact-multiple edge cases, N==0, naming/zero-pad/lexical order, per-chunk
atomic commit, the 99 999-index overflow guard, reader reassembly in index
order, non-matching-file rejection, stem/collision disambiguation, and
concat-identity."""

from pathlib import Path

import pytest

from lintle import chunking
from lintle.chunking import (
    CHUNK_RECORDS_DEFAULT,
    MAX_CHUNK_INDEX,
    ChunkedReader,
    ChunkedWriter,
    ChunkSetError,
)


def _names(directory):
    """Sorted basenames in a directory (excludes .partial temps)."""
    return sorted(
        p.name for p in Path(directory).iterdir() if not p.name.endswith(".partial")
    )


class TestChunkedWriterRoll:
    """The roll boundary: a chunk closes and the next opens the instant the
    unit count reaches units_per_chunk."""

    def test_seven_records_at_n3_makes_chunks_of_3_3_1(self, tmp_path):
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=3) as w:
            for i in range(7):
                w.write_record(f"1 line{i}".encode(), f"2 line{i}".encode())
        assert _names(tmp_path) == [
            "tle.00001.cleaned.txt",
            "tle.00002.cleaned.txt",
            "tle.00003.cleaned.txt",
        ]
        # 2 lines per record: 3/3/1 records -> 6/6/2 lines.
        assert (tmp_path / "tle.00001.cleaned.txt").read_bytes().count(b"\n") == 6
        assert (tmp_path / "tle.00003.cleaned.txt").read_bytes().count(b"\n") == 2

    def test_exact_multiple_makes_no_trailing_empty_chunk(self, tmp_path):
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=3) as w:
            for i in range(6):
                w.write_record(f"1 x{i}".encode(), f"2 x{i}".encode())
        assert _names(tmp_path) == ["tle.00001.cleaned.txt", "tle.00002.cleaned.txt"]

    def test_empty_stream_makes_one_empty_first_chunk(self, tmp_path):
        with ChunkedWriter(tmp_path, "import", ".txt", units_per_chunk=3):
            pass
        assert _names(tmp_path) == ["import.00001.txt"]
        assert (tmp_path / "import.00001.txt").read_bytes() == b""

    def test_n_zero_never_rolls(self, tmp_path):
        with ChunkedWriter(tmp_path, "import", ".txt", units_per_chunk=0) as w:
            for i in range(100):
                w.write_line(f"line {i}")
        assert _names(tmp_path) == ["import.00001.txt"]
        assert (tmp_path / "import.00001.txt").read_bytes().count(b"\n") == 100


class TestChunkedWriterNaming:
    """Names are {stem}.{index:05d}{suffix}, 1-based zero-padded, lexical==numeric."""

    def test_index_is_five_digit_zero_padded_and_lexically_sorted(self, tmp_path):
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(12):
                w.write_line(str(i))
        names = _names(tmp_path)
        assert names[0] == "s.00001.jsonl"
        assert names[-1] == "s.00012.jsonl"
        # lexical order over the zero-padded names is numeric order.
        assert names == sorted(names)


class TestChunkedWriterAtomicCommit:
    """Each chunk goes to a .partial temp and is committed to its final name
    only on roll/close — a reader never sees a torn chunk."""

    def test_final_name_absent_until_roll_or_close(self, tmp_path):
        w = ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=100)
        w.__enter__()
        w.write_record(b"1 a", b"2 a")
        # mid-write: a .partial exists, the committed name does not.
        assert (tmp_path / "tle.00001.cleaned.txt").exists() is False
        assert any(p.name.endswith(".partial") for p in tmp_path.iterdir())
        w.__exit__(None, None, None)
        assert (tmp_path / "tle.00001.cleaned.txt").exists() is True
        assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir())

    def test_exception_discards_in_progress_chunk_but_keeps_committed(self, tmp_path):
        try:
            with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=2) as w:
                w.write_record(b"1 a", b"2 a")
                w.write_record(b"1 b", b"2 b")  # fills .00001, commits it
                w.write_record(b"1 c", b"2 c")  # opens .00002 (in progress)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # committed chunk survives; the in-progress one is discarded, no temp left.
        assert _names(tmp_path) == ["tle.00001.cleaned.txt"]
        assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir())


class TestChunkedWriterScrubExisting:
    """A writer scrubs its stem's pre-existing chunk set on first open, so a
    re-run (or resume redo) producing FEWER chunks never orphans a prior run's
    high-index tail (spec invariant 5). Deterministic input → the rewritten
    prefix is byte-identical; only the longer prior tail is removed."""

    def test_shorter_rerun_leaves_no_orphaned_high_index_chunk(self, tmp_path):
        # First run: 5 records at N=1 -> .00001..00005
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=1) as w:
            for i in range(5):
                w.write_record(f"1 x{i}".encode(), f"2 x{i}".encode())
        assert len(_names(tmp_path)) == 5
        # Second (shorter) run: 2 records -> .00001..00002; .00003-.00005 must go
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=1) as w:
            for i in range(2):
                w.write_record(f"1 x{i}".encode(), f"2 x{i}".encode())
        assert _names(tmp_path) == ["tle.00001.cleaned.txt", "tle.00002.cleaned.txt"]

    def test_scrub_still_works_against_a_gapped_set(self, tmp_path):
        # The scrub path must keep tolerating holes (it exists to clean up
        # damage) even though read consumers now refuse them — regression
        # guard for the chunk_paths / complete_chunk_paths split.
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=1) as w:
            for i in range(4):
                w.write_record(f"1 x{i}".encode(), f"2 x{i}".encode())
        (tmp_path / "tle.00002.cleaned.txt").unlink()  # hand-punched hole
        with ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=1) as w:
            w.write_record(b"1 y", b"2 y")
        assert _names(tmp_path) == ["tle.00001.cleaned.txt"]

    def test_scrub_leaves_other_stems_alone(self, tmp_path):
        with ChunkedWriter(tmp_path, "tleA", ".cleaned.txt", units_per_chunk=1) as w:
            w.write_record(b"1 a", b"2 a")
        with ChunkedWriter(tmp_path, "tleB", ".cleaned.txt", units_per_chunk=1) as w:
            w.write_record(b"1 b", b"2 b")
        # re-running tleB must not touch tleA's set
        with ChunkedWriter(tmp_path, "tleB", ".cleaned.txt", units_per_chunk=1) as w:
            w.write_record(b"1 b2", b"2 b2")
        assert "tleA.00001.cleaned.txt" in _names(tmp_path)


class TestChunkedWriterWriteRaw:
    """write_raw() writes preamble bytes (a file header) into the first chunk
    without counting a unit, so the header lives in .00001 and the roll boundary
    still falls every units_per_chunk records."""

    def test_write_raw_preamble_not_counted_as_unit(self, tmp_path):
        with ChunkedWriter(tmp_path, "b", ".broken.txt", units_per_chunk=2) as w:
            w.write_raw(b"# header\n")
            for i in range(4):
                w.write_line(f"e{i}")
        # header in .00001; 4 entries at N=2 -> .00001(header,e0,e1), .00002(e2,e3)
        assert _names(tmp_path) == ["b.00001.broken.txt", "b.00002.broken.txt"]
        assert (tmp_path / "b.00001.broken.txt").read_bytes() == b"# header\ne0\ne1\n"
        assert (tmp_path / "b.00002.broken.txt").read_bytes() == b"e2\ne3\n"

    def test_write_raw_only_gives_header_only_first_chunk(self, tmp_path):
        with ChunkedWriter(tmp_path, "b", ".broken.txt", units_per_chunk=2) as w:
            w.write_raw(b"# header\n")
        assert _names(tmp_path) == ["b.00001.broken.txt"]
        assert (tmp_path / "b.00001.broken.txt").read_bytes() == b"# header\n"


class TestChunkedWriterDiscardAll:
    """discard_all() abandons the whole set — the in-progress temp AND every
    already-committed chunk — restoring per-file atomicity when a write must be
    thrown away mid-stream (e.g. a pipeline failure processing one input file)."""

    def test_discard_all_removes_committed_and_in_progress(self, tmp_path):
        w = ChunkedWriter(tmp_path, "tle", ".cleaned.txt", units_per_chunk=1)
        w.__enter__()
        w.write_record(b"1 a", b"2 a")  # opens .00001
        w.write_record(b"1 b", b"2 b")  # rolls/commits .00001, opens .00002
        w.discard_all()
        assert _names(tmp_path) == []
        assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir())


class TestChunkedWriterOverflow:
    """Rolling past index 99 999 is a hard error, not a silent .100000 wrap."""

    def test_raises_past_max_chunk_index(self, tmp_path):
        assert MAX_CHUNK_INDEX == 99_999
        w = ChunkedWriter(tmp_path, "s", ".txt", units_per_chunk=1)
        w.__enter__()
        w._index = MAX_CHUNK_INDEX - 1  # last opened chunk was .99998
        w.write_line("last")  # opens .99999 (the last legal index)
        with pytest.raises(ValueError, match="99999|chunk index"):
            w.write_line("overflow")  # would need .100000


class TestChunkedReader:
    """Reads a stem's chunk set as one logical stream, in index order,
    ignoring non-matching files and other stems' chunks."""

    def test_reassembles_multi_chunk_set_in_index_order(self, tmp_path):
        with ChunkedWriter(tmp_path, "import", ".txt", units_per_chunk=2) as w:
            for i in range(5):
                w.write_line(f"row{i}")
        reader = ChunkedReader(tmp_path, "import", ".txt")
        assert [line.decode() for line in reader.iter_lines()] == [
            "row0",
            "row1",
            "row2",
            "row3",
            "row4",
        ]

    def test_single_chunk_set(self, tmp_path):
        with ChunkedWriter(tmp_path, "import", ".txt", units_per_chunk=0) as w:
            w.write_line("only")
        assert [
            b.decode() for b in ChunkedReader(tmp_path, "import", ".txt").iter_lines()
        ] == ["only"]

    def test_ignores_non_matching_and_other_stems(self, tmp_path):
        (tmp_path / "summary.json").write_text("{}")
        with ChunkedWriter(tmp_path, "tleA", ".cleaned.txt", units_per_chunk=0) as w:
            w.write_record(b"1 a", b"2 a")
        with ChunkedWriter(tmp_path, "tleB", ".cleaned.txt", units_per_chunk=0) as w:
            w.write_record(b"1 b", b"2 b")
        got = [
            b.decode()
            for b in ChunkedReader(tmp_path, "tleA", ".cleaned.txt").iter_lines()
        ]
        assert got == ["1 a", "2 a"]

    def test_dotted_stem_does_not_capture_other_stems_chunks(self, tmp_path):
        # stem "tle" must not ingest stem "tle.00001"'s chunks (the collision
        # the debate flagged); the right-anchored 5-digit parse disambiguates.
        with ChunkedWriter(tmp_path, "tle", ".txt", units_per_chunk=0) as w:
            w.write_line("real-tle")
        with ChunkedWriter(tmp_path, "tle.00001", ".txt", units_per_chunk=0) as w:
            w.write_line("other-stem")
        got = [b.decode() for b in ChunkedReader(tmp_path, "tle", ".txt").iter_lines()]
        assert got == ["real-tle"]

    def test_chunk_paths_are_sorted(self, tmp_path):
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(3):
                w.write_line(str(i))
        paths = ChunkedReader(tmp_path, "s", ".jsonl").chunk_paths()
        assert [p.name for p in paths] == [
            "s.00001.jsonl",
            "s.00002.jsonl",
            "s.00003.jsonl",
        ]

    def test_complete_chunk_paths_accepts_a_contiguous_set(self, tmp_path):
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(3):
                w.write_line(str(i))
        reader = ChunkedReader(tmp_path, "s", ".jsonl")
        assert reader.complete_chunk_paths() == reader.chunk_paths()

    def test_complete_chunk_paths_names_a_missing_interior_chunk(self, tmp_path):
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(3):
                w.write_line(str(i))
        (tmp_path / "s.00002.jsonl").unlink()
        with pytest.raises(ChunkSetError, match="missing chunk 00002"):
            ChunkedReader(tmp_path, "s", ".jsonl").complete_chunk_paths()

    def test_complete_chunk_paths_names_a_missing_leading_chunk(self, tmp_path):
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(2):
                w.write_line(str(i))
        (tmp_path / "s.00001.jsonl").unlink()
        with pytest.raises(ChunkSetError, match="missing chunk 00001"):
            ChunkedReader(tmp_path, "s", ".jsonl").complete_chunk_paths()

    def test_iter_lines_refuses_a_gapped_set(self, tmp_path):
        # A silent read-around would truncate the stream AND renumber every
        # downstream record index (desyncing suspects.jsonl addresses).
        with ChunkedWriter(tmp_path, "s", ".jsonl", units_per_chunk=1) as w:
            for i in range(3):
                w.write_line(str(i))
        (tmp_path / "s.00002.jsonl").unlink()
        with pytest.raises(ChunkSetError):
            list(ChunkedReader(tmp_path, "s", ".jsonl").iter_lines())

    def test_empty_set_is_trivially_complete(self, tmp_path):
        assert ChunkedReader(tmp_path, "s", ".jsonl").complete_chunk_paths() == []


class TestConcatIdentity:
    """b"".join(chunk bytes in index order) is byte-identical to the
    equivalent single-file write — the property locking Critical Rules #1/#2."""

    def test_join_of_chunks_equals_single_file_bytes(self, tmp_path):
        rows = [f"record-{i}" for i in range(7)]
        # single-file baseline
        single = "".join(r + "\n" for r in rows).encode("utf-8")
        # chunked, crossing a boundary at N=3
        with ChunkedWriter(tmp_path, "import", ".txt", units_per_chunk=3) as w:
            for r in rows:
                w.write_line(r)
        joined = b"".join(
            p.read_bytes()
            for p in ChunkedReader(tmp_path, "import", ".txt").chunk_paths()
        )
        assert joined == single

    def test_default_chunk_size_constant(self):
        assert CHUNK_RECORDS_DEFAULT == 1_000_000
        assert chunking.CHUNK_RECORDS_DEFAULT == 1_000_000
