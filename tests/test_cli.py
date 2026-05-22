import os

from tlekit import cli


def test_discover_expands_directory(tmp_path):
    (tmp_path / "tle2001.txt").write_text("x")
    (tmp_path / "tle2002.txt").write_text("x")
    (tmp_path / "tle2001.cleaned.txt").write_text("x")  # tool output — excluded
    (tmp_path / "tle2001.broken.txt").write_text("x")   # tool output — excluded
    (tmp_path / "notes.md").write_text("x")             # not a TLE file

    found = cli.discover_paths([str(tmp_path)])

    names = sorted(os.path.basename(p) for p in found)
    assert names == ["tle2001.txt", "tle2002.txt"]


def test_discover_passes_through_explicit_files(tmp_path):
    explicit = tmp_path / "tle2001.txt"
    explicit.write_text("x")
    assert cli.discover_paths([str(explicit)]) == [str(explicit)]


def test_parser_defaults():
    args = cli.build_parser().parse_args(["validate"])
    assert args.command == "validate"
    assert args.paths == ["data/source"]
    assert args.out_dir == "data/output"
    assert args.report == "text"


def test_parser_accepts_jobs_and_paths():
    args = cli.build_parser().parse_args(
        ["clean", "a.txt", "b.txt", "--jobs", "4", "--report", "json"]
    )
    assert args.command == "clean"
    assert args.paths == ["a.txt", "b.txt"]
    assert args.jobs == 4
    assert args.report == "json"


def test_main_clean_returns_zero_on_clean_corpus(tmp_path, line1, line2):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

    assert rc == 0
    assert (out / "cleaned" / "tle2099.cleaned.txt").exists()
    assert (out / "broken" / "tle2099.broken.txt").exists()
    # A clean run writes a Markdown run report to the out-dir root.
    report_md = (out / "report.md").read_text()
    assert "# tlekit clean run report" in report_md
    assert "tle2099.txt" in report_md
    assert "Records:" in report_md


def test_main_returns_one_when_records_quarantined(tmp_path, line1, line2):
    src = tmp_path / "src"
    src.mkdir()
    bad_line1 = line1[:68] + "9"
    (src / "tle2099.txt").write_bytes(
        (bad_line1 + "\n" + line2 + "\n").encode("ascii")
    )
    out = tmp_path / "out"

    rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

    assert rc == 1


def test_main_returns_two_when_no_input_files(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main(["validate", str(empty)])
    assert rc == 2


def test_main_validate_prints_summary(tmp_path, line1, line2, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

    rc = cli.main(["validate", str(src), "--jobs", "1"])

    assert rc == 0
    assert "tle2099.txt" in capsys.readouterr().out


def test_main_returns_two_when_a_file_fails_to_process(tmp_path):
    # An explicit path to a missing file is passed through to a worker,
    # which raises when it cannot open it — an operational error.
    missing = tmp_path / "tle_missing.txt"  # never created
    rc = cli.main(["validate", str(missing), "--jobs", "1"])
    assert rc == 2


def test_main_returns_two_on_disk_shortfall(tmp_path, line1, line2, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    class _Usage:
        free = 1  # far below the doubled input size

    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: _Usage())
    rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
    assert rc == 2
