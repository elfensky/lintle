# Single-Input CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse `lintle validate` / `lintle clean` to accept exactly one positional input (a single file *or* a single directory), removing multi-input support, basename-collision detection, and the realpath dedup pass.

**Architecture:** Change the `paths` positional from `nargs="*"` to `nargs="?"` (renamed `path`). Refactor `discover_paths` and `check_paths` from `list[str]` to `str` signatures. Delete `_detect_basename_collisions` (its callsite, function, and tests) — basename collisions are structurally impossible within a single directory because the filesystem guarantees unique entries. Drop the realpath dedup loop since one input has nothing to dedup against. This is a **breaking CLI change**; the project is pre-1.0 so it lands on a `refactor/<desc>` branch into `develop`, with a Keep-a-Changelog `Unreleased` entry flagged BREAKING. The version bump itself is **not** part of this PR (per CLAUDE.md "no per-merge version bumps" — that happens in a later `chore/release-X.Y.Z` branch).

**Tech Stack:** Python 3.11 · uv · `argparse` (stdlib) · `pytest` · `ruff`.

**Rationale (carried from brainstorm):**

- The single-directory case (default invocation `lintle clean data/source`) covers the documented use; multi-input flexibility was theoretical and required a defensive check (`_detect_basename_collisions`) to police it.
- Removing plural inputs removes the failure mode *and* its defensive code path together.
- Aligns with the project's scope discipline (`project_scope_focused_cleaner` memory): trim speculative flexibility, stay a focused validator/cleaner.

---

## File Touch Map

**Modified:**
- `src/lintle/cli.py` — argparse, `discover_paths`, `check_paths`, `main()`; delete `_detect_basename_collisions`.
- `tests/test_cli.py` — update tests in `TestDiscoverPaths`, `TestBuildParser`, `TestCheckPaths`; delete `TestDetectBasenameCollisions` and three obsolete dedup edge-case tests; delete two `TestMain` collision tests.
- `README.md` — `paths` → `path`, "Files or directories" → "File or directory", remove `[paths...]` in usage.
- `CHANGELOG.md` — append BREAKING entry to `[Unreleased]`.

**Not modified (out of scope):**
- `pyproject.toml` — version bump deferred to the release branch.
- The other issue-#26 README sections (redundancy paradox, disk-space, counter meanings, stale example fix) — separate `docs:` work; **the collision README section becomes moot and must NOT be added.**

---

## Task 1: Worktree + branch setup

**Files:** none yet — environment setup.

- [ ] **Step 1: Switch the main checkout to `develop` and pull**

```bash
cd /Users/andrei/Developer/lintle
git checkout develop
git pull --ff-only
```

Expected: `develop` is fast-forwarded (or already up-to-date).

- [ ] **Step 2: Create the worktree off `develop`**

```bash
git worktree add .worktrees/refactor-single-input-cli -b refactor/single-input-cli develop
cd .worktrees/refactor-single-input-cli
```

Expected: new directory `.worktrees/refactor-single-input-cli/` containing a checkout of branch `refactor/single-input-cli`.

- [ ] **Step 3: Install dev deps and symlink the corpus**

```bash
uv sync
ln -s ../../data data
```

Expected: `.venv/` created; `data` is a symlink to the main checkout's `data/`.

- [ ] **Step 4: Baseline-verify the worktree**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: all three pass. (If not, do not proceed — surface the failure.)

---

## Task 2: Argparse single-input contract

**Files:**
- Modify: `src/lintle/cli.py:173-183` (the `paths` arg in `_build_subparser`), `cli.py:500-535` (main fallback + missing-file error)
- Modify: `tests/test_cli.py:32-48` (`TestBuildParser`), delete `tests/test_cli.py:304-345` (two `TestMain` collision tests)

- [ ] **Step 1: Add a failing test for the new contract**

Append inside `class TestBuildParser` in `tests/test_cli.py` (after `test_parser_accepts_jobs_and_paths`):

```python
    def test_parser_rejects_multiple_positional_inputs(self):
        # Single-input contract: only one positional allowed.
        import pytest

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["clean", "a.txt", "b.txt"])
        assert exc.value.code == 2  # argparse usage error
```

- [ ] **Step 2: Run the new test to confirm it fails**

```bash
uv run pytest tests/test_cli.py::TestBuildParser::test_parser_rejects_multiple_positional_inputs -v
```

Expected: FAIL — argparse currently accepts both positionals.

- [ ] **Step 3: Flip argparse to single-input**

In `src/lintle/cli.py`, replace lines 173-183:

