# Aggregate Reporting UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `clean`'s per-file stdout dump with one terminal-width-responsive aggregate panel
on **stderr**, persist the run envelope as a byte-deterministic `report.json`, add a read-only
`lintle report` command, and remove the `validate` subcommand (CLI-only).

**Architecture:** A new `summary.py` leaf renders the existing `build_run_envelope()` dict to a
passed `rich` Console (stderr for `clean`, stdout for `report`); width tiers + an ASCII-bar fallback
key off that console. `report.json` is the persisted, byte-identical twin of `--report json`.

**Tech Stack:** Python 3.14 · `rich` (only runtime dep) · `pytest` · `ruff` · `uv`.

**Design reference:** `docs/superpowers/archive/specs/2026-05-31-aggregate-reporting-ux-design.md`
(stress-tested by a four-way debate; §3 channel contract and §4 `report.json` contract are
load-bearing).

**Two PRs** via the worktree + rebase-merge flow (`CONTRIBUTING.md`): Phase 1 →
`refactor/remove-validate`; Phase 2 → `feature/aggregate-report`. Create each worktree at execution
time via `superpowers:using-git-worktrees`. **No version bump in these PRs** — the v0.5.0 bump +
dated `CHANGELOG.md` land later on `chore/release-0.5.0`; add CHANGELOG-worthy notes alongside the
code.

**Baseline before starting:** `uv run pytest -q` green, `uv run ruff check .` clean,
`uv run ruff format --check .` clean.

---

## PHASE 1 — Remove the `validate` subcommand (CLI-only)

Removes the user-facing command + its renderer + its CLI tests + doc mentions. **Does not touch
`pipeline.py`**: `process_file`'s `mode` parameter and its `"validate"` branch stay (internal wiring
`clean` uses), as do all `test_pipeline.py` / throughput / report-envelope tests and the golden
fixture. See spec §8.

### Task 1.1: argparse rejects `validate`; drop the subparser entry

**Files:**
- Test: `tests/test_cli.py` (add one test; the existing `parse_args(["validate"])` test at
  `:175–176` is replaced in Task 1.3)
