"""Shared terminal-output helpers: the single stderr ``Console`` and the
styled ``error:`` / ``warning:`` emitters used across the CLI surface.

rich styling is confined to stderr and only when it is a TTY; off a TTY (pipes,
``capsys``, ``NO_COLOR``) the Console strips styling and the output is plain,
so machine-readable stderr stays literal. This is the only Console attached to
stderr — stdout result data and the structured output files are never routed
through it. The module-level ``stderr_console`` is built before argument
parsing so the earliest error sites share it; rich reads ``sys.stderr`` lazily,
so a replaced stream (tests, redirection) is honoured at print time.
"""

import enum

from rich.console import Console
from rich.text import Text

stderr_console = Console(stderr=True)


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
