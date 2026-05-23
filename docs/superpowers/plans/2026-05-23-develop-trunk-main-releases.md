# develop-trunk, main-releases — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the repository's git workflow so `develop` is the long-running trunk with full history and `main` is an orphan branch carrying one squash-merge commit per release. Backfill `v0.1.1` and `v0.1.2` onto a fresh `main` with trees byte-identical to what is on PyPI, then re-tag. Update `CLAUDE.md` and `CONTRIBUTING.md` via a feature-branch PR — the docs PR is itself the first demonstration of the new `feature → develop --no-ff` flow.

**Architecture:** Eight ordered tasks. Tasks 1–5 rebuild `main` (verify → tear-down → rebuild orphan branch with two backfilled commits → push → recreate GitHub releases). Tasks 6–8 land the docs PR via the new flow. Tree-equality assertions before pushing are the load-bearing safety check — they guarantee what lands on the new `main` matches what shipped to PyPI.

**Tech Stack:** `git` 2.x · `gh` CLI · the existing repo (no code changes).

**Authoritative spec:** `docs/superpowers/specs/2026-05-23-develop-trunk-main-releases-design.md` (commit `4e13697`).

**Pre-conditions assumed:**
- Sole maintainer; no other contributors with `main` checked out.
- No branch protection on `main` or `develop` (verified `404` on `gh api …/branches/main/protection`).
- `publish.yml` is `workflow_dispatch`-only — no auto-CI on push or tag.
- Working tree clean. Starting branch: `develop`.

**Authoritative SHAs (verified at design time, commit `4e13697`):**
- v0.1.1 source commit: `d09f3143e81503e71bde521bf70e816576153ed9` (the only place its tree exists)
- v0.1.1 source tree:   `e8f19600628e89df2622886db0a07d602b723668`
- v0.1.2 source commit: `044594f68aabe1beab87874d0282e3fb671bdf5e` on `develop` ("chore: release v0.1.2")
- v0.1.2 source tree:   `ed5aa79b204895e72093718453c3351c640c3945`
- develop's design-doc root: `ab9fcec`

---

## Task 1: Pre-flight verification (read-only)

**Files:** none modified. This task only reads and asserts.

- [ ] **Step 1: Confirm clean working tree on `develop`**

Run: `git status`

Expected output:
```
On branch develop
Your branch is up to date with 'origin/develop'.

nothing to commit, working tree clean
```

If not clean, stop and resolve before proceeding. Untracked files inside `.worktrees/`, `.venv/`, `data/`, or `.claude/` are gitignored and do not count — `git status` will not show them.

- [ ] **Step 2: Confirm the authoritative tree SHAs**

Run:
```bash
git rev-parse d09f314^{tree}
git rev-parse 044594f^{tree}
```

Expected output:
```
e8f19600628e89df2622886db0a07d602b723668
ed5aa79b204895e72093718453c3351c640c3945
```

If either differs, **stop**. The plan's assertions are calibrated to these exact values and a mismatch means the spec is reading the repo wrong. Investigate before continuing.

- [ ] **Step 3: Confirm no auto-CI on push or tag**

Run: `grep -A 3 "^on:" .github/workflows/*.yml`

Expected: only `workflow_dispatch` triggers. No `push:` or `tag:` triggers anywhere.

If a new workflow has been added that triggers on push to `main` or tag push, decide whether to disable it for the duration of this operation or accept the run.

- [ ] **Step 4: Confirm no open PRs target `main`**

Run: `gh pr list --base main --state open`

Expected: empty output (no PRs).

If any are listed, retarget them at `develop` before continuing:
```bash
gh pr edit <PR-NUMBER> --base develop
```

- [ ] **Step 5: Confirm no branch protection on `main`**

Run: `gh api repos/elfensky/lintle/branches/main/protection 2>&1 | head -3`

Expected:
```
{"message":"Branch not protected","documentation_url":"https://docs.github.com/rest/branches/branch-protection#get-branch-protection","status":"404"}
```

If protection has been added, disable it first via the GitHub UI (Settings → Branches), then continue.

---

## Task 2: Tear down GitHub releases, tags, and main

**Files:** none in the working tree. This task mutates remote state and local refs only.

`gh release delete` removes the release object on GitHub but, with `--cleanup-tag=false`, leaves the underlying git tag intact. We delete the tags ourselves in the next steps so the order is auditable.