- Modify: `src/lintle/cli.py:167–187` (subparsers metavar + the validate/clean loop)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py  (inside the parser test class)
def test_validate_subcommand_is_rejected(self):
    import pytest
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["validate", "x"])
```

- [ ] **Step 2: Run it — expect FAIL** (validate is still a valid subcommand)

Run: `uv run pytest tests/test_cli.py -k validate_subcommand_is_rejected -q`
Expected: FAIL (no SystemExit — `validate` parses fine today).

- [ ] **Step 3: Remove `validate` from the parser**

In `src/lintle/cli.py`, change the subparsers metavar (line 170) from
`metavar="{validate,clean,diff,explain}"` to `metavar="{clean,diff,explain}"`.

Replace the `for name, help_text, description in (...)` loop (lines 173–187) so it defines **only**
`clean` — drop the entire `("validate", …)` tuple. The loop body (lines 188–255, including the
`if name == "clean":` resume block) is unchanged; it now runs once for `clean`.

```python
    for name, help_text, description in (
        (
            "clean",
            "write cleaned files and quarantine sidecars",
            "Apply validated repairs and write cleaned files plus a per-file "
            "quarantine sidecar to --out-dir; emit a corpus-wide report.md.",
        ),
    ):
        sub = subparsers.add_parser(
            name,
            help=help_text,
            description=description,
            epilog=_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        # ... (lines 195–255 unchanged: path, --out-dir, --jobs, --report,
        #      --max-quarantined, and the `if name == "clean":` resume group)
```

- [ ] **Step 4: Run it — expect PASS**

Run: `uv run pytest tests/test_cli.py -k validate_subcommand_is_rejected -q`
Expected: PASS.

- [ ] **Step 5: Do not commit yet** — the suite is red elsewhere (the ~20 `validate` CLI tests +
  the exemplar branch still reference the removed command). Tasks 1.2–1.3 bring it green, then commit.

### Task 1.2: Remove the validate-only exemplar renderer

**Files:**
- Modify: `src/lintle/cli.py:1009–1012` (the per-file print loop's validate branch)
- Modify: `src/lintle/report.py:407–452` (`format_quarantine_lines`) + docstrings at `:54` and `:383`
- Modify: `tests/test_report.py:664–922` (`TestFormatQuarantineLines`)

- [ ] **Step 1: Delete the validate exemplar branch in `cli.py`**

In the `else:` block of the report output (around `cli.py:1009–1012`), delete the two lines that
print exemplars for validate. The loop keeps only the per-file summary line for now (it is removed
wholesale in Phase 2, Task 2.6):

```python
        else:
            for stats in all_stats:
                print(report.format_summary(stats))
            if report_path:
                print(f"\nrun report: {report_path}")
            if noradids_path:
                print(f"broken NORAD IDs: {noradids_path}")
            if findings_path:
                print(f"findings: {findings_path}")
```

- [ ] **Step 2: Delete `format_quarantine_lines` and its tests**

Delete `def format_quarantine_lines(stats):` and its body (`report.py:407–452`). Delete the whole
`TestFormatQuarantineLines` class (`tests/test_report.py:664–922`). Fix the two docstrings that name
it: `report.py:54` and `report.py:383` — drop the `format_quarantine_lines` reference (the shared
`_format_diagnostic` is still used by the `.broken.txt` sidecar; keep that mention).

- [ ] **Step 3: Run the targeted suites — expect PASS**

Run: `uv run pytest tests/test_report.py -q`
Expected: PASS (no references to the removed function remain in `test_report.py`).

- [ ] **Step 4: Grep to confirm no stragglers**

Run: `rg -n "format_quarantine_lines" src tests`
Expected: no matches.

### Task 1.3: Migrate the ~20 `validate` CLI tests

These all call `cli.main(["validate", …])` and now break. **Mechanical, suite-gated.** For each,
apply ONE of two transforms, then run the suite green. (Line numbers are at HEAD; they shift as you
edit — work top-down or re-grep.)

**Rule A — DELETE** (asserts validate-specific behaviour that no longer exists):
- `:175–176` parse `validate` → command (replaced by Task 1.1's rejection test)
- `:322–328` "validate is read-only — no NDJSON/report/out-dir"
- `:520–527` `validate --report json` envelope `command=="validate"`
- `:553–571` "validate lists each quarantined record's location and rule ID" (exemplars)
- `:894`, `:958` "validate mode does not [write a checkpoint]"

**Rule B — RETARGET to `clean`** (only exercises shared argument surface; keep one copy, delete if a
`clean` equivalent already exists): the `--jobs` validation (`:356`, `:445`, `:453`), the
missing/empty-path handling (`:348`, `:366`), and the `--max-quarantined` parsing/threshold cases
(`:755`, `:766`, `:867`, `:875`, `:885`). Uniform transform:

```python
# before
rc = cli.main(["validate", str(src), "--jobs", "0"])
# after — clean needs an out-dir; drop any "no output written" assertions
rc = cli.main(["clean", str(src), "--out-dir", str(tmp_path / "out"), "--jobs", "0"])
```

- [ ] **Step 1:** Enumerate live references: `rg -n "\"validate\"|'validate'" tests/test_cli.py`
- [ ] **Step 2:** Apply Rule A (delete) / Rule B (retarget) per the lists above. For a retarget that
      duplicates an existing `clean` test (e.g. a `--jobs 0` clean test already exists), delete
      rather than duplicate (DRY).
- [ ] **Step 3:** Run: `uv run pytest tests/test_cli.py -q` → Expected: PASS.
- [ ] **Step 4:** Full suite: `uv run pytest -q` → Expected: PASS (pipeline/report validate-*mode*
      tests are untouched and still green — we did not alter `pipeline.py`).

### Task 1.4: Update user-facing docs

**Files:** `README.md` (`:56, 83, 175`), `ARCHITECTURE.md` (`:26, 76, 338`), `CONTRIBUTING.md`
(`:45`), `CLAUDE.md` (`:85, 128`).

- [ ] **Step 1:** Remove the `uv run lintle validate …` usage lines and the "validate writes
      nothing / reports defects" prose. Where a sentence contrasts `validate` vs `clean` (e.g.
      `ARCHITECTURE.md:26`, `README.md:119`), rewrite to describe `clean` only. Leave
      `ARCHITECTURE.md:338` (`run.command` is "validate" or "clean") **as-is for now** — it is the
      envelope field doc; Phase 2 Task 2.7 narrows it to "clean".
- [ ] **Step 2:** `rg -n "lintle validate" README.md ARCHITECTURE.md CONTRIBUTING.md CLAUDE.md` →
      Expected: no matches.

### Task 1.5: Verify + commit Phase 1

- [ ] **Step 1:** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` → all green.
- [ ] **Step 2:** Add a CHANGELOG-worthy note (Unreleased): `Removed: the \`lintle validate\`
      subcommand (read-only audit). Use \`lintle clean\` (its report.* artifacts cover audit needs).`
- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove the validate subcommand (CLI-only)" \
  -m "Removes lintle validate and its exemplar renderer (format_quarantine_lines); migrates its CLI tests to clean or deletes the validate-specific ones. process_file's mode plumbing and all pipeline/report validate-mode tests are untouched. Breaking change toward v0.5.0." \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PHASE 2 — `report.json`, `summary.py`, `lintle report`, and the stderr panel

### Task 2.1: Add `term.stdout_console`

**Files:**
- Modify: `src/lintle/term.py:18` (+ module docstring)
- Test: `tests/test_term.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_term.py
def test_stdout_console_targets_stdout():
    from lintle import term
    assert term.stdout_console.stderr is False
    assert term.stderr_console.stderr is True
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: module 'lintle.term' has no attribute 'stdout_console'`)

