---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# External Integrations

**Analysis Date:** 2026-11-02

## Summary

`lintle` is an offline CLI. It has no network client, no API calls, no database, and no auth provider. Its only external inputs and outputs are local files and the PyPI publish path in CI.

## APIs & External Services

**Network APIs called by the code:**
- None detected. No `urllib`, `http`, `requests`, or socket client usage in `src/lintle/`. The `socket` import in `src/lintle/fsutil.py` calls `socket.gethostname()` only, to record the run host.

**Upstream data source (documented, not called):**
- space-track.org - the origin of the TLE input files. The code reads local `tle*.txt` files and `TLEs.zip` from the source directory (`data/source/` by convention). See `docs/space-track-defect-report.md` for known upstream defects.
- Input layout and validation rules: `src/lintle/tle.py`.

**Physics library (local, not a network service):**
- `sgp4` 2.27 - SGP4 propagation, used only by `src/lintle/verify/orbit.py` when `lintle verify --orbit` runs. Lazily imported. Not used in clean, validate, or repair.

## Data Storage

**Databases:**
- None. No ORM, no SQL client, no migrations.

**File Storage:**
- Local filesystem only.
- Inputs: `data/source/` (raw `tle*.txt`, `TLEs.zip`). Read by streaming in `src/lintle/pipeline.py`. Files are never loaded whole.
- Outputs: `data/output/` numbered tree (`01-cleaned/`, `02-broken/`, `03-report/`, `04-verify/`, `05-dedup/`, `06-extract/`). Written as chunk sets `<stem>.NNNNN.<suffix>` by `src/lintle/chunking.py`.
- Atomic durable commits: `src/lintle/fsutil.py` (`durable_replace`, `fsync`, `F_FULLFSYNC` on macOS).
- Out-dir lock: advisory `fcntl.flock` on `.clean.lock` in the output directory (`src/lintle/fsutil.py`).
- Run checkpoint for `clean --resume`: `src/lintle/resume.py`.
- Per-user config: `./.lintle.json` (`src/lintle/config.py`). Git-ignored.

**Caching:**
- None.

## Authentication & Identity

**Auth Provider:**
- None for the CLI.
- PyPI publish: OIDC Trusted Publishing from GitHub Actions (`.github/workflows/publish.yml`, `permissions: id-token: write`). No stored tokens. Environment name `pypi`.

## Monitoring & Observability

**Error Tracking:**
- None. No Sentry or similar SDK in `pyproject.toml` or `uv.lock`.

**Logs:**
- Human output to stderr and stdout via `rich` consoles (`src/lintle/term.py`).
- Structured output: `report.json`, `report.jsonl`, `broken-noradids.ndjson`, `*.broken.txt` sidecars, `manifest.jsonl`, and `summary.{json,md}` written by `src/lintle/report.py`, `src/lintle/report_writers.py`, `src/lintle/output_artifacts.py`, `src/lintle/verify/report.py`. Byte-deterministic when unstyled.
- Debug: `LINTLE_DEBUG` env var (`src/lintle/cli.py`).

## CI/CD & Deployment

**Hosting:**
- Source: GitHub (`https://github.com/elfensky/lintle`, from `pyproject.toml` `[project.urls]`).
- Package index: PyPI (`https://pypi.org/project/lintle/`) and TestPyPI (`https://test.pypi.org/project/lintle/`).

**CI Pipeline:**
- GitHub Actions. Workflows listed in `.github/workflows/`:
  - `ci.yml` - lint and test on `develop` and `main`
  - `publish.yml` - TestPyPI on push to `main`; PyPI on manual dispatch with `target=pypi`
  - `version.yml` - version-bump gate on PRs to `main`
- Dependency update bot: Dependabot (`.github/dependabot.yml`), weekly, `uv` and `github-actions` ecosystems.
- Package index config: `[[tool.uv.index]] testpypi` in `pyproject.toml`, `explicit = true`. Used only by `uv publish --index testpypi`.

## Environment Configuration

**Required env vars:**
- None.

**Optional env vars:**
- `LINTLE_DEBUG` - full tracebacks on error
- `CI`, `NONINTERACTIVE` - disable interactive prompts

**Secrets location:**
- No application secrets. CI publish uses OIDC, not secrets.
- Local `.env` files: not used by the code. Not read by this analysis.

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

## Corpus Input Notes

- Input files come from space-track.org exports. The corpus is about 30 GB and is git-ignored under `data/`.
- Do not read corpus files whole. Use `head`, `awk`, or `sed -n` (per `CLAUDE.md`).
- Known upstream issue: about 15 percent of records in some exports lack the column 69 checksum digit. Use `--reconstruct-checksum` (see project memory and `docs/space-track-defect-report.md`).

---

*Integration audit: 2026-11-02*
