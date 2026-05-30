# `clean` resume-by-default + cancel/resume UX — Design

- **Date:** 2026-05-30
- **Status:** Designed; ready for implementation.
- **Revision:**
  - **rev 2 (2026-05-30)** — hardened by a multi-AI debate (Gemini + Codex adversarial
    reviews against Claude's three-lens review). Adopted: output **integrity** checks (not
    mere existence), **flock + host/boot-id-aware** lock (not bare PID), **SIGTERM/SIGHUP**
    handling, a **true-fresh** output scrub, an explicit **run identity**, a stronger
    **ctime+inode** fingerprint, a **completed flag×checkpoint matrix**, and a **conventional
    exit-code scheme**. Stale stays **all-or-nothing** (rejected Gemini's partial-reconcile
    blocker — preserves issue-#56 scope).
  - rev 1 (2026-05-30) — initial design, hardened by a three-lens Claude review.
- **Topic:** Make resume the **default** behaviour of `lintle clean` (today opt-in via
  `--resume`), with a clear cancel→resume cycle: Ctrl-C/SIGTERM stops promptly and prints
  how to continue; re-running picks up where it stopped. Interactivity follows standard TUI
  conventions. Resume stays `clean`-only — `validate` is unchanged.

## 1. Problem

`lintle clean` is a ~1-hour batch over ~30 GB. It is interruptible and already has a durable
per-run checkpoint (`resume.py`, issue #56): `.clean-state.json` in `--out-dir` records each
completed file plus input fingerprints, and is deleted on full success — so its *presence*
marks an interrupted run. But resuming requires the operator to know about and pass
`--resume`, and the cancel path gives no hint resuming is possible:

1. **Resume is undiscoverable.** Ctrl-C prints only `interrupted — workers stopped`; a user
   who doesn't know `--resume` exists repeats up to an hour of work.
2. **Opt-in is the wrong default after an interruption** — the common intent on re-run is
   "continue", not "start over".

The goal: cancelling and resuming should be the obvious default cycle, while keeping the
correctness guarantees (refuse-on-change, atomic writes) and *not* introducing the silent
footguns that a default-on resume invites — chiefly, never emitting mixed or stale output
(Critical Rule #2: correctness over recovery).

## 2. Decision summary

Resume becomes the default for `clean`. Resume-vs-fresh is driven, in priority order, by: an
explicit flag → else a TTY prompt → else a deterministic non-interactive default. A stale or
corrupt checkpoint never silently discards work. The default-on change is made safe by four
hardening measures the debate established as load-bearing: **run-identity validation**,
**output-integrity re-verification**, an **exclusive host-aware lock**, and **true-fresh
output semantics**.

### 2.1 Flags

- **`--resume`** — retained. Means *resume without prompting* (authoritative; also the
  scripted "assume yes" path). Keeping it avoids breaking existing invocations.
- **`--no-resume`** — new. *Start fresh without prompting*: archive the checkpoint, scrub
  staging **and the output trees** (§3.4), process all files.
- No `--force` (overloaded), `--ci`, or `--yes` — auto-detection + the explicit pair cover them.
- `--resume` and `--no-resume` are a mutually-exclusive argparse group.

### 2.2 Interactivity detection (standard TUI convention)

A run is **interactive** iff `sys.stdin.isatty()` is true **and** neither `CI` nor
`NONINTERACTIVE` is set truthy. The **prompt decision keys on stdin** (the answer is read
there); `stderr.isatty()` governs only colour/styling. Honouring `CI`/`NONINTERACTIVE`
prevents the classic hang where a CI runner allocates a pseudo-TTY and blocks forever on the
prompt.

### 2.3 Behaviour matrix (complete: flag × checkpoint state)

Evaluated at startup, after acquiring the lock (§3.3), discovering inputs, and classifying
the checkpoint. "Valid" = `validate_run_identity` passes (§3.1). "Stale" = it fails with a
reason. "Corrupt" = present but unparseable. Exit codes per §2.7.

| Checkpoint | none / prompt | `--resume` | `--no-resume` |
| --- | --- | --- | --- |
| **absent** | Fresh run | **Error (1)** — "nothing to resume" (honours explicit intent) | Fresh run |
| **corrupt** | Error (1) — "checkpoint unreadable; pass `--no-resume`"; keep file | **Error (1)** — unreadable; keep file | **Archive + fresh** (§3.4) |
| **valid** | interactive → **prompt** `Resume interrupted run (12/29 done)? [Y/n]`; non-interactive → **auto-resume + loud line** (§2.5) | **Resume** | **Fresh** (§3.4) |
| **stale** | interactive → **prompt** `Can't resume (<reason>). Reprocess all 29 from scratch? [y/N]` (y→fresh, n/Enter→**abort 1**); non-interactive → **Error (1)** + reason + recovery command | **Error (1)** — `--resume` cannot force a stale resume; print reason | **Fresh** (§3.4) |

Safety properties:

- **Stale never silently discards work** (notably a cosmetic version/identity drift):
  interactive prompts default **No**; non-interactive errors with the exact recovery command.
- **Stale/corrupt checkpoints are never *deleted* by a refusal** — only **archived**
  (renamed to `.clean-state.json.stale-<ts>`) when a fresh run is actually chosen, so the
  operator can downgrade/revert and recover. Corrupt ≠ absent throughout.

### 2.4 Prompt mechanics

- Enter → default: resume-prompt → **Yes**; stale-prompt → **No (abort)**. Capitalised
  default letter, standard convention.
- Unrecognised input → re-prompt, capped at 3, then **abort (1)** — never silently default.
- **EOF / Ctrl-D** mid-prompt → **abort (1)** (an operational refusal, *not* a signal —
  so not 130); never take the Yes default on EOF.
- **Ctrl-C** at the prompt → exit **130**, checkpoint untouched, no traceback.

### 2.5 Visibility — resume is never silent

- **Header before dispatch** (stderr, every resume): `resuming: 12/29 files already
  complete, processing 17 — pass --no-resume for a fresh run`.
- **Progress block** (issue #53): seed the overall total at the full file count with
  completed ones pre-counted, so skipped files read as *done*, not *missing*.
- **Run report** distinguishes reused-from-checkpoint vs processed-this-run counts.

### 2.6 Cancel message (SIGINT/SIGTERM)

Replaces the bare `interrupted — workers stopped`, emitted on the graceful-stop path (§3.2):

```
interrupted — workers stopped (12/29 files done).
Re-run the same command (same --out-dir) to continue where it stopped; inputs must be unchanged.
Pass --no-resume to start over.
```

Counts come from the checkpoint just written; conditional ("if inputs unchanged") so it does
not over-promise a resume that identity validation might decline.

### 2.7 Exit-code scheme (conventional)

One consistent scheme, fixing rev 1's inconsistencies (EOF was wrongly 130; stale differed
by mode):

- **0** — success.
- **1** — operational refusal / failure: stale, corrupt, lock held, declined prompt, EOF at
  prompt, or a file that failed to process. (One code, mode-independent.)
- **2** — CLI usage/syntax errors only (argparse).
- **130** — terminated by **SIGINT** (128+2), only.
- **143** — terminated by **SIGTERM** (128+15).

## 3. Hardening (what makes default-on resume safe)

### 3.1 Run identity (not just version + inputs)

`validate_resumable` is replaced by `validate_run_identity`, which pins a canonical identity
and refuses on any drift (all-or-nothing):

- **Cleaner identity:** lintle version + checkpoint schema version.
- **Output-affecting configuration:** the normalized set of CLI/config/env that can change
  output content (today effectively none beyond inputs/version, but pinned explicitly so a
  future output-affecting flag can't validate-through and mix policies).
- **Input set + per-input fingerprint** (§3.5).
- **Input→output mapping:** each completed input's expected output paths.

### 3.2 Signal handling (SIGINT, SIGTERM, SIGHUP)

The **parent** owns signal handling for SIGINT, SIGTERM, and SIGHUP; workers ignore them
(they are terminated by the parent). On any of the three: stop accepting new files, terminate
workers, ensure the checkpoint reflects all durably-completed files, print the §2.6 message,
and exit `128+signo` (130/143/129). The checkpoint is written incrementally as each file
completes (not at exit), so an un-trapped kill still preserves completed work; trapping
SIGTERM/SIGHUP additionally gives clean teardown and the helpful message in the
scheduler/preemption case that resume-by-default is partly *for*.

### 3.3 Exclusive, host-aware out-dir lock

Hold an exclusive OS lock for the run: an `fcntl.flock` (or `O_CREAT|O_EXCL`) on
`<out-dir>/.clean.lock`, whose contents record **hostname + boot-id (where available) + PID +
ISO start time**. Refuse to start (exit 1) if held. Reclaim a stale lock **only** when it is
same-host *and* its PID is dead — never reclaim across hosts (guards the NAS/NFS multi-node
case). Documented assumption: robust mutual exclusion requires a POSIX-lock-capable
filesystem; on exotic network filesystems the lock degrades to advisory and the operator owns
not running two writers. Released on every exit path (success, failure, signal).

### 3.4 True-fresh output semantics

A **fresh run** (explicit `--no-resume`, the stale/corrupt→fresh paths, or a declined resume)
scrubs the output trees to a clean slate before processing: remove `cleaned/`, `broken/`, and
`.shards/` under `--out-dir`, then archive any checkpoint and recreate the trees. This
guarantees a fresh run never leaves **orphaned outputs** from a prior, differently-scoped
input set for downstream consumers to mis-read. (Resume, by contrast, leaves completed
outputs in place by design.) The scrub is ordered checkpoint-archive-last so an interrupted
scrub is re-detected as a fresh run, not a resumable one.

### 3.5 Stronger fingerprint (still O(1))

`input_fingerprint` gains **`st_ctime_ns` and inode** alongside size, `mtime_ns`, and the
first/last-64 KB SHA-256. ctime catches metadata-preserving copies/restores
(`cp -p`/`rsync -t`/`touch -r`) that leave mtime intact; inode catches replace-by-rename.
Still constant-time — the interior is never read. **Documented residual risk:** a same-size
interior edit that also preserves ctime+mtime+inode (rare; e.g. `dd conv=notrunc` plus a
metadata reset) is not detected; §3.6 is the backstop, and a stronger mode is a noted future
option. The fingerprint is performance-biased by deliberate choice (constant-memory rule).

### 3.6 Output-integrity re-verification (not mere existence)

Before trusting a `completed` entry on resume, verify its outputs are **complete**, not just
present: the cleaned file exists **and** its size/line-count is consistent with the recorded
`clean_count` (the checkpoint already stores `clean_count`/`quarantined_count` via
`summary_dict`), and the broken sidecar matches `quarantined_count`. A missing, empty, or
size-mismatched output (truncated by SIGKILL/disk-full) → **drop the entry and reprocess that
file**. Guards against `os.stat`-passes-a-truncated-file silent corpus corruption.

## 4. Structure — pure decision, thin I/O

A pure function makes the §2.3 matrix testable without a real TTY, stdin, lock, or FS:

```python
def resolve_resume_action(status, *, resume, no_resume, interactive, prompt):
    """status ∈ ABSENT/CORRUPT/VALID/STALE(reason); resume/no_resume are the
    explicit flags; interactive is the detected mode; prompt is an injected
    y/n callable used only when a decision needs the operator. Returns an
    Action: FRESH, RESUME, or ABORT(message, exit_code)."""
```

`main()`: acquire lock (§3.3) → discover + fingerprint inputs → classify checkpoint
(run-identity, §3.1) → `resolve_resume_action(...)` → map to resume path (re-verify outputs
§3.6, skip-completed, seed `reused_stats`, emit §2.5 header) / fresh path (scrub §3.4) /
ABORT (print + exit). The prompt is a thin `input()` wrapper gated by §2.2, injected for
tests. `resolve_resume_action` joins `resolve_jobs`/`validate_run_identity` as pure,
table-tested logic.

## 5. Testing

- `resolve_resume_action`: full truth table over §2.3 (every flag×state cell), fake prompt.
- Interactivity: stdin/stderr `isatty` × `CI`/`NONINTERACTIVE`.
- Prompt: Enter-default, garbage→re-prompt→abort(1), EOF→abort(1), Ctrl-C→130.
- Run identity: version drift, output-affecting-arg drift, input add/remove/change all refuse.
- Output integrity: done-but-missing and done-but-truncated → reprocessed.
- Lock: second same-host run refused; dead-PID same-host reclaimed; cross-host never reclaimed.
- Signals: SIGINT→130, SIGTERM→143, both print the message and flush the checkpoint.
- True-fresh: fresh run removes orphaned outputs from a prior differing input set.
- Fingerprint: ctime/inode changes detected; documented interior-edit gap asserted as known.
- Exit codes: each row of §2.7.
- End-to-end: clean a small fixture, interrupt, resume → skips completed, finishes the rest.

## 6. Scope and non-goals

**In scope:** §2 behaviour, §3 hardening, §2.6 message — for `lintle clean` only.

**Out of scope / future:**

- **`validate` unchanged** — read-only, not resumable. (Separate open question: does
  `validate` earn its keep at all? Noted, not decided here.)
- **Stronger-than-O(1) fingerprint** (sampled-interior or full content hash, opt-in
  `--verify-resume`) — future, if the §3.5 residual ever bites in practice.
- **Validation-semantics version** distinct from package version (so doc-only bumps don't
  invalidate) — future refinement of §3.1.
- **Partial reconciliation** (re-queue only changed inputs, keep the rest) — explicitly
  rejected for this design: it would turn the checkpoint into the cross-run skip-cache that
  issue #56 deliberately declined (the rejected §13 manifest), and is unsafe for the
  version-drift case. Revisit only as its own spec.

## 7. Known limitations (documented for users)

- Resume is `--out-dir`-scoped; a different/forgotten `--out-dir` forks a second output tree
  (the §2.6 message stresses "same `--out-dir`").
- The §3.5 fingerprint cannot detect an interior edit that also preserves ctime+mtime+inode;
  §3.6 is the backstop.
- The §3.3 lock assumes a POSIX-lock-capable filesystem for hard mutual exclusion.