Run: `uv run pytest tests/test_term.py -k stdout_console -q`

- [ ] **Step 3: Add the console**

In `src/lintle/term.py`, below `stderr_console = Console(stderr=True)` (line 18):

```python
stdout_console = Console()
```

Extend the module docstring's first sentence to note it now owns **both** shared consoles (the
stderr one for styled ephemera; the stdout one for the `report` command's rendered view).

- [ ] **Step 4: Run — expect PASS.** Then `uv run pytest tests/test_term.py -q` (the byte-exact term
      tests must stay green).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/term.py tests/test_term.py
git commit -m "feat(term): add shared stdout Console for the report view" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.2: `report.write_run_json` (byte-identical to `--report json`)

**Files:**
- Modify: `src/lintle/report.py` (add after `write_run_report`, `:711–718`; ensure `import json` /
  `from lintle import fsutil` are present — they are)
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_report.py  (new class)
class TestWriteRunJson:
    def _envelope(self):
        # two stats from existing helpers; fixed volatile fields for determinism
        stats = [_stats_with_counts()]
        return report.build_run_envelope(
            stats, command="clean",
            started_at="2026-05-31T00:00:00Z", elapsed_seconds=1.5,
        )

    def test_bytes_match_report_json_serialization(self, tmp_path):
        env = self._envelope()
        path = tmp_path / "report.json"
        report.write_run_json(str(path), env)
        expected = json.dumps(env, indent=2) + "\n"
        assert path.read_text(encoding="utf-8") == expected

    def test_deterministic_for_same_logical_run(self, tmp_path):
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        report.write_run_json(str(a), self._envelope())
        report.write_run_json(str(b), self._envelope())
        assert a.read_bytes() == b.read_bytes()
```

(`_stats_with_counts` already exists in `tests/test_report.py`; reuse it. Add `import json` to the
test module if absent.)

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: ... 'write_run_json'`)

Run: `uv run pytest tests/test_report.py -k WriteRunJson -q`

- [ ] **Step 3: Implement**

```python
# src/lintle/report.py  (immediately after write_run_report)
def write_run_json(path, envelope):
    """Write the run ``envelope`` (the exact object ``--report json`` prints) to
    ``path``, atomically and durably via tmp + :func:`fsutil.durable_replace`.
    Serialised byte-for-byte like the ``--report json`` stdout path: ``indent=2``,
    insertion order, a trailing newline, UTF-8."""
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, indent=2) + "\n")
    fsutil.durable_replace(tmp, path)
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/lintle/report.py tests/test_report.py
git commit -m "feat(report): persist the run envelope via write_run_json" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.3: `summary.py` pure helpers

**Files:**
- Create: `src/lintle/summary.py`
- Test: `tests/test_summary.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_summary.py
from lintle import summary