- [ ] **Step 1: Delete the v0.1.1 GitHub release**

Run: `gh release delete v0.1.1 --yes --cleanup-tag=false`

Expected: silent success (exit 0). If your `gh` version does not accept `--cleanup-tag`, fall back to:
```bash
gh release delete v0.1.1 --yes
```
The tag will then be deleted both locally and remotely by later steps anyway — same end state.

- [ ] **Step 2: Delete the v0.1.2 GitHub release**

Run: `gh release delete v0.1.2 --yes --cleanup-tag=false`

Expected: silent success.

- [ ] **Step 3: Verify both releases are gone**

Run: `gh release list`

Expected: empty output. If anything else is listed, investigate before continuing.

- [ ] **Step 4: Delete the local tags**

Run: `git tag -d v0.1.1 v0.1.2`

Expected:
```
Deleted tag 'v0.1.1' (was d09f314)
Deleted tag 'v0.1.2' (was fe881ef)
```

- [ ] **Step 5: Delete the remote tags**

Run:
```bash
git push origin :refs/tags/v0.1.1 :refs/tags/v0.1.2
```

Expected:
```
To https://github.com/elfensky/lintle.git
 - [deleted]         v0.1.1
 - [deleted]         v0.1.2
```

- [ ] **Step 6: Delete the local main branch**

You are currently on `develop`, so this is safe.

Run: `git branch -D main`

Expected:
```
Deleted branch main (was fe881ef).
```

- [ ] **Step 7: Delete the remote main branch**

Run: `git push origin --delete main`

Expected:
```
To https://github.com/elfensky/lintle.git
 - [deleted]         main
```

- [ ] **Step 8: Sanity check — only `develop` remains**

Run: `git branch -a && git tag -l`

Expected output:
```
* develop
  remotes/origin/HEAD -> origin/develop
  remotes/origin/develop
```
(no tags listed)

The `remotes/origin/HEAD -> origin/develop` line confirms the GitHub default branch is still `develop`. The previously orphaned commits `d09f314` and `fe881ef` are unreachable but still in the object store; they will GC out over the standard reflog cycle.

---

## Task 3: Build orphan `main` with backfilled v0.1.1 and v0.1.2 commits

**Files:** none — only git refs and the working tree are mutated.

This is the load-bearing task. The tree-equality assertions at Steps 5 and 9 are non-negotiable; if either fails, **stop** before pushing.

- [ ] **Step 1: Create the orphan `main` branch**

Run: `git checkout --orphan main`

Expected:
```
Switched to a new branch 'main'
```

At this point `HEAD` points at `refs/heads/main` but the branch has no commit yet. The working tree and index still contain `develop`'s files — that is normal for `--orphan` and we clean it up in the next step.

- [ ] **Step 2: Empty the index and working tree**

Run: `git rm -rf .`

Expected: a long list of `rm '...'` lines, one per tracked file.

After this, the index is empty and the working tree contains only gitignored items (`.worktrees/`, `.venv/`, `data/`, etc., if present).

- [ ] **Step 3: Populate the index and working tree from `d09f314`'s tree (v0.1.1)**

Run: `git checkout d09f314 -- .`

Expected: silent success (exit 0).

This copies every path from `d09f314`'s tree into the index and working tree without changing `HEAD`. The new state: working tree matches v0.1.1's source exactly.

- [ ] **Step 4: Commit the v0.1.1 release commit**

Run: `git commit -m "Release v0.1.1"`

Expected:
```
[main (root-commit) <SHA>] Release v0.1.1
 NN files changed, NNNN insertions(+)
 create mode 100644 .github/workflows/publish.yml
 create mode 100644 .gitignore
 ...
```

The `(root-commit)` marker confirms this is the first commit on an orphan branch (no parent).

- [ ] **Step 5: Assert the v0.1.1 tree matches the original**

Run:
```bash
[ "$(git rev-parse HEAD^{tree})" = "e8f19600628e89df2622886db0a07d602b723668" ] && echo OK || echo "TREE MISMATCH — STOP"
```

Expected: `OK`

If you see `TREE MISMATCH — STOP`, **do not proceed**. Investigate what diverged before doing anything else. Likely causes: gitignored content accidentally tracked, line-ending normalisation, or wrong source SHA.

- [ ] **Step 6: Tag the v0.1.1 commit**

Run: `git tag -a v0.1.1 -m "Release v0.1.1"`

Expected: silent success.

