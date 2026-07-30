"""Tests for lintle extract — per-satellite history from dedup/import chunks."""

import json

import pytest

from lintle import CLEANED_DIRNAME, cli, dedup, extract
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
from tests.conftest import CANONICAL_LINE1, CANONICAL_LINE2


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

    def test_missing_interior_chunk_is_an_error_not_a_short_history(self, tmp_path):
        # Deleting import.00002.txt used to yield exit 0 and a confidently
        # truncated history claiming the full span with gap_count 0 — the
        # worst failure mode of the whole set. It must refuse instead.
        out = write_import_tree(
            tmp_path,
            recs(*[(100, 1.0 + i) for i in range(6)]),
            2,
        )
        (out / DEDUP_DIRNAME / f"{IMPORT_STEM}.00002{IMPORT_SUFFIX}").unlink()
        with pytest.raises(extract.ExtractError, match="missing chunk 00002"):
            extract.find_spans(str(out), 100)


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
        assert meta["schema_version"] == "2"
        assert meta["median_spacing_days"] == 4.5  # deltas [1.5, 7.5]
        assert meta["gap_count"] == 0 and meta["gaps"] == []
        assert meta["had_quarantined_records"] is None
        assert meta["norad_id"] == 200 and meta["records"] == 3
        assert meta["first_epoch"] == "2020-01-01T00:00:00Z"
        assert meta["last_epoch"] == "2020-01-10T00:00:00Z"
        assert meta["span_days"] == 9.0
        assert meta["largest_gap_days"] == 7.5
        assert meta["largest_gap_at"] == "2020-01-10T00:00:00Z"
        assert meta["source"]["dedup_records_written"] == 3

    def test_missing_summary_json_degrades_to_null_source(self, tmp_path, capsys):
        # A pruned tree used to crash each catalog AFTER its txt commit, and
        # the pair rollback then deleted a prior run's still-good outputs.
        # summary.json is read once, tolerantly: absent -> null source fields.
        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.5)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [200], str(dest)) == 0  # prior good run
        (out / DEDUP_DIRNAME / "summary.json").unlink()
        assert extract.run(str(out), [200], str(dest)) == 0
        assert (dest / "200.txt").is_file()  # prior pair survived the re-run
        meta = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta["source"]["dedup_records_written"] is None
        assert meta["source"]["dedup_schema_version"] is None

    def test_corrupt_summary_json_degrades_to_null_source(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.5)), 10)
        (out / DEDUP_DIRNAME / "summary.json").write_bytes(b"\xc3{not json")
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [200], str(dest)) == 0
        meta = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta["source"]["dedup_records_written"] is None

    def test_unusable_record_fails_preflight_before_anything_is_written(
        self, tmp_path, capsys
    ):
        # An Alpha-5 (or corrupt) record sorts to record 0 of chunk 1 and used
        # to fail EVERY per-catalog lookup blaming corruption. Now it is one
        # up-front preflight error naming the record, with nothing written.
        pairs = recs((100, 1.0), (200, 1.0))
        bad_l1 = "1 T7530U" + pairs[0][0][8:]  # Alpha-5 catalog in cols 3-7
        out = write_import_tree(tmp_path, [(bad_l1, pairs[0][1]), pairs[1]], 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(extract.ExtractError, match="record 0"):
            extract.run(str(out), [200], str(dest))
        assert list(dest.iterdir()) == []  # preflight: nothing written

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
        assert meta["median_spacing_days"] is None
        assert meta["gap_count"] == 0 and meta["gaps"] == []

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
        # Pre-commit failure on a re-run: pass-1 analysis (_epoch_dt) raises
        # before the txt is ever copied or committed, so a prior successful
        # run's <id>.txt + <id>.json must survive untouched (Finding 1): the
        # except-block cleanup must not blindly unlink the final txt path, or
        # a re-run failure destroys good output from an earlier run and
        # orphans its sidecar. This only covers the pre-txt-commit path — see
        # test_sidecar_failure_on_rerun_rolls_back_pair for the post-commit
        # case, where the pair is rolled back as a unit instead.
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

    def test_sidecar_failure_on_rerun_rolls_back_pair(self, tmp_path, monkeypatch):
        # Post-commit failure on a re-run: the new txt has already replaced
        # the old one, so the pair rolls back as a unit — both files removed,
        # never a mismatched txt/json pair left behind.
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.5)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        real = extract.fsutil.durable_write_text

        def flaky(path, text, *, encoding="utf-8"):
            if path.endswith("100.json"):
                raise OSError("boom")
            return real(path, text, encoding=encoding)

        monkeypatch.setattr(extract.fsutil, "durable_write_text", flaky)
        assert extract.run(str(out), [100], str(dest)) == 2
        assert not (dest / "100.txt").exists()
        assert not (dest / "100.json").exists()

    def test_copy_failure_cleans_written_partial(self, tmp_path, monkeypatch):
        # Failure during pass-2 _copy_spans — after real bytes hit the tmp —
        # must still remove the partial and leave the destination untouched.
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        real_copy = extract._copy_spans

        def flaky(spans, out_fh):
            real_copy(spans, out_fh)  # bytes genuinely written first
            raise OSError("disk full")

        monkeypatch.setattr(extract, "_copy_spans", flaky)
        assert extract.run(str(out), [100], str(dest)) == 2
        assert not (dest / "100.txt").exists()
        assert not (dest / "100.txt.partial").exists()
        assert list(dest.glob("*")) == []

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
            '  "gap_count": 0,\n'
            '  "gaps": [],\n'
            '  "had_quarantined_records": null,\n'
            '  "largest_gap_at": "2020-01-02T12:00:00Z",\n'
            '  "largest_gap_days": 1.5,\n'
            '  "last_epoch": "2020-01-02T12:00:00Z",\n'
            '  "mean_records_per_day": 1.333333,\n'
            '  "median_spacing_days": null,\n'
            '  "norad_id": 100,\n'
            '  "records": 2,\n'
            '  "schema_version": "2",\n'
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

    def test_shrunken_chunk_raises_instead_of_spinning(self, tmp_path):
        # A chunk truncated between find_spans' stat and the read used to make
        # fh.read() return b"" forever with `remaining` stuck — an infinite
        # spin under the status spinner. Both passes must raise instead.
        import io

        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.0), (100, 3.0)), 10)
        spans = extract.find_spans(str(out), 100)
        chunk = spans[0][0]
        chunk.write_bytes(chunk.read_bytes()[: extract.RECORD_BYTES])  # 1 of 3 left
        with pytest.raises(extract.ExtractError, match="shrank"):
            extract._analyze(spans)
        with pytest.raises(extract.ExtractError, match="shrank"):
            extract._copy_spans(spans, io.BytesIO())

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