```python
        sub.add_argument(
            "path",
            nargs="?",
            default=None,
            metavar="PATH",
            help=(
                f"file or directory to process "
                f"(default: {_DEFAULT_SOURCE}). "
                "A directory is globbed for tle*.txt."
            ),
        )
```

- [ ] **Step 4: Adapt `main()` to read `args.path` (temporary list-wrap for now)**

In `src/lintle/cli.py`, replace lines 500-504:

```python
    # `args.path` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = args.path is None
    path = args.path or _DEFAULT_SOURCE
    paths = [path]  # transitional: discover_paths/check_paths still take lists
```

(Tasks 3 and 4 collapse the `paths = [path]` line.)

- [ ] **Step 5: Update the two existing parser tests**

In `tests/test_cli.py`, replace `test_parser_defaults` (lines 32-39):

```python
    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["validate"])
        assert args.command == "validate"
        # path defaults to None so main() can tell "user passed nothing"
        # apart from "user explicitly passed the default" for error wording.
        assert args.path is None
        assert args.out_dir == "data/output"
        assert args.report == "text"
```

Replace `test_parser_accepts_jobs_and_paths` (lines 41-48):

```python
    def test_parser_accepts_jobs_and_path(self):
        args = cli.build_parser().parse_args(
            ["clean", "a.txt", "--jobs", "4", "--report", "json"]
        )
        assert args.command == "clean"
        assert args.path == "a.txt"
        assert args.jobs == 4
        assert args.report == "json"
```

- [ ] **Step 6: Delete the two `TestMain` collision tests**

In `tests/test_cli.py`, delete lines 304-345 in their entirety — both `test_main_returns_two_on_basename_collision` and `test_main_does_not_collide_when_same_file_listed_twice`. Both invoke `cli.main(["clean", str(a), str(b), ...])` with multiple positionals, which argparse now rejects.

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest -q
```

Expected: PASS, including the new `test_parser_rejects_multiple_positional_inputs`. If failures surface in tests other than the ones we've already touched, stop and report.

- [ ] **Step 8: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "refactor(cli): accept exactly one positional input (path)

argparse 'paths' (nargs='*') becomes 'path' (nargs='?'). main() temporarily
wraps in a list so discover_paths/check_paths continue working; subsequent
commits collapse those signatures. Deletes the two TestMain collision tests
whose invocations are no longer parseable."
```

---

## Task 3: `discover_paths(path: str)` — single-string signature

**Files:**
- Modify: `src/lintle/cli.py:42-72` (`discover_paths` function), `cli.py:522` (callsite)
- Modify: `tests/test_cli.py:12-28` (`TestDiscoverPaths`), `tests/test_cli.py:106-140` (`TestDiscoverPathsEdgeCases`)

- [ ] **Step 1: Rewrite `discover_paths` to take one string**

In `src/lintle/cli.py`, replace lines 42-72:

```python
def discover_paths(path):
    """Expand ``path``: a directory becomes its sorted ``tle*.txt`` files
    (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool output); a file is
    returned as a single-element list. A nonexistent entry yields ``[]`` —
    callers should validate inputs with :func:`check_paths` first.
    """
    if os.path.isdir(path):
        return [
            os.path.join(path, name)
            for name in sorted(os.listdir(path))
            if (
                name.startswith("tle")
                and name.endswith(".txt")
                and not name.endswith(".cleaned.txt")
                and not name.endswith(".broken.txt")
            )
        ]
    if os.path.isfile(path):
        return [path]
    return []
```

- [ ] **Step 2: Update the callsite in `main()`**

In `src/lintle/cli.py`, find the `files = discover_paths(paths)` line (was line 522) and replace its surrounding block with:

```python
    files = discover_paths(path)
    if not files:
        if os.path.isdir(path):
            print(
                f"error: no tle*.txt files found in {path!r}.\n"
                "  expected one or more files named tle*.txt "
                "(excluding *.cleaned.txt / *.broken.txt).",
                file=sys.stderr,
            )
        else:
            print("error: no input files found", file=sys.stderr)
        return 2
```

Also delete the transitional `paths = [path]` line from Task 2 — `main()` now uses `path` directly.

- [ ] **Step 3: Update the two `TestDiscoverPaths` tests**

In `tests/test_cli.py`, replace lines 12-28:

