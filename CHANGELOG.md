# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `lintle dedup` — emit a de-duplicated "latest re-issue only" import list from a
  clean run's cleaned output. Space-track republishes the same orbit at the same
  epoch with only a bumped element-set (or revolution) number; the faithful
  `cleaned/` archive keeps every copy, so `dedup` writes a separate
  `<out-dir>/dedup/import.txt` with one card per `(catalog, epoch)`, keeping the
  latest re-issue (highest element-set number). Benign re-issues — identical
  parsed orbital state — collapse silently; a genuine same-epoch orbit
  contradiction is kept-latest **and** flagged in `notes.jsonl` (exit 1), never
  resolved in silence. When a `verify` run's `suspects.jsonl` is present, hard
  suspects are excluded from the import list first. `cleaned/` is never modified;
  constant memory (records stream through the same external sort as `verify`,
  one `(catalog, epoch)` group held at a time); byte-deterministic output. Shares
  `verify`'s `orbital_state` / `element_set` so both agree on "same orbit" and
  "which is latest".
- `lintle verify` — post-run correctness auditing of a clean run's output
  (Increment 1, `sgp4`-free): re-validates every cleaned record against the one
  validator, flags any `(catalog, epoch, element-set)` contradiction — two
  records that share a satellite, epoch, *and* element-set number yet carry a
  different orbital state — and, when the source tree is available, confirms
  every cleaned line is a *sanctioned* edit of a real source line (no interior
  mutation) via a bounded-window source alignment. The contradiction check
  compares parsed orbital *values*, not raw bytes, so the many valid ASCII
  encodings space-track emits for one number never false-positive; benign
  same-epoch re-issues (a new element-set number) are counted in a summary
  census rather than flagged. Writes a deterministic suspects report under
  `<out-dir>/verify`; exit 1 on any hard suspect. Constant-memory: the
  group-by-satellite pass uses an external merge sort that spills to disk. The
  clean/validate/repair path is barred from importing `lintle.verify` (or
  `sgp4`) by an import-graph test.
- Interactive wizard: running `lintle` with no subcommand on a TTY opens a rich
  menu to configure paths and start a clean / verify / report. The chosen source
  and output directories are remembered in a project-local `./.lintle.json`
  (`lintle.config`, stdlib JSON), so `lintle clean` / `verify` / `report` run
  without repeating paths — precedence is always explicit CLI arg > stored
  config > built-in default, and stored paths are re-checked (and re-prompted)
  when they no longer exist. Off a TTY (scripts, CI, pipes) a bare `lintle`
  keeps the old behaviour: it prints help and exits 2, never blocking on a
  prompt.

## [0.6.0] - 2026-07-04

### Added