Verify: `git tag -l` should now show `v0.1.1`.

- [ ] **Step 7: Empty the index and working tree again (prepare for v0.1.2)**

Run: `git rm -rf .`

Expected: a long list of `rm '...'` lines.

- [ ] **Step 8: Populate from `044594f`'s tree (v0.1.2, from develop)**

Run: `git checkout 044594f -- .`

Expected: silent success.

- [ ] **Step 9: Commit the v0.1.2 release commit**

Run: `git commit -m "Release v0.1.2"`

Expected:
```
[main <SHA>] Release v0.1.2
 NN files changed, NNN insertions(+), NN deletions(-)
 ...
```

This is a normal commit (no `(root-commit)` marker) — `HEAD` was pointing at v0.1.1, so v0.1.2 has v0.1.1 as its parent.

- [ ] **Step 10: Assert the v0.1.2 tree matches the original**

Run:
```bash
[ "$(git rev-parse HEAD^{tree})" = "ed5aa79b204895e72093718453c3351c640c3945" ] && echo OK || echo "TREE MISMATCH — STOP"
```

Expected: `OK`

Same rule as Step 5 — `OK` is the only acceptable output. If anything else, **stop**.

- [ ] **Step 11: Tag the v0.1.2 commit**

Run: `git tag -a v0.1.2 -m "Release v0.1.2"`

Expected: silent success.

- [ ] **Step 12: Final shape check before push**

Run: `git log --oneline main && echo --- && git tag -l`

Expected:
```
<SHA-v0.1.2> Release v0.1.2
<SHA-v0.1.1> Release v0.1.1
---
v0.1.1
v0.1.2
```

Two commits, two tags. Nothing else.

---

## Task 4: Push the new main and tags

**Files:** none.

- [ ] **Step 1: Force-push `main` with lease protection**

`--force-with-lease` refuses to push if the remote `main` has moved since we last fetched. Since we deleted remote `main` in Task 2 Step 7, this effectively asserts no one has recreated it in the meantime.

Run: `git push --force-with-lease origin main`

Expected:
```
To https://github.com/elfensky/lintle.git
 * [new branch]      main -> main
```

