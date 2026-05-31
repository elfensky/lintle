# Aggregate reporting UX — stderr panel, persisted `report.json`, and `lintle report`

**Status:** Proposed · **Date:** 2026-05-31 · **Target:** v0.5.0 (breaking — removes `validate`)

Stress-tested by a 2-round, four-way AI debate (Gemini / Codex / Sonnet / Opus, cross-critique).
The debate is archived under `~/.claude-octopus/debates/no-session/001-aggregate-report-ux/`
(`synthesis.md`); its four unanimous conclusions are folded in below and called out in §11.

## 1. Problem

- `lintle clean` finishes by printing a 3-line block **per file** to stdout (×29 on the corpus):
  a header + a `fixes:` line + a `quarantined:` line. It is noisy and hard to read.
- The corpus totals already exist as a structured **envelope** (`report.build_run_envelope`,
  `schema_version "2"`), but it is computed **only** under `--report json` and printed to stdout —
  never persisted. There is no way to see the last run's summary on demand after it scrolls away.
- `lintle validate` (identical processing, writes nothing to disk) is judged not worth its surface
  area: a read-only audit that produces no artifact duplicates what `clean` already does.

## 2. Goals / non-goals

**Goals**
- Replace `clean`'s per-file terminal dump with **one** polished, terminal-width-responsive
  **aggregate** summary.
- **Persist** the run's totals to disk so a new read-only `lintle report` can re-render them on demand.
- **Remove** the `validate` subcommand.
- Hold every hard invariant; add **no** runtime dependency (`rich` + stdlib only).

**Non-goals**
- No per-file terminal output — per-file detail stays in `report.md`.
- No new analysis/queries (focused-cleaner scope: `report` only re-renders persisted totals).
- No change to `cleaned/*`, the `.broken.txt` sidecar, `report.jsonl`, `broken-noradids.ndjson`,
  the checkpoint, or the envelope `schema_version "2"`.

## 3. The channel contract (the core principle — decided by the debate)

stdout is the **machine/data/pipeable** channel; stderr carries **styled human UI** (live progress,
roster, and now the aggregate panel). This generalises `term.py`'s existing "styling is confined to
`stderr_console`" choke point into an explicit, codified contract:

| Command / mode | **stdout** | **stderr** |
|---|---|---|
| `clean` (text, default) | *(empty)* | progress · aggregate panel · artifact-path footer |
| `clean --report json` | `report.json` envelope (≡ the persisted file) | progress · aggregate panel · footer |
| `report` (text, default) | aggregate panel | *(errors only)* |
| `report --report json` | `report.json` bytes, verbatim | *(errors only)* |

**Consequence we exploit:** with `clean`'s panel on stderr, the human panel and `--report json`
**coexist with no interlock** — `lintle clean --report json > out.json` writes clean JSON to the
file while the operator still sees the panel on the terminal.

**Backward-compat note (flagged for sign-off):** text-mode `lintle clean` **stdout becomes empty**
(it previously carried the per-file summary). Scripts that want machine output use `--report json`
or the persisted files. This is a deliberate, breaking UX change for v0.5.0.

`report`'s panel goes to **stdout** because for that command the rendered view **is** the
deliverable (like `git log`); it still degrades to plain text when piped.

## 4. Persisted `report.json`

- On every `clean` run (`command == "clean" and all_stats`), write `<out-dir>/report.json` via
  `fsutil.durable_replace`, in the same finalisation block as `report.md` / `report.jsonl`.
- **Content:** the full `build_run_envelope(...)` output (`run` · `environment` · `summary` ·
  `files[]`) — the *same* object `--report json` prints. **`report.json` bytes ≡ `--report json`
  stdout.**
- **Serialisation:** `json.dumps(envelope, indent=2)` + trailing `\n`, UTF-8, LF — matching
  `cli.py:1007` exactly. **Insertion order; NO `sort_keys`** (sorting would diverge from the
  documented §6 envelope and break any diff comparing the persisted file to the live one).
- **Determinism:** byte-identical for the same logical run, modulo the *declared volatile* fields
  (`run.timestamp`, `run.elapsed_seconds`, `files[i].elapsed_seconds`, `files[i].records_per_sec`).
  The inner count-maps are already deterministically constructed (sorted `all_stats` + `Counter`
  insertion order). Locked by tests (§9).
- **Implementation:** build the envelope **unconditionally** for `clean` (today it is built only
  inside the `--report json` branch at `cli.py:1001`). Hoist the `run_started_iso` / `run_elapsed`
  computation, build once, then feed *both* the persisted file, the optional `--report json`
  stdout emission, and the panel renderer. Cost is negligible — an aggregation over ~29 `FileStats`
  (debate: unanimous non-issue).
