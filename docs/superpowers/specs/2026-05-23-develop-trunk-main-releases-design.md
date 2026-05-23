# Git Workflow — `develop` Trunk, `main` Releases — Design

- **Date:** 2026-05-23
- **Status:** Approved; ready for implementation planning
- **Topic:** Restructure the repository's git workflow so `develop` is the long-running
  trunk carrying full history, and `main` is a curated, orphan branch containing one
  squash-merge commit per release. Existing release commits and tags are reissued so that
  the new `main` is byte-identical to what is on PyPI.

## 1. Problem statement

Today the repository uses `main` as the trunk: features branch off `main`, PRs merge back
with `--no-ff`, and release tags are annotated tags on `main`. Two releases have shipped
under that model (`v0.1.1`, `v0.1.2`), each landed as a PR merge commit on `main`
(`d09f314`, `fe881ef`). The `develop` branch has accumulated all post-`v0.1.2` work and is
already the GitHub default branch.

We want to flip the model:

- `develop` is the long-running trunk. All history lives here. Features branch off
  `develop` and merge back with `--no-ff`, preserving branch shape.
- `main` is a release-only branch. Each release is a single squash-merge commit produced
  by collapsing `develop` (or, for back-filled releases, an existing release-state commit)
  onto `main`. Tags are annotated tags on `main`.

The two existing releases must be preserved as commits and as tags. The new tagged commits
must have trees byte-identical to what was published to PyPI under those versions, so that
anyone tracing PyPI → GitHub finds source matching the artifact.

## 2. Goal & non-goals

**Goal.** End up with a repository whose branch structure reflects the new contract,
whose tags accurately mark what shipped, and whose contributing docs (`CLAUDE.md`,
`CONTRIBUTING.md`) describe the new workflow rather than the old one.

**Non-goals.**

- Re-publishing or yanking anything on PyPI. PyPI artifacts are untouched.
- Changing CI behaviour. `publish.yml` is `workflow_dispatch`-only and is not affected.
- Rewriting `develop`'s history. `develop` is preserved as-is.
- Introducing release branches, GitFlow, or any process beyond "feature → develop, release
  → main."

## 3. End state

After the operation the repository looks like:

- **`develop`** — unchanged. 23 commits, root at `ab9fcec`. Remains the GitHub default
  branch.
- **`main`** — orphan branch, no shared ancestor with `develop`, exactly two commits:
  - `Release v0.1.1` — tree-identical to current `v0.1.1` tag (`tree e8f1960…`)
  - `Release v0.1.2` — tree-identical to current `v0.1.2` tag (`tree ed5aa79…`)
- **Tags** `v0.1.1` and `v0.1.2` re-pointed at the new commits on `main`. The previously
  tagged commits (`d09f314`, `fe881ef`) become unreachable and will GC out of the local
  repo over the standard 30–90 day reflog window.
- **GitHub releases** for `v0.1.1` and `v0.1.2` recreated against the new tags, so the
  release pages keep working.
- **Docs** (`CLAUDE.md`, `CONTRIBUTING.md`) updated to describe the new workflow. This
  doc update lands via the new flow itself (feature branch → PR → `--no-ff` into
  `develop`).

## 4. Why orphan `main`?

`main` and `develop` having no shared history is the cleanest expression of the
contract: `develop` is *the* history; `main` is a sequence of release snapshots, each
independent. A non-orphan `main` rooted at `ab9fcec` (the design-doc commit) would imply a
shared ancestor that the new workflow does not actually use — we never `git merge
main` into `develop` and we never branch features from `main`. Orphan removes that
implication.

Practical effects: `git log main..develop` still works (it lists everything on
`develop`, which is what you want for "what's not released yet"). `git diff main develop`
still works (it diffs trees, ancestry is irrelevant). There is no operational downside.

## 5. Why backfill v0.1.1 and v0.1.2 onto the new `main`?

Two alternatives were considered and rejected:

- **Leave the existing tags on their orphaned commits, start the new `main` empty.**
  Simplest, but the new `main` carries no history of what shipped before `v0.1.3`. The
  tags point at commits unreachable from any branch, which is technically valid but
  confusing to readers.
- **Drop the old tags entirely, start from `v0.1.3`.** Cleanest cut, but weakest
  provenance: a user landing on PyPI for `v0.1.1` and clicking "Source" finds no matching
  tag in the repo.

