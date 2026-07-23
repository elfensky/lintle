"""Tests for output_artifacts — the run-level finalization, focused on the
self-describing root README and the per-step READMEs ``clean`` ships for the
flat numbered out-dir layout (01-cleaned/02-broken/03-report)."""

from lintle import (
    BROKEN_DIRNAME,
    CLEANED_DIRNAME,
    REPORT_DIRNAME,
    __version__,
    output_artifacts,
)


class TestLayoutReadme:
    """A clean run drops a static README.md at the out-dir root explaining the
    per-step layout, so the output is self-describing."""

    def test_writes_readme_at_out_dir_root(self, tmp_path):
        output_artifacts.write_layout_readme(str(tmp_path))
        readme = tmp_path / "README.md"
        assert readme.is_file()
        text = readme.read_text(encoding="utf-8")
        # Names each step's dir and the current version.
        assert "# lintle output" in text
        assert __version__ in text
        for token in (
            "01-cleaned/",
            "02-broken/",
            "03-report/",
            "04-verify/",
            "05-dedup/",
            "06-extract/",
        ):
            assert token in text
        # No leftover `data/` grouping language from the retired layout.
        assert "data/" not in text

    def test_is_deterministic_for_a_version(self, tmp_path):
        output_artifacts.write_layout_readme(str(tmp_path))
        first = (tmp_path / "README.md").read_bytes()
        output_artifacts.write_layout_readme(str(tmp_path))
        assert (tmp_path / "README.md").read_bytes() == first


class TestStepReadmes:
    """``write_clean_artifacts`` also drops a README.md inside each of the
    three dirs ``clean`` owns (01-cleaned, 02-broken, 03-report), static and
    byte-deterministic — no counts, no timestamps."""

    def _run(self, out_dir):
        from lintle.report import FileStats

        stats = FileStats(src_name="tle01.txt")
        envelope = {"schema_version": "1", "files": []}
        output_artifacts.write_clean_artifacts(str(out_dir), [stats], envelope)

    def test_writes_a_readme_in_each_owned_dir(self, tmp_path):
        out = tmp_path / "output"
        (out / CLEANED_DIRNAME).mkdir(parents=True)
        (out / BROKEN_DIRNAME).mkdir(parents=True)
        self._run(out)
        for dirname, heading in (
            (CLEANED_DIRNAME, "01-cleaned"),
            (BROKEN_DIRNAME, "02-broken"),
            (REPORT_DIRNAME, "03-report"),
        ):
            readme = out / dirname / "README.md"
            assert readme.is_file()
            assert heading in readme.read_text(encoding="utf-8")

    def test_cleaned_readme_names_the_regen_command(self, tmp_path):
        out = tmp_path / "output"
        (out / CLEANED_DIRNAME).mkdir(parents=True)
        (out / BROKEN_DIRNAME).mkdir(parents=True)
        self._run(out)
        text = (out / CLEANED_DIRNAME / "README.md").read_text(encoding="utf-8")
        assert "lintle clean" in text
        assert ".cleaned.txt" in text

    def test_broken_readme_points_to_explain(self, tmp_path):
        out = tmp_path / "output"
        (out / CLEANED_DIRNAME).mkdir(parents=True)
        (out / BROKEN_DIRNAME).mkdir(parents=True)
        self._run(out)
        text = (out / BROKEN_DIRNAME / "README.md").read_text(encoding="utf-8")
        assert "lintle explain" in text
        assert ".broken.txt" in text

    def test_report_readme_names_diff_input_and_ndjson_list(self, tmp_path):
        out = tmp_path / "output"
        (out / CLEANED_DIRNAME).mkdir(parents=True)
        (out / BROKEN_DIRNAME).mkdir(parents=True)
        self._run(out)
        text = (out / REPORT_DIRNAME / "README.md").read_text(encoding="utf-8")
        assert "report.NNNNN.jsonl" in text
        assert "lintle diff" in text
        assert "broken-noradids.ndjson" in text

    def test_step_readmes_are_deterministic(self, tmp_path):
        out = tmp_path / "output"
        (out / CLEANED_DIRNAME).mkdir(parents=True)
        (out / BROKEN_DIRNAME).mkdir(parents=True)
        self._run(out)
        first = {
            dirname: (out / dirname / "README.md").read_bytes()
            for dirname in (CLEANED_DIRNAME, BROKEN_DIRNAME, REPORT_DIRNAME)
        }
        self._run(out)
        for dirname, before in first.items():
            assert (out / dirname / "README.md").read_bytes() == before
