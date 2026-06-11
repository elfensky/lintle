"""Shared terminal-output helpers: the two shared ``Console`` instances
(``stderr_console`` for status/errors; ``stdout_console`` for the ``report``
command's rendered view) and the styled ``error:`` / ``warning:`` emitters
used across the CLI surface.

``stderr_console`` carries all status/error ephemera; ``stdout_console`` carries
only the styled ``report`` result view. rich styling on each is confined to a
TTY; off a TTY (pipes, ``capsys``, ``NO_COLOR``) the Console strips styling and
the output is plain, so machine-readable output stays literal. The structured
output files and the ``--report json`` stdout bytes are never routed through
either Console — those go through plain ``json``/file writers for
byte-determinism. The module-level Consoles are built before argument parsing so
the earliest sites share them; rich reads ``sys.stderr`` / ``sys.stdout`` lazily,
so a replaced stream (tests, redirection) is honoured at print time.
"""

import enum
import os
import sys

from rich.console import Console
from rich.text import Text

stderr_console = Console(stderr=True)
stdout_console = Console()


class Severity(enum.Enum):
    """Severity of a CLI message: ``ERROR`` (exit-worthy) or ``WARNING``
    (advisory). The member value doubles as the stderr prefix word, so the
    styled prefix and the plain off-TTY text can never drift."""

    ERROR = "error"
    WARNING = "warning"


_STYLES = {Severity.ERROR: "bold red", Severity.WARNING: "yellow"}


def emit(severity, message):
    """Write ``"<severity>: <message>"`` to stderr — the prefix styled on a
    TTY, the whole line plain (no ANSI, unwrapped) when stderr is redirected.
    ``message`` is appended as literal text, so brackets in paths or values are
    never parsed as rich markup."""
    line = Text()
    line.append(f"{severity.value}: ", style=_STYLES[severity])
    line.append(message)
    stderr_console.print(line, soft_wrap=True, highlight=False)
    stderr_console.file.flush()


def error(message):
    """Emit an ``error: …`` line to stderr (see :func:`emit`)."""
    emit(Severity.ERROR, message)


def warning(message):
    """Emit a ``warning: …`` line to stderr (see :func:`emit`)."""
    emit(Severity.WARNING, message)


def note(message):
    """Write an unprefixed status/notice line to stderr (e.g. ``processing …``),
    plain off a TTY and routed through the shared Console so every stderr write
    goes through one channel."""
    stderr_console.print(Text(message), soft_wrap=True, highlight=False)
    stderr_console.file.flush()


def prompt(message):
    """Write a prompt to stderr with no trailing newline (the operator types on
    the same line) and flush, so it is visible before stdin is read. Plain text
    — a y/n prompt needs no styling — but routed through the shared Console for
    channel consistency."""
    stderr_console.print(Text(message), end="", soft_wrap=True, highlight=False)
    stderr_console.file.flush()


def is_interactive():
    """A run is interactive iff both stdin (where the prompt answer is read) and
    stderr (where :func:`prompt` writes the question) are TTYs, and no
    CI/NONINTERACTIVE env var forces non-interactive. Requiring stderr too
    prevents an invisible-prompt hang when stderr is redirected (e.g. ``lintle
    clean 2>errors.log``) while stdin stays a TTY, and stops a CI runner with a
    pseudo-TTY from blocking on the prompt (spec §2.2)."""
    if os.environ.get("CI") or os.environ.get("NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except AttributeError, ValueError:
        return False


def prompt_yes_no(message, *, default):
    """Ask a y/n question on stderr, reading the answer from stdin (spec §2.4).
    Enter takes ``default``; up to 3 unrecognised answers then give up; EOF/Ctrl-D
    gives up. Returns True/False, or None when the operator gave no usable answer
    (caller treats None as abort)."""
    for _ in range(3):
        prompt(message)
        line = sys.stdin.readline()
        if line == "":  # EOF / Ctrl-D
            note("")  # close the prompt line the operator never finished
            return None
        token = line.strip().lower()
        if token == "":
            return default
        if token in ("y", "yes"):
            return True
        if token in ("n", "no"):
            return False
        note("  please answer y or n.")
    return None