class TestHelpers:
    def test_humanize_duration(self):
        assert summary._humanize_duration(45.2) == "45.2s"
        assert summary._humanize_duration(124.0) == "2m 04s"
        assert summary._humanize_duration(3661.0) == "1h 01m 01s"

    def test_format_pct_honest_tiny_rate(self):
        assert summary._format_pct(0, 1000) == "0%"
        assert summary._format_pct(4, 1_000_000) == "<0.01%"   # 0.0004% -> not 0%
        assert summary._format_pct(103_228, 232_378_271) == "0.04%"
        assert summary._format_pct(5, 0) == "—"                # no whole

    def test_can_encode(self):
        assert summary._can_encode("utf-8", "█") is True
        assert summary._can_encode(None, "█") is True          # None -> utf-8
        assert summary._can_encode("ascii", "█") is False

    def test_pick_tier(self):
        assert summary._pick_tier(is_terminal=False, width=200, unicode_ok=True) == "plain"
        assert summary._pick_tier(is_terminal=True, width=60, unicode_ok=True) == "plain"
        assert summary._pick_tier(is_terminal=True, width=120, unicode_ok=False) == "plain"
        assert summary._pick_tier(is_terminal=True, width=80, unicode_ok=True) == "medium"
        assert summary._pick_tier(is_terminal=True, width=120, unicode_ok=True) == "wide"

    def test_bar_caps_and_fallback(self):
        # full share -> full cap width
        assert summary._bar(10, 10, width=10, use_unicode=True) == "█" * 10
        assert summary._bar(10, 10, width=10, use_unicode=False) == "#" * 10
        # half share -> half the cells, padded to width
        assert summary._bar(1, 2, width=10, use_unicode=False) == "#####     "
        # zero whole -> empty (padded)
        assert summary._bar(3, 0, width=4, use_unicode=False) == "    "
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: No module named 'lintle.summary'`)

Run: `uv run pytest tests/test_summary.py -k Helpers -q`

- [ ] **Step 3: Implement the helpers**

```python
# src/lintle/summary.py
"""Render the run envelope as a terminal-width-responsive aggregate panel, and
back the read-only ``lintle report`` command. The panel is styled human UI
(rich), keyed off the *target* console (stderr for ``clean``'s end-of-run panel,
stdout for ``report``); it is NOT byte-determinism-bound — only ``report.json``
is. Fed by the ``build_run_envelope`` dict in both paths, so there is one
renderer and one input shape."""

import json
import os

from rich.text import Text

from lintle import term

_BAR_CELLS = 24  # hard cap on bar length so very wide terminals don't draw absurd bars


def _humanize_duration(seconds):
    """``45.2`` -> ``"45.2s"``; ``124`` -> ``"2m 04s"``; ``3661`` -> ``"1h 01m 01s"``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _format_pct(part, whole):
    """Honest share string: ``"—"`` when ``whole`` is 0, ``"0%"`` when ``part`` is
    0, ``"<0.01%"`` for a nonzero-but-tiny rate, else two decimals."""
    if whole <= 0:
        return "—"
    if part == 0:
        return "0%"
    rate = 100.0 * part / whole
    if rate < 0.01:
        return "<0.01%"
    return f"{rate:.2f}%"


def _can_encode(encoding, sample):
    """True if ``sample`` survives ``encoding`` (defaulting to utf-8). Used to gate
    the Unicode block-bar glyphs against ASCII/legacy locales."""
    try:
        sample.encode(encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _pick_tier(*, is_terminal, width, unicode_ok):
    """``"plain"`` off a TTY, on a narrow terminal, or when Unicode can't be
    encoded; ``"medium"`` for 72–99 cols; ``"wide"`` at >= 100."""
    if not is_terminal or width < 72 or not unicode_ok:
        return "plain"
    return "medium" if width < 100 else "wide"


def _bar(part, whole, *, width, use_unicode):
    """A left-aligned fill bar of ``width`` cells, proportional to ``part/whole``,
    padded with spaces. Unicode full-block or ASCII ``#``."""
    fill_char = "█" if use_unicode else "#"
    cells = 0 if whole <= 0 else min(width, round(width * part / whole))
    return fill_char * cells + " " * (width - cells)
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/lintle/summary.py tests/test_summary.py
git commit -m "feat(summary): pure render helpers (duration, pct, bar, tier)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.4: `summary.render` — the three tiers

**Files:**
- Modify: `src/lintle/summary.py` (add `render` + the three `_render_*` functions + a `_rows` helper)
- Test: `tests/test_summary.py`

The renderer reads `envelope["run"]`, `envelope["summary"]`, and (for `report`)
`envelope["run"]["timestamp"]`. `summary.fix_counts` / `quarantine_counts` are `{label: count}`
dicts already in descending-construction order; render sorts by count descending for display.

- [ ] **Step 1: Write the failing tests** (behaviour-level — the panel is styled, not byte-locked)

