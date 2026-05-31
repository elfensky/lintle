# `--report json` versioned envelope — Design

- **Date:** 2026-05-25
- **Status:** Implemented (issue #20, PR #52); **schema bumped to `"2"` 2026-05-31**
- **Revision:** **2026-05-31 — schema_version `"1"` → `"2"` (BREAKING):** the per-rule
  quarantine map was renamed `reject_counts` → `quarantine_counts` in both `summary` and
  `files[]`, as part of the corpus-wide "quarantine" terminology unification. The shape is
  otherwise unchanged. Consumers keying on `schema_version == "1"` / `reject_counts` must
  update. · initial.
- **Topic:** Replace the flat-array `--report json` output with a versioned envelope
  carrying run metadata, environment, corpus summary, and per-file timing.

## 1. Problem

`cli.py:540-541` currently dumps `[summary_dict(s) for s in all_stats]` — a flat array
with no top-level envelope. Real corpus runs need more for reproducibility and
comparison: per-file timing, tool/Python version, corpus-wide aggregates. Without
those, two runs against the same input cannot be compared programmatically beyond
per-rule counts, and there is no way for downstream automation to recognise the
schema version of a captured report.

## 2. Decision summary

A single top-level JSON object replaces the flat array. The shape is:

```json
{
  "schema_version": "2",
  "run":   { "command": "...", "timestamp": "...", "elapsed_seconds": 0.0 },
  "environment": { "tool_version": "...", "python_version": "..." },
  "summary": { "files_processed": 0, "paired_records": 0, "clean_count": 0,
               "quarantined_count": 0, "fix_counts": {}, "quarantine_counts": {} },
  "files": [ {"src_name": "...", "elapsed_seconds": 0.0, "bytes": 0,
              "records_per_sec": 0.0, ...} ]
}
```

`schema_version` is a string (now `"2"`) to leave room for non-numeric tags like `"2.1"`
in additive minor revisions. Adding optional fields stays under the current major; renaming
or removing a field bumps the major — which is exactly why the `reject_counts` →
`quarantine_counts` rename took it from `"1"` to `"2"`.

## 3. Field contract (normative)

| Field | Type | Required | Nullable | Invariants |
|---|---|---|---|---|
| `schema_version` | string | yes | no | Exactly `"2"` in this release |
| `run.command` | string | yes | no | `"validate"` or `"clean"` |
| `run.timestamp` | string | yes | no | ISO 8601 UTC, suffix `Z` (e.g. `2026-05-25T13:00:00Z`) |
| `run.elapsed_seconds` | float | yes | no | Parent-process wall-clock; `>= 0` |
| `environment.tool_version` | string | yes | no | `lintle.__version__` |
| `environment.python_version` | string | yes | no | `f"{sys.version_info.major}.{minor}.{micro}"` |
| `summary.files_processed` | int | yes | no | `== len(files)` |
| `summary.paired_records` | int | yes | no | Corpus-wide sum |
| `summary.orphan_entries` | int | yes | no | Corpus-wide sum |
| `summary.input_lines_seen` | int | yes | no | Corpus-wide sum |
| `summary.clean_count` | int | yes | no | Corpus-wide sum |
| `summary.quarantined_count` | int | yes | no | Corpus-wide sum |
| `summary.fix_counts` | object<str,int> | yes | no | `FixClass` StrEnum keys; empty `{}` when none |
| `summary.quarantine_counts` | object<str,int> | yes | no | `RuleID` StrEnum keys; empty `{}` when none |
| `files[].src_name` | string | yes | no | basename only (already a basename in `FileStats`) |
| `files[].elapsed_seconds` | float | yes | no | Per-file worker wall-clock; `>= 0` |
| `files[].bytes` | int | yes | no | `os.path.getsize(src_path)`; `>= 0` |
| `files[].records_per_sec` | float | yes | no | `paired_records / max(elapsed_seconds, 0.001)` |
| `files[].paired_records` | int | yes | no | |
| `files[].orphan_entries` | int | yes | no | |
| `files[].input_lines_seen` | int | yes | no | |
| `files[].clean_count` | int | yes | no | |
| `files[].quarantined_count` | int | yes | no | |
| `files[].fix_counts` | object<str,int> | yes | no | |
| `files[].quarantine_counts` | object<str,int> | yes | no | |
| `files[].dropped_counts` | object<str,int> | yes | no | issue #46 surface, may be `{}` |
| `files[].quarantined_norad_ids` | object<str,object<str,int>> | yes | no | issue #47 surface |

Every field is required and non-nullable. Empty maps render as `{}`, empty arrays
as `[]` — never omitted, never `null`. This rigid contract is the entire point of
the envelope: consumers can declare static types against it without sentinel-aware
parsing.

## 4. Timing semantics (critical)

Two independent measurements, never mixed:

- **`run.elapsed_seconds`** — parent process. `cli.main()` captures `time.monotonic()`
  before dispatching workers and again after the last future completes. This is the
  total wall-clock spent running `lintle`.
- **`files[i].elapsed_seconds`** — per worker. `pipeline.process_file` captures
  `time.monotonic()` at start and at successful completion. Each worker measures its
  own wall-clock duration on the file it processed.

**The two are not equal.** With `--jobs N` and parallel processing, the sum of
per-file durations exceeds parent wall-clock — that is expected and not corrected.
Downstream consumers wanting per-file throughput use `files[i].records_per_sec`;
consumers wanting end-to-end wall time use `run.elapsed_seconds`. Never sum
`files[i].elapsed_seconds` to derive a corpus-wide duration.

## 5. `records_per_sec` clamp

The denominator is clamped: `paired_records / max(elapsed_seconds, 0.001)`. This
keeps the field a stable `float` regardless of how briefly a file processed — a
1-byte test fixture won't divide by zero, and statically-typed consumers (Go
unmarshalers, strict TypeScript) never see `null`. The 0.001 floor is documented
as the lower bound; sub-millisecond files report an upper-bound throughput rather
than an undefined one.

