"""Tests for term.py — the shared stderr output helpers.

The load-bearing guarantee: off a TTY (pipes, capsys, NO_COLOR) the output is
byte-identical to the plain ``print(file=sys.stderr)`` it replaces — no ANSI, no
wrapping, no cropping — so machine-readable stderr and the existing substring
assertions stay intact. rich styling is reserved for a real terminal.
"""

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