```python
# tests/test_summary.py
import io
from rich.console import Console
from lintle import report


def _console(width, *, terminal):
    return Console(file=io.StringIO(), width=width, force_terminal=terminal,
                   color_system=None, legacy_windows=False)


def _demo_envelope():
    # one clean + one quarantined record is enough to exercise both sections
    stats = [_stats_with_counts()]  # reuse test_report's helper via a shared import
    return report.build_run_envelope(
        stats, command="clean", started_at="2026-05-31T12:00:00Z", elapsed_seconds=124.0,
    )


class TestRender:
    def test_wide_has_bars_and_totals(self):
        con = _console(120, terminal=True)
        summary.render(_demo_envelope(), console=con)
        out = con.file.getvalue()
        assert "clean" in out and "quarantined" in out
        assert "█" in out                      # bars present in wide

    def test_medium_has_no_bars(self):
        con = _console(80, terminal=True)
        summary.render(_demo_envelope(), console=con)
        out = con.file.getvalue()
        assert "█" not in out                   # bars dropped at medium
        assert "quarantined" in out

    def test_plain_when_piped_is_ascii(self):
        con = _console(120, terminal=False)     # not a TTY -> plain
        summary.render(_demo_envelope(), console=con)
        out = con.file.getvalue()
        assert "█" not in out and "─" not in out and "→" not in out  # ASCII only
        assert "clean" in out
```

(For `_stats_with_counts`, import it from `tests.test_report` or copy the small builder into
`tests/test_summary.py`; the executor picks whichever keeps tests independent — prefer a local
builder to avoid cross-module test imports.)

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: ... 'render'`)

Run: `uv run pytest tests/test_summary.py -k Render -q`

- [ ] **Step 3: Implement `render` + the tier functions**

```python
# src/lintle/summary.py  (append)

def _sorted_counts(d):
    """``{label: count}`` -> list of ``(label, count)`` sorted by count desc, then label."""
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))


def render(envelope, *, console, command_label="clean"):
    """Render the aggregate panel for ``envelope`` to ``console``, choosing a width
    tier off the console (plain when piped / narrow / non-UTF-8). Styled human UI —
    not byte-deterministic. ``command_label`` heads the banner ("clean"/"report")."""
    run = envelope["run"]
    s = envelope["summary"]
    unicode_ok = _can_encode(console.encoding, "█─→")
    tier = _pick_tier(is_terminal=console.is_terminal, width=console.width, unicode_ok=unicode_ok)
    if tier == "plain":
        _render_plain(console, command_label, run, s)
    elif tier == "medium":
        _render_sections(console, command_label, run, s, bars=False)
    else:
        _render_sections(console, command_label, run, s, bars=True)


def _totals_lines(run, s):
    """Shared ``(label, value)`` rows for the totals block (used by every tier)."""
    routed = s["clean_count"] + s["quarantined_count"]
    return [
        ("files", f"{s['files_processed']:,}"),
        ("records", f"{s['paired_records']:,}"),
        ("clean", f"{s['clean_count']:,}   {_format_pct(s['clean_count'], routed)}"),
        ("quarantined", f"{s['quarantined_count']:,}   {_format_pct(s['quarantined_count'], routed)}"),
        ("orphans", f"{s['orphan_entries']:,}"),
        ("lines", f"{s['input_lines_seen']:,}"),
        ("elapsed", _humanize_duration(run["elapsed_seconds"])),
    ]


def _render_plain(console, label, run, s):
    """ASCII-only key/value + dense fix/quarantine lines. No color, bars, or box glyphs."""
    console.print(Text(f"lintle {label} - {run['timestamp']}", style=""), highlight=False)
    for k, v in _totals_lines(run, s):
        console.print(Text(f"  {k:<12} {v}"), highlight=False)
    if s["fix_counts"]:
        fixes = " | ".join(f"{k} {n:,}" for k, n in _sorted_counts(s["fix_counts"]))
        console.print(Text(f"  fixes:       {fixes}"), highlight=False)
    if s["quarantine_counts"]:
        q = " | ".join(f"{k} {n:,}" for k, n in _sorted_counts(s["quarantine_counts"]))
        console.print(Text(f"  quarantined: {q}"), highlight=False)