class TestSidecarV2:
    def test_quarantine_flag_true_and_false(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0), (200, 1.0)), 10)
        rdir = out / "03-report"
        rdir.mkdir()
        (rdir / "broken-noradids.ndjson").write_text(
            '{"noradId":100}\n', encoding="ascii"
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100, 200], str(dest)) == 0
        meta100 = json.loads((dest / "100.json").read_text(encoding="ascii"))
        meta200 = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta100["had_quarantined_records"] is True
        assert meta200["had_quarantined_records"] is False

    def test_gap_fields_in_sidecar(self, tmp_path):
        days = [1.0 + i for i in range(10)] + [50.0, 51.0, 52.0]
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        meta = json.loads((dest / "100.json").read_text(encoding="ascii"))
        assert meta["median_spacing_days"] == 1.0
        assert meta["gap_count"] == 1
        assert meta["gaps"] == [
            {
                "days": 40.0,
                "end": "2020-02-19T00:00:00Z",
                "start": "2020-01-10T00:00:00Z",
            }
        ]

    def test_year_boundary_span_non_negative(self, tmp_path):
        # The import stream is instant-ordered post-#199: Dec 31 2019 12:00Z
        # (spelled 20/000.5) precedes Jan 1 2020 12:00Z (spelled 19/366.5),
        # so the sidecar span is +1.0 — never the pre-fix -1.0.
        out = write_import_tree(
            tmp_path,
            [
                (l1(100, yy=20, day=0.5), l2(100)),
                (l1(100, yy=19, day=366.5), l2(100)),
            ],
            10,
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        meta = json.loads((dest / "100.json").read_text(encoding="ascii"))
        assert meta["span_days"] == 1.0
        assert meta["mean_records_per_day"] == 2.0
        assert meta["first_epoch"] == "2019-12-31T12:00:00Z"
        assert meta["last_epoch"] == "2020-01-01T12:00:00Z"


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


GAPPY_DAYS = [1.0 + i for i in range(10)] + [50.0, 51.0, 52.0]


def gappy_tree(tmp_path, cat=100):
    return write_import_tree(tmp_path, recs(*[(cat, d) for d in GAPPY_DAYS]), 10000)


class TestWarnConfirm:
    def test_non_tty_warns_and_proceeds(self, tmp_path, capsys):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()
        err = capsys.readouterr().err
        assert "1 gap" in err and "40.0 d" in err
        assert "2020-01-10" in err and "2020-02-19" in err

    def test_interactive_decline_skips_exit_0(self, tmp_path, monkeypatch, capsys):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(
            extract.term, "prompt_yes_no", lambda msg, *, default: False
        )
        assert extract.run(str(out), [100], str(dest)) == 0
        assert not (dest / "100.txt").exists()
        assert not (dest / "100.json").exists()
        assert "skipped 100" in capsys.readouterr().err

    def test_interactive_accept_writes(self, tmp_path, monkeypatch):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(extract.term, "prompt_yes_no", lambda msg, *, default: True)
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()

    def test_prompt_eof_proceeds(self, tmp_path, monkeypatch):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(extract.term, "prompt_yes_no", lambda msg, *, default: None)
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()

    def test_clean_history_never_prompts(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)

        def boom(msg, *, default):
            raise AssertionError("prompted on a clean history")

        monkeypatch.setattr(extract.term, "prompt_yes_no", boom)
        assert extract.run(str(out), [100], str(dest)) == 0

    def test_quarantine_only_triggers_warning(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.0)), 10)
        rdir = out / "03-report"
        rdir.mkdir()
        (rdir / "broken-noradids.ndjson").write_text(
            '{"noradId":100}\n', encoding="ascii"
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert "quarantined during clean" in capsys.readouterr().err

    def test_cap_prints_and_more_line(self, tmp_path, capsys):
        days = [r * 30 + s for r in range(12) for s in (1.0, 2.0, 3.0)]
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert "and 1 more" in capsys.readouterr().err

    def test_spinner_scopes_exclude_warn_and_confirm(self, tmp_path, monkeypatch):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        events: list[str] = []
        active_status = 0
        real_find_spans = extract.find_spans
        real_analyze = extract._analyze
        real_copy_spans = extract._copy_spans
        real_durable_replace = extract.fsutil.durable_replace
        real_durable_write_text = extract.fsutil.durable_write_text

        class _Status:
            def __enter__(self):
                nonlocal active_status
                active_status += 1
                events.append("status-enter")
                return None

            def __exit__(self, exc_type, exc, tb):
                nonlocal active_status
                active_status -= 1
                events.append("status-exit")
                return False

        def fake_status(message):
            events.append(f"status:{message}")
            return _Status()

        def traced_find_spans(out_dir, catalog):
            events.append(f"find_spans:{active_status}")
            return real_find_spans(out_dir, catalog)

        def traced_analyze(spans):
            events.append(f"analyze:{active_status}")
            return real_analyze(spans)

        def traced_warn(catalog, hs, had_quarantined):
            events.append(f"warn:{active_status}")
            return True

        def traced_copy(spans, out_fh):
            events.append(f"copy:{active_status}")
            return real_copy_spans(spans, out_fh)

        def traced_replace(src, dst):
            events.append(f"replace:{active_status}")
            return real_durable_replace(src, dst)

        def traced_write_text(path, text, *, encoding):
            events.append(f"write:{active_status}")
            return real_durable_write_text(path, text, encoding=encoding)

        monkeypatch.setattr(extract.cli_progress, "status", fake_status)
        monkeypatch.setattr(extract, "find_spans", traced_find_spans)
        monkeypatch.setattr(extract, "_analyze", traced_analyze)
        monkeypatch.setattr(extract, "_warn_and_confirm", traced_warn)
        monkeypatch.setattr(extract, "_copy_spans", traced_copy)
        monkeypatch.setattr(extract.fsutil, "durable_replace", traced_replace)
        monkeypatch.setattr(extract.fsutil, "durable_write_text", traced_write_text)

        assert extract.run(str(out), [100], str(dest)) == 0
        # Anchor on the per-catalog work rather than absolute positions: the
        # run opens other spinners before the loop, and this test is about the
        # prompt never sitting inside one, not about how many precede it.
        start = events.index("status:analyzing 100…")
        assert events[start : start + 6] == [
            "status:analyzing 100…",
            "status-enter",
            "find_spans:1",
            "analyze:1",
            "status-exit",
            "warn:0",
        ]
        assert events[start + 6] == "status:writing 100…"
        assert events[start + 7] == "status-enter"
        assert "copy:1" in events[start + 8 : -1]
        assert "write:1" in events[start + 8 : -1]
        assert events[-1] == "status-exit"
        assert all(
            event.endswith(":1")
            for event in events[start + 8 : -1]
            if ":" in event and not event.startswith("status")
        )
        # Nothing before the loop may leave a spinner open either.
        assert "warn:0" in events and not any(
            event.startswith("warn:") and not event.endswith(":0") for event in events
        )


def real_dedup_tree(tmp_path):
    """Build a genuine ``01-cleaned`` tree and run ``dedup`` over it (rather
    than ``write_import_tree``'s hand-built import chunk set), so
    ``summary.json`` carries a real ``cleaned_fingerprint`` to check
    staleness against."""
    out = tmp_path / "output"
    cdir = out / CLEANED_DIRNAME
    cdir.mkdir(parents=True)
    (cdir / "tle01.00001.cleaned.txt").write_text(
        f"{CANONICAL_LINE1}\n{CANONICAL_LINE2}\n", encoding="ascii"
    )
    assert dedup.run(str(out)) == 0
    return out


class TestStalenessWarning:
    """``extract.run`` recomputes ``01-cleaned``'s structural fingerprint at
    run start and compares it with the one ``dedup`` stored — a mismatch
    warns (extract's existing warn-and-proceed philosophy) but never changes
    the exit code, which stays reserved for absent/torn dedup output."""

    def test_mismatch_warns_but_exits_zero(self, tmp_path, capsys):
        out = real_dedup_tree(tmp_path)
        # mutate cleaned/ after dedup so the fingerprint drifts (appended
        # record changes the stem's total chunk-byte size)
        with (out / CLEANED_DIRNAME / "tle01.00001.cleaned.txt").open(
            "a", encoding="ascii"
        ) as f:
            f.write(f"{CANONICAL_LINE1}\n{CANONICAL_LINE2}\n")
        dest = tmp_path / "dest"
        dest.mkdir()
        code = extract.run(str(out), [5], str(dest))
        assert code == 0  # warn-and-proceed, never exit 2
        assert "stale" in capsys.readouterr().err.lower()

    def test_no_drift_is_silent(self, tmp_path, capsys):
        out = real_dedup_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [5], str(dest)) == 0
        assert "stale" not in capsys.readouterr().err.lower()

    def test_no_stored_fingerprint_is_silent(self, tmp_path, capsys):
        # write_import_tree's summary.json has no cleaned_fingerprint key
        # (a hand-built dedup tree, or an older dedup run) -> nothing to
        # compare against, so the check is skipped rather than warning.
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert "stale" not in capsys.readouterr().err.lower()