(`new branch` rather than `forced update` because we deleted it in Task 2 — that's correct.)

- [ ] **Step 2: Push the new tags**

Run: `git push origin v0.1.1 v0.1.2`

Expected:
```
To https://github.com/elfensky/lintle.git
 * [new tag]         v0.1.1 -> v0.1.1
 * [new tag]         v0.1.2 -> v0.1.2
```

- [ ] **Step 3: Verify the remote state**

Run:
```bash
git fetch origin && git log --oneline origin/main && git ls-remote --tags origin
```

Expected: `origin/main` shows the two release commits; `ls-remote` shows v0.1.1 and v0.1.2 entries pointing at the new commits.

- [ ] **Step 4: Confirm GitHub default branch is still `develop`**

Run: `gh api repos/elfensky/lintle | grep '"default_branch"'`

Expected: `"default_branch":"develop",`

If `main` somehow re-became the default, fix it via:
```bash
gh api -X PATCH repos/elfensky/lintle -f default_branch=develop
```

---

## Task 5: Recreate GitHub releases

**Files:** none — GitHub release objects only.

- [ ] **Step 1: Recreate the v0.1.1 release**

Run:
```bash
gh release create v0.1.1 --title "v0.1.1" --notes-from-tag
```

Expected: a URL to the new release page (e.g. `https://github.com/elfensky/lintle/releases/tag/v0.1.1`).

The annotated tag's message ("Release v0.1.1") becomes the release notes via `--notes-from-tag`. If you want richer notes (matching the original PR #29 description), use `--notes-file` instead, sourced from the relevant `CHANGELOG.md` section:
```bash
gh release create v0.1.1 --title "v0.1.1" --notes "$(awk '/^## \[0\.1\.1\]/,/^## \[/{print}' CHANGELOG.md | sed '$d')"
```

- [ ] **Step 2: Recreate the v0.1.2 release as the latest**

Run:
```bash
gh release create v0.1.2 --title "v0.1.2" --notes-from-tag --latest
```

`--latest` marks this as the "Latest release" on the releases page.

- [ ] **Step 3: Verify**

Run: `gh release list`

Expected:
```
TITLE   TYPE    TAG NAME  PUBLISHED
v0.1.2  Latest  v0.1.2    <recent>
v0.1.1          v0.1.1    <recent>
```

At this point the main rewrite is fully published. The docs PR (Tasks 6–8) lands separately and demonstrates the new flow.

---

## Task 6: Start the docs PR — create branch and update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

This is the first feature branch under the new contract: it branches from `develop`, lands a PR to `develop`, merges with `--no-ff`. The change being made (the doc edits themselves) describes the contract this PR is demonstrating.

- [ ] **Step 1: Switch back to `develop` and pull**

Run:
```bash
git checkout develop && git pull --ff-only origin develop
```

Expected: clean fast-forward (or "Already up to date.") — your local `develop` was the source of truth and nothing else has been pushed there.

- [ ] **Step 2: Create the feature branch**

Run: `git checkout -b feature/develop-trunk-workflow-docs`

Expected: `Switched to a new branch 'feature/develop-trunk-workflow-docs'`

- [ ] **Step 3: Update `CLAUDE.md` § Worktree Workflow preamble**

In `CLAUDE.md`, replace this block (currently lines 102–105):

```markdown
Single trunk on `main`; every change goes through a branch + PR off `main` (see
`CONTRIBUTING.md` § Git Workflow). **Worktrees are the parallel-development
mechanism** — they let multiple branches share one clone without contention, so
you can keep a long-running test run in one worktree while editing in another.
```

with:

```markdown
Trunk is `develop`; `main` carries one squash-merge commit per release. Every
non-release change goes through a branch + PR off `develop` and merges back with
`--no-ff` (see `CONTRIBUTING.md` § Git Workflow). **Worktrees are the
parallel-development mechanism** — they let multiple branches share one clone
without contention, so you can keep a long-running test run in one worktree
while editing in another.
```

- [ ] **Step 4: Update `CLAUDE.md` § Worktree Workflow step 1**

In `CLAUDE.md`, replace this line (currently lines 117–118):

```markdown
1. From the main checkout, create the worktree off `main`:
   `git worktree add .worktrees/<branch-dir> -b <branch-name> main`
```

with:

```markdown
1. From the main checkout, create the worktree off `develop`:
   `git worktree add .worktrees/<branch-dir> -b <branch-name> develop`
```

- [ ] **Step 5: Update `CLAUDE.md` § Worktree Workflow step 7**

In `CLAUDE.md`, replace this line (currently line 127):

```markdown
7. Merge back: from the main checkout, `git checkout main && git merge --no-ff <branch-name>`
   (or open a PR — never squash, preserve branch history)
```

with:

```markdown
7. Merge back: from the main checkout, `git checkout develop && git merge --no-ff <branch-name>`
   (or open a PR — never squash, preserve branch history)
```

- [ ] **Step 6: Update `CLAUDE.md` § Worktree Workflow — small-chore paragraph**

In `CLAUDE.md`, replace this paragraph (currently lines 135–137):

```markdown
**Small-chore workflow (branch in main checkout):** branch (`git checkout -b
<branch-name>`), edit, run the same verification chain, commit, PR to `main`.
Skip steps 1, 2, 4, 9 above.
```

with:

```markdown
**Small-chore workflow (branch in main checkout):** branch (`git checkout -b
<branch-name>`), edit, run the same verification chain, commit, PR to `develop`.
Skip steps 1, 2, 4, 9 above.
```

- [ ] **Step 7: Update `CLAUDE.md` § Conventions — Git bullet**

In `CLAUDE.md`, replace this bullet (currently lines 174–178):

```markdown
- Git: single trunk on `main`. Never commit to `main` directly; branch
  (`feature/`, `bugfix/`, `chore/`) off `main` and PR back with `--no-ff`
  (never squash, preserve branch history). Releases are annotated tags on
  `main`. Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`,
  `style:`, `chore:`).
```

with:

```markdown
- Git: `develop` is the trunk; `main` carries one squash-merge commit per
  release. Never commit to `develop` directly; branch (`feature/`, `bugfix/`,
  `chore/`) off `develop` and PR back with `--no-ff` (never squash, preserve
  branch history). Releases squash-merge `develop` into `main` and are tagged
  on `main`. Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`,
  `style:`, `chore:`).
```

- [ ] **Step 8: Verify all `main` references in `CLAUDE.md` are intentional**

Run: `grep -n '\bmain\b' CLAUDE.md`

Expected: the only remaining mentions of `main` should be in contexts that legitimately still refer to `main` — e.g., release commits on `main`, the project's "main checkout" terminology for the non-worktree clone, etc. Skim each match. If any still describe branching/PRs against `main`, fix them inline.