Backfilling preserves provenance (tags exist, point at commits with the right trees) and
populates `main`'s history with the actual releases. The cost is one operation today.

## 6. Why source v0.1.1 from `d09f314`, not from `develop`?

The two release PRs were not symmetric:

- **v0.1.2.** Tree of `fe881ef` (current `v0.1.2` tag on `main`) equals tree of
  `044594f` ("chore: release v0.1.2" on `develop`). The release PR contained no edits
  beyond the merge — develop already had the release-bump commit. A squash-merge of
  `044594f` onto a fresh `main` produces a byte-identical tree.
- **v0.1.1.** Tree of `d09f314` (current `v0.1.1` tag on `main`) differs from the
  closest equivalent on `develop` (`3f1ec99`). Four files diverge: `CHANGELOG.md`,
  `CLAUDE.md`, `CONTRIBUTING.md`, `src/lintle/__init__.py`. The release PR edited those
  files during the merge rather than landing the edits on `develop` first. So there is no
  commit on `develop` whose tree matches what shipped as `v0.1.1`.

The cleanest way to preserve `v0.1.1`'s shipped tree is to source the new release commit
from `d09f314` itself: `git checkout d09f314 -- .` into an empty index, then commit. The
resulting tree is identical to the original tagged tree by construction.

This asymmetry is a quiet artifact of the old workflow. Under the new workflow (release
housekeeping on `develop` first, then squash to `main`) the trees will stay symmetric and
back-filling like this will never be necessary again.

## 7. Procedure

### 7.1 Pre-flight (read-only)

```bash
git status                                  # working tree clean
gh pr list --state open                     # no open PRs targeting main
                                            # (if any exist: gh pr edit <N> --base develop)
git rev-parse d09f314^{tree}                # e8f1960…  v0.1.1 source tree
git rev-parse 044594f^{tree}                # ed5aa79…  v0.1.2 source tree
grep -A 3 "^on:" .github/workflows/*.yml    # publish.yml is workflow_dispatch — safe
gh api repos/elfensky/lintle/branches/main/protection  # 404 (no protection) — force-push safe
```

### 7.2 Tear down

```bash
# GitHub releases first — they are bound to tags
gh release delete v0.1.1 --yes --cleanup-tag=false
gh release delete v0.1.2 --yes --cleanup-tag=false

# Local tags
git tag -d v0.1.1 v0.1.2

# Remote tags
git push origin :refs/tags/v0.1.1 :refs/tags/v0.1.2

# main itself
git branch -D main
git push origin --delete main
```

### 7.3 Rebuild `main` as an orphan branch

```bash
git checkout --orphan main
git rm -rf .                          # empty the index/working tree

# v0.1.1 — source from the original tagged commit
git checkout d09f314 -- .
git add -A
git commit -m "Release v0.1.1"
git tag -a v0.1.1 -m "Release v0.1.1"

# v0.1.2 — source from develop's release-bump commit
git rm -rf .
git checkout 044594f -- .
git add -A
git commit -m "Release v0.1.2"
git tag -a v0.1.2 -m "Release v0.1.2"

# Tree-equality assertion — must hold before pushing
[ "$(git rev-parse v0.1.1^{tree})" = "e8f19600628e89df2622886db0a07d602b723668" ] \
    || { echo "v0.1.1 tree mismatch"; exit 1; }
[ "$(git rev-parse v0.1.2^{tree})" = "ed5aa79b204895e72093718453c3351c640c3945" ] \
    || { echo "v0.1.2 tree mismatch"; exit 1; }
```

Note on the tree SHAs above: `git rev-parse` printed
`e8f19600628e89df2622886db0a07d602b723668` and
`ed5aa79b204895e72093718453c3351c640c3945` during the design exploration. The assertions
use those full SHAs; do not shorten them in the actual run.

### 7.4 Push

```bash
git push --force-with-lease origin main
git push origin v0.1.1 v0.1.2
```

### 7.5 Recreate GitHub releases

```bash
gh release create v0.1.1 --title "v0.1.1" --notes-from-tag
gh release create v0.1.2 --title "v0.1.2" --notes-from-tag --latest
```

If `--notes-from-tag` produces empty or minimal notes, fall back to `--notes-file` sourced
from `CHANGELOG.md` (the relevant sections already exist there) or to hand-written notes
based on the original PR descriptions for #29 and #30.

### 7.6 Doc update (separate PR into `develop`)