```python
class TestDiscoverPaths:
    def test_discover_expands_directory(self, tmp_path):
        (tmp_path / "tle2001.txt").write_text("x")
        (tmp_path / "tle2002.txt").write_text("x")
        (tmp_path / "tle2001.cleaned.txt").write_text("x")  # tool output — excluded
        (tmp_path / "tle2001.broken.txt").write_text("x")  # tool output — excluded
        (tmp_path / "notes.md").write_text("x")  # not a TLE file

        found = cli.discover_paths(str(tmp_path))

        names = sorted(os.path.basename(p) for p in found)
        assert names == ["tle2001.txt", "tle2002.txt"]

    def test_discover_passes_through_explicit_file(self, tmp_path):
        explicit = tmp_path / "tle2001.txt"
        explicit.write_text("x")
        assert cli.discover_paths(str(explicit)) == [str(explicit)]
```

- [ ] **Step 4: Collapse `TestDiscoverPathsEdgeCases` to the one remaining test**

In `tests/test_cli.py`, replace lines 106-140 (the entire `TestDiscoverPathsEdgeCases` class) with:

```python
class TestDiscoverPathsEdgeCases:
    def test_nonexistent_path_yields_empty(self, tmp_path):
        # main() validates first, but discover_paths must be robust on its own.
        assert cli.discover_paths(str(tmp_path / "missing")) == []
```

The three deleted tests (`test_duplicate_explicit_paths_are_deduped`, `test_dir_and_explicit_file_inside_it_are_deduped`, `test_symlinked_path_is_deduped`) exercised the realpath dedup loop, which a single-input contract makes structurally impossible.

- [ ] **Step 5: Run the suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "refactor(cli): discover_paths takes a single path string

Signature collapses from list[str] to str. The realpath dedup loop is gone
— a single input has nothing to dedup against. Three TestDiscoverPathsEdgeCases
tests for the dedup behaviour are deleted; one (nonexistent-path-yields-empty)
is kept and updated."
```

---

## Task 4: `check_paths(path: str, using_default: bool)` — single-string signature

**Files:**
- Modify: `src/lintle/cli.py:99-128` (`check_paths`), `cli.py:517` (callsite)
- Modify: `tests/test_cli.py:69-104` (`TestCheckPaths`)

- [ ] **Step 1: Rewrite `check_paths` to take one string**

In `src/lintle/cli.py`, replace lines 99-128 (read `cli.py:99-128` first to see the full current body — preserve the `using_default` branch's friendly hint about `lintle --help`):

```python
def check_paths(path, using_default):
    """Return a user-facing error string if ``path`` does not exist, else
    ``None``. ``using_default`` tailors the message for the case where the
    user passed nothing and the default (``data/source``) is what's missing.

    Readability is *not* checked here — :func:`os.access` consults the
    POSIX mode bits only and is a false-negative on filesystems that grant
    read through ACLs (NFSv4, SMB, FUSE). The authoritative answer is
    whatever the worker's :func:`open` returns; a genuine permission error
    surfaces through the per-file failure path in :func:`main` with the
    same exit code 2.
    """
    if os.path.exists(path):
        return None
    if using_default:
        return (
            f"default input directory {_DEFAULT_SOURCE!r} does not exist.\n"
            "  pass a path or create the directory; "
            "see `lintle --help` for usage."
        )
    return f"no such file or directory: {path}"
```

(Verify the exact existing wording for the "default missing" hint by reading `cli.py:99-128` before editing; preserve any reference to `lintle --help` and the trailing newline conventions.)

- [ ] **Step 2: Update the callsite in `main()`**

In `src/lintle/cli.py`, the `check_paths` call (was line 517) becomes:

```python
    path_error = check_paths(path, using_default=using_default)
    if path_error:
        print(f"error: {path_error}", file=sys.stderr)
        return 2
