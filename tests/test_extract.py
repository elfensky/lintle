"""Tests for lintle extract — per-satellite history from dedup/import chunks."""

import json

import pytest

from lintle import cli, extract
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX


def l1(cat: int, yy: int = 20, day: float = 100.0, elset: int = 1) -> str:
    """A 69-char line 1 with real catalog/epoch/elset columns (checksum fake —
    extract never revalidates; it slices verbatim)."""
    epoch = f"{yy:02d}{day:012.8f}"  # YYDDD.DDDDDDDD -> cols 19-32
    base = f"1 {cat:5d}U 58002B   {epoch}  .00000023  00000-0  28098-4 0 {elset:4d}"
    return (base + "0" * 69)[:69]


def l2(cat: int) -> str:
    base = f"2 {cat:5d} 034.2682 348.7242 1859667 331.7664  19.3264 10.82419157413"
    return (base + "0" * 69)[:69]


def write_import_tree(tmp_path, records, chunk_records=3):
    """Build a fake dedup import chunk set from (line1, line2) pairs, rolling
    every ``chunk_records`` records like ChunkedWriter would."""
    ddir = tmp_path / DEDUP_DIRNAME
    ddir.mkdir(parents=True, exist_ok=True)
    for idx in range((len(records) + chunk_records - 1) // chunk_records or 1):
        chunk = records[idx * chunk_records : (idx + 1) * chunk_records]
        path = ddir / f"{IMPORT_STEM}.{idx + 1:05d}{IMPORT_SUFFIX}"
        path.write_bytes(b"".join(f"{a}\n{b}\n".encode("ascii") for a, b in chunk))
    (ddir / "summary.json").write_text(
        json.dumps({"records_written": len(records), "schema_version": "1"}),
        encoding="ascii",
    )
    return tmp_path


def recs(*cats_epochs):
    """(catalog, day) pairs -> sorted record list, mirroring dedup's order."""
    return [(l1(c, day=d), l2(c)) for c, d in cats_epochs]


class TestFindSpans:
    def test_single_chunk_middle_catalog(self, tmp_path):
        out = write_import_tree(
            tmp_path, recs((100, 1.0), (200, 1.0), (200, 2.0), (300, 1.0)), 10
        )
        spans = extract.find_spans(str(out), 200)
        assert [(s[1], s[2]) for s in spans] == [(1, 3)]

    def test_absent_catalog_returns_empty(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0), (300, 1.0)), 10)
        assert extract.find_spans(str(out), 200) == []

    def test_run_straddles_chunk_seam(self, tmp_path):
        # chunk_records=2: [100, 200] [200, 200] [300, ...]
        out = write_import_tree(
            tmp_path,
            recs((100, 1.0), (200, 1.0), (200, 2.0), (200, 3.0), (300, 1.0)),
            2,
        )
        spans = extract.find_spans(str(out), 200)
        assert [(s[0].name, s[1], s[2]) for s in spans] == [
            (f"{IMPORT_STEM}.00001{IMPORT_SUFFIX}", 1, 2),
            (f"{IMPORT_STEM}.00002{IMPORT_SUFFIX}", 0, 2),
        ]

    def test_first_and_last_catalog_in_set(self, tmp_path):
        out = write_import_tree(
            tmp_path, recs((100, 1.0), (200, 1.0), (300, 1.0), (300, 2.0)), 2
        )
        assert [(s[1], s[2]) for s in extract.find_spans(str(out), 100)] == [(0, 1)]
        assert [(s[0].name, s[1], s[2]) for s in extract.find_spans(str(out), 300)] == [
            (f"{IMPORT_STEM}.00002{IMPORT_SUFFIX}", 0, 2)
        ]

    def test_torn_chunk_is_operational_error(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        chunk = out / DEDUP_DIRNAME / f"{IMPORT_STEM}.00001{IMPORT_SUFFIX}"
        chunk.write_bytes(chunk.read_bytes() + b"x")  # 141 bytes — torn
        with pytest.raises(extract.ExtractError, match="not a multiple"):
            extract.find_spans(str(out), 100)

    def test_missing_dedup_tree_is_operational_error(self, tmp_path):
        with pytest.raises(extract.ExtractError, match="lintle dedup"):
            extract.find_spans(str(tmp_path), 100)


class TestRun:
    def test_writes_txt_and_json(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.5), (200, 10.0)), 2)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [200], str(dest)) == 0
        txt = (dest / "200.txt").read_bytes()
        records = recs((200, 1.0), (200, 2.5), (200, 10.0))
        assert txt == b"".join(f"{a}\n{b}\n".encode("ascii") for a, b in records)
        meta = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta["schema_version"] == "1"
        assert meta["norad_id"] == 200 and meta["records"] == 3
        assert meta["first_epoch"] == "2020-01-01T00:00:00Z"
        assert meta["last_epoch"] == "2020-01-10T00:00:00Z"
        assert meta["span_days"] == 9.0
        assert meta["largest_gap_days"] == 7.5
        assert meta["largest_gap_at"] == "2020-01-10T00:00:00Z"
        assert meta["source"]["dedup_records_written"] == 3

    def test_missing_id_partial_success_exit_2(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100, 424242], str(dest)) == 2
        assert (dest / "100.txt").exists()
        assert not (dest / "424242.txt").exists()
        assert "424242" in capsys.readouterr().err

    def test_single_record_satellite_null_rate(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        meta = json.loads((dest / "100.json").read_text(encoding="ascii"))
        assert meta["records"] == 1 and meta["span_days"] == 0.0
        assert meta["mean_records_per_day"] is None
        assert meta["largest_gap_days"] == 0.0 and meta["largest_gap_at"] is None

    def test_no_partial_debris_on_success(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        extract.run(str(out), [100], str(dest))
        assert list(dest.glob("*.partial")) == []

    def test_multi_block_catalog_stats_and_bytes(self, tmp_path):
        # 8000 records in a single chunk: 8000 * 140 = 1_120_000 bytes, which
        # spans multiple _COPY_BLOCK reads — exercises the 140-aligned block
        # boundary (Critical 1).
        records = recs(*[(500, 1.0 + i * 0.001) for i in range(8000)])
        out = write_import_tree(tmp_path, records, 10000)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [500], str(dest)) == 0
        txt = (dest / "500.txt").read_bytes()
        assert txt == b"".join(f"{a}\n{b}\n".encode("ascii") for a, b in records)
        meta = json.loads((dest / "500.json").read_text(encoding="ascii"))
        assert meta["records"] == 8000
        assert meta["first_epoch"] == "2020-01-01T00:00:00Z"
        assert list(dest.glob("*.partial")) == []

    def test_failure_cleans_partial_and_continues(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((100, 1.0), (300, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        real_epoch_dt = extract._epoch_dt

        def flaky(line1):
            if " 300U" in line1:
                raise ValueError("boom")
            return real_epoch_dt(line1)

        monkeypatch.setattr(extract, "_epoch_dt", flaky)
        assert extract.run(str(out), [300, 100], str(dest)) == 2
        assert not (dest / "300.txt").exists()
        assert not (dest / "300.txt.partial").exists()
        assert (dest / "100.txt").exists()
        assert (dest / "100.json").exists()

    def test_run_missing_tree_raises_before_any_output(self, tmp_path):
        dest = tmp_path / "dest"
        with pytest.raises(extract.ExtractError, match="lintle dedup"):
            extract.run(str(tmp_path), [100], str(dest))
        assert not dest.exists() or list(dest.iterdir()) == []

    def test_sidecar_failure_leaves_nothing_for_that_catalog(
        self, tmp_path, monkeypatch
    ):
        out = write_import_tree(tmp_path, recs((100, 1.0), (200, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        real_durable_write_text = extract.fsutil.durable_write_text

        def flaky(path, text, *, encoding="utf-8"):
            if path.endswith("200.json"):
                raise OSError("boom")
            return real_durable_write_text(path, text, encoding=encoding)

        monkeypatch.setattr(extract.fsutil, "durable_write_text", flaky)
        assert extract.run(str(out), [200, 100], str(dest)) == 2
        assert sorted(p.name for p in dest.iterdir()) == ["100.json", "100.txt"]
        assert not (dest / "200.txt").exists()
        assert not (dest / "200.json").exists()
        assert list(dest.glob("*.partial")) == []

    def test_failed_rerun_preserves_previous_good_pair(self, tmp_path, monkeypatch):
        # A prior successful run's <id>.txt + <id>.json must survive a later
        # failed re-run untouched (Finding 1): the except-block cleanup must
        # not blindly unlink the final txt path, or a re-run failure destroys
        # good output from an earlier run and orphans its sidecar.
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.5)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        txt_before = (dest / "100.txt").read_bytes()
        json_before = (dest / "100.json").read_bytes()

        def always_raise(line1):
            raise ValueError("boom")

        monkeypatch.setattr(extract, "_epoch_dt", always_raise)
        assert extract.run(str(out), [100], str(dest)) == 2
        assert (dest / "100.txt").read_bytes() == txt_before
        assert (dest / "100.json").read_bytes() == json_before

    def test_sidecar_bytes_golden(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.5)))
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        expected = (
            "{\n"
            '  "element_set_first": 1,\n'
            '  "element_set_last": 1,\n'
            '  "first_epoch": "2020-01-01T00:00:00Z",\n'
            '  "largest_gap_at": "2020-01-02T12:00:00Z",\n'
            '  "largest_gap_days": 1.5,\n'
            '  "last_epoch": "2020-01-02T12:00:00Z",\n'
            '  "mean_records_per_day": 1.333333,\n'
            '  "norad_id": 100,\n'
            '  "records": 2,\n'
            '  "schema_version": "1",\n'
            '  "source": {\n'
            '    "dedup_records_written": 2,\n'
            '    "dedup_schema_version": "1",\n'
            f'    "out_dir": "{out}"\n'
            "  },\n"
            '  "span_days": 1.5\n'
            "}\n"
        )
        assert (dest / "100.json").read_bytes() == expected.encode("ascii")


class TestCli:
    def test_end_to_end(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.0)), 10)
        dest = tmp_path / "dest"
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["extract", "200", "--out-dir", str(out), "--dest", str(dest)])
        assert rc == 0
        assert (dest / "200.txt").exists() and (dest / "200.json").exists()

    def test_dest_defaults_to_out_dir_extract(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((300, 1.0)), 10)
        workdir = tmp_path / "wd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        assert cli.main(["extract", "300", "--out-dir", str(out)]) == 0
        extract_dir = out / "06-extract"
        assert (extract_dir / "300.txt").exists()
        assert (extract_dir / "README.md").exists()
        assert not (workdir / "300.txt").exists()

    def test_explicit_dest_gets_no_readme(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((300, 1.0)), 10)
        dest = tmp_path / "somewhere"
        monkeypatch.chdir(tmp_path)
        assert (
            cli.main(["extract", "300", "--out-dir", str(out), "--dest", str(dest)])
            == 0
        )
        assert (dest / "300.txt").exists()
        assert not (dest / "README.md").exists()

    def test_missing_tree_exit_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["extract", "200", "--out-dir", str(tmp_path / "nope")])
        assert rc == 2
        assert "dedup" in capsys.readouterr().err

    def test_rejects_non_numeric_id(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            cli.main(["extract", "ISS", "--out-dir", str(tmp_path)])


class TestAnalyze:
    """_analyze: pure pass-1 history stats — median spacing + reportable gaps."""

    def _hs(self, tmp_path, *days):
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        return extract._analyze(extract.find_spans(str(out), 100))

    def test_uniform_cadence_no_gaps(self, tmp_path):
        hs = self._hs(tmp_path, *[1.0 + i for i in range(10)])
        assert hs.count == 10
        assert hs.median_spacing_days == 1.0
        assert hs.gaps == () and hs.gap_count == 0
        assert hs.largest_gap_days == 1.0

    def test_one_hole_is_one_gap(self, tmp_path):
        # daily cadence days 1-10, then a 40-day hole to day 50
        hs = self._hs(tmp_path, *[1.0 + i for i in range(10)], 50.0, 51.0, 52.0)
        assert hs.median_spacing_days == 1.0
        assert hs.gap_count == 1 and len(hs.gaps) == 1
        gap = hs.gaps[0]
        assert gap.days == 40.0
        assert gap.start == extract._epoch_dt(l1(100, day=10.0))
        assert gap.end == extract._epoch_dt(l1(100, day=50.0))
        assert hs.largest_gap_days == 40.0 and hs.largest_gap_at == gap.end

    def test_under_three_records_skips_analysis(self, tmp_path):
        hs = self._hs(tmp_path, 1.0, 2.5)
        assert hs.count == 2
        assert hs.median_spacing_days is None
        assert hs.gaps == () and hs.gap_count == 0
        assert hs.largest_gap_days == 1.5  # largest gap still tracked

    def test_cap_keeps_ten_largest_chronological(self, tmp_path):
        # 12 runs of 3 daily records, 28-day holes between runs: 11 reportable
        days = [r * 30 + s for r in range(12) for s in (1.0, 2.0, 3.0)]
        hs = self._hs(tmp_path, *days)
        assert hs.gap_count == 11 and len(hs.gaps) == 10
        starts = [g.start for g in hs.gaps]
        assert starts == sorted(starts)  # chronological
        assert all(g.days == 28.0 for g in hs.gaps)

    def test_stats_match_extract_one(self, tmp_path):
        hs = self._hs(tmp_path, 1.0, 2.5, 10.0)
        assert hs.count == 3
        assert hs.elset_first == 1 and hs.elset_last == 1
        assert hs.first == extract._epoch_dt(l1(100, day=1.0))
        assert hs.last == extract._epoch_dt(l1(100, day=10.0))
        assert hs.largest_gap_days == 7.5


class TestReadme:
    """``run``'s ``write_readme`` keyword, default False, is inert this task —
    Task 3 wires the cli to pass True only for the default
    ``<out-dir>/06-extract`` dest, never for an explicit ``--dest``."""

    def test_defaults_to_no_readme(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert not (dest / "README.md").exists()

    def test_write_readme_true_writes_it(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest), write_readme=True) == 0
        readme = dest / "README.md"
        assert readme.is_file()
        text = readme.read_text(encoding="utf-8")
        assert "06-extract" in text
        assert "lintle extract" in text

    def test_readme_is_deterministic(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        extract.run(str(out), [100], str(dest), write_readme=True)
        first = (dest / "README.md").read_bytes()
        extract.run(str(out), [100], str(dest), write_readme=True)
        assert (dest / "README.md").read_bytes() == first

    def test_cli_writes_readme_only_for_default_dest(self, tmp_path, monkeypatch):
        # No --dest -> default <out-dir>/06-extract gets a README; an explicit
        # --dest is the user's own directory and is never decorated.
        out = write_import_tree(tmp_path, recs((200, 1.0)), 10)
        workdir = tmp_path / "wd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        assert cli.main(["extract", "200", "--out-dir", str(out)]) == 0
        assert (out / "06-extract" / "README.md").exists()
        assert not (workdir / "README.md").exists()

        explicit_dest = tmp_path / "explicit"
        assert (
            cli.main(
                [
                    "extract",
                    "200",
                    "--out-dir",
                    str(out),
                    "--dest",
                    str(explicit_dest),
                ]
            )
            == 0
        )
        assert not (explicit_dest / "README.md").exists()


class TestQuarantinedIds:
    def _write_ndjson(self, tmp_path, text):
        rdir = tmp_path / "03-report"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "broken-noradids.ndjson").write_text(text, encoding="ascii")

    def test_present_ids(self, tmp_path):
        self._write_ndjson(tmp_path, '{"noradId":100}\n{"noradId":200}\n')
        assert extract._quarantined_ids(str(tmp_path)) == {100, 200}

    def test_missing_file_is_unknown(self, tmp_path):
        assert extract._quarantined_ids(str(tmp_path)) is None

    def test_malformed_file_is_unknown_with_warning(self, tmp_path, capsys):
        self._write_ndjson(tmp_path, "not json\n")
        assert extract._quarantined_ids(str(tmp_path)) is None
        assert "broken-noradids" in capsys.readouterr().err

    def test_unreadable_file_is_unknown_with_warning(
        self, tmp_path, monkeypatch, capsys
    ):
        self._write_ndjson(tmp_path, '{"noradId":100}\n')
        monkeypatch.setattr(
            extract.Path,
            "read_text",
            lambda self, encoding=None: (_ for _ in ()).throw(PermissionError("boom")),
        )
        assert extract._quarantined_ids(str(tmp_path)) is None
        assert "broken-noradids" in capsys.readouterr().err