This lands as the first PR under the new workflow — a working demonstration of the
contract. From a fresh feature branch off `develop`:

- `CLAUDE.md` § *Worktree Workflow*, step 1: replace `… main` with `… develop`.
- `CLAUDE.md` § *Worktree Workflow*, step 7: replace `git checkout main && git merge
  --no-ff …` with `git checkout develop && git merge --no-ff …`.
- `CLAUDE.md` § *Worktree Workflow* preamble: replace "Single trunk on `main`" with
  "Trunk is `develop`; `main` carries one squash-merge commit per release."
- `CLAUDE.md` § *Conventions*: rewrite the Git paragraph as: "`develop` is the trunk;
  `main` carries releases. Branch (`feature/`, `bugfix/`, `chore/`) off `develop` and PR
  back to `develop` with `--no-ff` (preserves branch history). Releases are squash-merges
  from `develop` to `main`, tagged on `main`."
- `CLAUDE.md` § *Conventions*: the line "Releases are annotated tags on `main`" stays —
  tags still live on `main`.
- `CONTRIBUTING.md`: same edits. Search for every `main` reference and update branching
  and release flow descriptions to match.

PR merges with `--no-ff` into `develop`.

## 8. The new workflow (the contract)

| Operation | Command |
|---|---|
| Start a feature | `git checkout develop && git pull && git checkout -b feature/<name>` |
| Land a feature | PR to `develop`; merge with `--no-ff` |
| Cut a release | `git checkout main && git merge --squash develop && git commit -m "Release vX.Y.Z" && git tag -a vX.Y.Z -m "Release vX.Y.Z"` |
| Publish | `gh release create vX.Y.Z`, then trigger `publish.yml` |
| Hotfix on release | Branch off `main` as `hotfix/<name>`, PR to `main` (squash), cherry-pick the fix onto `develop` |

The worktree workflow described in `CLAUDE.md` still applies — feature worktrees branch
off `develop` instead of `main`, and the merge-back target becomes `develop`. Nothing else
about worktree usage changes.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Force-push to `main` collides with concurrent work | `--force-with-lease` refuses if the remote ref moved; sole maintainer today |
| `gh release delete --cleanup-tag=false` flag unsupported in the installed `gh` version | Fall back to `gh release delete vX.Y.Z --yes` and then explicitly `git push origin :refs/tags/vX.Y.Z` — same end state |
| Doc and code drift while docs PR is in flight | Order: §7.1–§7.5 run as a single sitting; §7.6 lands via a normal PR within the same day |
| Open PRs targeting `main` get orphaned | §7.1 pre-flight checks for them; if any exist, `gh pr edit <N> --base develop` retargets before tear-down |
| New release commits drift from PyPI artifacts | §7.3 tree-equality assertion fails the run before any push if trees diverge |
| Orphaned `d09f314` / `fe881ef` interfere with later operations | They are unreachable but tagged-then-untagged commits; Git GC reaps them on the standard reflog cycle. No operational impact in the meantime. |
| `CHANGELOG.md` and release notes diverge | §7.5 uses `--notes-from-tag` with `CHANGELOG.md` fallback; both already contain the relevant text |

## 10. Out of scope

- Branch protection on `main` or `develop`. Not enabled today, not added by this change.
- Required CI status checks on PRs. Not configured today, not added by this change.
- Conventional Commits enforcement. The convention is documented; enforcement is a
  separate decision.
- Auto-release workflows (tag-triggered publish). `publish.yml` stays
  `workflow_dispatch`-only.

## 11. Verification

After §7.5 the following must be true:

```bash
# Branch shape
git log --oneline main           # exactly two commits: Release v0.1.2, Release v0.1.1
git rev-parse main^^             # should fail — only two commits, no parent below v0.1.1

# Tag shape
git tag -l                       # v0.1.1  v0.1.2
git rev-parse v0.1.1^{tree}      # e8f19600628e89df2622886db0a07d602b723668
git rev-parse v0.1.2^{tree}      # ed5aa79b204895e72093718453c3351c640c3945

# Remote state
gh release list                  # v0.1.1, v0.1.2 (v0.1.2 marked latest)
gh api repos/elfensky/lintle | jq -r .default_branch   # develop

# develop untouched
git log --oneline develop | wc -l   # still 23
git rev-parse develop               # still 044594f
```

After §7.6 the doc edits land on `develop` as a merge commit with `--no-ff`, visible in
`git log --oneline --graph develop`.