class TestThreePhaseDisplay:
    """extract's roster (2+ ids only) and its per-id results table, rendered
    from the sidecars it just committed."""

    def _run(self, tmp_path, monkeypatch, catalogs, width=120):
        import io

        from rich.console import Console

        from lintle import term

        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.5), (300, 1.0)), 2)
        dest = tmp_path / "dest"
        dest.mkdir()
        console = Console(file=io.StringIO(), force_terminal=True, width=width)
        monkeypatch.setattr(term, "stderr_console", console)
        code = extract.run(str(out), catalogs, str(dest))
        return code, console.file.getvalue()

    def test_single_id_gets_results_but_no_roster(self, tmp_path, monkeypatch):
        code, out = self._run(tmp_path, monkeypatch, [200])
        assert code == 0
        assert "norad id" in out and "status" in out
        # A one-row roster above a one-row table is noise: only one table here.
        assert out.count("norad id") == 1

    def test_multiple_ids_get_a_roster_and_a_row_each(self, tmp_path, monkeypatch):
        code, out = self._run(tmp_path, monkeypatch, [200, 300])
        assert code == 0
        assert out.count("norad id") == 2  # roster + results
        assert "written" in out

    def test_absent_id_renders_dashes_not_invented_numbers(self, tmp_path, monkeypatch):
        code, out = self._run(tmp_path, monkeypatch, [200, 999])
        assert code == 2  # an absent id is an operational error
        rows = [line for line in out.splitlines() if "999" in line]
        assert rows and "absent" in rows[-1] and "—" in rows[-1]
