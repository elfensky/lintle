"""Tests for output_artifacts — the run-level finalization, focused on the
self-describing root README the per-step ``data/`` layout ships (0.10.1)."""

from lintle import __version__, output_artifacts


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
        for token in ("data/", "data/cleaned/", "data/report/", "verify/", "dedup/"):
            assert token in text

    def test_is_deterministic_for_a_version(self, tmp_path):
        output_artifacts.write_layout_readme(str(tmp_path))
        first = (tmp_path / "README.md").read_bytes()
        output_artifacts.write_layout_readme(str(tmp_path))
        assert (tmp_path / "README.md").read_bytes() == first
