"""Render the run envelope as a terminal-width-responsive aggregate panel, and
back the read-only ``lintle report`` command. The panel is styled human UI
(rich), keyed off the *target* console (stderr for ``clean``'s end-of-run panel,
stdout for ``report``); it is NOT byte-determinism-bound — only ``report.json``
is. Fed by the ``build_run_envelope`` dict in both paths, so there is one
renderer and one input shape."""

import json
import os

from rich import box
from rich.table import Table
from rich.text import Text

from lintle import term

_BAR_CELLS = 24


def _humanize_duration(seconds):
    """Return a human-readable duration string for ``seconds``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _format_pct(part, whole):
    """Return a percentage string for ``part / whole``, honest about tiny rates."""
    if whole <= 0:
        return "—"
    if part == 0:
        return "0%"
    rate = 100.0 * part / whole
    if rate < 0.01:
        return "<0.01%"
    return f"{rate:.2f}%"


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


def _totals_lines(run, s):
    """Return a list of ``(label, value_str, pct_str)`` triples for the totals block."""
    routed = s["clean_count"] + s["quarantined_count"]
    return [
        ("files", f"{s['files_processed']:,}", ""),
        ("records", f"{s['paired_records']:,}", ""),
        ("clean", f"{s['clean_count']:,}", _format_pct(s["clean_count"], routed)),
        (
            "quarantined",
            f"{s['quarantined_count']:,}",
            _format_pct(s["quarantined_count"], routed),
        ),
        ("orphans", f"{s['orphan_entries']:,}", ""),
        ("lines", f"{s['input_lines_seen']:,}", ""),
        ("elapsed", _humanize_duration(run["elapsed_seconds"]), ""),
    ]


def _print_totals(console, run, s):
    """Print one totals field per line to ``console``; right-aligns the value column."""
    rows = _totals_lines(run, s)
    width = max(len(value) for _, value, _ in rows)
    for label, value, pct in rows:
        line = f"  {label:<12} {value:>{width}}"
        if pct:
            line += f"   {pct}"
        console.print(Text(line), highlight=False)


def _render_plain(console, label, run, s):
    """Render a plain ASCII-only summary line block (no box, no bars, no arrows)."""
    console.print(Text(f"lintle {label} - {run['timestamp']}"), highlight=False)
    _print_totals(console, run, s)
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


def _render_sections(console, label, run, s, *, bars):
    """Render a rich-styled section panel (medium: no bars; wide: with bars)."""
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


def render(envelope, *, console, command_label="clean"):
    """Render the run envelope as a responsive aggregate panel to ``console``."""
    run = envelope["run"]
    s = envelope["summary"]
    unicode_ok = _can_encode(console.encoding, "█─→")
    tier = _pick_tier(
        is_terminal=console.is_terminal, width=console.width, unicode_ok=unicode_ok
    )
    if tier == "plain":
        _render_plain(console, command_label, run, s)
    else:
        _render_sections(console, command_label, run, s, bars=(tier == "wide"))


_SCHEMA = "2"


def run(out_dir, fmt):
    """Render the last run's aggregate panel from ``<out_dir>/report.json`` (read-only).
    ``fmt`` ``"text"`` -> panel to stdout; ``"json"`` -> the file's bytes verbatim.
    Missing file or unexpected ``schema_version`` -> ``term.error`` + exit 2."""
    path = os.path.join(out_dir, "report.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        term.error(f"no run found in {out_dir!r} — run `lintle clean` first")
        return 2
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        term.error(f"{path}: invalid report.json ({exc})")
        return 2
    if envelope.get("schema_version") != _SCHEMA:
        term.error(
            f"{path}: unsupported schema_version {envelope.get('schema_version')!r}"
            f" (expected {_SCHEMA!r})"
        )
        return 2
    if fmt == "json":
        print(raw, end="")
        return 0
    render(envelope, console=term.stdout_console, command_label="report")
    return 0