(Note: "main checkout" as a directory term remains valid — it means "the primary clone, not a worktree" — and does not need changing.)

---

## Task 7: Update `CONTRIBUTING.md`

**Files:**
- Modify: `CONTRIBUTING.md`

- [ ] **Step 1: Rewrite the § Git Workflow preamble**

In `CONTRIBUTING.md`, replace these lines (currently lines 106–119):

```markdown
## Git Workflow

Single trunk on `main`. Branch off it for every change, work, PR back to `main`,
merge with `--no-ff` so branch history is preserved. Releases are annotated tags
on `main` — there is no separate release branch.

- Branch names: `feature/<desc>`, `bugfix/<desc>`, `chore/<desc>` — lowercase,
  hyphens.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`.
- Never commit directly to `main`. Open a PR; run the verification commands
  above before merging.
- Never squash PRs to `main` — use `--no-ff` (or "Create a merge commit" in the
  GitHub UI) so branch history survives.
```

with:

```markdown
## Git Workflow

Two branches, two roles:

- **`develop`** is the long-running trunk. All non-release history lives here.
  Branch off it for every change, work, PR back to `develop`, merge with
  `--no-ff` so branch history is preserved.
- **`main`** is the release branch. Each release is one squash-merge commit
  collapsing `develop` (or, for past releases, the relevant historical commit)
  onto `main`. Releases are annotated tags on `main`. There is no separate
  release branch.

- Branch names: `feature/<desc>`, `bugfix/<desc>`, `chore/<desc>` — lowercase,
  hyphens.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`.
- Never commit directly to `develop` or `main`. Open a PR; run the verification
  commands above before merging.
- Never squash PRs to `develop` — use `--no-ff` (or "Create a merge commit" in
  the GitHub UI) so branch history survives. (`main` is the opposite: every
  merge into `main` is a squash, by definition of the release flow.)
```

- [ ] **Step 2: Update § Parallel development with git worktrees — step 1**

In `CONTRIBUTING.md`, replace this line (currently line 129):

```markdown
git worktree add .worktrees/<branch-dir> -b feature/<desc> main
```

with:

```markdown
git worktree add .worktrees/<branch-dir> -b feature/<desc> develop
```

And update the surrounding comment on line 128 from `# 1. Create the worktree from main` to `# 1. Create the worktree from develop`.

- [ ] **Step 3: Update § Parallel development with git worktrees — merge-back step**

In `CONTRIBUTING.md`, replace this line (currently line 142):

```markdown
cd ../.. && git checkout main && git merge --no-ff feature/<desc>
```

with:

```markdown
cd ../.. && git checkout develop && git merge --no-ff feature/<desc>
```

- [ ] **Step 4: Rewrite § Versioning — release flow**

In `CONTRIBUTING.md`, replace this block (currently lines 175–190):

````markdown
Release flow:

1. On a `chore/release-X.Y.Z` branch off `main`, bump `version` in
   `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Open a PR to `main`, merge with `--no-ff` once it's green.
5. Tag the merge commit on `main` and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin vX.Y.Z
   ```
6. Trigger the `Publish` workflow.
````

with:

````markdown
Release flow:

1. On a `chore/release-X.Y.Z` branch off `develop`, bump `version` in
   `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Open a PR to `develop`, merge with `--no-ff` once it's green.
5. Squash-merge `develop` into `main`, tag the merge commit, and push:
   ```bash
   git checkout main && git pull
   git merge --squash develop
   git commit -m "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin main vX.Y.Z
   ```
6. Create the GitHub release:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-from-tag --latest
   ```
7. Trigger the `Publish` workflow.
````

- [ ] **Step 5: Verify all `main` references in `CONTRIBUTING.md` are intentional**

Run: `grep -n '\bmain\b' CONTRIBUTING.md`

Expected: every remaining mention should refer to `main` as the release branch (e.g., "Releases are annotated tags on `main`", "merge into `main` is a squash") or as the "main checkout" terminology. If any still describe branching/PRs against `main` as a trunk, fix them inline.

- [ ] **Step 6: Review the full diff**

Run: `git diff CLAUDE.md CONTRIBUTING.md`

Read it end-to-end. Confirm every replacement landed, nothing extra changed, no half-edited paragraphs.

---