def _render_sections(console, label, run, s, *, bars):
    """Medium/wide: a totals banner + Fixes / Quarantined sections. Bars only when
    ``bars`` (wide tier); the console here is always a UTF-8-capable TTY."""
    from rich.table import Table
    from rich import box

    console.rule(f"lintle {label} · {run['timestamp']}")
    totals = "   ".join(f"{k} {v}" for k, v in _totals_lines(run, s))
    console.print(Text("  " + totals), highlight=False)

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
                row.append(_bar(n, total, width=_BAR_CELLS, use_unicode=True)
                           + f" {_format_pct(n, total)}")
            t.add_row(*row)
        return t

    if s["fix_counts"]:
        console.print(_section("Fixes applied", s["fix_counts"], with_bars=False))
    if s["quarantine_counts"]:
        console.print(_section("Quarantined by rule", s["quarantine_counts"], with_bars=bars))
```

- [ ] **Step 4: Run — expect PASS.** Iterate on the tier functions until all three render tests pass
      (the assertions are deliberately substring-level so layout can be tuned freely).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/summary.py tests/test_summary.py
git commit -m "feat(summary): responsive aggregate panel (plain/medium/wide)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.5: `summary.run` — the `report` command entry

**Files:**
- Modify: `src/lintle/summary.py` (add `run` + a `_ReportError`)
- Test: `tests/test_summary.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_summary.py
class TestRun:
    def _write_report(self, tmp_path, env):
        report.write_run_json(str(tmp_path / "report.json"), env)

    def test_text_renders_to_stdout(self, tmp_path, capsys):
        self._write_report(tmp_path, _demo_envelope())
        rc = summary.run(str(tmp_path), "text")
        assert rc == 0
        out = capsys.readouterr().out
        assert "clean" in out and "quarantined" in out

    def test_json_emits_bytes_verbatim(self, tmp_path, capsys):
        self._write_report(tmp_path, _demo_envelope())
        raw = (tmp_path / "report.json").read_text(encoding="utf-8")
        rc = summary.run(str(tmp_path), "json")
        assert rc == 0
        assert capsys.readouterr().out == raw

    def test_missing_report_is_exit_2(self, tmp_path, capsys):
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "no run found" in capsys.readouterr().err

    def test_bad_schema_is_exit_2(self, tmp_path, capsys):
        (tmp_path / "report.json").write_text('{"schema_version": "99"}', encoding="utf-8")
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "schema" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run — expect FAIL.**  Run: `uv run pytest tests/test_summary.py -k Run -q`

- [ ] **Step 3: Implement**

```python
# src/lintle/summary.py  (append)
_SCHEMA = "2"


def run(out_dir, fmt):
    """Render the last run's aggregate panel from ``<out_dir>/report.json`` (read-only).
    ``fmt`` ``"text"`` -> panel to stdout; ``"json"`` -> the file's bytes verbatim.
    Missing file or unexpected ``schema_version`` -> ``term.error`` + exit 2."""
    path = os.path.join(out_dir, "report.json")
    try:
        raw = open(path, encoding="utf-8").read()
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
            f"{path}: unsupported schema_version "
            f"{envelope.get('schema_version')!r} (expected {_SCHEMA!r})"
        )
        return 2
    if fmt == "json":
        print(raw, end="")
        return 0
    render(envelope, console=term.stdout_console, command_label="report")
    return 0
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/lintle/summary.py tests/test_summary.py
git commit -m "feat(summary): lintle report reads report.json and renders/emits it" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.6: Wire `clean` (persist + panel) and add the `report` subcommand

**Files:**
- Modify: `src/lintle/cli.py` — imports; subparser (add `report`); dispatch; envelope build; report
  block; output block
- Modify: `src/lintle/report.py:208–224` (remove `format_summary`) + `tests/test_report.py:488,504`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/test_cli.py
class TestReportArtifactAndCommand:
    def test_clean_writes_report_json(self, tmp_path):
        src = _write_demo_tle(tmp_path)            # reuse an existing fixture helper
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        data = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert data["schema_version"] == "2"
        assert data["run"]["command"] == "clean"

    def test_report_json_file_equals_report_json_stdout(self, tmp_path, capsys):
        src = _write_demo_tle(tmp_path)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1", "--report", "json"])
        stdout = capsys.readouterr().out
        assert (out / "report.json").read_text(encoding="utf-8") == stdout

    def test_clean_panel_goes_to_stderr_not_stdout(self, tmp_path, capsys):
        src = _write_demo_tle(tmp_path)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])  # text mode
        cap = capsys.readouterr()
        assert cap.out == ""                       # text-mode stdout is empty
        assert "clean" in cap.err                  # panel on stderr

    def test_report_command_renders_last_run(self, tmp_path, capsys):
        src = _write_demo_tle(tmp_path)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        capsys.readouterr()
        rc = cli.main(["report", str(out)])
        assert rc == 0 and "quarantined" in capsys.readouterr().out
```

