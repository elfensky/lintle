import pytest

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


def test_whitespace_only_line_dropped(tmp_path, line1, line2):
    # A line of spaces/tabs between records is blank — dropped, not
    # quarantined, and it must not orphan the surrounding record.
    src = tmp_path / "in.txt"
    src.write_bytes(
        (line1 + "\n" + "   \t \n" + line2 + "\n").encode("ascii")
    )
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.RecordCandidate)


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


def test_process_file_clean_mode(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    # One clean record, then one checksumless record (both repairable).
    src.write_bytes((
        line1 + "\n" + line2 + "\n" + line1[:68] + "\n" + line2[:68] + "\n"
    ).encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "clean")

    assert stats.total_records == 2
    assert stats.clean_count == 2
    assert stats.quarantined_count == 0
    cleaned = (out / "tle2099.cleaned.txt").read_text()
    assert cleaned == line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
    assert (out / "tle2099.broken.txt").exists()


def test_process_file_quarantines_bad_record(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    bad_line1 = line1[:68] + "9"  # 69 chars, wrong checksum
    src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "clean")

    assert stats.quarantined_count == 1
    assert stats.reject_categories.get("checksum-mismatch") == 1
    assert b"checksum" in (out / "tle2099.broken.txt").read_bytes()


def test_validate_mode_writes_nothing(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "validate")

    assert stats.clean_count == 1
    assert not out.exists()  # validate mode never creates the output dir


def test_internal_error_is_quarantined_not_raised(tmp_path, line1, line2,
                                                  monkeypatch):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(pipeline.repair, "process_record", boom)
    stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

    assert stats.quarantined_count == 1
    assert stats.reject_categories.get("internal-error") == 1


def test_clean_run_leaves_no_temp_file(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"
    pipeline.process_file(str(src), str(out), "clean")
    assert not list(out.glob("*.partial"))  # temp file was renamed away
    # The published cleaned file is world-readable, not owner-only (0600).
    cleaned = out / "tle2099.cleaned.txt"
    assert cleaned.stat().st_mode & 0o044  # group/other read bits set


def test_failed_run_does_not_leak_temp_file(tmp_path):
    # A non-existent source makes iter_records raise when it opens the file.
    out = tmp_path / "out"
    with pytest.raises(OSError):
        pipeline.process_file(
            str(tmp_path / "does_not_exist.txt"), str(out), "clean"
        )
    assert out.exists()  # the output dir was created
    assert not list(out.glob("*.partial"))  # but no partial temp file leaked