```

(Replace `paths` with `path`.)

- [ ] **Step 3: Update `TestCheckPaths` tests**

In `tests/test_cli.py`, replace lines 69-104 (the entire `TestCheckPaths` class) with:

```python
class TestCheckPaths:
    def test_missing_default_yields_friendly_hint(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no data/source here
        err = cli.check_paths("data/source", using_default=True)
        assert err is not None
        assert "data/source" in err
        assert "lintle --help" in err

    def test_missing_explicit_path_yields_plain_message(self, tmp_path):
        err = cli.check_paths(str(tmp_path / "nope.txt"), using_default=False)
        assert err is not None
        assert "no such file or directory" in err
        assert "data/source" not in err  # not the default-hint variant

    def test_existing_path_returns_none(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("x")
        assert cli.check_paths(str(f), using_default=False) is None

    def test_os_access_false_negative_does_not_refuse_run(self, tmp_path, monkeypatch):
        # os.access() consults POSIX mode bits and is a false-negative on
        # filesystems that grant read via ACLs (NFSv4, SMB, FUSE). The
        # preflight must not refuse a run on os.access() alone — the
        # authoritative answer is whatever the worker's open() returns.
        f = tmp_path / "readable.txt"
        f.write_text("x")
        monkeypatch.setattr(cli.os, "access", lambda _p, _m: False)
        assert cli.check_paths(str(f), using_default=False) is None
```

(`test_multiple_missing_paths_are_listed` is deleted — a single input cannot have multiple missing entries.)

- [ ] **Step 4: Run the suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "refactor(cli): check_paths takes a single path string

Signature collapses from list[str] to str; the friendly default-missing hint
is preserved. test_multiple_missing_paths_are_listed is deleted (a single
input cannot have multiple missing entries)."
```

---

## Task 5: Delete `_detect_basename_collisions` and its tests

**Files:**
- Modify: `src/lintle/cli.py` — delete `_detect_basename_collisions` (lines 75-96) and its callsite in `main()` (lines 537-540)
- Modify: `tests/test_cli.py` — delete the entire `TestDetectBasenameCollisions` class (lines 142-176)

- [ ] **Step 1: Delete the function**

In `src/lintle/cli.py`, delete lines 75-96 — the entire `_detect_basename_collisions` function and the blank line that separates it from `check_paths` below.

- [ ] **Step 2: Delete the callsite**

In `src/lintle/cli.py`, delete the four-line block in `main()` that calls the function (was lines 537-540):

```python
    collision_error = _detect_basename_collisions(files)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 2
```

- [ ] **Step 3: Delete the test class**

In `tests/test_cli.py`, delete the entire `class TestDetectBasenameCollisions` block (lines 142-176, the three test methods inside it, and the blank line below the class).

- [ ] **Step 4: Run the suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_cli.py
git commit -m "refactor(cli): drop basename-collision detection

A single positional input cannot collide with itself: within one directory
the filesystem guarantees unique basenames, and the file-input case has
exactly one source. _detect_basename_collisions, its callsite in main(),
and TestDetectBasenameCollisions are removed."
```

---

## Task 6: README updates

**Files:**
- Modify: `README.md:83` (validate usage), `README.md:86` (clean usage), `README.md:98` (paths argument row)

- [ ] **Step 1: Update the usage block (lines 81-90)**

Replace:

```markdown
# Audit only — report defects, write nothing
uv run lintle validate [paths...]

# Produce cleaned output + quarantine sidecars
uv run lintle clean [paths...]
```

with:

```markdown
# Audit only — report defects, write nothing
uv run lintle validate [path]

# Produce cleaned output + quarantine sidecars
uv run lintle clean [path]
```

- [ ] **Step 2: Update the `paths` row in the options table (line 98)**

Replace the row:

```markdown
| `paths` | `data/source` | Files or directories. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
```

with:

```markdown
| `path` | `data/source` | A single file or directory. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
```

- [ ] **Step 3: Verify no other `paths` references linger**

```bash
grep -n "paths\|\[paths" README.md
```

Expected: no operational-CLI references to a `paths` positional remain. (Hits inside the `Output` section's narrative — e.g. "source paths" — are fine if they describe inputs in general; only the CLI-argument references need updating.)

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): single positional input (path), not paths"
```

---

## Task 7: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md:7-25` (append to the existing `[Unreleased]` section)

- [ ] **Step 1: Add a `### Changed` and `### Removed` entry under `[Unreleased]`**

In `CHANGELOG.md`, immediately after the existing fsync-durability `### Changed` paragraph (ends at line 25), insert:

```markdown
- **Breaking change.** `lintle validate` and `lintle clean` now accept exactly
  one positional input — a single file *or* a single directory — instead of
  zero-or-more. The default remains `data/source`. Scripts invoking
  `lintle clean dirA dirB` (or multiple explicit files) will now fail at
  argparse with a usage error; run the tool once per input directory, or
  stage the inputs into a single directory. This trims speculative flexibility
  the documented workflow never exercised: the per-file output names are
  derived from each input's basename alone, so multi-input runs needed a
  defensive collision check whose existence was the only reason multi-input
  was risky in the first place. With single-input, basenames within one
  directory are unique by filesystem guarantee, so the failure mode and its
  guard disappear together.

### Removed

- `cli._detect_basename_collisions` and its `TestDetectBasenameCollisions`
  tests — no callers after the single-input change above.
- The realpath dedup loop inside `cli.discover_paths` (a single input has
  nothing to dedup against). `discover_paths` and `check_paths` now take a
  single path string rather than a list.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record breaking single-input CLI change"
```

---

## Task 8: Full verification

**Files:** none — verification gate before opening the PR.

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest
```

Expected: PASS. Report the actual final line (e.g. `=== N passed in Xs ===`) — do not summarise as "tests pass" without the count.

- [ ] **Step 2: Lint**

```bash
uv run ruff check .
```

Expected: PASS (no findings). If `ruff` flags an unused import in `cli.py` after the deletions (e.g. an `os.path.basename`-only usage gone), fix it inline and re-run.

- [ ] **Step 3: Format check**

```bash
uv run ruff format --check .
```

Expected: PASS. If a deleted block left stray blank lines that `ruff format` wants to fix, run `uv run ruff format .` and commit as `style: ruff format`.

- [ ] **Step 4: Smoke-test the CLI manually**

```bash
uv run lintle clean --help | head -30
uv run lintle validate --help | head -30
```

Expected: `path` (not `paths`) in the usage line, with the new help text "file or directory to process".

```bash
# Single-positional should parse; multi-positional should error.
uv run lintle clean data/source/tle2022.txt --out-dir /tmp/lintle-smoke --jobs 1 || true
uv run lintle clean a.txt b.txt --out-dir /tmp/lintle-smoke --jobs 1 ; echo "exit=$?"
```

Expected: the second command prints an argparse usage error and exits non-zero (typically `2`).

---

## Task 9: Open the PR

**Files:** none — branch publication.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin refactor/single-input-cli
```

- [ ] **Step 2: Open the PR against `develop`**

```bash
gh pr create \
  --base develop \
  --title "refactor: collapse multi-input CLI to a single positional" \
  --body "$(cat <<'EOF'
Replaces \`paths\` (\`nargs="*"\`) with \`path\` (\`nargs="?"\`) across
\`lintle validate\` / \`lintle clean\`. Deletes \`_detect_basename_collisions\`,
its tests, the realpath dedup loop in \`discover_paths\`, and the
two-positional \`TestMain\` collision tests. \`discover_paths\` and
\`check_paths\` now take a single path string. README and CHANGELOG updated.

**Breaking CLI change** (pre-1.0; CHANGELOG entry under \`[Unreleased]\`).
The version bump itself lands later in a \`chore/release-X.Y.Z\` branch per
CLAUDE.md.

### Why

The multi-input flexibility was theoretical — the documented workflow uses
\`lintle clean data/source\` (one directory). Its only failure mode (two
inputs writing to one basename-keyed output) required a defensive
\`_detect_basename_collisions\` to police. Removing plural inputs removes
both the failure and the guard.

### Verification

- \`uv run pytest\` — passes
- \`uv run ruff check .\` — passes
- \`uv run ruff format --check .\` — passes
- Manual: \`lintle clean a.txt b.txt\` now fails at argparse; \`lintle clean
  data/source/tle2022.txt\` still works.
EOF
)"
```

- [ ] **Step 3: Note the merge style**

The PR should be landed via GitHub's **"Rebase and merge"** button (or `gh pr merge --rebase --delete-branch`) so `develop` stays linear, per CLAUDE.md. **Do not** use "Create a merge commit" or "Squash and merge".

- [ ] **Step 4: After merge — clean up the worktree**

From the main checkout:

```bash
cd /Users/andrei/Developer/lintle
git fetch --prune
git checkout develop
git pull --ff-only
git worktree remove .worktrees/refactor-single-input-cli
git branch -D refactor/single-input-cli
```

(Use `-D`, not `-d`: rebase-and-merge rewrites the SHAs on `develop`, so the local branch won't look "merged" to git even though its content has landed.)

---

## Self-review checklist

**Spec coverage:**
- Argparse single-input contract ✓ (Task 2)
- `discover_paths` single-string signature ✓ (Task 3)
- `check_paths` single-string signature ✓ (Task 4)
- Deletion of `_detect_basename_collisions` ✓ (Task 5)
- README update ✓ (Task 6)
- CHANGELOG entry (BREAKING) ✓ (Task 7)
- Verification ✓ (Task 8)
- PR + merge style + worktree cleanup ✓ (Task 9)

**Out of scope (intentionally not covered):**
- `pyproject.toml` version bump — release-branch concern.
- Issue #26's other README sections (redundancy paradox, disk-space, counter meanings) — separate `docs:` work.
- The stale README example at line 203 (missing `(N orphan, N lines)` parenthetical) — also separate `docs:` work.

**No-placeholder check:** All file paths are absolute or repo-relative; all code blocks contain real code; all commands have expected outputs; commit messages are concrete.

**Type/identifier consistency:**
- `args.path` (singular) used uniformly from Task 2 onward.
- `discover_paths(path: str)` and `check_paths(path, using_default)` consistent across tasks 3-4 and their callsites.
- `using_default = args.path is None` consistent with the existing default-hint UX.
