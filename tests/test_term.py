"""Tests for term.py — the shared stderr output helpers.

The load-bearing guarantee: off a TTY (pipes, capsys, NO_COLOR) the output is
byte-identical to the plain ``print(file=sys.stderr)`` it replaces — no ANSI, no
wrapping, no cropping — so machine-readable stderr and the existing substring
assertions stay intact. rich styling is reserved for a real terminal.
"""

import io

import lintle.term as term
from lintle.term import Severity


class TestEmit:
    """term.error / term.warning / term.emit — the prefixed stderr lines."""

    def test_error_prefixes_message_and_adds_newline(self, capsys):
        term.error("disk full")
        out = capsys.readouterr()
        assert out.err == "error: disk full\n"
        assert out.out == ""  # never leaks to stdout

    def test_warning_uses_warning_prefix(self, capsys):
        term.warning("cutting it close")
        assert capsys.readouterr().err == "warning: cutting it close\n"

    def test_emit_uses_severity_value_as_prefix(self, capsys):
        term.emit(Severity.ERROR, "boom")
        assert capsys.readouterr().err == "error: boom\n"

    def test_no_ansi_escape_off_tty(self, capsys):
        term.error("anything")
        assert "\x1b" not in capsys.readouterr().err

    def test_message_brackets_are_literal_not_markup(self, capsys):
        # rich markup must not consume bracketed tokens in paths or values.
        term.error("bad token [Y/n] in 'a[b].txt'")
        assert capsys.readouterr().err == "error: bad token [Y/n] in 'a[b].txt'\n"

    def test_long_message_is_not_wrapped_or_cropped(self, capsys):
        msg = "z" * 200
        term.error(msg)
        assert capsys.readouterr().err == f"error: {msg}\n"

    def test_multiline_message_preserved(self, capsys):
        term.error("first line\n  indented second")
        assert capsys.readouterr().err == "error: first line\n  indented second\n"


class TestNote:
    """term.note — unprefixed stderr status lines (resuming/processing/cancel)."""

    def test_note_writes_message_plus_newline(self, capsys):
        term.note("processing 3 file(s) with 2 worker(s)...")
        assert capsys.readouterr().err == "processing 3 file(s) with 2 worker(s)...\n"

    def test_note_never_leaks_to_stdout(self, capsys):
        term.note("status")
        assert capsys.readouterr().out == ""


class TestPrompt:
    """term.prompt — a stderr prompt with no trailing newline (typed inline)."""

    def test_prompt_has_no_trailing_newline(self, capsys):
        term.prompt("resume this run? [Y/n] ")
        out = capsys.readouterr()
        assert out.err == "resume this run? [Y/n] "
        assert out.out == ""


class TestIsInteractive:
    def test_requires_stdin_tty_and_no_ci(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO())  # not a tty
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NONINTERACTIVE", raising=False)
        assert term.is_interactive() is False

    def test_ci_env_forces_non_interactive(self, monkeypatch):
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(term.sys, "stdin", _TTY())
        monkeypatch.setenv("CI", "true")
        assert term.is_interactive() is False

    def test_interactive_when_stdin_and_stderr_tty_and_no_ci(self, monkeypatch):
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(term.sys, "stdin", _TTY())
        monkeypatch.setattr(term.sys, "stderr", _TTY())
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NONINTERACTIVE", raising=False)
        assert term.is_interactive() is True

    def test_not_interactive_when_stderr_redirected(self, monkeypatch):
        # The resume prompt is written to stderr; if stderr is redirected
        # (e.g. `lintle clean 2>errors.log`) the question is invisible, so a
        # stdin-only check would block on an unseen prompt. A run is
        # interactive only when the prompt's own stream is a TTY too.
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(term.sys, "stdin", _TTY())
        monkeypatch.setattr(term.sys, "stderr", io.StringIO())  # not a tty
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NONINTERACTIVE", raising=False)
        assert term.is_interactive() is False


class TestPromptYesNo:
    def test_enter_takes_default(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("\n"))
        assert term.prompt_yes_no("go? ", default=True) is True

    def test_explicit_no(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("n\n"))
        assert term.prompt_yes_no("go? ", default=True) is False

    def test_eof_returns_none(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO(""))
        assert term.prompt_yes_no("go? ", default=True) is None

    def test_garbage_then_abort(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("maybe\nhuh\nwhat\n"))
        assert term.prompt_yes_no("go? ", default=True) is None


class TestConsoles:
    """Structural checks on the two shared Console instances: status/errors on
    stderr, the ``report`` result view on stdout."""

    def test_stdout_console_targets_stdout(self):
        assert term.stdout_console.stderr is False

    def test_stderr_console_targets_stderr(self):
        assert term.stderr_console.stderr is True
