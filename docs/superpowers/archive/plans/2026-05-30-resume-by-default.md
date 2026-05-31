# Resume-by-default for `lintle clean` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make resume the default behaviour of `lintle clean` with a discoverable cancel→resume cycle, hardened per the rev-2 spec (run-identity, output-integrity, host-aware lock, SIGINT/SIGTERM/SIGHUP, true-fresh scrub, stronger fingerprint).

**Architecture:** Keep the existing durable checkpoint (`resume.py`, issue #56). Add a pure decision core — `classify_checkpoint` → `resolve_resume_action` — that `main()` drives, mapping its `Decision` onto the existing resume/fresh code paths plus new lock, output-verification, and scrub helpers. All branching logic is pure and table-tested; I/O (TTY prompt, lock, scrub, signals) is thin and injected.

**Tech Stack:** Python 3.14 · stdlib only (argparse, signal, fcntl, os, json, hashlib, socket) · pytest · ruff. Spec: `docs/superpowers/specs/2026-05-30-resume-by-default-design.md`.

**Authoritative spec sections** are cited per task (e.g. "spec §3.5"). Read the spec before starting.

---

## File structure

- `src/lintle/resume.py` — checkpoint + decision core. Modify `input_fingerprint`, `build_checkpoint`; rename `validate_resumable`→`validate_run_identity`; add `CheckpointStatus`/`Classification`/`ResumeAction`/`Decision` types, `classify_checkpoint`, `resolve_resume_action`, `verify_completed_outputs`, `archive_checkpoint`. Bump `SCHEMA_VERSION` to 2.
- `src/lintle/fsutil.py` — add `out_dir_lock` (host-aware exclusive lock context manager).
- `src/lintle/cli.py` — add `--no-resume` (mutually exclusive with `--resume`); add `_is_interactive`, `_prompt_yes_no`, `_scrub_outputs`, signal handlers; rewrite the `main()` resume branch to drive the decision core; new messages; consolidated exit codes.
- `tests/test_resume.py` — unit tests for the decision core, fingerprint, verification.
- `tests/test_cli.py` — interactivity, prompt, scrub, flags, signals, end-to-end.
- `tests/test_fsutil.py` — lock tests.
- `CHANGELOG.md`, `README.md`, `CONTRIBUTING.md` — doc updates.

**Exit-code scheme (spec §2.7), used throughout:** `0` success · `1` operational refusal/failure (stale, corrupt, lock held, declined, EOF, file failed) · `2` argparse usage only · `130` SIGINT · `143` SIGTERM.

---

### Task 1: Strengthen the input fingerprint (spec §3.5)

**Files:**
- Modify: `src/lintle/resume.py:28-48` (`input_fingerprint`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
import os
from lintle import resume

class TestInputFingerprint:
    def test_includes_ctime_and_inode(self, tmp_path):
        f = tmp_path / "tle.txt"
        f.write_bytes(b"1 line\n2 line\n")
        fp = resume.input_fingerprint(str(f))
        # New fields harden against mtime-preserving copies / replace-by-rename.
        assert set(fp) == {
            "size", "mtime_ns", "ctime_ns", "inode",
            "head_sha256", "tail_sha256",
        }
        st = os.stat(str(f))
        assert fp["ctime_ns"] == st.st_ctime_ns
        assert fp["inode"] == st.st_ino
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestInputFingerprint -v`
Expected: FAIL — `KeyError`/assertion (current fingerprint has no `ctime_ns`/`inode`).

- [ ] **Step 3: Add the fields**

In `input_fingerprint`, extend the returned dict (keep the existing head/tail hashing):

```python
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "inode": st.st_ino,
        "head_sha256": hashlib.sha256(head).hexdigest(),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
    }
```

Update the docstring to mention ctime_ns + inode and that they catch metadata-preserving copies / replace-by-rename, with the documented residual (interior edit preserving all of size+mtime+ctime+inode is not detected — spec §3.5/§7).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestInputFingerprint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): fingerprint adds ctime_ns + inode (spec §3.5)"
```

---

### Task 2: Bump schema + record output sizes in the checkpoint (spec §3.1, §3.6)

`verify_completed_outputs` (Task 4) needs the output sizes captured at completion. Store them per completed file, and bump `SCHEMA_VERSION` so old checkpoints are rejected as stale.

**Files:**
- Modify: `src/lintle/resume.py:20` (`SCHEMA_VERSION`), `:51-62` (`build_checkpoint`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
class TestBuildCheckpoint:
    def test_schema_version_is_2_and_records_run_identity(self):
        ckpt = resume.build_checkpoint(
            inputs={"a.txt": {"size": 1}},
            completed={"a.txt": {"summary": {"clean_count": 3}, "outputs": {}}},
            run_identity={"args": []},
        )
        assert ckpt["schema_version"] == 2
        assert ckpt["run_identity"] == {"args": []}
        assert ckpt["completed"]["a.txt"]["summary"]["clean_count"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestBuildCheckpoint -v`
Expected: FAIL — `build_checkpoint` has no `run_identity` param; `SCHEMA_VERSION` is 1.

- [ ] **Step 3: Update SCHEMA_VERSION and build_checkpoint**

Set `SCHEMA_VERSION = 2`. Change `build_checkpoint` to accept and store `run_identity`:

```python
def build_checkpoint(*, inputs, completed, run_identity):
    """Assemble the checkpoint payload, pinning schema, lintle version, and the
    run identity (spec §3.1). ``inputs`` maps each discovered input path to its
    :func:`input_fingerprint`; ``completed`` maps each fully-processed path to a
    ``{"summary": summary_dict, "outputs": {name: size}}`` record (the output
    sizes back the integrity re-verification of :func:`verify_completed_outputs`).
    ``run_identity`` pins output-affecting configuration beyond version+inputs.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "lintle_version": __version__,
        "run_identity": run_identity,
        "inputs": inputs,
        "completed": completed,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestBuildCheckpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): checkpoint schema v2 — run_identity + completed.outputs (spec §3.1)"
```

---

### Task 3: Rename `validate_resumable` → `validate_run_identity` (spec §3.1)

**Files:**
- Modify: `src/lintle/resume.py:104-136` (rename + extend), `src/lintle/cli.py:646` (call site — updated fully in Task 11; here just keep imports valid)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
class TestValidateRunIdentity:
    def _ckpt(self, **over):
        base = dict(schema_version=2, lintle_version=resume.__version__,
                    run_identity={"args": []}, inputs={"a.txt": {"size": 1}},
                    completed={})
        base.update(over); return base

    def test_passes_when_everything_matches(self):
        ck = self._ckpt()
        assert resume.validate_run_identity(ck, {"a.txt": {"size": 1}}, {"args": []}) is None

    def test_refuses_on_run_identity_drift(self):
        ck = self._ckpt()
        reason = resume.validate_run_identity(ck, {"a.txt": {"size": 1}}, {"args": ["--x"]})
        assert reason and "configuration" in reason.lower()

    def test_refuses_on_schema_bump(self):
        ck = self._ckpt(schema_version=1)
        assert resume.validate_run_identity(ck, {"a.txt": {"size": 1}}, {"args": []})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestValidateRunIdentity -v`
Expected: FAIL — `validate_run_identity` does not exist.

- [ ] **Step 3: Rename and extend**

Rename `validate_resumable(checkpoint, current_inputs)` to
`validate_run_identity(checkpoint, current_inputs, current_run_identity)`. Keep the existing
schema/version/input checks; add a run-identity check before the input checks:

```python
def validate_run_identity(checkpoint, current_inputs, current_run_identity):
    """Return a human-readable reason the checkpoint cannot be resumed against the
    current run, or None if it can. Refuse-on-change (spec §3.1, all-or-nothing):
    schema, lintle version, output-affecting configuration, or any input identity
    drift invalidates the whole checkpoint."""
    schema = checkpoint.get("schema_version")
    if schema != SCHEMA_VERSION:
        return (f"checkpoint schema_version {schema!r} is not supported "
                f"(this lintle writes schema {SCHEMA_VERSION})")
    recorded_version = checkpoint.get("lintle_version")
    if recorded_version != __version__:
        return (f"lintle version changed since the interrupted run "
                f"({recorded_version} -> {__version__})")
    if checkpoint.get("run_identity") != current_run_identity:
        return "run configuration changed since the interrupted run"
    recorded = checkpoint.get("inputs", {})
    added = sorted(set(current_inputs) - set(recorded))
    if added:
        return f"new input file(s) not in the interrupted run: {', '.join(added)}"
    removed = sorted(set(recorded) - set(current_inputs))
    if removed:
        return f"input file(s) missing since the interrupted run: {', '.join(removed)}"
    for path in sorted(current_inputs):
        if current_inputs[path] != recorded[path]:
            return f"input changed since the interrupted run: {path}"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestValidateRunIdentity -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "refactor(resume): validate_resumable -> validate_run_identity, pin run identity (spec §3.1)"
```

---

### Task 4: Output-integrity re-verification (spec §3.6)

**Files:**
- Modify: `src/lintle/resume.py` (add `verify_completed_outputs`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
from lintle import stem

class TestVerifyCompletedOutputs:
    def _completed(self, name, cleaned_size):
        return {name: {"summary": {"src_name": name},
                       "outputs": {stem(name) + ".cleaned.txt": cleaned_size}}}

    def test_intact_outputs_are_trusted(self, tmp_path):
        out = tmp_path
        (out / "cleaned").mkdir()
        (out / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 100)
        bad = resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(out))
        assert bad == []  # nothing to reprocess

    def test_missing_output_flags_reprocess(self, tmp_path):
        bad = resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(tmp_path))
        assert bad == ["tle2099.txt"]

    def test_truncated_output_flags_reprocess(self, tmp_path):
        out = tmp_path
        (out / "cleaned").mkdir()
        (out / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 7)  # short
        bad = resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(out))
        assert bad == ["tle2099.txt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestVerifyCompletedOutputs -v`
Expected: FAIL — function not defined.

- [ ] **Step 3: Implement**

```python
def verify_completed_outputs(completed, out_dir):
    """Return the list of input paths whose recorded outputs are missing or do
    not match their recorded size (spec §3.6). A checkpoint entry is trusted only
    when every output file it named still exists on disk at the exact byte size
    captured at completion — guarding against a SIGKILL/disk-full truncation that
    `os.stat`-existence alone would not catch. Flagged files are reprocessed."""
    reprocess = []
    for path, entry in completed.items():
        for rel_name, expected_size in entry.get("outputs", {}).items():
            actual = os.path.join(out_dir, "cleaned", rel_name) \
                if rel_name.endswith(".cleaned.txt") \
                else os.path.join(out_dir, "broken", rel_name)
            try:
                if os.path.getsize(actual) != expected_size:
                    reprocess.append(path)
                    break
            except OSError:
                reprocess.append(path)
                break
    return reprocess
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestVerifyCompletedOutputs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): verify_completed_outputs — integrity not existence (spec §3.6)"
```

---

### Task 5: Checkpoint classification (spec §2.3)

**Files:**
- Modify: `src/lintle/resume.py` (add `CheckpointStatus`, `Classification`, `classify_checkpoint`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
class TestClassifyCheckpoint:
    def test_absent(self, tmp_path):
        c = resume.classify_checkpoint(str(tmp_path), {"a.txt": {"size": 1}}, {"args": []})
        assert c.status is resume.CheckpointStatus.ABSENT

    def test_corrupt(self, tmp_path):
        (tmp_path / resume.CHECKPOINT_NAME).write_text("{not json")
        c = resume.classify_checkpoint(str(tmp_path), {"a.txt": {"size": 1}}, {"args": []})
        assert c.status is resume.CheckpointStatus.CORRUPT

    def test_valid(self, tmp_path):
        ck = resume.build_checkpoint(inputs={"a.txt": {"size": 1}}, completed={},
                                     run_identity={"args": []})
        resume.write_checkpoint(str(tmp_path), ck)
        c = resume.classify_checkpoint(str(tmp_path), {"a.txt": {"size": 1}}, {"args": []})
        assert c.status is resume.CheckpointStatus.VALID
        assert c.checkpoint is not None

    def test_stale(self, tmp_path):
        ck = resume.build_checkpoint(inputs={"a.txt": {"size": 1}}, completed={},
                                     run_identity={"args": []})
        resume.write_checkpoint(str(tmp_path), ck)
        c = resume.classify_checkpoint(str(tmp_path), {"a.txt": {"size": 2}}, {"args": []})
        assert c.status is resume.CheckpointStatus.STALE
        assert "changed" in c.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestClassifyCheckpoint -v`
Expected: FAIL — symbols not defined.

- [ ] **Step 3: Implement**

```python
import dataclasses
import enum

class CheckpointStatus(enum.Enum):
    ABSENT = "absent"
    CORRUPT = "corrupt"
    VALID = "valid"
    STALE = "stale"

@dataclasses.dataclass
class Classification:
    status: "CheckpointStatus"
    reason: str | None = None       # populated for STALE
    checkpoint: dict | None = None  # parsed payload for VALID / STALE

def classify_checkpoint(out_dir, current_inputs, current_run_identity):
    """Classify the checkpoint in ``out_dir`` against the current run (spec §2.3).
    Distinguishes ABSENT (no file), CORRUPT (present but unparseable — never
    treated as absent, so a damaged interrupted run is surfaced, not silently
    discarded), VALID, and STALE(reason)."""
    if not os.path.exists(_checkpoint_path(out_dir)):
        return Classification(CheckpointStatus.ABSENT)
    checkpoint = load_checkpoint(out_dir)
    if checkpoint is None:
        return Classification(CheckpointStatus.CORRUPT)
    reason = validate_run_identity(checkpoint, current_inputs, current_run_identity)
    if reason is not None:
        return Classification(CheckpointStatus.STALE, reason=reason, checkpoint=checkpoint)
    return Classification(CheckpointStatus.VALID, checkpoint=checkpoint)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestClassifyCheckpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): classify_checkpoint — ABSENT/CORRUPT/VALID/STALE (spec §2.3)"
```

---

### Task 6: The decision core `resolve_resume_action` (spec §2.3, §2.4, §4)

**Files:**
- Modify: `src/lintle/resume.py` (add `ResumeAction`, `Decision`, `resolve_resume_action`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test (the full truth table)**

```python
# tests/test_resume.py
class TestResolveResumeAction:
    def _c(self, status, reason=None):
        return resume.Classification(status, reason=reason)

    def call(self, status, *, resume_flag=False, no_resume=False,
             interactive=False, answer=True, reason="inputs changed"):
        cls = self._c(status, reason=reason)
        prompt = lambda msg, *, default: answer  # noqa: E731
        return resume.resolve_resume_action(
            cls, resume=resume_flag, no_resume=no_resume,
            interactive=interactive, prompt=prompt)

    def test_absent_default_is_fresh(self):
        d = self.call(resume.CheckpointStatus.ABSENT)
        assert d.action is resume.ResumeAction.FRESH

    def test_absent_with_resume_aborts(self):
        d = self.call(resume.CheckpointStatus.ABSENT, resume_flag=True)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 1

    def test_corrupt_default_aborts_no_resume_freshes(self):
        assert self.call(resume.CheckpointStatus.CORRUPT).action is resume.ResumeAction.ABORT
        assert self.call(resume.CheckpointStatus.CORRUPT, no_resume=True).action is resume.ResumeAction.FRESH

    def test_valid_flags(self):
        assert self.call(resume.CheckpointStatus.VALID, resume_flag=True).action is resume.ResumeAction.RESUME
        assert self.call(resume.CheckpointStatus.VALID, no_resume=True).action is resume.ResumeAction.FRESH

    def test_valid_non_interactive_auto_resumes(self):
        assert self.call(resume.CheckpointStatus.VALID, interactive=False).action is resume.ResumeAction.RESUME

    def test_valid_interactive_prompt(self):
        assert self.call(resume.CheckpointStatus.VALID, interactive=True, answer=True).action is resume.ResumeAction.RESUME
        assert self.call(resume.CheckpointStatus.VALID, interactive=True, answer=False).action is resume.ResumeAction.FRESH

    def test_valid_interactive_eof_aborts(self):
        d = self.call(resume.CheckpointStatus.VALID, interactive=True, answer=None)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 1

    def test_stale_no_resume_freshes_resume_aborts(self):
        assert self.call(resume.CheckpointStatus.STALE, no_resume=True).action is resume.ResumeAction.FRESH
        assert self.call(resume.CheckpointStatus.STALE, resume_flag=True).action is resume.ResumeAction.ABORT

    def test_stale_non_interactive_aborts(self):
        d = self.call(resume.CheckpointStatus.STALE, interactive=False)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 1
        assert "--no-resume" in d.message

    def test_stale_interactive_prompt(self):
        assert self.call(resume.CheckpointStatus.STALE, interactive=True, answer=True).action is resume.ResumeAction.FRESH
        assert self.call(resume.CheckpointStatus.STALE, interactive=True, answer=False).action is resume.ResumeAction.ABORT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestResolveResumeAction -v`
Expected: FAIL — symbols not defined.

- [ ] **Step 3: Implement the decision core**

```python
class ResumeAction(enum.Enum):
    FRESH = "fresh"
    RESUME = "resume"
    ABORT = "abort"

@dataclasses.dataclass
class Decision:
    action: "ResumeAction"
    message: str | None = None   # printed by the caller
    exit_code: int | None = None # set for ABORT

def resolve_resume_action(classification, *, resume, no_resume, interactive, prompt):
    """Pure decision for the §2.3 matrix. ``resume``/``no_resume`` are the explicit
    flags (authoritative); ``interactive`` is the detected mode; ``prompt`` is a
    callable ``(message, *, default) -> bool | None`` (None = EOF/no-answer) used
    only when a decision needs the operator. Returns a Decision."""
    status = classification.status
    St = CheckpointStatus
    if status is St.ABSENT:
        if resume:
            return Decision(ResumeAction.ABORT, "no interrupted run to resume", 1)
        return Decision(ResumeAction.FRESH)
    if status is St.CORRUPT:
        if no_resume:
            return Decision(ResumeAction.FRESH)
        return Decision(ResumeAction.ABORT,
                        "checkpoint is unreadable; pass --no-resume to start fresh", 1)
    if status is St.VALID:
        if resume:
            return Decision(ResumeAction.RESUME)
        if no_resume:
            return Decision(ResumeAction.FRESH)
        if not interactive:
            return Decision(ResumeAction.RESUME)
        answer = prompt("Resume interrupted run? [Y/n] ", default=True)
        if answer is None:
            return Decision(ResumeAction.ABORT, "aborted", 1)
        return Decision(ResumeAction.RESUME if answer else ResumeAction.FRESH)
    # STALE
    reason = classification.reason or "inputs changed"
    if no_resume:
        return Decision(ResumeAction.FRESH)
    if resume:
        return Decision(ResumeAction.ABORT, f"cannot resume: {reason}", 1)
    if not interactive:
        return Decision(ResumeAction.ABORT,
                        f"cannot resume: {reason}. Pass --no-resume to start fresh", 1)
    answer = prompt(f"Can't resume ({reason}). Reprocess all from scratch? [y/N] ",
                    default=False)
    if answer:
        return Decision(ResumeAction.FRESH)
    return Decision(ResumeAction.ABORT, "aborted", 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestResolveResumeAction -v`
Expected: PASS (all rows).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): resolve_resume_action decision core — §2.3 truth table"
```

---

### Task 7: Archive (don't delete) stale/corrupt checkpoints (spec §2.3, §3.4)

**Files:**
- Modify: `src/lintle/resume.py` (add `archive_checkpoint`)
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py
class TestArchiveCheckpoint:
    def test_renames_with_stale_suffix(self, tmp_path):
        ck = resume.build_checkpoint(inputs={}, completed={}, run_identity={})
        resume.write_checkpoint(str(tmp_path), ck)
        archived = resume.archive_checkpoint(str(tmp_path), timestamp="20260530T0000Z")
        assert archived is not None
        assert not (tmp_path / resume.CHECKPOINT_NAME).exists()
        assert (tmp_path / archived).exists()
        assert archived.startswith(resume.CHECKPOINT_NAME + ".stale-")

    def test_noop_when_absent(self, tmp_path):
        assert resume.archive_checkpoint(str(tmp_path), timestamp="x") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume.py::TestArchiveCheckpoint -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Implement**

```python
def archive_checkpoint(out_dir, *, timestamp):
    """Rename the checkpoint to ``.clean-state.json.stale-<timestamp>`` so a fresh
    run never silently destroys a recoverable interrupted run (spec §2.3): the
    operator can downgrade/revert and recover it. Returns the archived basename,
    or None if there was no checkpoint. ``timestamp`` is supplied by the caller
    (clock access is forbidden in pure helpers)."""
    src = _checkpoint_path(out_dir)
    if not os.path.exists(src):
        return None
    archived = f"{CHECKPOINT_NAME}.stale-{timestamp}"
    os.replace(src, os.path.join(out_dir, archived))
    return archived
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resume.py::TestArchiveCheckpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/resume.py tests/test_resume.py
git commit -m "feat(resume): archive_checkpoint — never delete recoverable state (spec §2.3)"
```

---

### Task 8: Host-aware exclusive out-dir lock (spec §3.3)

**Files:**
- Modify: `src/lintle/fsutil.py` (add `out_dir_lock`, `LockHeldError`)
- Test: `tests/test_fsutil.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fsutil.py
import os
import pytest
from lintle import fsutil

class TestOutDirLock:
    def test_acquires_and_releases(self, tmp_path):
        with fsutil.out_dir_lock(str(tmp_path)):
            assert os.path.exists(os.path.join(str(tmp_path), ".clean.lock"))
        # lock content is removed/released on exit
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # re-acquire succeeds

    def test_refuses_when_held_by_live_same_host(self, tmp_path):
        with fsutil.out_dir_lock(str(tmp_path)):
            with pytest.raises(fsutil.LockHeldError):
                with fsutil.out_dir_lock(str(tmp_path)):
                    pass

    def test_reclaims_dead_same_host_pid(self, tmp_path):
        # A lockfile naming this host but a dead PID is reclaimable.
        lock = os.path.join(str(tmp_path), ".clean.lock")
        with open(lock, "w") as h:
            h.write(f'{{"host": "{fsutil._host_id()}", "pid": 999999999, "started": "x"}}')
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # reclaimed, no error

    def test_refuses_cross_host_even_if_pid_dead(self, tmp_path):
        lock = os.path.join(str(tmp_path), ".clean.lock")
        with open(lock, "w") as h:
            h.write('{"host": "some-other-host-xyz", "pid": 999999999, "started": "x"}')
        with pytest.raises(fsutil.LockHeldError):
            with fsutil.out_dir_lock(str(tmp_path)):
                pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fsutil.py::TestOutDirLock -v`
Expected: FAIL — `out_dir_lock`/`LockHeldError`/`_host_id` not defined.

- [ ] **Step 3: Implement**

```python
# src/lintle/fsutil.py
import contextlib
import json
import os
import socket

LOCK_NAME = ".clean.lock"


class LockHeldError(RuntimeError):
    """Raised when the out-dir lock is held by another live run."""


def _host_id():
    """Stable per-host identity for the lock. Hostname + boot id where the latter
    is available (Linux); hostname alone elsewhere. Lets reclaim be same-host-only
    so a dead PID on host A is never falsely reclaimed from host B (spec §3.3)."""
    host = socket.gethostname()
    try:
        with open("/proc/sys/kernel/random/boot_id") as h:
            return f"{host}:{h.read().strip()}"
    except OSError:
        return host


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours
    return True


@contextlib.contextmanager
def out_dir_lock(out_dir, *, started="unknown"):
    """Exclusive, host-aware lock over ``out_dir`` for the duration of a run
    (spec §3.3). Refuses (LockHeldError) when held by a live process on this host
    or by any process on a different host. Reclaims only a same-host dead-PID
    lock. ``started`` is an ISO timestamp passed in by the caller (no clock here)."""
    path = os.path.join(out_dir, LOCK_NAME)
    host = _host_id()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_lock(path)
            if holder and holder.get("host") == host and not _pid_alive(holder.get("pid", -1)):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)  # stale same-host lock — reclaim and retry
                continue
            raise LockHeldError(
                f"another lintle clean is using {out_dir!r} "
                f"(held by {holder}); wait for it to finish"
            )
        else:
            with os.fdopen(fd, "w") as h:
                json.dump({"host": host, "pid": os.getpid(), "started": started}, h)
            break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _read_lock(path):
    try:
        with open(path) as h:
            return json.load(h)
    except (OSError, json.JSONDecodeError):
        return None
```

(Keep the existing `durable_replace` in this file unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_fsutil.py::TestOutDirLock -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/fsutil.py tests/test_fsutil.py
git commit -m "feat(fsutil): host-aware exclusive out-dir lock (spec §3.3)"
```

---

### Task 9: Interactivity detection + prompt (spec §2.2, §2.4)

**Files:**
- Modify: `src/lintle/cli.py` (add `_is_interactive`, `_prompt_yes_no`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import io
from lintle import cli

class TestIsInteractive:
    def test_requires_stdin_tty_and_no_ci(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO())  # not a tty
        monkeypatch.delenv("CI", raising=False)
        assert cli._is_interactive() is False

    def test_ci_env_forces_non_interactive(self, monkeypatch):
        class _TTY(io.StringIO):
            def isatty(self): return True
        monkeypatch.setattr(cli.sys, "stdin", _TTY())
        monkeypatch.setenv("CI", "true")
        assert cli._is_interactive() is False

class TestPromptYesNo:
    def _stdin(self, text):
        return io.StringIO(text)

    def test_enter_takes_default(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", self._stdin("\n"))
        assert cli._prompt_yes_no("go? ", default=True) is True

    def test_explicit_no(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", self._stdin("n\n"))
        assert cli._prompt_yes_no("go? ", default=True) is False

    def test_eof_returns_none(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", self._stdin(""))  # immediate EOF
        assert cli._prompt_yes_no("go? ", default=True) is None

    def test_garbage_then_abort(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", self._stdin("maybe\nhuh\nwhat\n"))
        assert cli._prompt_yes_no("go? ", default=True) is None  # 3 strikes -> None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestIsInteractive tests/test_cli.py::TestPromptYesNo -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Implement**

```python
# src/lintle/cli.py  (near the other helpers)
def _is_interactive():
    """A run is interactive iff stdin is a TTY (the prompt answer is read there)
    and no CI/NONINTERACTIVE env var forces non-interactive — which prevents a
    CI runner that allocates a pseudo-TTY from hanging on the prompt (spec §2.2)."""
    if os.environ.get("CI") or os.environ.get("NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _prompt_yes_no(message, *, default):
    """Ask a y/n question on stderr, reading the answer from stdin (spec §2.4).
    Enter takes ``default``; up to 3 unrecognised answers then give up; EOF/Ctrl-D
    gives up. Returns True/False, or None when the operator gave no usable answer
    (caller treats None as abort)."""
    for _ in range(3):
        print(message, end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if line == "":            # EOF / Ctrl-D
            print(file=sys.stderr)
            return None
        token = line.strip().lower()
        if token == "":
            return default
        if token in ("y", "yes"):
            return True
        if token in ("n", "no"):
            return False
        print("  please answer y or n.", file=sys.stderr)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestIsInteractive tests/test_cli.py::TestPromptYesNo -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "feat(cli): _is_interactive + _prompt_yes_no (spec §2.2, §2.4)"
```

---

### Task 10: True-fresh output scrub (spec §3.4)

**Files:**
- Modify: `src/lintle/cli.py` (add `_scrub_outputs`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import os
class TestScrubOutputs:
    def test_removes_output_trees(self, tmp_path):
        out = tmp_path
        for sub in ("cleaned", "broken", ".shards"):
            d = out / sub
            d.mkdir()
            (d / "stale.txt").write_text("old")
        cli._scrub_outputs(str(out))
        for sub in ("cleaned", "broken", ".shards"):
            assert not (out / sub).exists()

    def test_noop_on_empty_dir(self, tmp_path):
        cli._scrub_outputs(str(tmp_path))  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestScrubOutputs -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Implement**

```python
# src/lintle/cli.py
def _scrub_outputs(out_dir):
    """Clear the cleaned/, broken/, and .shards/ trees so a fresh run starts from
    a clean slate and never leaves orphaned outputs from a prior, differently
    scoped input set (spec §3.4). Idempotent — missing trees are ignored."""
    for sub in ("cleaned", "broken", ".shards"):
        shutil.rmtree(os.path.join(out_dir, sub), ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestScrubOutputs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "feat(cli): _scrub_outputs — true-fresh slate (spec §3.4)"
```

---

### Task 11: Add `--no-resume`; rewrite the `main()` resume branch (spec §2.1, §2.3, §2.5)

**Files:**
- Modify: `src/lintle/cli.py:214-224` (flags), `:610-669` (resume branch), the loop at `:754-761` (record output sizes)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
class TestResumeWiring:
    def _make_src(self, tmp_path, line1, line2, n=2):
        src = tmp_path / "src"; src.mkdir()
        for i in range(n):
            (src / f"tle20{i:02d}.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        return src

    def test_no_resume_and_resume_are_mutually_exclusive(self, tmp_path, line1, line2):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        rc = cli.main(["clean", str(src), "--out-dir", str(out),
                       "--resume", "--no-resume", "--jobs", "1"])
        assert rc == 2  # argparse usage error

    def test_default_run_with_no_checkpoint_is_fresh_and_succeeds(self, tmp_path, line1, line2):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0
        # checkpoint deleted on success
        assert not (out / ".clean-state.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestResumeWiring -v`
Expected: FAIL — `--no-resume` unknown (argparse error is exit 2, but the mutual-exclusion message differs; the second test fails because `--no-resume` doesn't exist yet → actually exits 2 for unknown arg, so adjust once implemented).

- [ ] **Step 3a: Add `--no-resume` as a mutually-exclusive flag**

Replace the `if name == "clean":` block at `cli.py:214-224` with a mutually-exclusive group:

```python
        if name == "clean":
            resume_group = sub.add_mutually_exclusive_group()
            resume_group.add_argument(
                "--resume",
                action="store_true",
                help=(
                    "resume an interrupted run in --out-dir without prompting "
                    "(resume is the default when an interrupted run is found)"
                ),
            )
            resume_group.add_argument(
                "--no-resume",
                action="store_true",
                help=(
                    "ignore any interrupted run and start fresh, clearing prior "
                    "outputs in --out-dir"
                ),
            )
```

- [ ] **Step 3b: Build the run identity and rewrite the resume branch**

Replace `cli.py:619-669` (from the `inputs = {...}` line through the `else:` scrub block) with:

```python
        inputs = {path: resume.input_fingerprint(path) for path in files}
        shard_dir = os.path.join(args.out_dir, ".shards")
        # Output-affecting configuration pinned into the checkpoint identity
        # (spec §3.1). Today only the input set + version affect output content;
        # this is the explicit, future-proof hook so a new output-affecting flag
        # cannot validate-through and mix policies.
        run_identity = {"max_quarantined": args.max_quarantined}

        classification = resume.classify_checkpoint(args.out_dir, inputs, run_identity)
        decision = resume.resolve_resume_action(
            classification,
            resume=args.resume,
            no_resume=args.no_resume,
            interactive=_is_interactive(),
            prompt=_prompt_yes_no,
        )
        if decision.action is resume.ResumeAction.ABORT:
            print(f"error: {decision.message}", file=sys.stderr)
            return decision.exit_code
        if decision.action is resume.ResumeAction.RESUME:
            checkpoint = classification.checkpoint
            completed = dict(checkpoint["completed"])
            # Integrity re-verification (spec §3.6): drop any completed entry whose
            # outputs are missing or truncated, so they are reprocessed.
            for path in resume.verify_completed_outputs(completed, args.out_dir):
                completed.pop(path, None)
            reused_stats = [
                report.stats_from_summary(e["summary"]) for e in completed.values()
            ]
            files_to_process = [f for f in files if f not in completed]
            print(
                f"resuming: {len(completed)}/{len(files)} files already complete, "
                f"processing {len(files_to_process)} — pass --no-resume for a fresh run",
                file=sys.stderr, flush=True,
            )
        else:  # FRESH
            # True-fresh slate (spec §3.4): archive any checkpoint (never delete a
            # recoverable run), then scrub output trees so no orphans linger.
            resume.archive_checkpoint(args.out_dir, timestamp=run_started_stamp())
            _scrub_outputs(args.out_dir)
```

Note: `completed` entries are now `{"summary": ..., "outputs": ...}` (schema v2), so
`stats_from_summary` reads `e["summary"]`.

- [ ] **Step 3c: Record output sizes when a file completes**

At `cli.py:754-761`, change the checkpoint-write block so each completed entry stores
the summary **and** its output sizes:

```python
                        if args.command == "clean":
                            completed[path] = {
                                "summary": report.summary_dict(stats),
                                "outputs": _output_sizes(args.out_dir, stats),
                            }
                            resume.write_checkpoint(
                                args.out_dir,
                                resume.build_checkpoint(
                                    inputs=inputs,
                                    completed=completed,
                                    run_identity=run_identity,
                                ),
                            )
```

Add the `_output_sizes` helper near `_scrub_outputs`:

```python
def _output_sizes(out_dir, stats):
    """Map each output basename this file produced to its on-disk size, captured
    at completion for the resume integrity check (spec §3.6). The broken sidecar
    is present only when something was quarantined."""
    sizes = {}
    cleaned = stem(stats.src_name) + ".cleaned.txt"
    with contextlib.suppress(OSError):
        sizes[cleaned] = os.path.getsize(os.path.join(out_dir, "cleaned", cleaned))
    if stats.quarantined_count:
        broken = stem(stats.src_name) + ".broken.txt"
        with contextlib.suppress(OSError):
            sizes[broken] = os.path.getsize(os.path.join(out_dir, "broken", broken))
    return sizes
```

Ensure `from lintle import ... stem` and `import contextlib` are present in `cli.py`
(add if missing).

- [ ] **Step 3d: Add the `run_started_stamp` helper (first used here)**

It is referenced by the FRESH branch above (and by Tasks 12–13). Define it near the
other helpers so it exists before first use:

```python
def run_started_stamp():
    """ISO-8601 UTC timestamp for archive/lock naming. Isolated so the rest of the
    resume logic stays clock-free and testable."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
```

(`datetime` is already imported — `main()` uses it for `run_started_iso`.)

- [ ] **Step 3e: Seed the progress display so skipped files read as done (spec §2.5)**

So the live block shows `12/29`, not `0/17`, pass the full file count and the
already-completed count to `_ProgressDisplay`. At the construction site
(`cli.py` ~`:735`, `_ProgressDisplay(len(files_to_process), progress_queue, console, sizes)`),
change it to pass the full total and a starting-done count:

```python
            with _ProgressDisplay(
                len(files), progress_queue, console, sizes,
                already_done=len(completed),
            ) as progress:
```

In `_ProgressDisplay.__init__`, accept `already_done=0` and initialise
`self._files_done = already_done` (instead of `0`), so the overall bar starts at the
resumed count. Fresh runs pass `already_done=0` (the default), unchanged behaviour.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestResumeWiring -v`
Expected: PASS. Then run the whole CLI suite: `uv run pytest tests/test_cli.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "feat(cli): resume by default — --no-resume, decision core, integrity, scrub (spec §2)"
```

---

### Task 12: Signal handling (SIGINT/SIGTERM/SIGHUP) + cancel message + exit codes (spec §2.6, §2.7, §3.2)

**Files:**
- Modify: `src/lintle/cli.py:762-774` (the interrupt handler), add a `run_started_stamp` helper and SIGTERM/SIGHUP traps
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import signal
class TestSignalHandling:
    def test_cancel_message_names_counts_and_flag(self, capsys):
        # _format_cancel_message is the pure message builder.
        msg = cli._format_cancel_message(done=12, total=29)
        assert "12/29" in msg
        assert "--no-resume" in msg
        assert "same --out-dir" in msg

    def test_signal_exit_code(self):
        assert cli._signal_exit_code(signal.SIGINT) == 130
        assert cli._signal_exit_code(signal.SIGTERM) == 143
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestSignalHandling -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Implement the pure helpers and wire the handler**

Add near the other helpers (`run_started_stamp` was already added in Task 11, step 3d —
do not redefine it):

```python
def _signal_exit_code(signo):
    """Conventional 128 + signal number (spec §2.7): 130 SIGINT, 143 SIGTERM,
    129 SIGHUP."""
    return 128 + int(signo)


def _format_cancel_message(*, done, total):
    return (
        f"interrupted — workers stopped ({done}/{total} files done).\n"
        "Re-run the same command (same --out-dir) to continue where it stopped; "
        "inputs must be unchanged.\n"
        "Pass --no-resume to start over."
    )
```

Then, in `main()`, replace the bare `KeyboardInterrupt` handler (`cli.py:762-774`) so it
also catches SIGTERM/SIGHUP. Install handlers that raise `KeyboardInterrupt` for SIGTERM and
SIGHUP just before the executor loop, and record which signal fired:

```python
        caught = {"signo": signal.SIGINT}

        def _raise_interrupt(signo, _frame):
            caught["signo"] = signo
            raise KeyboardInterrupt

        prev_term = signal.signal(signal.SIGTERM, _raise_interrupt)
        prev_hup = signal.signal(signal.SIGHUP, _raise_interrupt)
        try:
            # ... existing try-body (futures dispatch + _ProgressDisplay loop) ...
        except KeyboardInterrupt:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            interrupted = True
            interrupted_signo = caught["signo"]
            _terminate_workers(executor)
            executor.shutdown(wait=False, cancel_futures=True)
            print(
                _format_cancel_message(done=len(completed), total=len(files_to_process)),
                file=sys.stderr, flush=True,
            )
        else:
            executor.shutdown(wait=True)
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGHUP, prev_hup)
```

Change the interrupted return (`cli.py:773`) from `return 130` to:

```python
    if interrupted:
        return _signal_exit_code(interrupted_signo)
```

Initialise `interrupted_signo = signal.SIGINT` next to `interrupted = False`. The checkpoint
is already written incrementally per completed file, so completed work survives any of the
three signals; trapping them adds clean teardown + the helpful message.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestSignalHandling -v`
Expected: PASS. Then `uv run pytest tests/test_cli.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "feat(cli): SIGINT/SIGTERM/SIGHUP handling + cancel message + 128+signo exits (spec §2.6,§2.7,§3.2)"
```

---

### Task 13: Hold the out-dir lock around the run (spec §3.3)

**Files:**
- Modify: `src/lintle/cli.py` (wrap the clean dispatch in `fsutil.out_dir_lock`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from lintle import fsutil
class TestLockWiring:
    def test_refuses_when_locked(self, tmp_path, line1, line2):
        src = tmp_path / "src"; src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"; out.mkdir()
        with fsutil.out_dir_lock(str(out)):  # simulate a concurrent run
            rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 1  # operational refusal
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestLockWiring -v`
Expected: FAIL — no lock yet, the run proceeds (rc 0).

- [ ] **Step 3: Acquire the lock for clean runs**

In `main()`, wrap the `if args.command == "clean":` dispatch so the lock is held for the
whole run. Import `fsutil` at the top of `cli.py` if not already. Acquire right after
`os.makedirs(args.out_dir, exist_ok=True)`:

```python
    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        try:
            lock_cm = fsutil.out_dir_lock(args.out_dir, started=run_started_stamp())
            lock_cm.__enter__()
        except fsutil.LockHeldError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        lock_cm = None
```

Release it on every exit path: wrap the remainder of `main()` after acquisition in
`try: ... finally: if lock_cm is not None: lock_cm.__exit__(None, None, None)`. (Place the
`finally` so it runs before every `return`. If the function structure makes that awkward,
use `contextlib.ExitStack` registered at acquisition instead.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestLockWiring -v`
Expected: PASS. Then the full suite: `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "feat(cli): hold host-aware out-dir lock around clean (spec §3.3)"
```

---

### Task 14: End-to-end cancel→resume + true-fresh integration test

**Files:**
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the test (simulate a partial run via a pre-seeded checkpoint)**

A real interrupt is hard to script deterministically; instead pre-seed a checkpoint as if one
file completed, then assert resume skips it and a `--no-resume` run reprocesses everything.

```python
# tests/test_cli.py
class TestResumeEndToEnd:
    def test_resume_skips_completed_then_no_resume_redoes(self, tmp_path, line1, line2):
        src = tmp_path / "src"; src.mkdir()
        for i in range(2):
            (src / f"tle200{i}.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        # First full run -> success deletes the checkpoint.
        assert cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"]) == 0
        assert not (out / ".clean-state.json").exists()
        first_mtime = (out / "cleaned" / "tle2000.cleaned.txt").stat().st_mtime_ns

        # A --no-resume run scrubs and reprocesses everything (true-fresh).
        assert cli.main(["clean", str(src), "--out-dir", str(out),
                         "--no-resume", "--jobs", "1"]) == 0
        assert (out / "cleaned" / "tle2000.cleaned.txt").exists()

    def test_stale_non_interactive_errors_with_guidance(self, tmp_path, line1, line2, monkeypatch):
        src = tmp_path / "src"; src.mkdir()
        f = src / "tle2000.txt"; f.write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"; out.mkdir()
        # Seed a checkpoint whose input fingerprint won't match (different size record).
        ck = resume.build_checkpoint(
            inputs={str(f): {"size": 999999}}, completed={},
            run_identity={"max_quarantined": "0"})
        resume.write_checkpoint(str(out), ck)
        monkeypatch.setenv("CI", "true")  # force non-interactive
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 1  # stale + non-interactive -> error with guidance
```

- [ ] **Step 2: Run to verify**

Run: `uv run pytest tests/test_cli.py::TestResumeEndToEnd -v`
Expected: PASS (after Tasks 11–13). If the stale test's seeded checkpoint path keys differ
from how `main` keys inputs (full path vs basename), align the seed with `main`'s
`inputs = {path: ...}` (full discovered path).

- [ ] **Step 3: Commit**

```bash
git add tests/test_cli.py
git commit -m "test(cli): end-to-end resume / true-fresh / stale-non-interactive"
```

---

### Task 15: Docs — CHANGELOG, README, CONTRIBUTING

**Files:**
- Modify: `CHANGELOG.md`, `README.md`, `CONTRIBUTING.md`

- [ ] **Step 1: CHANGELOG entry**

Add under the unreleased/next section:

```markdown
### Changed
- `clean` now **resumes by default**: after an interruption, re-running the same
  command (same `--out-dir`) continues where it stopped. Interactive terminals
  prompt; CI/non-TTY auto-resumes with a loud notice. Use `--no-resume` to start
  fresh (clears prior outputs); `--resume` forces resume without prompting.
- Cancelling (`Ctrl-C`, or `SIGTERM`/`SIGHUP` from a scheduler) now prints how to
  continue or start over.

### Added
- Host-aware out-dir lock prevents two concurrent `clean` runs from corrupting a
  shared output directory.
```

- [ ] **Step 2: README — document the cancel/resume cycle**

In the `clean` usage section, add a short "Cancelling and resuming" subsection describing:
the default-resume behaviour, `--no-resume`/`--resume`, that resume is `--out-dir`-scoped,
and that a fresh run clears prior outputs. Keep it to one paragraph + a 2-line example
(`uv run lintle clean` → `Ctrl-C` → `uv run lintle clean` resumes).

- [ ] **Step 3: CONTRIBUTING — note the new resume model**

If CONTRIBUTING documents `--resume`, update it to reflect resume-by-default + `--no-resume`.

- [ ] **Step 4: Verify docs build/links (manual skim) and run the full suite**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md CONTRIBUTING.md
git commit -m "docs: resume-by-default cancel/resume cycle + --no-resume"
```

---

## Final verification

- [ ] `uv run pytest -q` — all pass
- [ ] `uv run ruff check .` — clean
- [ ] `uv run ruff format --check .` — clean
- [ ] Manual TTY smoke (own terminal): `uv run lintle clean <tiny-fixture-dir>`, `Ctrl-C` mid-run, confirm the cancel message; re-run, confirm the resume prompt and the "resuming N/M" header; `--no-resume` confirms a clean slate.