(`_write_demo_tle` — reuse whatever fixture builder `tests/test_cli.py` already uses to make a small
TLE file; grep the file for an existing helper and reuse it.)

- [ ] **Step 2: Run — expect FAIL** (no `report.json`, panel still per-file on stdout, no `report` cmd).

Run: `uv run pytest tests/test_cli.py -k ReportArtifactAndCommand -q`

- [ ] **Step 3: Add the `report` subparser**

After the `explain` subparser block (`cli.py:281–297`), add:

```python
    # `report` is a read-only render of a prior clean run's report.json. Positional
    # out-dir (like diff's run dirs), default matches clean's --out-dir. Writes nothing.
    report_parser = subparsers.add_parser(
        "report",
        help="render the last clean run's aggregate summary from report.json",
        description=(
            "Read report.json from a clean-run output directory and render the "
            "aggregate run summary. --report json re-emits report.json verbatim. "
            "Read-only; writes nothing."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report_parser.add_argument(
        "out_dir", nargs="?", default=_DEFAULT_OUTPUT, metavar="OUT-DIR",
        help=f"a clean-run output directory (default: {_DEFAULT_OUTPUT})",
    )
    report_parser.add_argument(
        "--report", choices=["text", "json"], default="text",
        help="render the panel (text) or re-emit report.json (json)",
    )
```

Update the subparsers metavar (`cli.py:170`) to `metavar="{clean,diff,explain,report}"`.

- [ ] **Step 4: Add the dispatch + the `summary` import**

Add `from lintle import summary` to the imports. After the `explain` dispatch block (`cli.py:722`):

```python
    # `report` is a read-only render of a prior run's report.json.
    if args.command == "report":
        return summary.run(args.out_dir, args.report)
```

- [ ] **Step 5: Build the envelope unconditionally + persist `report.json`**

Inside `if args.command == "clean" and all_stats:` (the report block, `cli.py:980–992`), build the
envelope once and add the `report.json` write (place the build before the file writes so the same
object feeds the panel and `--report json`):

```python
        report_path = None
        noradids_path = None
        findings_path = None
        envelope = None
        if args.command == "clean" and all_stats:
            run_elapsed = time.monotonic() - run_monotonic_start
            envelope = report.build_run_envelope(
                all_stats, command=args.command,
                started_at=run_started_iso, elapsed_seconds=run_elapsed,
            )
            with _status("finalizing report…"):
                report_path = os.path.join(args.out_dir, "report.md")
                report.write_run_report(report_path, all_stats)
                report_json_path = os.path.join(args.out_dir, "report.json")
                report.write_run_json(report_json_path, envelope)
                noradids_path = os.path.join(args.out_dir, "broken-noradids.ndjson")
                report_writers.write_broken_noradids_ndjson(noradids_path, all_stats)
                findings_path = os.path.join(args.out_dir, "report.jsonl")
                report_writers.concat_findings_shards(args.out_dir, findings_path, all_stats)
```

- [ ] **Step 6: Replace the output block with the stderr panel**

Replace the whole `if args.report == "json": … else: …` block (`cli.py:994–1018`) with:

```python
        if envelope is not None:
            # Human aggregate panel -> stderr (styled ephemera; coexists with the
            # machine envelope on stdout). The per-file detail lives in report.md.
            summary.render(envelope, console=term.stderr_console, command_label="clean")
            if args.report == "json":
                print(json.dumps(envelope, indent=2))
```

(`envelope is None` only when `all_stats` is empty — nothing processed; nothing to summarise. Keep
the existing empty-input handling above this block as-is.)

- [ ] **Step 7: Remove the now-unused `format_summary`**

Delete `def format_summary(stats):` (`report.py:208–224`) and its two tests
(`test_report.py:488` `test_format_summary_shows_counts`, `:497`
`test_format_summary_distinguishes_paired_from_orphan`). Confirm: `rg -n "format_summary" src tests`
→ no matches.

- [ ] **Step 8: Run the targeted + full suites — expect PASS**