## 6. Privacy

The envelope strictly contains:

- Tool version, Python version.
- The CLI subcommand name.
- The wall-clock timestamp.
- File **basenames** (never absolute paths — `FileStats.src_name` is already
  basename-only).
- Numeric counts.

No environment variables, no hostnames, no usernames, no absolute paths. This is
a hard rule, validated by tests.

## 7. Migration

Clean break: the prior flat-array output is removed. There is no `--report
json-legacy` flag; consumers upgrade together. The change lands as a feature
commit on a `feature/report-json-envelope` branch and merges into `develop`.

`CHANGELOG.md`'s `Unreleased` block records the breaking change. The version
bump happens at release-cut time per `CONTRIBUTING.md`, not per feature merge.

## 8. Out of scope (deferred)

- **Resumable manifest** — no `status` field in v1. The resumable runs design
  (`2026-05-21-tle-corpus-cleaner-design.md` §13) tracks this. Until that ships,
  every file is processed; emitting a fake `"status": "processed"` would commit
  the schema to a state machine that does not exist.
- **JSON Schema validation** — this design doc + golden fixtures
  (`tests/fixtures/report-envelope-v1.golden.json`) define the contract. No
  `jsonschema` dependency.
- **Per-rule trends across runs** — out of scope; the report is a single-run
  snapshot.
- **Streaming for massive corpora** — single in-memory `json.dumps`. The
  envelope's `files` array scales linearly with file count. For runs over ~10⁵
  files the existing per-record `report.jsonl` streaming format is the right
  vehicle.

## 9. Implementation

- `src/lintle/report.py` — add `elapsed_seconds: float` and `bytes: int` fields
  to `FileStats`. `summary_dict()` returns the per-file shape extended with
  `elapsed_seconds`, `bytes`, `records_per_sec`. New `build_run_envelope(...)`
  assembles the top-level dict.
- `src/lintle/pipeline.py` — `process_file` captures `time.monotonic()` and
  `os.path.getsize()`, populating `stats.elapsed_seconds` and `stats.bytes`
  before returning.
- `src/lintle/cli.py` — capture parent-process wall-clock around the worker
  dispatch. Replace `print(json.dumps([summary_dict(s) for s in all_stats]))`
  with `print(json.dumps(build_run_envelope(all_stats, command=..., started_at=..., elapsed_seconds=...)))`.
- `tests/test_report.py` — `TestBuildRunEnvelope`, `TestRecordsPerSec`,
  `TestEnvelopeSchemaLock`, and a golden-fixture comparison.

## 10. Validation

`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` — must
pass before merge. `lintle validate --report json | python -c 'import sys, json;
json.load(sys.stdin)'` confirms the wire output is parseable JSON.
