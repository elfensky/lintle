"""Tests for the optional project-local config (`lintle.config`)."""

from lintle import config


class TestConfigRoundTrip:
    def test_load_missing_returns_empty(self, tmp_path):
        assert config.load(str(tmp_path)) == {}

    def test_save_then_load(self, tmp_path):
        path = config.save({"source": "a", "output": "b"}, str(tmp_path))
        assert path.name == config.CONFIG_FILENAME
        assert config.load(str(tmp_path)) == {"source": "a", "output": "b"}

    def test_only_known_nonempty_keys_kept(self, tmp_path):
        config.save({"source": "a", "output": "", "junk": "x"}, str(tmp_path))
        assert config.load(str(tmp_path)) == {"source": "a"}

    def test_corrupt_file_returns_empty(self, tmp_path):
        config.config_path(str(tmp_path)).write_text("{not json", encoding="utf-8")
        assert config.load(str(tmp_path)) == {}

    def test_non_object_returns_empty(self, tmp_path):
        config.config_path(str(tmp_path)).write_text("[1, 2]", encoding="utf-8")
        assert config.load(str(tmp_path)) == {}

    def test_save_is_deterministic_regardless_of_key_order(self, tmp_path):
        path = config.save({"output": "b", "source": "a"}, str(tmp_path))
        first = path.read_bytes()
        config.save({"source": "a", "output": "b"}, str(tmp_path))
        assert path.read_bytes() == first
