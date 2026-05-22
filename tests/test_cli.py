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