- Failed input files are now recorded in the run envelope (issue #83). When a worker
  raises, `run.failed_files` carries a `[{"file": basename, "error": str}]` list
  (sorted, always present — `[]` on a clean run) and `summary.failed_count` mirrors
  its length. `report.md` gains a `## Failures` table when any file failed (omitted on
  a clean run). Exit code 2 is unchanged for this case. Schema version bumped
  `"2"` → `"3"` because both new fields are required (not additive-optional).
- `clean --reconstruct-checksum` opts in to tier-2 missing-checksum reconstruction.

### Changed

- **`#120`/`#106` — the validator now returns a typed `tle.FieldError` instead of
  a bare error string.** `FieldError` subclasses `str` (so every consumer that
  treats an error as text — substring tests, `"; ".join(...)`, f-string interpolation
  — keeps working byte-for-byte) while carrying structured fields: `kind`
  (`"length"`/`"column"`/`"semantic"`/`"checksum"`/`"catalog"`), a 1-indexed
  inclusive `column_range`, and `observed`/`expected`. `repair` now routes on
  `FieldError.kind` rather than grepping the prose for `"checksum"` (the brittle
  contract #106 pinned as a tripwire), and populates `report.jsonl`'s
  `column_range`/`observed`/`expected` for **column, semantic, and catalog**
  findings — previously they were filled only for checksum mismatches. The
  `report.jsonl` line schema stays `"1"`: the field set and types are unchanged;
  only previously-`null` optional values are now filled in. Human-facing output
  (`report.md`, the `.broken.txt` sidecar, the `note` field) is byte-identical —
  pinned by the sgp4 oracle and the full existing suite.
- **Missing-checksum reconstruction is now opt-in (default off).** A checksumless 68-char
  line is quarantined by default rather than having a recomputed checksum appended: a dropped
  trailing *data* character is indistinguishable from a dropped checksum, so reconstructing it
  by default could silently emit wrong-but-valid data (Critical Rule #2, issue #82). Pass
  `--reconstruct-checksum` to restore the recompute. The flag is part of the resume
  run-identity, so changing it forces a re-run rather than folding mismatched outputs.

### Fixed

- **`#87` / `#99` — the out-dir lock had a TOCTOU reclaim race, a blind release, a
  post-reboot wedge, and a PID-reuse hostage.** The hand-rolled pidfile read the
  holder's PID, checked liveness with `os.kill(pid, 0)`, and `unlink`+retried to
  reclaim a dead lock — none of which re-verified the file it was deleting, so two
  runs could both reclaim and proceed (`#87`, P0, reproduced), and a run whose lock
  was raced away blind-unlinked the *current* holder's lock on exit. Identity also
  embedded Linux `boot_id`, so a crash-then-reboot left an unreclaimable "different
  host" lock (`#99`), and a recycled PID kept a dead lock alive. Replaced the whole
  scheme with an advisory **`fcntl.flock`** held for the run: the kernel releases it
  the instant the holder closes its fd, exits, is killed, or the host reboots, so
  liveness needs no PID check or boot-id and there is no reclaim step to race.
  Release is the bare `os.close` of *our own* fd — a run can only ever drop its own
  lock, never a successor's. The `.clean.lock` file is deliberately never unlinked
  (`flock` binds to the inode; unlinking would let a racing opener lock an orphaned
  inode), and now records `{host, pid, started}` only as informational text for the
  `LockHeldError` message, which names the file and the manual-removal escape hatch.
  POSIX-only; Windows is out of scope (use WSL). A shared out-dir across hosts over a
  network FS is documented as untested (relies on server-side `flock` propagation).
- **`#95` — a newline-free or CR-only multi-GB file was materialised as one giant `bytes`
  object, violating constant-memory (Critical Rule #3).** `iter_records` previously iterated
  over the binary handle with `for raw in handle`, which splits only on `\n`; a file with no
  `\n` (or only `\r` terminators) buffered the entire file as one `raw` chunk — a 3.2 GB file
  would load whole, OOM the worker, and then be pickled across the pool boundary. Fixed by
  replacing the iterator with `handle.readline(_MAX_LINE_BYTES)` (C-level, throughput
  unchanged for normal lines). A chunk of exactly `_MAX_LINE_BYTES = 4096` with no trailing
  `\n` is the start of an oversized line: the excerpt is kept as a bounded quarantine payload,
  the remainder is drained in fixed-size chunks (bytes still counted into `bytes_consumed`),
  and one `Orphan` with `RuleID.LINE_LENGTH` is emitted for the logical line. The raw bytes in
  the quarantine entry are noted as truncated — the one place byte-faithfulness yields to
  constant-memory, and only for a pathological input. Normal lines (the entire real corpus) are
  processed byte-identically. `stats.bytes_consumed` still reaches `st_size` at EOF, and
  `input_lines_seen` counts each logical line exactly once.
- **`#104` — `QuarantineSink.__enter__` was not exception-safe; `cleaned_handle` was opened
  outside the sink's `with` block.** `QuarantineSink.__enter__` entered its `BrokenFileWriter`
  and then its `JsonlFindingsWriter` sequentially: if the jsonl writer's `open` failed (disk
  full, unwritable `.shards`), the already-entered `BrokenFileWriter.__exit__` never ran,
  leaving a leaked body handle and `.broken.txt.body.partial` debris. In `pipeline._run`,
  `cleaned_handle = open(...)` happened before `with sink:` so a `sink.__enter__` failure
  leaked the cleaned `.partial`. Fixed with two changes: (1) `QuarantineSink.__enter__` now
  uses a `contextlib.ExitStack` — each sub-writer is entered onto the stack; on success
  `stack.pop_all()` transfers ownership to `self._stack` (closed by `__exit__`); a mid-enter
  failure unwinds already-entered writers via the stack's own cleanup. (2) In `pipeline._run`,
  `cleaned_handle = open(...)` is now opened inside the `with sink:` block (before the inner
  try/finally) so a `sink.__enter__` failure cannot leak it — the handle simply doesn't exist
  at that point.
- **`#101a` — broken sidecar excluded from resume integrity check when no records quarantined.**
  `resume.output_sizes` previously recorded the `.broken.txt` sidecar only when
  `stats.quarantined_count > 0`, but `pipeline` always writes a header-only sidecar even
  for a clean file. A file whose sidecar was deleted or truncated would not be detected
  on resume. The sidecar is now recorded unconditionally.
- **`#101b` — output naming convention duplicated across modules.** Suffix and dirname
  strings (`.cleaned.txt`, `.broken.txt`, `.findings.jsonl`, `cleaned`, `broken`, `.shards`)
  were re-encoded independently in `pipeline._clean_output_paths`, `resume.output_sizes`,
  `cli.discover_paths`, and `report_writers.concat_findings_shards`. They now live as
  module-level constants (`CLEANED_SUFFIX`, `BROKEN_SUFFIX`, `FINDINGS_SUFFIX`,
  `CLEANED_DIRNAME`, `BROKEN_DIRNAME`, `SHARDS_DIRNAME`) in `lintle/__init__.py` — the
  single source of truth — and all consumers import from there.
- **`#117` — `concat_findings_shards` silently skipped a missing shard, causing `report.jsonl`
  to underreport vs `report.json` on resume.** On a resumed run, completed files' stats come
  from the checkpoint (not reprocessed), so their findings shards are not regenerated. If a
  shard was deleted out-of-band, `report.jsonl` would omit those findings while `report.json`
  counted them — a silent disagreement. Fixed with two defenses: (1) the findings shard is now
  recorded in `resume.output_sizes`, so a missing or truncated shard on resume triggers
  reprocessing — regenerating the shard and keeping `report.jsonl` complete; (2)
  `concat_findings_shards` now returns the list of source filenames whose shard was missing but
  had quarantined records so the caller (`output_artifacts`) can surface a `warning:` on stderr.
- **`#105` — stale-checkpoint archives accumulated unboundedly.** `archive_checkpoint` now
  prunes older archives after creating a new one, keeping only the newest 3
  (`_STALE_ARCHIVE_KEEP`). The ISO-8601 timestamp suffix is lexicographically sortable so the
  oldest entries are reliably identified and removed.
- **`#94` — disk-space guard charged the wrong amount.** The 2× guard now runs at the
  right moment in each branch. For a `--resume` run it charges 2× the *remaining*
  (unprocessed) input bytes — so a nearly-complete resume on a tight disk is no longer
  wrongly refused. For a fresh run it runs *after* `scrub_outputs` so the freed prior
  outputs are already reflected in the available space before the guard fires.
- **`#93` — `scrub_outputs` had no ownership check.** A fresh run on a mistyped
  `--out-dir` pointing at a directory with user content (e.g. a `cleaned/` subdirectory)
  could silently destroy it. The preflight now refuses (exit 2, no data destroyed) when
  the out-dir is non-empty and carries no lintle-ownership signal (`.lintle-output`
  marker, checkpoint, or stale-checkpoint archive). A `.lintle-output` marker is written
  on every first fresh run so subsequent runs and scrubs recognise the directory.
- **`#102` — `scrub_outputs` left prior-run report artifacts.** An interrupted fresh run
  could leave a stale `report.json` (from the prior run) that `lintle report` would then
  render as current. `scrub_outputs` now also removes `report.md`, `report.json`,
  `report.jsonl`, and `broken-noradids.ndjson` so the out-dir is truly clean before a
  new run's workers write fresh outputs.
- Records whose lines carry leading whitespace now pair and repair via the `leading-trim`
  fix class instead of being quarantined as `BAD_PREFIX`. `iter_records` matches the
  `1 `/`2 ` prefix on a whitespace-trimmed view while carrying the raw bytes forward to the
  repairer (issue #88).

### Performance

- **`#109` — every accepted record was validated twice.** `repair_record` called
  `tle.validate_record(line1, line2)` after both lines had already passed
  `repair_line`'s full `validate_line`. The only new information for two
  individually-valid lines is the catalog-number cross-check. A new
  `tle.validate_record_catalog(l1, l2)` helper performs only that check,
  returning the byte-identical error string; `repair_record` now calls it instead.
  `validate_record` is unchanged. Property tests confirm equivalence for all
  matching and mismatched valid pairs.
- **`#110A` — `compute_checksum` hot-path: per-char membership tests replaced with
  a precomputed lookup table.** The original loop called `ch in _DIGIT` then
  `int(ch)` for every character. A module-level `_CHECKSUM_CONTRIB` dict (ASCII
  digits `'0'`–`'9'` → their integer value, `'-'` → 1, absent = 0) reduces the
  loop body to `sum(_CHECKSUM_CONTRIB.get(c, 0) for c in line[:68]) % 10` — one
  dict lookup per character. Byte-equivalent by construction; the existing
  checksum property tests confirm invariance.
- **`#123` — pipeline allocation micro-optimisations.**
  (a) `slots=True` added to `RecordCandidate`, `Orphan`, `_ProgressBatcher`
  (pipeline.py) and `Accepted`, `Quarantined` (repair.py) — eliminates
  per-instance `__dict__` allocation on every record; slotted dataclasses
  pickle correctly across the worker pool.
  (b) `_record_acceptance` now writes both cleaned lines in a single
  `handle.write(line1 + "\n" + line2 + "\n")` call — byte-identical output,
  half the Python-level write calls on the accepted-record hot path.
  (c) `_ProgressBatcher.enabled` was a `@property` re-evaluated every call;
  replaced with a `_enabled: bool` field computed once in `__post_init__`.

## [0.5.0] - 2026-06-08

### Added

- A clean run now persists its run envelope as `report.json` in the output directory —
  byte-identical to the `--report json` stdout output — alongside `report.md`,
  `report.jsonl`, and `broken-noradids.ndjson`.
- A new read-only `lintle report [out-dir]` subcommand re-renders a prior clean run's
  aggregate summary from its `report.json` (text → panel on stdout; `--report json` →
  the file's bytes verbatim). A missing or unreadable `report.json` exits `2`.
- New runtime dependency: **`humanize>=4,<5`** — human-readable durations and sizes in
  the human display (panel duration via `precisedelta`, roster sizes via
  `naturalsize(gnu=True)`). Pure-Python, zero transitive deps; confined to `summary.py`
  and `cli_progress.py` (stderr/stdout panel only — structured output is unaffected).
- New dev dependencies: **`hypothesis>=6,<7`** (property-based tests for the validator
  and repair logic) and **`pytest-xdist>=3,<4`** (parallel test execution — the default
  suite now runs with `-n auto`).

### Changed

- `clean` now renders a terminal-width-responsive **aggregate summary panel** to stderr at
  the end of every run (replacing the per-file stdout summary dump); text-mode stdout is now
  empty, and the per-file detail lives in `report.md` / `report.json`.
- The `clean` summary panel now shows elapsed time in human-readable form (e.g. "2 minutes
  and 4 seconds" instead of raw seconds) and the pre-run roster shows file sizes in
  `gnu`-unit notation (e.g. "3.0G") via `humanize`. Fixes a roster unit bug where the old
  hand-rolled `_format_size` used binary (1024-based) division but decimal labels — so
  3 GiB rendered as "3.0 GB" (binary value, wrong "GB" label) rather than the correct
  "3.0G"; `naturalsize(gnu=True)` is now used consistently. Display-format change only —
  structured outputs carry raw numbers as before.

### Removed

- The `lintle validate` subcommand (read-only audit mode) has been removed from the CLI.
  Use `lintle clean`; its `report.md`, `report.jsonl`, and `--report json` envelope cover
  all audit needs that `validate` previously addressed. The validator definition (`tle.py`)
  and the streaming pipeline are unchanged — this was a CLI-surface removal only.

## [0.4.1] - 2026-05-31

### Added

- The `clean` live progress block now shows, per in-flight file, a **byte
  throughput** (`rich.progress.TransferSpeedColumn`) and a **time-remaining ETA**
  (`TimeRemainingColumn`) — derived from the per-file byte total already supplied,
  so a multi-hour 30 GB run shows real per-file speed and ETA. The overall row
  gains a **files-done/total counter** (`MofNCompleteColumn`). These columns are
  gated by task kind (a small `_ForKind` wrapper) so the byte columns never render
  on the file-count overall row and the counter never renders raw bytes on a
  per-file row. TTY-only, additive UX — off a TTY the plain per-file summary lines
  are unchanged, and stdout / structured output are untouched.
- A **spinner** (`rich` status) now covers the otherwise-silent report
  finalization after the progress block exits — writing `report.md`,
  `broken-noradids.ndjson`, and concatenating the per-worker shards into
  `report.jsonl` (the slow part on a large corpus). TTY-only; a no-op context off
  a TTY, so piped/structured output is unaffected.

### Changed

- Upgraded the `rich` runtime dependency from the 13.x series to **15.x**
  (`rich>=15,<16`). No behavioural change — the stderr-only progress UI, roster,
  and `error:`/`warning:` rendering are unchanged (verified by the byte-exact
  `term` tests and the progress/roster suite); stdout and structured outputs
  never touched `rich`.
- **Dependency pinning policy:** every dependency (runtime and dev) is now pinned
  `>=current_major,<next_major` — minor/patch releases resolve automatically, but
  major upgrades are deliberate and manual, one at a time. Caps added to the dev
  group (`pytest<10`, `pytest-cov<8`, `ruff<0.16`, `sgp4<3`). See `ARCHITECTURE.md` §7.

### Fixed

- The `clean` cancel message no longer claims it will "continue where it stopped".
  Resume granularity is a whole file: re-running skips fully-completed files and
  restarts the file interrupted mid-stream, so a single-file run that is cancelled
  starts over from the beginning. The message now says so, and drops the dangling
  `--no-resume` hint when nothing had completed (there is no checkpoint to ignore).

### Documentation

- **README restructured for newcomers/evaluators** — it now leads with the pitch
  and the common commands, with the deeper design rationale moved to
  `ARCHITECTURE.md`. Reorganised for faster onboarding; no content lost.
- README "Cancelling and resuming" and ARCHITECTURE §5 now state the per-file
  resume granularity (completed files skipped, in-progress file restarted) upfront,
  rather than leaving it to be inferred.

## [0.4.0] - 2026-05-31

### Added

- `clean` gains a redesigned live progress UI (issue #53): a one-shot
  **size-only roster** of the files to be processed (printed instantly from
  `os.stat` — no pre-read of the corpus), a **multi-file per-worker progress
  block** showing each active file's byte progress and running record count,
  and exact per-file counts at completion. The `--jobs` default is now
  *CPU count − 1, capped at the file count* (reserving a core during the long
  run; an explicit `--jobs` is still honoured as-is). This adopts **`rich`**
  (`>=13,<14`) as the first runtime dependency, clearing the four-bar policy
  (authoritative spec §3.1): it replaces ~150 lines of hand-rolled ANSI in
  `cli.py`, is the de-facto standard live-display library (`pip`, `uv`, `pdm`),
  is pure-Python with a small transitive surface (`markdown-it-py`, `pygments`),
  and is confined to terminal rendering in `cli.py`.

- `clean` now prints a **borderline disk-space warning** when free space on
  the `--out-dir` volume sits between the 2× input-size abort floor and a
  2.5× ceiling. The abort path is unchanged — exit `2` below 2×, message
  unchanged — but a run that previously fell silent above the floor now
  surfaces a `warning:` line on stderr (`free space in <out-dir> is close to
  the 2× safety guard: N bytes free of ~M recommended; the run will proceed
  but may exhaust the disk`) when free is in the 2×-to-2.5× band, so users
  know they are cutting it close before commits start exhausting the disk.
  Internal: `cli._check_disk_space` now returns a `(severity, message)`
  tuple — `"error"` (caller aborts) or `"warn"` (caller prints and
  proceeds) — or `None` when free is comfortably above the warn ceiling.

- `--max-quarantined` (on both `validate` and `clean`) now accepts a trailing
  `%` to express the exit-code threshold as a **rate** rather than an absolute
  count. `--max-quarantined 1%` exits non-zero if more than 1% of routed
  records (`clean_count + quarantined_count`) were quarantined; the integer
  form (`--max-quarantined 100`) is unchanged and the default `0` still means
  "any quarantine fails". The two modes are mutually exclusive by construction
  — a single value is either a count or a rate, never both — which sidesteps
  the combination semantics that a separate `--max-quarantined-pct` flag would
  have forced. Comparison is strictly greater (`100*q > p*r`,
  cross-multiplied to avoid divide-by-zero on an empty corpus and float drift
  at the boundary); `0%` ≡ `0` and `100%` effectively never trips. Design at
  `docs/superpowers/archive/specs/2026-05-27-max-quarantined-percentage-design.md`.

- Host-aware out-dir lock: refuses to start a second concurrent `clean` against
  the same `--out-dir`.

### Changed

- **Terminology unified on "quarantine".** The codebase and outputs used "reject"
  and "quarantine" interchangeably; everything now says **quarantine** (the act of
  setting a bad record aside). The stdout summary label `rejects:` is now
  `quarantined:`, and `lintle explain` calls a rule a "quarantine rule". Internals
  renamed to match (`QuarantineSink`, `QuarantineEntry`, `Quarantined`, etc.).
  **Breaking change** to two machine-readable surfaces:
  - `--report json`: the per-rule map `reject_counts` is renamed `quarantine_counts`
    (in both `summary` and `files[]`), and `schema_version` bumps **`"1"` → `"2"`**.
    Consumers keying on `schema_version == "1"` or `reject_counts` must update.
  - The `clean --resume` checkpoint `SCHEMA_VERSION` bumps **`2` → `3`**; a checkpoint
    written by an older `lintle` is refused and the run restarts fresh (the existing
    refuse-on-change behaviour — no data loss, the prior outputs are archived).

  The `report.jsonl` findings stream and `lintle diff` are **unaffected** (they never
  carried `reject_counts`; their `schema_version` stays `"1"`). Stable `RuleID` wire
  tokens (`TLE-CHK-001`, …) are unchanged.
- All CLI stderr messages now route through `rich`: `error:` lines render
  bold-red and `warning:` lines yellow on a terminal, while status, prompt, and
  cancel notices share the one stderr `Console`. Output is unchanged off a TTY
  (pipes, CI, redirects) — no ANSI, no wrapping — so machine-readable stderr and
  stdout/result data stay plain. Internally a new `term.py` leaf owns the shared
  Console and the `error`/`warning`/`note`/`prompt` emitters, so the styled
  prefix lives in one place (used by both `cli.py` and `diff.py`).
- `clean` now resumes by default after an interruption: re-run the same command
  (same `--out-dir`, unchanged inputs) to continue where it stopped. Interactive
  terminals prompt; CI/non-TTY auto-resumes with a notice. `--no-resume` starts
  fresh (clearing prior outputs); `--resume` resumes without prompting.
- Cancelling (`Ctrl-C`, or `SIGTERM`/`SIGHUP` from a scheduler) prints how to
  continue or start over.
- **Breaking change.** Minimum Python is now **3.14** (was 3.11). `requires-python`,
  `tool.ruff.target-version`, `.python-version`, and the trove classifiers all
  bumped together; drops 3.11 / 3.12 / 3.13 support. Aligns lintle with the
  drunik-org Python stack standard (drunik / lintle / descent-engine all on
  Python 3.14, `line-length = 88`, `target-version = "py314"`, ruff rule set
  `["E","F","I","UP","B","SIM"]`, `pytest-cov` in the dev group).
- Every output file `clean` commits — the `cleaned/` files, `.broken.txt`
  sidecars, findings shards, `report.jsonl`/`report.md`/`broken-noradids.ndjson`,
  and the `--resume` checkpoint — is now committed **durably**, not just
  atomically: a new `lintle.fsutil.durable_replace` helper `fsync`s the file's
  data, `os.replace`s it into place, then `fsync`s the containing directory, so
  a committed file survives a hard power loss or kernel panic rather than only a
  clean Ctrl-C / sleep / crash. On macOS the true power-loss barrier is
  `F_FULLFSYNC` (plain `fsync` does not flush the drive's write cache); `fsutil`
  uses it there and plain `os.fsync` on Linux/other platforms. This closes the
  gap that mattered most for `clean --resume` (#56), which trusts a
  previously-committed output without reprocessing it: the worker now makes its
  outputs durable before the parent records the file `completed`, so the
  checkpoint can never name a file whose bytes are not yet on disk. Durability
  is always-on (no flag) — measured at roughly 1 second of overhead across a
  full ~120-commit run on the 30 GB corpus. Closes #58.
- **Breaking change.** `lintle validate` and `lintle clean` now accept exactly
  one positional input — a single file *or* a single directory — instead of
  zero-or-more. The default remains `data/source`. Scripts invoking
  `lintle clean dirA dirB` (or multiple explicit files) will now fail at
  argparse with a usage error. Run the tool once per input directory
  (`for d in dirA dirB; do lintle clean "$d"; done`), or stage the inputs
  into a single directory first (e.g. `mkdir merged && cp dirA/* dirB/*
  merged/ && lintle clean merged`). This trims speculative flexibility
  the documented workflow never exercised: the per-file output names are
  derived from each input's basename alone, so multi-input runs needed a
  defensive collision check whose existence was the only reason multi-input
  was risky in the first place. With single-input, basenames within one
  directory are unique by filesystem guarantee, so the failure mode and its
  guard disappear together.

### Removed

- `cli._detect_basename_collisions` and its `TestDetectBasenameCollisions`
  tests — no callers after the single-input `validate`/`clean` change above.
- The realpath dedup loop inside `cli.discover_paths` (a single input has
  nothing to dedup against). `discover_paths` and `check_paths` now take a
  single path string rather than a list.

## [0.3.0] - 2026-05-27

### Added

- New `clean --resume` flag for **single-run resume**: continue an interrupted
  `clean` (Ctrl-C, a closed laptop, a crash) so it processes only the files not
  yet completed, rather than restarting the whole corpus. Checkpointing is
  always-on — the parent fingerprints every input up front and atomically
  rewrites a `.clean-state.json` in `--out-dir` after each file commits,
  deleting it on full success, so the checkpoint's presence marks an interrupted
  run and a finished run leaves none behind. `--resume` validates refuse-on-
  change: any drift in the `lintle` version or an input's identity (size,
  `mtime_ns`, head/tail 64 KB hash) aborts with a specific message (exit `2`)
  rather than mixing outputs from two states. Completed files' findings shards
  survive the interruption, so a resumed run's `report.jsonl`, `report.md`, and
  `broken-noradids.ndjson` match a non-interrupted full run. This is *not* a
  cross-run cache (the rejected design §13, #12) — it is scoped to finishing one
  run and never skips re-validation of records it emits. New `lintle.resume`
  module; `report.stats_from_summary` reconstructs a `FileStats` from its JSON
  summary so reused files appear in the final report. Closes #56.
- New `lintle explain <TAG>` subcommand turns the validator into its own
  reference: it documents **both** public vocabularies lintle stamps on a
  report — the rejection rules (`RuleID`, e.g. `TLE-CHK-001`) and the repair
  tags (`FixClass`, e.g. `reconstructed-checksum`). For any tag it prints a
  plain-English definition (single-sourced from `RuleSpec`/`FixSpec`, never
  re-described), a good/bad or before/after example with the failing column
  marked, the repair-tier linkage, and a source-of-truth citation into the
  code. Read-only; an unknown tag exits `2` listing every valid tag. Every
  example is the *same object* the test suite validates against the live
  `tle.py`/`repair.py` across all classification layers (line, pairing,
  record), so the docs cannot silently drift from validator behaviour;
  import-time guards make explain-coverage and tag-namespace disjointness
  structural. A new `FixSpec`/`FIXES` registry gives each repair tag a
  canonical one-line definition, mirroring `RuleSpec`/`RULES`. The
  `reconstructed-checksum` entry carries an explicit safety note (the only
  sanctioned reconstruction: a deterministic recompute, re-validated in full
  before commit — never a guessed data character). Closes #11.
- New `lintle diff RUN-A RUN-B` subcommand compares two clean-run output
  directories by streaming each one's `report.jsonl` and printing the defect
  classes new in B, the classes fixed (present in A, absent in B), the
  per-rule count deltas, and a per-file (per-basename) breakdown — turning
  "eyeball two `report.md` files" into a focused delta of what the upstream
  export pipeline broke, fixed, or shifted between runs. Read-only; writes
  nothing. Counts the *primary* `rule_id` of each finding only — never the
  `related[]` array — mirroring `pipeline._record_reject` so the diff's
  per-rule totals agree with each run's own `report.md`. The corpus-level
  totals are derived by summing the per-file counts, so the two sections can
  never disagree. A mismatched (or missing) `schema_version`, a malformed
  line, non-UTF-8 bytes, or a missing `report.jsonl` is a hard error (exit
  `2`); a clean comparison exits `0`. The per-file breakdown is keyed by the
  `report.jsonl` `file` basename: because `clean` refuses inputs with
  colliding basenames (`_detect_basename_collisions`), each basename names
  exactly one file within a run, so the key is unambiguous. A basename present
  in only one run is *flagged* ("only in run A/B — fixed, removed, or
  renamed") rather than attributed, and never rendered as a misleading
  `N -> 0`, since `report.jsonl` lists only files that had findings. Memory is
  bounded by (distinct files × distinct rule IDs), not the number of findings.
  Decision recorded in `debates/010-lintle-diff-implementation/`. Closes #10.
- New `--max-quarantined N` flag on both `validate` and `clean` (issue #13).
  Exit code stays `0` when the total quarantined record count is at or below
  `N`; flips to `1` only when *more than* `N` records were quarantined. The
  default `N=0` preserves the historical "any quarantine fails" contract, so
  the flag is purely opt-in for CI/DataOps callers that need a tolerance
  budget. Unlike `lintle ... || true; jq -e '.summary.quarantined_count <= N'`,
  the flag preserves the meaningful `2` (operational error) and `130` (Ctrl-C)
  exit codes that a swallow-and-parse wrapper would mask. The two other
  thresholds floated in the original issue (`--threshold RATIO` and
  `--fail-on RULE-ID=N`) were intentionally NOT shipped: `--threshold` is
  redundant with `--max-quarantined` and adds denominator ambiguity, and
  `--fail-on` would promote `RuleID` strings from "report artifact" to
  "CI YAML public-forever contract" — a meaningfully bigger compatibility
  promise that should wait on real user demand. Decision recorded in
  `debates/013-fail-on-threshold-flags/`.
- `lintle validate --report json` (and `lintle clean --report json`) now
  emits a top-level versioned envelope object instead of the prior flat
  array of per-file summaries. The shape is
  `{schema_version, run, environment, summary, files}` —
  `run` carries the subcommand name, the ISO 8601 UTC start timestamp,
  and the parent-process wall-clock `elapsed_seconds`; `environment`
  carries `tool_version` and `python_version` (no env vars, paths, or
  hostnames); `summary` carries corpus-wide aggregates
  (`files_processed`, `paired_records`, `clean_count`,
  `quarantined_count`, `fix_counts`, `reject_counts`); `files` is the
  per-file array, where each entry is the existing `summary_dict()`
  shape extended with `elapsed_seconds`, `bytes`, and
  `records_per_sec`. The throughput field is always a stable float
  (denominator clamped to 1 ms) — never `null` — so statically-typed
  consumers can declare a single type without sentinel handling. Per-
  file timing is captured by each worker via `time.monotonic()`;
  `summary` aggregates are NOT summed worker durations (`--jobs N`
  parallelism would inflate that), so `run.elapsed_seconds` is the
  authoritative end-to-end duration. The contract is locked by
  `docs/superpowers/archive/specs/2026-05-25-report-json-envelope.md` and the
  golden fixture at `tests/fixtures/report-envelope-v1.golden.json` (the envelope was
  later bumped to schema `"2"` and the fixture renamed `-v2`; see the Unreleased section).
  Closes #20.
- New `lintle.diagnostics` module defines a stable, citable rule-ID registry
  (`TLE-COL-001`, `TLE-CHK-001`, `TLE-PAIR-001`, …) and a structured
  `Diagnostic` dataclass with `rule_id`, `source_line_nos`, `tier_attempted`,
  `column_range`, `observed`, `expected`, and `note` fields. Reject reasons
  are no longer free-form prose — they are now structured records keyed by a
  stable identifier that downstream consumers can pin in `report.md`, the
  `.broken.txt` sidecar, JSON output, and future tooling. Rule IDs follow
  the `TLE-<FAMILY>-<NNN>` shape (families: COL, CHK, PAIR, SEM, INT) and
  are never recycled — retired IDs stay readable forever. Includes a
  `RuleSpec` registry (`RULES`) with metadata about every rule, queryable
  for future `lintle explain TLE-XXX-NNN` tooling. Closes #8.
- The `report.md` run report now includes a "Rule reference" section,
  auto-generated from the `diagnostics.RULES` registry, listing every rule
  that fired in the run with its short title so the report is
  self-explanatory.
- `report.md` now ends with a `## Per-NORAD breakdown` table: one row per
  satellite catalog number whose records were quarantined, with the
  corpus-wide quarantine count, the per-rule defect breakdown, and the
  source filenames the satellite appeared in. Rows are sorted by
  quarantined-record count descending (NORAD ID ascending on ties); the
  Files column shows the first five filenames alphabetically followed by a
  `+N more` suffix when the satellite spans more files than that, keeping
  the cell bounded for persistent NORADs. The table caps at
  `format_run_report(all_stats, top_n=100)` rows by default with an
  italicised "...and N more — see broken-noradids.ndjson for the full list."
  footer when truncation activates; pass `top_n=None` to render every row.
  The richer per-NORAD data is the human-facing counterpart to
  `broken-noradids.ndjson`, whose `{"noradId":N}` contract stays minimal.
  Closes #40.
- Per-rule drop visibility everywhere `lintle` surfaces reject totals.
  `FileSample` gains a `dropped_count: dict[RuleID, int]` field, populated
  by `RejectSink.add` when the per-rule bucket is at cap (the bound that
  in-memory exemplars are capped at — full byte-faithful catalog reaches
  `.broken.txt` regardless). The new data threads through three surfaces:
  the `lintle validate` summary's per-rule heading switches from `(M):`
  to `(N of M hits, K dropped):` when `K > 0`; the JSON output
  (`lintle validate --report json`) gains a `dropped_counts` field
  parallel to `reject_counts`, keyed by stable rule IDs; and
  `report.md`'s "Records quarantined (by rule)" table gains a `Dropped`
  column summed across files. The trailing `...and X more` under each
  rule block in the validate summary stays, so the existing
  truncation-indicator stays visually anchored to the exemplars it
  applies to. Closes #46.
- `lintle clean` now emits a corpus-wide `report.jsonl` alongside
  `report.md` and `broken-noradids.ndjson`: one JSON object per
  quarantined record, citing the stable `RuleID` (`TLE-CHK-001`,
  `TLE-COL-003`, …) and carrying the structured fields downstream
  automation needs — `file`, `source_lines`, `tier_attempted`,
  `norad_id`, `column_range`, `observed`, `expected`, `note`, and
  `related` (secondary diagnostics). Every line carries
  `schema_version: "1"` and `outcome: "quarantined"` (the latter
  reserves space for future `"fixed"` emission without breaking
  consumers). The format is compact (`json.dumps(..., separators=(",", ":"))`),
  key-sorted (`sort_keys=True`), UTF-8, LF-terminated — byte-deterministic
  across runs on identical input, enabling content-hash caching and the
  `lintle diff` consumer (issue #10). Streaming is per-worker: each
  worker writes `<out_dir>/.shards/<stem>.findings.jsonl`; the main
  process concatenates shards in alphabetical `src_name` order at end
  of run and removes the shard directory. A pre-run shard-dir scrub
  prevents contamination from prior aborted runs. The byte-faithful
  catalog stays in `broken/<stem>.broken.txt`; `report.jsonl` is the
  structured-findings stream consumers can `jq` against. The
  `RejectEntry` dataclass gains a trailing optional `norad_id` field
  decoded once at quarantine time. Closes #9.

### Changed

- **Breaking — `--report json` output shape.** The flat array of
  per-file `summary_dict()` entries previously emitted by
  `lintle ... --report json` is replaced by the top-level envelope
  described under Added above. Consumers that did `payload[0]` to read
  the first file's stats now do `payload["files"][0]`; the per-file
  keys (`src_name`, `paired_records`, …) are unchanged but join three
  new ones (`elapsed_seconds`, `bytes`, `records_per_sec`). No legacy
  flag is provided; the schema is pinned by `schema_version: "1"` so
  future minor revisions stay additive within `"1"` and any breaking
  rename bumps to `"2"`.
- Internal: extracted `RejectSink` and `FileSample` from `FileStats` so
  the 5-per-rule exemplar cap is enforced by construction rather than by
  convention in a single caller. `pipeline.process_file` no longer
  juggles a separate `broken_writer` and exemplar dict — `RejectSink`
  owns both responsibilities and the cap is now a structural property of
  the sink type. `FileStats.reject_exemplars` is replaced by
  `FileStats.reject_sample: FileSample` (a frozen, per-rule bounded
  sample). `FileSample.from_bounded(cap=N, entries_by_rule={...})` is
  the test-fixture entry point; production code writes through
  `sink.add(entry)`. Renderers (`format_reject_lines`,
  `write_broken_file`) read from `stats.reject_sample.buckets`. No
  user-visible byte format changes (`.broken.txt`, JSON output, and
  `report.md` are byte-identical to the pre-refactor baseline). Closes
  #19.
- Internal: encapsulated `FileStats.quarantined_norad_ids` behind a
  `NoradTracker` type with a single `record(norad_id, rule_id)` mutation
  entry point. `pipeline._record_reject` no longer hand-rolls the
  `setdefault`/`get`/`+1` dance — future writers will find `.record(...)`
  by name instead of reinventing the pattern. Field name unchanged
  (`quarantined_norad_ids` preserved so the `summary_dict` JSON-key
  contract and `git log -S` history stay intact); only the type changed
  from `dict` to `NoradTracker`. Renderers (`summary_dict`,
  `_aggregate_per_norad`, `aggregate_broken_norad_ids`) read via
  `tracker.counts`. Sibling refactor to issue #19's `RejectSink`
  extraction, deliberately simpler — no cap, no file resource, no
  context-manager, no `merge`, no freeze boundary (half-encapsulation
  by deliberate choice so the per-NORAD data shape stays free to evolve
  toward per-satellite timestamps or provenance without breaking a
  monoid contract). No user-visible byte format changes
  (`broken-noradids.ndjson`, JSON output, and `report.md` are
  byte-identical to the pre-refactor baseline). Closes #47.
- Free-form short tags used across `repair.py`, `pipeline.py`, and tests
  are now defined in `lintle.categories` (for `FixClass`, the successful-repair
  taxonomy) and `lintle.diagnostics` (for `RuleID`, the rejection taxonomy)
  as `enum.StrEnum` classes, so typos and renames are caught rather than
  silently drifting across call sites. Closes #18.
- **Breaking — `.broken.txt` sidecar line format.** The per-entry headline
  now cites the rule ID and structured fields instead of a free-form
  sentence: `[N] source lines X-Y - rule: TLE-CHK-001 (tier-1) - col 69
  observed='7' expected='3'`. Related diagnostics on the same record
  (when both lines of a record fail) render on indented `    and: ...`
  continuation lines. The sidecar header (`# source: ... | generated:
  ... | lintle <version>`) is unchanged and already pins the format to
  a release, so downstream parsers can dispatch on version.
- **Breaking — JSON output via `lintle validate --report json`.** The
  per-file `"reject_categories"` field is renamed `"reject_counts"` and its
  inner keys change from free-form tags (`"checksum-mismatch"`) to stable
  rule IDs (`"TLE-CHK-001"`). `fix_counts` and its inner keys are
  unchanged. The per-file payload also gains `"quarantined_norad_ids"`
  carrying the per-satellite per-rule breakdown that backs the new
  Markdown per-NORAD section (see Added above), shaped as
  `{"<noradId>": {"TLE-CHK-001": count, ...}, ...}` — integer NORAD keys
  auto-stringify, `RuleID` keys serialise as their stable wire token.
- `FileStats.reject_categories` is renamed `FileStats.reject_counts` to
  match the new vocabulary; values are keyed by `diagnostics.RuleID`
  (which compares and hashes as its stable string value).
- `FileStats.quarantined_norad_ids` is now a `dict[int, dict[RuleID, int]]`
  instead of a `set[int]`: outer keys are still the satellite catalog
  numbers, but each value is a per-rule count dict tallying which
  diagnostics that satellite hit in this file. `pipeline._record_reject`
  records the rule ID alongside the satellite at quarantine time, feeding
  the new `## Per-NORAD breakdown` section. The `broken-noradids.ndjson`
  sidecar still emits one `{"noradId":N}` line per ID —
  `aggregate_broken_norad_ids` now iterates the dict's keys — so that
  downstream contract is byte-identical. The per-file map is O(IDs × 9),
  and the corpus-wide rollup adds an O(IDs × source files) term for the
  Files column; both are bounded by the satellite catalog and the small
  fixed number of source files, preserving the constant-memory invariant.
  Closes #40.
- `validate` mode now groups reject exemplars by rule ID (up to 5 per
  rule, sorted by descending occurrence count with alphabetic tiebreak),
  so a single noisy defect class can no longer hide rarer rules in the
  operator summary. `FileStats.reject_exemplars` is now
  `dict[RuleID, list[RejectEntry]]` capped at
  `_PER_RULE_EXEMPLAR_BOUND = 5` per rule; the per-file memory ceiling
  drops from 1000 entries to `|RuleID| × 5 = 45`. Each exemplar line
  reuses `_format_diagnostic` so column ranges, observed/expected, repair
  tier, and related-diagnostic continuations carry over. The on-disk
  `.broken.txt` streaming path is untouched — every reject still reaches
  the byte-faithful catalog. Closes #21.

### Removed

- `lintle.categories.RejectCategory` (replaced by
  `lintle.diagnostics.RuleID`). Call sites updated; `RejectCategory` was
  internal — no external API breakage beyond the JSON / `.broken.txt`
  changes noted above.

## [0.2.0] - 2026-05-24

### Fixed

- `pipeline.process_file` no longer conflates unpaired orphan lines with
  paired 2-line records in its counter. `FileStats.total_records` is replaced
  by three independent counters: `paired_records` (true 2-line entries),
  `orphan_entries` (unpaired single lines surfaced as findings), and
  `input_lines_seen` (every physical line read from the file). Per-file
  summary, JSON output (`--report json`), `.broken.txt` sidecar header, and
  `report.md` run report all surface the three counters in their own columns
  so percentages and breakdowns are unambiguous. `clean_count` /
  `quarantined_count` semantics are unchanged: orphans still go to
  `.broken.txt` and remain tallied under `reject_categories['orphan-line']`.
  Closes #5.
- `cli.main` now refuses to run when two distinct inputs share a basename,
  because their `cleaned/` and `broken/` sidecars would otherwise silently
  overwrite each other under `data/output/` — exactly the kind of
  wrong-but-valid-looking outcome the spec forbids. `discover_paths` also
  dedupes inputs by `os.path.realpath`, so the same canonical file listed
  twice (literally, via a parent directory, or through a symlink) is
  processed once. Closes #4.
- `cli.check_paths` no longer pre-checks readability via `os.access`. That
  call consults POSIX mode bits only and false-negatives on filesystems
  that grant read via ACLs (NFSv4, SMB, FUSE), producing a misleading
  "unreadable" verdict on inputs the worker can in fact open. The
  authoritative readability test is the worker's `open()`; a real
  permission failure surfaces through the per-file processing path with
  the same exit code 2. Landed alongside the basename-collision fix in
  commit `a898fb9`. Closes #7.

### Added

- `lintle clean` now emits a corpus-wide `broken-noradids.ndjson` at the
  `--out-dir` root, alongside `cleaned/`, `broken/`, and `report.md`. One
  `{"noradId":N}` object per line, deduplicated and sorted ascending,
  listing every NORAD catalog number whose records were quarantined
  anywhere in the run. Records whose line 1 is itself unreadable are
  omitted (no catalog number to recover). Intended for downstream
  consumers (e.g. descent-app) that need to flag affected satellites
  without parsing the human-readable `broken/*.txt` defect reports. The
  file is always written in `clean` mode — empty when nothing was
  quarantined — so the artifact is always present. Schema is deliberately
  minimal (one field); future releases can extend each record additively
  without breaking compat. Closes #2.
- `tle.extract_norad_id()` — recovers the 5-digit catalog number from a
  TLE line 1, used by the new NDJSON emitter.

- The live progress line on a TTY now reports throughput (`records/sec`) and
  the longest-running file currently in flight (with `+N more` when other
  files are also being processed). With `--jobs N` the oldest active file
  surfaces alone once peers finish — making a single slow file visible at a
  glance during long runs of the 29-file corpus. The progress queue now
  carries `("start", name)` / `("end", name)` lifecycle events alongside the
  existing integer record-count deltas; `process_file` always emits
  `("end", name)` from a `finally`, so a failed file is correctly cleared
  from the display's active set. Closes #24.

- `tests/test_pipeline_throughput.py` — an opt-in end-to-end throughput
  regression test for `pipeline.process_file()` that streams synthetic TLE
  records and fails on a severe slowdown. Gated by the new `slow` pytest
  marker (registered in `pyproject.toml`, excluded from the default suite via
  `addopts`), so the existing CI matrix is unaffected. Combines a within-run
  stability check (no timed run more than 30% slower than the median) with an
  opt-in per-machine stored baseline at `tests/.throughput_baseline.json`
  (git-ignored). Run with `uv run pytest -m slow -s`; refresh the baseline
  after intentional perf changes with
  `LINTLE_UPDATE_BASELINE=1 uv run pytest -m slow`. Closes #23.

## [0.1.2] - 2026-05-23

### Fixed

- `report.py` now streams the `.broken.txt` reject sidecar line-by-line instead
  of holding the full reject set in memory, so the constant-memory invariant
  survives files with a high reject ratio.

### Added

- `CLAUDE.md` § Worktree Workflow and `CONTRIBUTING.md` § Parallel development
  with git worktrees — how to iterate on several branches at once while sharing
  the ~30 GB corpus across worktrees via a symlink.
- `.gitignore` excludes `/.worktrees/`.

## [0.1.1] - 2026-05-22

### Fixed

- `lintle clean` (and `validate`) no longer crash with a `FileNotFoundError`
  traceback when the default input directory `data/source` does not exist on the
  host. A new input-validation step in `cli.main()` catches the situation
  upfront and prints a friendly hint that points the user at `--help` and
  explains how to pass paths or create the directory.
- `discover_paths` no longer silently treats a nonexistent path as a file;
  missing entries are dropped (and the new `check_paths` helper rejects them at
  the boundary with a clear `no such file or directory` message instead of a
  crash deeper in the pipeline).
- `--jobs 0` is rejected upfront instead of silently spinning up a zero-worker
  pool that hangs.

### Added

- `--version` / `-V` on the top-level `lintle` command.
- Top-level and per-subcommand help now include an `Examples:` block and an
  `Exit codes:` reference. Subcommands carry richer descriptions and metavars
  (`PATH`, `DIR`, `N`) so `lintle --help` and `lintle clean --help` are
  self-explanatory.
- `check_paths(paths, using_default)` — a small public helper in `cli.py` that
  returns a user-facing error string for missing or unreadable inputs, or
  `None` if everything is fine.

### Changed

- The `paths` positional argument's argparse default is now `None` (resolved to
  `data/source` inside `main()`) so the CLI can tell "user passed nothing" apart
  from "user explicitly passed `data/source`" and tailor the error wording.
- The version string is now tracked in **one place**: `pyproject.toml`. The
  `__version__` attribute in `src/lintle/__init__.py` is resolved at runtime via
  `importlib.metadata.version("lintle")` (falling back to `0.0.0+local` for
  uninstalled source checkouts). Future releases need only a single bump in
  `pyproject.toml` — see `CONTRIBUTING.md` for the release flow.

## [0.1.0] - 2026-05-22

### Added

- `lintle` console script with two modes: `validate` (read-only audit) and `clean`
  (writes corrected files plus quarantine sidecars).
- `tle.py` — the single TLE validator: column layout, mod-10 checksum, semantic range
  checks, and paired-record validation.
- `repair.py` — speculative, validated repairs: trailing-`\` stripping, CRLF
  normalisation, whitespace trimming, and deterministic checksum reconstruction.
- `pipeline.py` — constant-memory streaming with prefix-driven `1 `/`2 ` line pairing.
- `report.py` — per-file statistics, the byte-faithful `.broken.txt` quarantine sidecar,
  and the Markdown run report.
- `cli.py` — argument parsing, path globbing, per-file `ProcessPoolExecutor` parallelism,
  a live single-line progress display, and graceful Ctrl-C shutdown (exit code `130`).
- Test suite: 92 tests across 7 files, including an `sgp4` oracle cross-check and
  golden-output / idempotence integration tests; `cli.py` is fully covered.
- Project tooling: `ruff` for linting and formatting, `pytest-cov` for coverage.
- Documentation: `README.md`, `CONTRIBUTING.md`, and this changelog.