Run: `uv run pytest tests/test_cli.py -k ReportArtifactAndCommand -q` then `uv run pytest -q`.
Expected: PASS. (Existing `clean` CLI tests that asserted per-file stdout summaries must be updated
to assert the panel on **stderr** / empty stdout — grep `capsys` + `validate`/`format_summary`
expectations in `test_cli.py` and fix; the new tests above are the template.)

- [ ] **Step 9: Commit**

```bash
git add src/lintle/cli.py src/lintle/report.py tests/test_cli.py tests/test_report.py
git commit -m "feat(cli): persist report.json, stderr aggregate panel, lintle report" \
  -m "clean builds the envelope unconditionally, persists it as report.json (byte-identical to --report json), and renders one responsive aggregate panel to stderr (replacing the per-file stdout dump). New read-only lintle report renders/re-emits report.json. format_summary removed." \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.7: Docs — `report.json`, the channel contract, `run.command`

**Files:** `ARCHITECTURE.md`, `README.md`, `CONTRIBUTING.md`, `CLAUDE.md`

- [ ] **Step 1:** In `ARCHITECTURE.md` §6 (Outputs), add `report.json` as a persisted artifact (the
      durable, byte-identical twin of `--report json`); document the **channel contract** (stdout =
      machine/data; stderr = the styled panel + progress); add `report` to the command list. Narrow
      `ARCHITECTURE.md:338` `run.command` doc to `"clean"` (note the API still accepts any string).
- [ ] **Step 2:** In `README.md`, replace the old per-file `clean` summary example (`:175`) with the
      aggregate panel; document `lintle report [out-dir]`. Update the `src/lintle/` module list in
      `CLAUDE.md:78–96` to add `summary.py` and amend the `report.py` line (drop "validate
      summaries"). Add `report.json` to the `data/output/` artifact list.
- [ ] **Step 3:** Add a CHANGELOG-worthy note: `Added: report.json (persisted run envelope) and the
      \`lintle report\` command. Changed: clean's end-of-run summary is now one aggregate panel on
      stderr (per-file detail remains in report.md).`

### Task 2.8: Final verification + commit

- [ ] **Step 1:** `uv run pytest --cov=lintle --cov-report=term-missing --cov-branch -q` → all green;
      confirm `summary.py` is covered.
- [ ] **Step 2:** `uv run ruff check . && uv run ruff format --check .` → clean.
- [ ] **Step 3:** Manual smoke (real terminal, then piped):
  - `uv run lintle clean <small-sample> --out-dir /tmp/lr` → panel on stderr, stdout empty;
    `/tmp/lr/report.json` exists.
  - `uv run lintle report /tmp/lr` → panel on stdout; `uv run lintle report /tmp/lr | cat` → plain
    ASCII (no `█`/`─`); `uv run lintle report /tmp/lr --report json | jq .` → valid JSON.
- [ ] **Step 4: Commit** the docs (`git add ARCHITECTURE.md README.md CONTRIBUTING.md CLAUDE.md
      CHANGELOG.md && git commit -m "docs: report.json, channel contract, lintle report"` + the
      Co-Authored-By trailer).

---

## Self-review (done while writing)

- **Spec coverage:** §3 channel contract → Tasks 2.1, 2.6 (panel→stderr, empty stdout, report→stdout).
  §4 report.json → 2.2, 2.6 (+ determinism + ≡ `--report json` tests). §5 `lintle report` → 2.5, 2.6.
  §6 renderer/responsiveness/bars/honest-% → 2.3, 2.4. §8 validate removal (CLI-only) → Phase 1.
  §9 testing → embedded per task. §10 sequencing/PRs → headers. All sections covered.
- **Placeholder scan:** the only non-verbatim items are deliberate, suite-gated discovery steps
  (the bulk `validate`-CLI-test migration in 1.3 and reuse of existing fixture helpers `_stats_with_counts`
  / `_write_demo_tle`), each with an exact `rg` to enumerate and a green-suite gate. No "TBD"/"handle
  edge cases" hand-waving in the implementation code.
- **Type/name consistency:** `render(envelope, *, console, command_label)`, `run(out_dir, fmt)`,
  `write_run_json(path, envelope)`, `_pick_tier(*, is_terminal, width, unicode_ok)`,
  `_bar(part, whole, *, width, use_unicode)`, `_format_pct(part, whole)` — used consistently across
  Tasks 2.2–2.6.
