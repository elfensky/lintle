from tlekit import report


def test_write_broken_file(tmp_path):
    stats = report.FileStats(src_name="tle2099.txt")
    stats.total_records = 5
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 garbage"], source_lines=[42],
        reason="bad-prefix: line does not start with '1 ' or '2 '"))
    out = tmp_path / "tle2099.broken.txt"

    report.write_broken_file(str(out), "tle2099.txt", stats)

    text = out.read_bytes()
    assert b"# source: tle2099.txt" in text
    assert b"1 records quarantined of 5 total" in text
    assert b"source line 42" in text
    assert b"1 garbage" in text


def test_broken_file_is_byte_faithful(tmp_path):
    # A line quarantined for a non-ASCII byte must appear verbatim.
    stats = report.FileStats(src_name="x.txt")
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 \xff\xfe non-ascii"], source_lines=[7],
        reason="non-ascii"))
    out = tmp_path / "x.broken.txt"

    report.write_broken_file(str(out), "x.txt", stats)

    assert b"\xff\xfe" in out.read_bytes()


def test_two_line_record_location(tmp_path):
    stats = report.FileStats(src_name="x.txt")
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 aaa", b"2 bbb"], source_lines=[14820, 14821],
        reason="line 2: checksum mismatch"))
    out = tmp_path / "x.broken.txt"

    report.write_broken_file(str(out), "x.txt", stats)

    assert b"source lines 14820-14821" in out.read_bytes()
