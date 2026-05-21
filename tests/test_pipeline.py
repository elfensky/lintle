from tlekit import pipeline


def test_pairs_simple_records(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.RecordCandidate)
    assert records[0].src1 == 1 and records[0].src2 == 2


def test_blank_and_cr_only_lines_dropped(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n\n" + "\r\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.RecordCandidate)
    # Line numbers count skipped blank/CR lines: line 2 is at source line 4.
    assert records[0].src1 == 1 and records[0].src2 == 4


def test_lone_line1_at_eof_is_orphaned(tmp_path, line1):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.Orphan)
    assert records[0].reason == "orphan line 1 at end of file"


def test_two_line1s_orphan_the_first(tmp_path, line1):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n" + line1 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert all(isinstance(r, pipeline.Orphan) for r in records)
    assert records[0].category == "orphan-line"


def test_orphan_line2(tmp_path, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1 and isinstance(records[0], pipeline.Orphan)


def test_bad_prefix_line(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes(("garbage\n" + line1 + "\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    orphans = [r for r in records if isinstance(r, pipeline.Orphan)]
    assert any(o.category == "bad-prefix" for o in orphans)
    # The valid record after the garbage line still pairs.
    assert any(isinstance(r, pipeline.RecordCandidate) for r in records)
