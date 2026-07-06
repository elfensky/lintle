"""Render the run envelope as a terminal-width-responsive aggregate panel, and
back the read-only ``lintle report`` command. The panel is styled human UI
(rich), keyed off the *target* console (stderr for ``clean``'s end-of-run panel,
stdout for ``report``); it is NOT byte-determinism-bound — only ``report.json``
is. Fed by the ``build_run_envelope`` dict in both paths, so there is one
renderer and one input shape."""

import json
from pathlib import Path

import humanize
from rich import box
from rich.table import Table
from rich.text import Text

from lintle import term

_BAR_CELLS = 24


def _humanize_duration(seconds):
    """Return a human-readable duration string for ``seconds`` (e.g.
    ``"2 minutes and 4 seconds"``), via humanize for the operator panel."""
    return humanize.precisedelta(seconds, minimum_unit="seconds", format="%d")


def _format_pct(part, whole, *, zero_marker="—"):
    """Return a percentage string for ``part / whole``, honest about tiny rates.
    ``zero_marker`` is returned for a zero denominator — the default em dash suits
    the medium and wide tiers whose consoles can encode it; the plain tier passes
    ``"-"`` since it is chosen precisely when the console cannot encode Unicode
    (issue #97)."""
    if whole <= 0:
        return zero_marker
    if part == 0:
        return "0%"
    rate = 100.0 * part / whole
    if rate < 0.01:
        return "<0.01%"
    return f"{rate:.2f}%"


def _format_pct_plain(part, whole):
    """ASCII-safe percentage for the plain tier — delegates to :func:`_format_pct`
    with a hyphen zero-marker so the output is 7-bit ASCII (issue #97)."""
    return _format_pct(part, whole, zero_marker="-")


def _can_encode(encoding, sample):
    """Return ``True`` if ``sample`` encodes without error under ``encoding``."""
    try:
        sample.encode(encoding or "utf-8")
    except UnicodeEncodeError, LookupError:
        return False
    return True


def _pick_tier(*, is_terminal, width, unicode_ok):
    """Return the render tier: ``"plain"``, ``"medium"``, or ``"wide"``."""
    if not is_terminal or width < 72 or not unicode_ok:
        return "plain"
    return "medium" if width < 100 else "wide"


def _bar(part, whole, *, width, use_unicode):
    """Return a fixed-``width`` progress-bar string for ``part / whole``."""
    fill_char = "█" if use_unicode else "#"
    cells = 0 if whole <= 0 else min(width, round(width * part / whole))
    return fill_char * cells + " " * (width - cells)


def _sorted_counts(d):
    """Return items from ``d`` sorted by descending count, then ascending key."""
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))


def _totals_lines(run, s, *, fmt_pct=_format_pct):
    """Return a list of ``(label, value_str, pct_str)`` triples for the totals block.
    ``fmt_pct`` selects the percentage formatter: pass :func:`_format_pct_plain` for
    the plain tier to avoid emitting the non-ASCII em dash (issue #97)."""
    routed = s["clean_count"] + s["quarantined_count"]
    return [
        ("files", f"{s['files_processed']:,}", ""),
        ("records", f"{s['paired_records']:,}", ""),
        ("clean", f"{s['clean_count']:,}", fmt_pct(s["clean_count"], routed)),
        (
            "quarantined",
            f"{s['quarantined_count']:,}",
            fmt_pct(s["quarantined_count"], routed),
        ),
        ("orphans", f"{s['orphan_entries']:,}", ""),
        ("lines", f"{s['input_lines_seen']:,}", ""),
        ("elapsed", _humanize_duration(run["elapsed_seconds"]), ""),
    ]


def _print_totals(console, run, s, *, fmt_pct=_format_pct):
    """Print one totals field per line to ``console``; right-aligns the value column.
    ``fmt_pct`` is forwarded to :func:`_totals_lines`."""
    rows = _totals_lines(run, s, fmt_pct=fmt_pct)
    width = max(len(value) for _, value, _ in rows)
    for label, value, pct in rows:
        line = f"  {label:<12} {value:>{width}}"
        if pct:
            line += f"   {pct}"
        console.print(Text(line), highlight=False)


def _render_plain(console, label, run, s):
    """Render a plain ASCII-only summary line block (no box, no bars, no arrows).
    Uses :func:`_format_pct_plain` throughout to guarantee the output is 7-bit
    ASCII — the plain tier is chosen precisely when the console cannot encode
    Unicode, so the em dash from :func:`_format_pct` would cause UnicodeEncodeError
    (issue #97). Prints a ``failures:`` line when any files failed (issue #83)."""
    console.print(Text(f"lintle {label} - {run['timestamp']}"), highlight=False)
    _print_totals(console, run, s, fmt_pct=_format_pct_plain)
    if s["fix_counts"]:
        console.print(
            Text(
                "  fixes:       "
                + " | ".join(f"{k} {n:,}" for k, n in _sorted_counts(s["fix_counts"]))
            ),
            highlight=False,
        )
    if s["quarantine_counts"]:
        console.print(
            Text(
                "  quarantined: "
                + " | ".join(
                    f"{k} {n:,}" for k, n in _sorted_counts(s["quarantine_counts"])
                )
            ),
            highlight=False,
        )
    for entry in run.get("failed_files", []):
        console.print(
            Text(f"  failed: {entry['file']} - {entry['error']}"), highlight=False
        )


