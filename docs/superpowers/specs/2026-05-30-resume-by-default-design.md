# `clean` resume-by-default + cancel/resume UX — Design

- **Date:** 2026-05-30
- **Status:** Designed; ready for implementation.
- **Revision:** rev 1 (2026-05-30) — initial design, hardened by a three-lens adversarial
  review (CLI/TUI conventions · correctness/data-safety · UX/footguns) before approval.
- **Topic:** Make resume the **default** behaviour of `lintle clean` (today it is opt-in via
  `--resume`), with a clear cancel→resume cycle: Ctrl-C stops promptly and prints how to
  continue; re-running picks up where it stopped. Interactivity follows standard TUI
  conventions (prompt only on a real terminal; deterministic in CI). Resume stays
  `clean`-only — `validate` is unchanged.

## 1. Problem

`lintle clean` is a ~1-hour batch over ~30 GB. It is interruptible (Ctrl-C → exit 130) and
already has a durable per-run checkpoint (`resume.py`, issue #56): `.clean-state.json` in
`--out-dir` records each completed file and the input fingerprints, and is deleted on full
success — so its *presence* marks an interrupted run. But resuming requires the operator to
know about and pass `--resume`, and the cancel path gives no hint that resuming is even
possible:

1. **Resume is undiscoverable.** Ctrl-C prints only `interrupted — workers stopped`. A user
   who doesn't know `--resume` exists re-runs from scratch, repeating up to an hour of work.
2. **Opt-in is the wrong default for an interrupted long job.** After an interruption, the
   overwhelmingly common intent on re-run is "continue", not "start over".

The goal: cancelling and resuming should be the obvious, default cycle, while keeping the
existing correctness guarantees (refuse-on-change, atomic writes) and not introducing the
silent-footgun failure modes that resume-by-default invites.

## 2. Decision summary

Resume becomes the default for `clean`. The decision of *resume vs. fresh* is driven, in
priority order, by: an explicit flag → else a TTY prompt → else a deterministic
non-interactive default. A stale or corrupt checkpoint never silently discards work. Two
safety nets close the gaps a default-on resume exposes: **output-existence re-verification**
and an **exclusive out-dir lock**.

### 2.1 Flags

- **`--resume`** — retained. No longer "opt in to resume" (that is now default); it means
  *resume without prompting* (authoritative; also the scripted "assume yes" path). Keeping
  it avoids breaking existing invocations.
- **`--no-resume`** — new. *Start fresh without prompting*: discard the checkpoint, scrub
  staging, process all files. The symmetric negation of `--resume`.
- No `--force` (conventionally means "override a safety refusal", not "pick a run mode"), no
  `--ci`, no `--yes` (auto-detection + the explicit pair cover those).
- Explicit flags are **authoritative** — they override both the prompt and auto-detection.
  `--resume` and `--no-resume` together is an argparse error (mutually exclusive group).

### 2.2 Interactivity detection (standard TUI convention)

A run is **interactive** iff **both** `sys.stdin.isatty()` and `sys.stderr.isatty()` are
true **and** neither `CI` nor `NONINTERACTIVE` is set to a truthy value. Rationale:

- The prompt's *answer is read from stdin*, so stdin must be a TTY — checking only the
  output stream (the original draft checked stderr) would prompt into the void on
  `clean </dev/null` and auto-resume silently on `clean 2>log`.
- `CI`/`NONINTERACTIVE` are honoured because CI runners frequently allocate a pseudo-TTY
  (GitHub Actions, `ssh -t`); TTY-detection alone would hang such jobs forever on the
  prompt. This is the single most common "my pipeline hung" failure and the convention
  exists to prevent it.

### 2.3 Behaviour matrix

Evaluated at `clean` startup, after discovering inputs and locating the checkpoint in
`--out-dir`. "Valid" = `validate_resumable` passes (same lintle version, identical input
set + fingerprints). "Stale" = it fails (returns a reason). "Corrupt" = present but
unparseable.

| Checkpoint | Flag | Interactive | Action |
| --- | --- | --- | --- |
| absent | — | — | **Fresh run** |
| corrupt | — | — | **Error, exit 2** — "checkpoint unreadable; pass `--no-resume` to start fresh". Never silently fresh-run (a corrupt checkpoint may be a recoverable interrupted run). |
| valid | `--resume` | — | **Resume** (no prompt) |
| valid | `--no-resume` | — | **Fresh** (no prompt) |
| valid | none | yes | **Prompt:** `Resume interrupted run (12/29 files done)? [Y/n]` — `Y`/Enter → resume, `n` → fresh |
| valid | none | no | **Auto-resume**, with a mandatory stderr line (§2.5) |
| stale | `--resume` | — | **Error, exit 2** — `--resume` cannot force a stale resume; print the reason |
| stale | `--no-resume` | — | **Fresh** (no prompt) |
| stale | none | yes | **Prompt:** `Can't resume (<reason>). Reprocess all 29 files from scratch? [y/N]` — `y` → fresh, `n`/Enter → **abort, exit 1**, no changes |
| stale | none | no | **Error, exit 2** — print the reason + "pass `--no-resume` to start fresh" |

Key safety properties from the adversarial review:

- **Stale never silently discards work.** A stale checkpoint (notably a cosmetic
  `__version__` bump) does not trigger a silent hour-long restart. Interactive → prompt
  defaulting to **No** (the conservative direction). Non-interactive → fail with guidance.
- **The stale checkpoint is never deleted by the refusal/abort path** — the operator can
  downgrade lintle or revert an input and recover the interrupted run. It is overwritten
  only when a fresh run is actually chosen.
- **Corrupt ≠ absent.** Preserved from the current opt-in code.

### 2.4 Prompt mechanics

- Default on Enter: resume-prompt → **Yes (resume)**; stale-prompt → **No (abort)**.
- Capitalised default letter (`[Y/n]` / `[y/N]`), standard convention.
- Unrecognised input → re-prompt, capped at 3 attempts, then **abort (exit 1)** — never
  silently take the default on garbage.
- **EOF / Ctrl-D** mid-prompt → **abort (exit 130)**; never take the Yes default on EOF.
- **Ctrl-C** at the prompt → exit 130, checkpoint left untouched, no traceback.

### 2.5 Visibility — resume is never silent

- **Header before dispatch** (stderr, every resume — prompted, flagged, or CI):
  `resuming: 12/29 files already complete, processing 17 — pass --no-resume for a fresh run`.
- **Progress block** (issue #53): seed the overall total at the full file count with the
  completed ones pre-counted, so the skipped files are visibly *done*, not *missing*.
- **Run report** distinguishes reused-from-checkpoint vs processed-this-run counts.

### 2.6 Ctrl-C message

Replaces the bare `interrupted — workers stopped`:

```
interrupted — workers stopped (12/29 files done).
Re-run the same command (same --out-dir) to continue where it stopped; inputs must be unchanged.
Pass --no-resume to start over.
```

Counts come from the checkpoint just written. The message is conditional ("if inputs
unchanged") so it does not over-promise a resume that refuse-on-change might decline.

## 3. Safety nets exposed by default-on resume

These close correctness gaps that were tolerable as opt-in operator gambles but become the
routine path once resume is the default.

### 3.1 Output-existence re-verification

Before trusting a `completed` entry on resume, `os.stat` its expected outputs
(`cleaned/<stem>.cleaned.txt` and, when present, `broken/<stem>.broken.txt`). If an
expected output is missing, **drop the file from `completed` and reprocess it**. This
guards the case where the checkpoint records a file as done but its output was later
deleted/truncated out-of-band — otherwise resume would claim-complete a corpus with
missing output (a "correctness over recovery" breach). Cheap: one or two `stat`s per
already-done file, no re-read.

### 3.2 Exclusive out-dir lock

Acquire an exclusive lock on `--out-dir` for the duration of a `clean` run
(`O_CREAT | O_EXCL` lockfile, e.g. `.clean.lock`, recording PID + ISO start time). If the
lock is held by a **live** PID, refuse to start (exit 2) with a clear message. If the
recorded PID is dead, treat the lock as stale and reclaim it. Released on exit (success,
failure, or interrupt). This prevents two concurrent `clean` runs sharing one `--out-dir`
from corrupting the checkpoint (last-writer-wins) and stomping each other's findings
shards — a race CLAUDE.md already warns about and that resume-by-default would otherwise
let the *next* run silently consume.

## 4. Structure — pure decision, thin I/O

A new pure function makes the §2.3 matrix testable without a real TTY, stdin, or lock:

```python
def resolve_resume_action(status, *, resume, no_resume, interactive, prompt):
    """Decide how to handle a checkpoint at clean startup.

    `status` is one of ABSENT / CORRUPT / VALID / STALE(reason).
    `resume`/`no_resume` are the explicit flags; `interactive` is the
    detected mode; `prompt` is an injected callable (default-y/n -> bool)
    used only when a decision needs the operator. Returns an Action:
    FRESH, RESUME, or ABORT(message, exit_code).
    """
```

`main()`:
1. Acquire the out-dir lock (§3.2).
2. Discover inputs; fingerprint them; classify the checkpoint (`ABSENT/CORRUPT/VALID/STALE`).
3. Call `resolve_resume_action(...)`; map the Action onto the existing resume path
   (skip-completed, seed `reused_stats`) or fresh path (delete checkpoint + scrub
   `.shards`), or print + exit for ABORT.
4. On the resume path, apply output-existence re-verification (§3.1) and emit the
   visibility header (§2.5).

The prompt itself is a thin `input()` wrapper, gated by §2.2, injected so tests pass a fake.
This replaces the current `if args.resume / else` branch in `main()` with one call plus the
decision function. `resolve_resume_action` joins `resolve_jobs` and `validate_resumable` as
pure, table-tested logic.

## 5. Testing

- `resolve_resume_action`: a truth table over the §2.3 matrix (every row), with a fake
  `prompt` for the interactive cases — no real TTY needed.
- Interactivity detection: stdin/stderr `isatty` × `CI`/`NONINTERACTIVE` combinations.
- Prompt mechanics: Enter-default, garbage→re-prompt→abort, EOF→exit 130.
- Output re-verification: checkpoint says done but output missing → file reprocessed.
- Out-dir lock: second run refuses while first holds; stale (dead-PID) lock reclaimed.
- Ctrl-C message: asserts counts + the new wording; exit 130.
- End-to-end: clean a small fixture, interrupt, resume → skips completed, finishes the rest.

## 6. Scope and non-goals

**In scope:** the §2 behaviour, §3 safety nets, §2.6 message, for `lintle clean` only.

**Out of scope / future:**

- **`validate` is unchanged** — read-only, writes nothing, not resumable. (A separate open
  question — "does `validate` earn its keep at all, given it costs ~the same as `clean` but
  produces no output?" — is noted but not decided here.)
- **Fingerprint interior-blindness** (documented limitation): the input fingerprint is
  size + `mtime_ns` + SHA-256 of the first/last 64 KB — it cannot detect an interior edit
  that preserves size and restores `mtime_ns` (e.g. `touch -r`, `dd conv=notrunc`). On
  virtually all filesystems any rewrite changes `mtime_ns`, so the residual risk is narrow;
  §3.1 is a partial net. A full-file hash is rejected — it would defeat instant resume and
  re-read 30 GB. Called out so the trade-off is conscious.
- **Orphaned outputs on a changed input set**: a fresh run over a prior run whose input set
  differed leaves stale `cleaned/`/`broken/` files for now-removed inputs (pre-existing).
  Optionally addressed later by scrubbing the output trees on an explicit fresh run.
- **Validation-semantics version** (vs package version) to avoid invalidating a checkpoint
  on doc-only bumps — future refinement.

## 7. Known limitations

Carried forward and documented in the user-facing docs: resume is `--out-dir`-scoped (a
different/forgotten `--out-dir` silently forks a second output tree — the Ctrl-C message
emphasises "same `--out-dir`"); and the fingerprint interior-blindness of §6.