- Keep the **full** envelope, including `files[].quarantined_norad_ids` — one schema, two
  consumers; `broken-noradids.ndjson` stays the canonical NORAD stream. Size is bounded by
  quarantine count (~103K → a few MB), acceptable. If it ever bites, add an explicit compact
  format later (do not silently fork the schema now).

## 5. `lintle report` command (read-only)

- New subparser `report [out-dir]` (positional `out-dir`, default `data/output`) + the shared
  `--report {text,json}` flag.
- Entry point `summary.run(out_dir, fmt) -> exit_code`, mirroring `diff.run` / `explain.render`.
- Reads `<out-dir>/report.json`:
  - `text` → render the aggregate panel to **stdout** (the deliverable).
  - `json` → emit the `report.json` bytes **verbatim** to stdout.
- Errors via `term.error` → **exit 2** (mirrors `diff`'s `DiffError`): missing file
  (`no run found in <out-dir> — run \`lintle clean\` first`), unreadable/invalid JSON, or
  `schema_version != "2"` (a forward-compat guard).

## 6. Renderer & responsiveness (new `summary.py` leaf)

- `summary.render(envelope, *, console)` builds the `rich` renderable from the envelope **dict** and
  prints to the given `Console`. Fed by the in-process envelope (`clean`) or the on-disk envelope
  (`report`) — a single input shape.
- **Console wiring:** `clean` passes a **stderr**-bound Console (the human channel); `report` passes
  a **stdout**-bound Console. Add `term.stdout_console` (a `Console()` sibling of `stderr_console`)
  for injection/testability. All tier logic keys off **the passed console's** `.is_terminal`,
  `.width`, and `.encoding` — never the wrong stream (debate: Sonnet/Codex).
- **Width tiers** (off the *target* console):
  - **not `is_terminal`** (piped/redirected) **OR** encoding can't encode the block glyphs **OR**
    `width < 72` → **compact plain**: aligned key/value totals + dense `fixes:` / `quarantined:`
    lines, **ASCII only**, no bars, no color, no box characters. Greppable.
  - `is_terminal` and `72 ≤ width < 100` → **medium**: stacked sections, no bars.
  - `is_terminal` and `width ≥ 100` → **wide**: one-line totals banner + side-by-side *Fixes
    applied* / *Quarantined by rule* + percentage **bars**.
- **Bars (the unanimous P0 hardening):**
  - Use Unicode block chars (`█ ▌ …`) **only** when the target console's encoding can encode them
    (probe via a guarded `.encode()` against `console.encoding`); otherwise fall back to an ASCII
    fill (`#`) or drop bars. **Gate on encoding + `is_terminal`, not width alone.**
  - **Cap** bar length to a fixed maximum (≈24 cells) so very wide terminals don't draw absurd
    bars; length ∝ share of total quarantined, scaled to the cap.
  - **Honest percentages:** enough precision that tiny rates aren't misleading — show `0.04%`, and
    `<0.01%` rather than `0.00%`/`0%` for a nonzero-but-tiny rate. **Never render a nonzero count as
    `0%`.**
  - Fixed, **right-aligned** numeric columns with thousands separators; labels in a fixed-width
    column, truncated with `…` rather than wrapped.
- **Content:** `files_processed`; `run.elapsed_seconds` (humanised, e.g. `2m 04s`); records
  (paired); clean (count + %); quarantined (count + %); orphans; lines; rec/s; *Fixes applied*
  (per `FixClass`, descending); *Quarantined by rule* (per `RuleID`, descending, with bars in
  wide); and an artifact-pointer footer (`report.md · report.json · report.jsonl ·
  broken-noradids.ndjson → <out-dir>`). For `report`, also show `run.timestamp` ("last run: …").
- The rendered panel is **not** bound by the byte-determinism invariant — it is styled human
  ephemera (like the roster). Only `report.json` is bound.

## 7. Module & dependency flow

- **New `summary.py`** (`rich` + stdlib `json` only): `render(envelope, *, console)` +
  `run(out_dir, fmt)`. A read-only `cli → summary` consumer, exactly like `cli → diff` /
  `cli → explain`. Depends only on `rich` + stdlib (the envelope is a plain dict of string keys;
  labels come from the dict, so no import of the data leaves is required).
- **`report.py`:** add `write_run_json(path, envelope)` beside `write_run_report` (via
  `durable_replace`). `build_run_envelope` unchanged.
- **`term.py`:** add `stdout_console` beside `stderr_console`; update the module docstring (it now
  owns both shared consoles). `NO_COLOR` / off-TTY handling is `rich`-native on both.
- **`cli.py`:** remove `validate`; build the envelope unconditionally for `clean`; persist
  `report.json`; render the panel to `term.stderr_console` for `clean` **always** (regardless of
  `--report`); keep the `--report json` stdout emission; **replace the per-file `format_summary`
  print loop (`cli.py:1009–1010`) with the panel** — `report.format_summary` then becomes unused and
  is removed with its 2 tests (`test_report.py:488,497`); add the `report` subparser → `summary.run`.
- `fsutil.durable_replace` reused for `report.json`.

## 8. Removing `validate`

- **Delete:** the `validate` subparser (help/description); its dispatch; the validate-only terminal
  branch (`cli.py:1011–1012`); `report.format_quarantine_lines` **and its ~10 tests**
  (`TestFormatQuarantineLines`, `tests/test_report.py:664–922` — verified); and update the two
  docstrings in `report.py` that reference it (lines 54, 383).
- **Keep (shared with `clean`):** `quarantine_sample` / `FileSample` / `_PER_RULE_EXEMPLAR_BOUND`,
  `dropped_counts` (envelope + `report.md`), `_format_diagnostic` (the sidecar's renderer), and all
  pipeline internals. **Envelope `schema_version` stays `"2"`** — verified these are populated by
  `pipeline.py:328` and serialised at `report.py:271`, so they are not validate-only.
- `run.command` is now always `"clean"`; keep the field (string); update docs that say
  "validate or clean".
- Breaking change → CHANGELOG + v0.5.0.

## 9. Testing (TDD — Claude writes tests + implementation; multi-LLM is review-only)

- `summary.render`: a known envelope rendered to `Console(file=StringIO, force_terminal=True,
  width=120)` → assert bars + totals present; `width=80` → medium, no bars; `force_terminal=False`
  → compact plain, **assert no box/bar/ANSI chars** (greppable); an ASCII-only-encoding Console →
  **no Unicode block chars** (ASCII fallback fires); a nonzero tiny rate → **not `0%`**.
- `summary.run`: temp `report.json` → exit 0 + totals on stdout; missing → exit 2 + "no run found";
  `schema_version` mismatch → exit 2.
- `report.write_run_json`: **byte-determinism** (two builds with fixed volatile fields → identical
  bytes); **`report.json` bytes == `--report json` stdout bytes** (the cross-check that locks the
  contract); atomic (`durable_replace`).
- `cli` integration: `clean` writes `report.json` and it round-trips through `report`; the `report`
  subcommand is wired; argparse **rejects** `validate`.
- Remove `TestFormatQuarantineLines`; retarget any validate-specific CLI tests.

## 10. Versioning & sequencing

- **v0.5.0** (breaking: removes `validate`; empties text-mode `clean` stdout). The version bump +
  dated `CHANGELOG.md` section land on a `chore/release-0.5.0` branch later (not in feature PRs),
  per `CONTRIBUTING.md`; feature PRs add CHANGELOG-worthy notes alongside the code.
- **Two PRs** via the worktree + rebase-merge flow:
  - **PR1 `refactor/remove-validate`:** delete the subcommand + `format_quarantine_lines` + its
    tests + doc references. Small, self-contained, reviewable on its own.
  - **PR2 `feature/aggregate-report`:** `report.json` persistence + `summary.py` + `lintle report`
    + the stderr panel + renderer hardening + the ARCHITECTURE.md updates.
  (Flexible — could be one PR; two keeps the deletion reviewable separately.)
- **ARCHITECTURE.md** updates: document the new `report.json` artifact (§6), the codified channel
  contract (§3 here), and `validate`'s removal.

## 11. Alternatives considered (debate outcomes)

- **Panel on stdout** (this spec's original choice): **rejected** — contradicts the `stdout = data`
  invariant and risks polluting `--report json` / pipes / CI capture. stderr chosen (unanimous, R2).
- **`sort_keys=True` for `report.json`:** **rejected** — would diverge from the documented
  insertion-order `--report json` envelope and break diffs comparing persisted vs. live. Match
  `--report json` exactly (both external models retracted `sort_keys` in R2).
- **Trim `files[].quarantined_norad_ids` from `report.json`:** **rejected** — forks the schema with
  no versioning story to save a few MB; keep one envelope, two consumers (unanimous).
- **Parse `report.md` for totals** (instead of persisting `report.json`): rejected earlier —
  brittle coupling to human Markdown vs. a structured single source.

## 12. Accepted tradeoffs

- Removing `validate` gives up a **pre-write read-only audit** (you now must run `clean`, which
  writes outputs, to see findings). The user has explicitly accepted this. A future
  `clean --dry-run` (clean's analysis without writing `cleaned/*`) could restore it if ever
  wanted — **out of scope** here.