def _render_sections(console, label, run, s, *, bars):
    """Render a rich-styled section panel (medium: no bars; wide: with bars).

    The ``bars`` flag is forwarded only to the "Quarantined by rule" section;
    "Fixes applied" is always rendered without bars regardless of tier. Adds a
    "Failures" table when any files failed (issue #83)."""
    console.rule(f"lintle {label} · {run['timestamp']}")
    _print_totals(console, run, s)

    def _section(title, counts, with_bars):
        t = Table(title=title, box=box.SIMPLE, pad_edge=False, title_justify="left")
        t.add_column("name")
        t.add_column("count", justify="right")
        if with_bars:
            t.add_column("share")
        total = sum(counts.values())
        for name, n in _sorted_counts(counts):
            row = [name, f"{n:,}"]
            if with_bars:
                row.append(
                    _bar(n, total, width=_BAR_CELLS, use_unicode=True)
                    + f" {_format_pct(n, total)}"
                )
            t.add_row(*row)
        return t

    if s["fix_counts"]:
        console.print(_section("Fixes applied", s["fix_counts"], with_bars=False))
    if s["quarantine_counts"]:
        console.print(
            _section("Quarantined by rule", s["quarantine_counts"], with_bars=bars)
        )
    failed = run.get("failed_files", [])
    if failed:
        t = Table(
            title="Failures", box=box.SIMPLE, pad_edge=False, title_justify="left"
        )
        t.add_column("file")
        t.add_column("error")
        for entry in failed:
            t.add_row(entry["file"], entry["error"])
        console.print(t)


def render(envelope, *, console, command_label="clean"):
    """Render the run envelope as a responsive aggregate panel to ``console``."""
    run = envelope["run"]
    s = envelope["summary"]
    unicode_ok = _can_encode(console.encoding, "█─·")
    tier = _pick_tier(
        is_terminal=console.is_terminal, width=console.width, unicode_ok=unicode_ok
    )
    if tier == "plain":
        _render_plain(console, command_label, run, s)
    else:
        _render_sections(console, command_label, run, s, bars=(tier == "wide"))


_SCHEMA = "3"


def run(out_dir, fmt):
    """Render the last run's aggregate panel from ``<out_dir>/report.json`` (read-only).
    ``fmt`` ``"text"`` -> panel to stdout; ``"json"`` -> the file's bytes verbatim.
    Missing file or unexpected ``schema_version`` -> ``term.error`` + exit 2."""
    path = Path(out_dir) / "report.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        term.error(f"no run found in {out_dir!r} — run `lintle clean` first")
        return 2
    except UnicodeDecodeError as exc:
        # Issue #92: invalid-UTF-8 bytes in report.json must not propagate —
        # treat it as a corrupt/invalid report with a clear error message.
        term.error(f"{path}: invalid report.json ({exc})")
        return 2
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        term.error(f"{path}: invalid report.json ({exc})")
        return 2
    if not isinstance(envelope, dict):
        term.error(f"{path}: invalid report.json (not a JSON object)")
        return 2
    if envelope.get("schema_version") != _SCHEMA:
        term.error(
            f"{path}: unsupported schema_version {envelope.get('schema_version')!r}"
            f" (expected {_SCHEMA!r})"
        )
        return 2
    # Issue #97(a): validate the envelope shape before rendering so missing or
    # wrong-typed keys raise a clean error instead of KeyError/TypeError in render().
    bad = _check_envelope_shape(envelope)
    if bad:
        term.error(f"{path}: invalid report.json ({bad})")
        return 2
    if fmt == "json":
        print(raw, end="")
        return 0
    render(envelope, console=term.stdout_console, command_label="report")
    return 0


_SUMMARY_KEYS = (
    "files_processed",
    "paired_records",
    "orphan_entries",
    "input_lines_seen",
    "clean_count",
    "quarantined_count",
    "failed_count",
    "fix_counts",
    "quarantine_counts",
)


def _check_envelope_shape(envelope):
    """Return a human-readable description of the first envelope shape violation
    found, or ``None`` if the envelope looks renderable. Checks that ``run`` and
    ``summary`` are dicts, ``run`` carries ``timestamp``, ``elapsed_seconds``, and
    the schema-3 ``failed_files`` list, and ``summary`` carries all keys that
    :func:`render` unconditionally indexes (issue #97, #83)."""
    run = envelope.get("run")
    if not isinstance(run, dict):
        return "missing or non-object 'run' block"
    if "timestamp" not in run:
        return "'run' block missing 'timestamp'"
    if "elapsed_seconds" not in run:
        return "'run' block missing 'elapsed_seconds'"
    if not isinstance(run.get("failed_files"), list):
        return "'run' block missing or non-list 'failed_files'"
    s = envelope.get("summary")
    if not isinstance(s, dict):
        return "missing or non-object 'summary' block"
    for key in _SUMMARY_KEYS:
        if key not in s:
            return f"'summary' block missing '{key}'"
    return None
