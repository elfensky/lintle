"""Tests for the interactive wizard and config-backed path resolution."""

import argparse

from lintle import cli, config, wizard


def _script(monkeypatch, answers):
    """Feed a scripted sequence of answers to every ``Prompt.ask`` the wizard
    makes, replacing the rich prompt so nothing blocks on stdin."""
    it = iter(answers)
    monkeypatch.setattr(
        wizard, "Prompt", type("P", (), {"ask": staticmethod(lambda *a, **k: next(it))})
    )


class TestApplyConfigPaths:
    def test_clean_fills_source_and_output_from_config(self):
        args = argparse.Namespace(command="clean", path=None, out_dir="data/output")
        cli._apply_config_paths(args, ["clean"], {"source": "/src", "output": "/out"})
        assert args.path == "/src"
        assert args.out_dir == "/out"

    def test_explicit_path_is_never_overridden(self):
        args = argparse.Namespace(command="clean", path="x.txt", out_dir="data/output")
        cli._apply_config_paths(args, ["clean", "x.txt"], {"source": "/src"})
        assert args.path == "x.txt"

    def test_explicit_out_dir_flag_is_never_overridden(self):
        args = argparse.Namespace(command="clean", path=None, out_dir="build")
        cli._apply_config_paths(args, ["clean", "--out-dir", "build"], {"output": "/o"})
        assert args.out_dir == "build"

    def test_verify_resolves_config_then_default(self):
        args = argparse.Namespace(command="verify", out_dir=None, source=None)
        cli._apply_config_paths(args, ["verify"], {"output": "/out"})
        assert args.out_dir == "/out"
        assert args.source == "data/source"  # no config source -> built-in default

    def test_report_resolves_from_config(self):
        args = argparse.Namespace(command="report", out_dir=None)
        cli._apply_config_paths(args, ["report"], {"output": "/out"})
        assert args.out_dir == "/out"


class TestNoCommand:
    def test_non_interactive_prints_help_and_exits_2(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.term, "is_interactive", lambda: False)
        assert cli.main([]) == 2
        assert "Examples:" in capsys.readouterr().err

    def test_interactive_launches_wizard(self, monkeypatch):
        monkeypatch.setattr(cli.term, "is_interactive", lambda: True)
        monkeypatch.setattr("lintle.wizard.run", lambda: 7)
        assert cli.main([]) == 7


class TestWizardDispatch:
    def test_dispatch_clean_builds_argv(self, monkeypatch):
        calls = []
        monkeypatch.setattr("lintle.cli.main", lambda argv: calls.append(argv) or 0)
        wizard._dispatch({"source": "/s", "output": "/o"}, "clean")
        assert calls == [["clean", "/s", "--out-dir", "/o"]]

    def test_dispatch_verify_builds_argv(self, monkeypatch):
        calls = []
        monkeypatch.setattr("lintle.cli.main", lambda argv: calls.append(argv) or 0)
        wizard._dispatch({"source": "/s", "output": "/o"}, "verify")
        assert calls == [["verify", "/o", "--source", "/s"]]


class TestWizardLoop:
    def test_quit_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "s").mkdir()
        (tmp_path / "o").mkdir()
        config.save({"source": str(tmp_path / "s"), "output": str(tmp_path / "o")})
        _script(monkeypatch, ["5"])  # menu -> quit
        assert wizard.run() == 0

    def test_clean_then_quit_dispatches(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "s").mkdir()
        (tmp_path / "o").mkdir()
        config.save({"source": str(tmp_path / "s"), "output": str(tmp_path / "o")})
        calls = []
        monkeypatch.setattr("lintle.cli.main", lambda argv: calls.append(argv) or 0)
        _script(monkeypatch, ["1", "5"])  # clean, then quit
        assert wizard.run() == 0
        assert calls == [
            ["clean", str(tmp_path / "s"), "--out-dir", str(tmp_path / "o")]
        ]

    def test_ensure_paths_prompts_and_saves_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _script(monkeypatch, [str(tmp_path / "s"), str(tmp_path / "o")])
        cfg = wizard._ensure_paths({})
        assert cfg == {"source": str(tmp_path / "s"), "output": str(tmp_path / "o")}
        assert config.load(str(tmp_path)) == cfg

    def test_ensure_paths_reprompts_when_stored_path_vanished(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "o").mkdir()
        good = {"source": str(tmp_path / "gone"), "output": str(tmp_path / "o")}
        (tmp_path / "s").mkdir()
        _script(monkeypatch, [str(tmp_path / "s"), str(tmp_path / "o")])
        cfg = wizard._ensure_paths(good)  # source path missing -> reconfigure
        assert cfg["source"] == str(tmp_path / "s")