## Task 8: Commit, push, open PR, merge into `develop` with `--no-ff`

**Files:** none new; just publishing the changes from Tasks 6 and 7.

This task is the demonstration: the workflow being documented is the workflow being used to land the documentation.

- [ ] **Step 1: Stage and commit the doc changes**

Run:
```bash
git add CLAUDE.md CONTRIBUTING.md
git commit -m "$(cat <<'EOF'
docs: switch workflow to develop-trunk, main-releases

CLAUDE.md and CONTRIBUTING.md described the old "single trunk on main"
flow. Update them to match the new contract: develop is the trunk,
main carries one squash-merge commit per release. Feature branches
branch off develop and PR back with --no-ff; release commits squash
from develop into main and are tagged on main.

Spec: docs/superpowers/specs/2026-05-23-develop-trunk-main-releases-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: a single new commit on `feature/develop-trunk-workflow-docs` touching two files.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin feature/develop-trunk-workflow-docs`

Expected: branch published; a PR-create URL printed in the push output.

- [ ] **Step 3: Open the PR against `develop`**

Run:
```bash
gh pr create --base develop --title "docs: switch workflow to develop-trunk, main-releases" --body "$(cat <<'EOF'
## Summary

- Update `CLAUDE.md` and `CONTRIBUTING.md` to describe the new workflow: `develop` = trunk, `main` = release branch with one squash-merge commit per release.
- This PR is itself the first demonstration of the new flow — feature branch off `develop`, PR back to `develop`, merge with `--no-ff`.

## Spec

`docs/superpowers/specs/2026-05-23-develop-trunk-main-releases-design.md`

## Test plan

- [ ] Re-read the diff end-to-end; every `main` reference that previously meant "trunk" now says `develop`.
- [ ] After merge, `grep -rn '\bmain\b' CLAUDE.md CONTRIBUTING.md` returns only `main`-as-release-branch and "main checkout" mentions.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: a PR URL is printed.

- [ ] **Step 4: Merge the PR with `--no-ff`**

`gh pr merge --merge` always produces a merge commit (the equivalent of `--no-ff`). `--squash` would defeat the purpose — do not use it.

Run:
```bash
gh pr merge --merge --delete-branch
```

Expected: merge succeeds; remote branch deleted.

If you prefer to merge locally to verify behaviour first:
```bash
git checkout develop && git pull
git merge --no-ff feature/develop-trunk-workflow-docs -m "Merge pull request from feature/develop-trunk-workflow-docs"
git push origin develop
git push origin --delete feature/develop-trunk-workflow-docs
git branch -d feature/develop-trunk-workflow-docs
```

- [ ] **Step 5: Verify the merge commit and final state**

Run:
```bash
git checkout develop && git pull
git log --oneline --graph -5
```

Expected: a merge commit at the tip of `develop` with two parents (the previous `develop` tip and the feature branch tip). The branch shape should look like:
```
*   <merge-SHA> Merge pull request from feature/develop-trunk-workflow-docs
|\
| * <commit-SHA> docs: switch workflow to develop-trunk, main-releases
|/
* 4e13697 docs: add design for develop-trunk, main-releases git workflow
* 044594f chore: release v0.1.2
...
```

The visible bifurcation is `--no-ff` working correctly. If `git log` shows a linear history with no merge commit, the merge was a fast-forward — undo and redo with `--no-ff`.

- [ ] **Step 6: Final repo-wide sanity check**

Run:
```bash
git fetch --prune origin
git branch -a
git tag -l
git log --oneline main
gh release list
```

Expected:
- Branches: `develop` and `main` (local and remote), nothing else.
- Tags: `v0.1.1`, `v0.1.2`.
- `main` log: two commits, "Release v0.1.2" and "Release v0.1.1".
- Releases: `v0.1.2` (Latest), `v0.1.1`.

If everything matches, the rewrite and the first demonstration of the new flow are complete.

---

## Done state

- `develop` has its full history plus the spec commit (`4e13697`) and the docs-PR merge commit at the tip.
- `main` is two commits: `Release v0.1.1` and `Release v0.1.2`, both with trees byte-identical to what was published to PyPI.
- Tags `v0.1.1` and `v0.1.2` point at the new release commits on `main`.
- GitHub releases for both versions exist, with `v0.1.2` marked as Latest.
- `CLAUDE.md` and `CONTRIBUTING.md` describe the new workflow.
- The first PR under the new flow has landed via the new flow.
