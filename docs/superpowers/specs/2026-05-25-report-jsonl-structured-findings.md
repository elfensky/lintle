# Structured Findings — `report.jsonl` Emission — Design

- **Date:** 2026-05-25
- **Status:** Draft; post-adversarial-review; ready for implementation planning
- **Revision:** **2026-05-25:** applied findings from a multi-AI adversarial
  review (Codex + Gemini). §4.1 adds `outcome: "quarantined"` for forward
  compat with future fix-entry emission and standardizes `note` to JSON null
  when empty (consumers see uniform null-vs-string semantics). §4.3 pins
  `encoding="utf-8"`, `newline="\n"`, and `sort_keys=True` for
  byte-determinism. §4.5 pins `RejectEntry.norad_id` as the trailing field
  (the existing positional construction in `pipeline._record_reject` would
  silently corrupt arguments otherwise). §4.6 mandates pre-run shard-dir
  scrub and references the existing `_detect_basename_collisions` safeguard.
  §7 discloses the cleaned-vs-broken publish-order hazard (inherited from
  the existing `.broken.txt` design) and weakens the SIGINT crash-recovery
  guarantee to match `cli._terminate_workers` reality. §8 adds forced-I/O
  failure tests, a `RejectEntry` constructor regression test, a
  `summary_dict` no-new-keys lock, and a stale-shard contamination test.
  §11 gains a fourth deliberate departure (the `outcome` field).
- **Topic:** Issue #9 — emit `report.jsonl` alongside `report.md` during
  `lintle clean`, with one JSON object per quarantined record citing its
  stable `RuleID`. The catalog of rejections becomes a structured,
  machine-readable product so downstream automation (CI gates, defect-drift
  tracking, `lintle diff`, space-track defect reports) can consume it
  without parsing prose.

## 1. Problem statement

`lintle clean` currently produces three human-readable artifacts and one
machine-readable artifact:

| Artifact | Audience | Form |
|----------|----------|------|
| `cleaned/<stem>.cleaned.txt` | downstream SGP4 ingest | clean ASCII TLE |
| `broken/<stem>.broken.txt` | analyst reading rejections | indexed prose, byte-faithful raw lines |
| `report.md` | analyst reviewing a run | Markdown summary tables |
| `broken-noradids.ndjson` | downstream filters | NDJSON, one `{"noradId": N}` per quarantined satellite |

The `broken-noradids.ndjson` artifact answers *"which satellites had any
rejections in this run?"* — a useful but coarse signal. It does **not**
answer:

- Which rules fired, with what counts per file?
- What were the `observed` / `expected` values on each finding?
- Which source lines should an operator inspect?
- Did the cleaner attempt a repair tier before quarantining, and which?

Today those answers live only inside `.broken.txt` prose blocks and
`report.md` tables — both authored for humans. A CI bot, a drift dashboard,
or a `lintle diff` implementation has to parse English to extract them,
which is fragile and rots whenever the prose is reworded for clarity.

The structured form already exists internally: `RejectSink.add` (`src/lintle/report.py:303`)
receives a `RejectEntry` carrying a `Diagnostic` with `rule_id`, `source_line_nos`,
`tier_attempted`, `column_range`, `observed`, `expected`, and `note`. That data
flows through the streaming `.broken.txt` writer into ASCII prose and is then
lost from the operator-facing artifact set. This spec extends the same `add()`
callsite with a parallel emitter that preserves the structured fields verbatim.

## 2. Goal & non-goals

**Goal.** Emit `<out_dir>/report.jsonl` after every `lintle clean` run, with
one JSON object per quarantined `RejectEntry`. Every field that downstream
automation needs to triage findings is a first-class JSON value, keyed by
the stable rule-ID registry. The file is *always* present after a successful
`clean` run — empty when zero records were quarantined, parallel to
`broken-noradids.ndjson`'s contract.

**Non-goals — explicitly excluded:**

| Excluded | Rationale |
|----------|-----------|
| Emitting raw line bytes inside `report.jsonl` | The byte-faithful catalog lives in `broken/*.broken.txt`. JSON-encoding raw TLE lines forces an encoding choice (base64 multiplies disk by ~16x; `errors="replace"` is lossy on actual non-ASCII bytes), duplicates information already on disk, and bloats the streaming write. Findings cite `file` + `source_lines` — consumers that need the raw bytes already know where to look. The issue's schema sketch includes `"raw": "..."`; this spec deliberately omits it (see §11). |
| Emitting successful-repair entries | Issue #9 frames the catalog as "rejections" — successful fixes are tallied in `fix_counts` and surfaced in `report.md`'s "Fixes applied" table. Adding fix entries would inflate the file by ~190M lines on a typical corpus run with no obvious consumer. Tracked as a follow-up (§10). |
| Emitting in `validate` mode | Issue #9 specifies `clean`. `validate` mode owns no `--out-dir` artifacts today; adding one would expand its contract. Tracked as a follow-up (§10). |
| Per-file `report.jsonl` sidecars in `broken/` | The user-facing artifact is one canonical file. Per-file shards are an implementation detail of the streaming pipeline (§4.3) and are cleaned up at end-of-run. |
| SARIF / OASIS-format output | SARIF is the right format for IDE/CI integration but is dramatically heavier (~10x line count, nested rules/results/locations sections). Out of scope for v1; a SARIF emitter could layer on top of `report.jsonl` later without re-instrumenting the pipeline. |
| CLI flags to filter or limit emission | YAGNI. `report.jsonl` is `jq`-shaped — downstream consumers filter with `jq`, `grep`, or `lintle diff` (issue #10). |
| Threshold gating (`--fail-on RULE-ID=N`) | That is issue #13, which will consume `report.jsonl` as its input. Belongs on issue #13's spec. |

## 3. Constraints inherited from the authoritative spec

The corpus-cleaner design (`2026-05-21-tle-corpus-cleaner-design.md`) and
`CLAUDE.md`'s critical rules bind this change:

- **Constant memory.** The new emitter streams per finding via the existing
  `RejectSink.add` callsite. No accumulator buffers entries; per-worker
  shards write straight to disk. Total in-memory footprint per file
  unchanged (the existing 5-per-rule sample); aggregate footprint adds at
  most one open file handle per worker.
- **One validator definition.** This spec is an emitter — `tle.py` is not
  modified. The `rule_id` values on every JSONL line come from
  `diagnostics.RULES`; no parallel taxonomy is introduced.
- **Correctness over recovery.** Findings emitted to `report.jsonl` are
  exactly those `Diagnostic` objects that reached `RejectSink.add` — the
  same set that lands in `.broken.txt` and tallies into `reject_counts`.
  No reinterpretation, no synthesis.
- **Validated transformation.** Untouched — no repair behavior changes.
- **Atomicity.** Final `report.jsonl` is written via tmp-file +
  `os.replace`, matching `.cleaned.txt`'s atomicity discipline. Interrupted
  runs never publish a half-written `report.jsonl`.

This spec also inherits the existing rule-ID stability contract from
[`2026-05-24-stable-rule-id-registry-design.md`](2026-05-24-stable-rule-id-registry-design.md):
`RuleID` values are forever; retired IDs stay in the enum. `report.jsonl`'s
`rule_id` field directly exposes that contract to downstream consumers.

## 4. Design

### 4.1 Wire schema — one JSON object per `RejectEntry`

```json
{
  "schema_version": "1",
  "outcome": "quarantined",
  "file": "tle2022.txt",
  "rule_id": "TLE-CHK-001",
  "source_lines": [12345, 12346],
  "tier_attempted": "none",
  "norad_id": 25544,
  "column_range": [69, 69],
  "observed": "0",
  "expected": "3",
  "note": null,
  "related": []
}
```

Field contracts:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `schema_version` | string | yes | `"1"` for this design. Bumped on any non-additive change. Consumers MUST check; producers MAY ship additive fields without a bump. |
| `outcome` | string | yes | Always `"quarantined"` in v1. Forward-compat field: future versions may add `"fixed"` (issue #9 follow-up) so consumers can filter by outcome from day one without parsing absence semantics. New outcome values are additive (no version bump). |
| `file` | string | yes | Source-file basename (matches `FileStats.src_name`). Always unambiguous within a run because `cli._detect_basename_collisions` (`cli.py:73`) refuses runs whose inputs share basenames. Consumers wanting the original full path resolve `file` against the `args.paths` recorded in the run's `report.md` header — out of scope for v1 but the basename contract is stable for that lookup. |
| `rule_id` | string | yes | Stable `RuleID` value (`"TLE-CHK-001"`, …). |
| `source_lines` | int[] | yes | 1-indexed source line numbers. Length 1 for orphans; length 2 for paired records. |
| `tier_attempted` | string | yes | `RepairTier` value: `"none"`, `"tier-1"`, `"tier-2"`. |
| `norad_id` | int \| null | yes | 5-digit catalog ID extracted from line-1 cols 3-7, or `null` when line 1 is unreadable (orphan-line-2, bad-prefix). |
| `column_range` | [int, int] \| null | yes | 1-indexed inclusive column range where the defect lives, or `null` when not column-localized (pairing failures, internal errors). |
| `observed` | string \| null | yes | What was found (≤16 chars; truncation marker `...` if cut at construction). |
| `expected` | string \| null | yes | What was expected (≤16 chars). |
| `note` | string \| null | yes | Free-text detail (≤80 chars, ASCII-printable). `null` when the source `Diagnostic.note` is the empty string — the renderer coerces `""` → `null` so consumers see uniform null-vs-string semantics across `note`, `observed`, and `expected` (Gemini's review surfaced the asymmetry). |
| `related` | object[] | yes | Secondary diagnostics, in the same shape minus the envelope fields (`schema_version`, `outcome`, `file`, `norad_id`). Empty array when none. |

All fields are present on every line (no missing keys). Optional values are
JSON `null` (or `[]` for arrays) rather than omitted, so streaming consumers
can use a fixed parser and `jq` queries don't need `// empty` guards.

The `related` object shape:

```json
{
  "rule_id": "TLE-COL-001",
  "source_lines": [12345],
  "tier_attempted": "tier-1",
  "column_range": null,
  "observed": null,
  "expected": null,
  "note": "line length 70 not 69"
}
```

Related entries carry their own `source_lines` because a paired record can
produce diagnostics rooted on either physical line; the envelope's
`source_lines` covers the entry's full span.

### 4.2 Construction — `diagnostic_to_dict`

A pure function in `report.py` converts a `Diagnostic` to its JSON-ready
dict. Two forms — envelope (primary) and nested (related) — so the
schema-version and per-file fields appear exactly once per JSONL line:

```python
def _diagnostic_to_nested(diag: Diagnostic) -> dict:
    """Render one Diagnostic as the nested shape used inside ``related``."""
    return {
        "rule_id": diag.rule_id.value,
        "source_lines": list(diag.source_line_nos),
        "tier_attempted": diag.tier_attempted.value,
        "column_range": list(diag.column_range) if diag.column_range else None,
        "observed": diag.observed,
        "expected": diag.expected,
        # Coerce the internal "" sentinel to JSON null so the wire format
        # is uniform across the three optional string fields. The internal
        # Diagnostic keeps "" because dataclass defaults are simpler that
        # way; the JSON boundary is where we standardize.
        "note": diag.note or None,
    }


def entry_to_jsonl_dict(entry: RejectEntry, *, file: str, norad_id: int | None) -> dict:
    """Render one RejectEntry as the envelope shape used in report.jsonl."""
    nested = _diagnostic_to_nested(entry.primary)
    return {
        "schema_version": "1",
        "outcome": "quarantined",
        "file": file,
        "norad_id": norad_id,
        **nested,
        "related": [_diagnostic_to_nested(d) for d in entry.related],
    }
```

`RuleID` and `RepairTier` are `StrEnum`, so `.value` yields the stable
wire token (`"TLE-CHK-001"`, `"tier-1"`). Tuples become lists (JSON has no
tuple type). The 16/80-char bounds from `Diagnostic.__post_init__`
guarantee `observed`/`expected`/`note` are already size-safe — no
re-bounding here.

### 4.3 Streaming integration — `JsonlFindingsWriter`

A new class in `report.py`, mirroring `BrokenFileWriter`'s pattern but with
JSON-line output:

```python
class JsonlFindingsWriter:
    """Streaming writer for one file's findings shard.

    Each ``write_entry`` call emits one line:
    ``json.dumps(payload, separators=(",", ":"), sort_keys=True,
    ensure_ascii=False) + "\\n"`` — compact (no whitespace), key-sorted (so
    byte output is deterministic across Python dict-iteration changes and
    refactors), and UTF-8 (``ensure_ascii=False`` keeps any non-ASCII
    pass-through compact rather than ``\\uXXXX``-encoded; in practice every
    field is ASCII because ``Diagnostic`` already replaces non-printables).
    The underlying file is opened with ``encoding="utf-8"`` and
    ``newline="\\n"`` so the artifact is byte-deterministic across
    platforms (Windows would otherwise translate ``\\n`` → ``\\r\\n``).

    Writes go to ``<out_dir>/.shards/<stem>.findings.jsonl.partial``; on
    ``finalize`` the partial is atomically renamed to
    ``<stem>.findings.jsonl``. Use as a context manager so a worker
    interrupted between ``write_entry`` and ``finalize`` leaves only the
    ``.partial`` behind — which the next run's shard-dir scrub (§4.6)
    removes before any new writer opens.
    """
```

`RejectSink.__init__` gains an optional `jsonl_path: str | None` parameter
and a `src_name`/`extract_norad_id` shim so it can build the JSONL dict
per `add()`. When `jsonl_path` is set, the sink owns a
`JsonlFindingsWriter`'s lifecycle alongside its existing
`BrokenFileWriter`:

```python
class RejectSink:
    def __init__(self, *, cap=..., broken_path=None, src_name=None,
                 jsonl_path=None):
        ...
        if jsonl_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when jsonl_path is set")
            self._jsonl_writer = JsonlFindingsWriter(jsonl_path, src_name)
        else:
            self._jsonl_writer = None
    ...
    def add(self, entry):
        ...                                  # existing cap/buckets logic
        if self._writer is not None:
            self._writer.write_entry(entry)
        if self._jsonl_writer is not None:
            self._jsonl_writer.write_entry(entry)
```

Dropped-from-sample entries (cap exceeded) still write to the JSONL stream —
the cap governs only the in-memory sample, not the on-disk catalog. This
matches `.broken.txt`'s existing behavior.

`RejectSink.__enter__` / `__exit__` propagate to both writers; `finalize`
calls both writers' `finalize` in sequence (broken first, then JSONL — same
order means a crash midway leaves at most one of the two unfinalized,
and the abnormal-exit branch in each writer's `__exit__` cleans its own
partials independently).

### 4.4 Pipeline change — `pipeline._run`

One added line to build the JSONL path and pass it to `RejectSink`:

```python
if mode == "clean":
    ...
    broken_path = os.path.join(out_dir, "broken", stem(src_name) + ".broken.txt")
    shard_dir = os.path.join(out_dir, ".shards")
    os.makedirs(shard_dir, exist_ok=True)
    jsonl_path = os.path.join(shard_dir, stem(src_name) + ".findings.jsonl")

sink = report.RejectSink(
    broken_path=broken_path, src_name=src_name, jsonl_path=jsonl_path,
)
```

In `validate` mode, `jsonl_path` stays `None` — the sink is purely
in-memory, same as today.

### 4.5 NORAD ID resolution at write time

`_record_reject` (`pipeline.py:271`) already calls
`tle.extract_norad_id(raw_lines[0])` to populate the per-NORAD breakdown.
The same value is what `report.jsonl`'s `norad_id` field needs.

The cleanest factoring: `RejectEntry` gains an optional `norad_id: int | None`
field, populated by `_record_reject` once at insert time, and read by
`JsonlFindingsWriter.write_entry`. Single decode per entry; no duplicate
parse. The new field is internal-only — it is NOT rendered into
`.broken.txt` (the existing prose format makes no commitment to NORAD ID
visibility and the existing format-lock tests would fail if we added it).

```python
@dataclasses.dataclass
class RejectEntry:
    raw_lines: list
    source_lines: list
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()
    norad_id: int | None = None  # MUST be the trailing field — see below
```

**Field order constraint.** The production call site
(`pipeline._record_reject`, `pipeline.py:299`) constructs `RejectEntry`
positionally:

```python
entry = report.RejectEntry(raw_lines, source_lines, primary, related)
```

If `norad_id` is inserted anywhere except after `related` in the dataclass,
this positional call silently passes the wrong value into the wrong slot
(e.g. `norad_id` would receive `related` if placed before it, and `related`
would become its default empty tuple). The implementer MUST keep
`norad_id` as the last field and update the call site to:

```python
entry = report.RejectEntry(
    raw_lines, source_lines, primary, related, norad_id=norad_id,
)
```

Adding the kwarg here also doubles as documentation that the field is
populated at this site and only at this site. Tests construct
`RejectEntry` exclusively via kwargs (verified across 18 call sites in
`tests/test_report.py`), so an appended optional trailing field with a
`None` default does not break any existing fixture.

### 4.6 Aggregation — `cli.main` end-of-run concat

After all workers finish and `all_stats` is sorted by `src_name` (existing
behavior at `cli.py:510`), the main process concatenates shards into the
final artifact:

```python
if args.command == "clean" and all_stats:
    report_path = os.path.join(args.out_dir, "report.md")
    report.write_run_report(report_path, all_stats)
    noradids_path = os.path.join(args.out_dir, "broken-noradids.ndjson")
    report.write_broken_noradids_ndjson(noradids_path, all_stats)
    findings_path = os.path.join(args.out_dir, "report.jsonl")
    report.concat_findings_shards(args.out_dir, findings_path, all_stats)
```

**Pre-run shard-dir scrub.** Before any worker writes a shard, the main
process MUST purge any leftover `.shards/` from a prior aborted run. This
is essential because (a) `os.makedirs(exist_ok=True)` preserves existing
contents, (b) SIGINT terminates workers outright via `_terminate_workers`
(`cli.py:496`) so context-manager cleanup is not guaranteed to fire, and
(c) `concat_findings_shards`'s own best-effort `shutil.rmtree` may fail
silently on a write error. Without the pre-run scrub, a finalized shard
from a previous run for `tle2022.txt` could be concatenated into the
current run's `report.jsonl` even though the current run does not process
`tle2022.txt`. This is added in `cli.py` immediately after the `out_dir`
is resolved, before the worker pool starts:

```python
shard_dir = os.path.join(args.out_dir, ".shards")
if os.path.exists(shard_dir):
    shutil.rmtree(shard_dir)
```

`concat_findings_shards` in `report.py`:

```python
def concat_findings_shards(out_dir, dest_path, all_stats):
    """Concatenate per-file findings shards into the corpus-wide report.jsonl.

    Shards live in ``<out_dir>/.shards/<stem>.findings.jsonl``, written by
    each worker's RejectSink. We walk ``all_stats`` (already sorted by
    ``src_name``) so the concatenated order is deterministic and matches
    the per-file table in report.md. After concat the shard directory is
    removed in its entirety. Always creates ``dest_path`` even when every
    shard is empty — matches ``broken-noradids.ndjson``'s contract that
    the artifact is always present after a successful clean run.
    """
    shard_dir = os.path.join(out_dir, ".shards")
    tmp_path = dest_path + ".partial"
    with open(tmp_path, "wb") as out:
        for stats in all_stats:
            shard = os.path.join(shard_dir, stem(stats.src_name) + ".findings.jsonl")
            if not os.path.exists(shard):
                continue  # worker crashed before finalize, or future opt-in
                          # mode where empty shards are suppressed
            with open(shard, "rb") as src:
                shutil.copyfileobj(src, out, length=65536)
    os.replace(tmp_path, dest_path)
    with contextlib.suppress(OSError):
        shutil.rmtree(shard_dir)
```

Zero-reject files DO produce a shard in v1 — an empty one — so the
existence check above is defensive (covers the worker-crash path). The
tmp-file + `os.replace` is the existing atomicity pattern.

**Basename collision safeguard.** Shard names use `stem(src_name)`, so
two inputs with the same basename would race on the same shard path.
This is impossible at run time because `_detect_basename_collisions`
(`cli.py:73-94`) already refuses such runs *before* any output is
written, with a user-facing error pointing at `--out-dir`. The shard
naming inherits that safeguard for free; no additional guard is needed
in `pipeline.py` or `report.py`.

### 4.7 CLI surface — no new flags

`report.jsonl` is always written in `clean` mode if any file was processed,
parallel to `broken-noradids.ndjson`. The post-run summary print at
`cli.py:531-534` gains one line announcing the artifact:

```python
if report_path:
    print(f"\nrun report: {report_path}")
if noradids_path:
    print(f"broken NORAD IDs: {noradids_path}")
if findings_path:
    print(f"findings: {findings_path}")
```

`--report json` (existing) is unchanged — it prints per-file
`summary_dict` results to stdout for tooling that wants the aggregate
counters. `report.jsonl` is the *per-finding* artifact, written to disk,
suitable for `jq` queries and `lintle diff`.

## 5. Schema stability and evolution

**Forever:** `schema_version`, `file`, `rule_id`, `source_lines`. Removing
or repurposing any of these requires a major schema-version bump.

**Stable but extensible:** the `tier_attempted` enum vocabulary, the
`column_range` shape, the `RuleID` value set. Adding new enum values or
new `RuleID`s is non-breaking — downstream parsers must tolerate unknown
values (treat as opaque strings).

**Schema-version bump rules:**

| Change | Version action |
|--------|----------------|
| Add a new optional field | none — `"1"` stays |
| Add a new `RuleID` enum value | none |
| Add a new `RepairTier` enum value | none |
| Rename a field | bump to `"2"` |
| Change a field's type or semantic | bump to `"2"` |
| Remove a field | bump to `"2"` (also: leave the old `RuleID`-style "deprecated_for" trail in spec) |

Consumers SHOULD check `schema_version == "1"` and fail loudly on
unrecognized values rather than try to parse forward. The corollary:
breaking changes accumulate at the major bump; minor improvements ship
freely.

## 6. Output ordering and byte-determinism

Per-file order: `RejectEntry`s are emitted in encounter order — the order
in which `RejectSink.add` is called from `pipeline._run`'s record loop.
This matches the existing `.broken.txt` order exactly.

Corpus order: `concat_findings_shards` walks `all_stats` (sorted by
`src_name` at `cli.py:510`), so the concatenated `report.jsonl` is
alphabetical by source filename.

Within each JSON object, `json.dumps(..., sort_keys=True)` guarantees keys
are emitted in lexicographic order regardless of how
`entry_to_jsonl_dict` constructs the underlying dict. Combined with
LF-only line endings (`newline="\n"`) and compact separators
(`(",", ":")`), this means a run on the same inputs produces a
byte-identical `report.jsonl`, making cross-run diffs meaningful (issue
#10's premise) and enabling content-hash-based caching by downstream
consumers.

## 7. Error handling

| Edge case | Behavior |
|-----------|----------|
| Zero quarantines in a file | The shard file exists but is empty. Concat appends zero bytes. |
| Zero quarantines corpus-wide | `report.jsonl` exists, is empty. Matches `broken-noradids.ndjson`'s zero-quarantine contract (empty file always present). |
| **Zero files processed** (`all_stats` is empty: no inputs, all inputs failed, or `validate` mode) | `report.jsonl` is **not** written. This matches the existing `if args.command == "clean" and all_stats:` guard at `cli.py:518` for `report.md` and `broken-noradids.ndjson`. The artifact's "always present" contract holds only when at least one file processed successfully. |
| Worker crashes during ordinary execution (raises an exception inside `_run`) | `JsonlFindingsWriter.__exit__`'s abnormal-exit branch unlinks the `.partial`. Concat sees no shard for that file. The pipeline already classifies this run as `failed_files` → exit code 2 (`cli.py:538`). |
| **Worker killed by `_terminate_workers` (SIGINT / Ctrl-C)** | The Python interpreter is terminated outright (`cli.py:496`). Context-manager `__exit__` does **not** run. The shard's `.partial` remains on disk; a finalized `.findings.jsonl` from a previous file in the same worker also remains. The pre-run scrub in §4.6 is what recovers from this — it is not optional, it is the cleanup path. The current run after Ctrl-C exits with code 130 and writes no `report.jsonl` (`all_stats` may be partial; existing `interrupted` branch returns early at `cli.py:507-508`). |
| **Publish-order hazard between `.cleaned.txt` and reject artifacts** | Inherited from the pre-existing pipeline: `os.replace(cleaned_tmp, cleaned_path)` (`pipeline.py:262`) publishes the cleaned file *before* `sink.finalize` (line 264) finishes writing `.broken.txt` and (now) the JSONL shard. If finalize raises, the cleaned file is already visible but reject artifacts are incomplete. This is not introduced by this spec — it is the same hazard `.broken.txt` already has — but the spec doubles the surface area. Mitigation deferred to a separate cleanup task: future work could move `os.replace(cleaned_tmp, ...)` after `sink.finalize`. Tracked as a follow-up (§10). |
| Concat write fails midway | `report.jsonl.partial` is left behind; `report.jsonl` is unchanged from the prior run (if any). `.shards/` is **not** deleted on concat failure. The next run's pre-run scrub purges both. |
| User passes `--out-dir` to a non-empty directory | Existing behavior — files overwrite. The pre-run `.shards/` scrub (§4.6) handles leftover shards explicitly; `report.jsonl.partial` from a prior failed concat is overwritten by the new tmp-file write. |
| `RuleID.INTERNAL_ERROR` rejects | Emitted into `report.jsonl` like any other rule, with `column_range: null` and `note` carrying `repr(exc)` (already truncated to 80 chars in the diagnostic helper). Surfaces cleaner bugs in the structured stream alongside data defects. |
| Concurrent runs sharing `--out-dir` | Same race today: two `lintle clean` runs writing the same `--out-dir` already collide on `cleaned/` and `broken/`. Adding `report.jsonl` does not change the recommendation (use per-worktree `--out-dir`, per `CLAUDE.md` § Worktree Workflow). The pre-run shard scrub makes the collision strictly more visible (one run scrubs the other's shards mid-flight) — same severity, easier to diagnose. |

## 8. Testing

Test-driven: tests are added first for the new behaviors, then made to
pass. The build order (§9) keeps each step independently verifiable.

### 8.1 Diagnostics rendering (`tests/test_report.py`)

A new `TestEntryToJsonlDict` class:

- `test_envelope_carries_required_fields` — populate a `RejectEntry` with
  one primary `Diagnostic` and zero related; assert the resulting dict has
  exactly the documented keys with the documented types (`schema_version
  == "1"`, `file`, `rule_id`, `source_lines`, `tier_attempted`, `norad_id`,
  `column_range`, `observed`, `expected`, `note`, `related`).
- `test_related_diagnostics_nested` — populate one primary + two related;
  assert `related` is a length-2 list with each element matching the
  nested shape (no envelope fields).
- `test_strenum_values_render_as_strings` — assert `rule_id` is `"TLE-CHK-001"`
  (the string value), not `"RuleID.CHECKSUM_MISMATCH"` (the enum repr).
  Same for `tier_attempted`.
- `test_tuples_become_lists` — `source_lines` and `column_range` are lists
  in the output (JSON has no tuple type).
- `test_none_fields_stay_none` — `column_range=None`, `observed=None`,
  `expected=None` render as JSON `null` (Python `None`), not dropped keys.
- `test_norad_id_null_when_unreadable` — a `RejectEntry` constructed by
  the orphan-line-2 path (`norad_id=None`) renders `"norad_id": null`.

### 8.2 JsonlFindingsWriter (`tests/test_report.py`)

A new `TestJsonlFindingsWriter` class mirroring `TestBrokenFileWriter`:

- `test_writes_one_line_per_entry` — write 3 entries; assert the finalized
  file has exactly 3 lines, each parseable with `json.loads`.
- `test_compact_json_no_whitespace` — assert no spaces around separators
  (`json.dumps(..., separators=(",", ":"))`), so line counts equal entry
  counts even when grep is used on the artifact.
- `test_finalize_atomic_rename` — write entries, finalize, assert the
  final path exists and the `.partial` does not.
- `test_interrupted_run_leaves_no_partial` — write entries, exit context
  manager without finalizing; assert the `.partial` is gone and the
  final path was never created.
- `test_empty_finalize_creates_empty_file` — finalize with zero entries;
  assert the final path exists with zero bytes.

### 8.3 RejectSink integration (`tests/test_report.py`, `tests/test_pipeline.py`)

- `test_sink_streams_to_jsonl_when_path_given` — construct a
  `RejectSink(jsonl_path=...)`, push 3 entries via `add()`, finalize;
  assert the JSONL file has 3 well-formed lines whose `rule_id` matches
  the entries.
- `test_sink_skips_jsonl_when_no_path` — construct without `jsonl_path`,
  push entries, finalize; assert no JSONL artifact exists. Locks the
  validate-mode behavior.
- `test_jsonl_path_requires_src_name` — `RejectSink(jsonl_path="x")` with
  no `src_name` raises `ValueError` (mirrors `broken_path`'s contract).
- `test_dropped_from_sample_still_in_jsonl` — push N >> cap entries of
  one rule; assert the JSONL file has N lines (cap governs the sample,
  not the stream).
- `test_pipeline_writes_jsonl_in_clean_mode` — drive a small fixture file
  through `pipeline.process_file(..., mode="clean")`; assert
  `<out_dir>/.shards/<stem>.findings.jsonl` exists and is well-formed
  JSONL.
- `test_pipeline_skips_jsonl_in_validate_mode` — same with
  `mode="validate"`; assert no shard.

### 8.4 Concatenation (`tests/test_report.py`)

A new `TestConcatFindingsShards` class:

- `test_concat_orders_alphabetically_by_src_name` — create 3 shards with
  identifying content; assert the concatenated file emits them in
  alphabetical order regardless of write order.
- `test_concat_creates_empty_file_when_no_shards` — empty `.shards/` and
  empty `all_stats` → `report.jsonl` exists with zero bytes.
- `test_concat_handles_missing_shard_gracefully` — `all_stats` references
  a file whose shard does not exist; concat skips it without error
  (covers the validate-mode worker case).
- `test_concat_removes_shard_directory` — after success, `.shards/` is
  gone.
- `test_concat_atomic_rename` — concat writes via `.partial`; the final
  path is the result of `os.replace`.
- `test_concat_failure_leaves_old_jsonl_intact` — preseed
  `report.jsonl` with prior content; cause concat to fail (e.g.
  unwritable tmp); assert the prior content is unchanged. (Skipped on
  platforms where the failure path can't be reliably simulated.)

### 8.5 CLI (`tests/test_cli.py`)

- `test_clean_emits_report_jsonl` — drive `cli.main(["clean", ...])`;
  assert `<out_dir>/report.jsonl` exists and is well-formed JSONL with
  the expected rule IDs.
- `test_clean_jsonl_is_empty_when_zero_quarantines` — drive a fixture
  with only clean records; assert `report.jsonl` exists with zero bytes.
- `test_clean_summary_line_announces_findings_path` — assert the post-run
  stdout contains `"findings: "` followed by the resolved path. Locks
  the operator-visible announcement.
- `test_validate_does_not_emit_jsonl` — drive
  `cli.main(["validate", ...])`; assert no `report.jsonl` and no
  `.shards/` in any directory under cwd.

### 8.6 Schema lock (`tests/test_report.py`)

- `test_schema_version_is_pinned` — a single assertion: every line of a
  synthesized `report.jsonl` carries `"schema_version": "1"`. Future
  schema bumps will fail this test by name, forcing the spec revision.
- `test_outcome_field_pinned` — every line carries `"outcome": "quarantined"`.
  Future additions of `"fixed"` outcomes will require updating this test
  along with the spec.
- `test_envelope_field_set_is_locked` — the exact set of top-level keys
  is `{"schema_version", "outcome", "file", "rule_id", "source_lines",
  "tier_attempted", "norad_id", "column_range", "observed", "expected",
  "note", "related"}`. A `set(parsed.keys()) == EXPECTED` assertion
  catches both accidental additions and accidental removals.

### 8.7 Forced I/O failures (`tests/test_report.py`, `tests/test_pipeline.py`)

The reviews flagged that I/O write failures mid-stream are a real failure
mode the current test plan would not catch. These are tested with
monkeypatching:

- `test_jsonl_write_entry_failure_after_counters_advance` — monkeypatch
  `JsonlFindingsWriter.write_entry` to raise after the first call;
  drive `_record_reject` with two rejects; assert (a) the exception
  propagates out of `pipeline._run`, (b) `stats.quarantined_count == 2`
  (counter advanced before the sink-add call, by spec design — see the
  existing docstring at `pipeline.py:285-290`), (c) the
  `.findings.jsonl.partial` is unlinked by `__exit__`'s abnormal-exit
  branch.
- `test_jsonl_finalize_failure_leaves_partial` — monkeypatch `os.replace`
  to raise during `JsonlFindingsWriter.finalize`; assert the `.partial`
  is left behind (matches `BrokenFileWriter`'s existing semantics) and
  the next run's pre-run scrub purges it.
- `test_concat_failure_preserves_prior_report_jsonl` — preseed
  `report.jsonl` with prior content; monkeypatch `os.replace` in
  `concat_findings_shards` to raise; assert the preseeded content is
  unchanged and the `.partial` exists. Skipped on platforms where the
  failure mode can't be reliably simulated.

### 8.8 RejectEntry constructor stability (`tests/test_report.py`)

A new `TestRejectEntryConstructorContract` class:

- `test_existing_keyword_construction_unchanged` — construct
  `RejectEntry(raw_lines=..., source_lines=..., primary=..., related=...)`
  with no `norad_id`; assert `entry.norad_id is None`. Locks the
  default-value contract for the 18 existing test-fixture call sites.
- `test_positional_construction_pins_field_order` — construct
  `RejectEntry([b"x"], [1], _diag(...), ())` positionally; assert all four
  positional slots land in the expected attributes. This locks the
  field-order constraint from §4.5 so a future refactor that reorders
  fields fails loudly here, not silently in `pipeline._record_reject`.
- `test_norad_id_must_be_keyword_to_avoid_corruption` — construct with
  `norad_id=12345` as a kwarg; assert `entry.norad_id == 12345`. Combined
  with the previous test, this documents the construction pattern the
  implementer must use in `_record_reject`.

### 8.9 `summary_dict` shape invariance (`tests/test_report.py`)

A defensive test against schema leakage between artifacts:

- `test_summary_dict_keys_unchanged_after_jsonl_feature` — assert
  `set(summary_dict(stats).keys())` equals the documented set in
  `report.py:399-427` (`src_name`, `paired_records`, `orphan_entries`,
  `input_lines_seen`, `clean_count`, `quarantined_count`, `fix_counts`,
  `reject_counts`, `dropped_counts`, `quarantined_norad_ids`). The
  test exists to catch an implementer who "helpfully" leaks per-finding
  fields into the `--report json` stdout output.

### 8.10 Stale-shard contamination (`tests/test_pipeline.py`, `tests/test_cli.py`)

- `test_pre_run_scrub_purges_stale_shards` — preseed
  `<out_dir>/.shards/tle1999.findings.jsonl` with bogus content; drive
  `cli.main(["clean", ...])` against an input that does NOT include
  `tle1999`; assert the resulting `report.jsonl` does not contain any of
  the preseeded bogus content and the `.shards/` directory is gone
  post-run.
- `test_pre_run_scrub_purges_partials` — preseed
  `<out_dir>/.shards/tleXXXX.findings.jsonl.partial`; assert the same.

## 9. Build order

Test-driven; each step is independently verifiable.

1. Add `RejectEntry.norad_id` as the **trailing field** with a `None`
   default (§4.5 constraint); write `TestRejectEntryConstructorContract`
   (§8.8) first, then the field. Update `pipeline._record_reject` to pass
   `norad_id=norad_id` as a kwarg (no behavior change yet, but locks the
   construction pattern).
2. Add `_diagnostic_to_nested` and `entry_to_jsonl_dict` in `report.py`;
   write `TestEntryToJsonlDict` (§8.1) first, then the helpers.
3. Add `JsonlFindingsWriter` in `report.py` with the explicit
   `encoding="utf-8"`, `newline="\n"`, `sort_keys=True`,
   `ensure_ascii=False` invariants from §4.3; write
   `TestJsonlFindingsWriter` (§8.2) first.
4. Extend `RejectSink` with `jsonl_path`; write the sink-integration tests
   (§8.3) first, then the wiring.
5. Wire `pipeline._run` to pass `jsonl_path` in clean mode; write the
   pipeline tests (§8.3) first.
6. Add `concat_findings_shards` in `report.py`; write the concat tests
   (§8.4) first.
7. Add the pre-run shard-dir scrub in `cli.py` (§4.6); write
   `test_pre_run_scrub_*` (§8.10) first.
8. Wire `cli.main` to call concat after `write_broken_noradids_ndjson`;
   write the CLI tests (§8.5) first.
9. Add the forced-I/O-failure tests (§8.7), constructor-stability tests
   (§8.8), and `summary_dict` invariance test (§8.9).
10. Add the schema-lock tests (§8.6) last so any prior step that drifted
    the schema fails these single assertions.
11. Run the verification chain: `uv run pytest`, `uv run ruff check .`,
    `uv run ruff format --check .`.

## 10. Out-of-scope follow-ups

Recorded here so they are not forgotten and not silently smuggled into
this change:

- **`validate` mode emission.** A `--report jsonl` flag (parallel to the
  existing `--report json`) could emit findings to stdout in `validate`
  mode. Useful for CI gates that want to consume findings without writing
  to a `--out-dir`. Open a follow-up issue when needed.
- **Successful-repair entries.** Issue #9 explicitly scopes the catalog
  to rejections; the `outcome` field (§4.1) reserves space for future
  `"fixed"` entries. Adding them needs its own design (volume of ~190M
  lines/run is non-trivial; consumers may want a separate artifact
  entirely, e.g. `repairs.jsonl`).
- **SARIF emitter.** A separate `report.sarif` artifact for IDE/CI
  consumers. Could layer over `report.jsonl` without changing the
  pipeline.
- **`lintle diff <run-a> <run-b>`.** Issue #10 — consumes `report.jsonl`
  as its input. The schema is shaped here to make that consumer
  straightforward (`schema_version` + stable `rule_id` + deterministic
  ordering).
- **`--fail-on RULE-ID=N` / `--max-quarantined N`.** Issue #13 —
  threshold gating. Reads `report.jsonl` (or in-memory stats).
- **`broken-noradids.ndjson` deprecation.** Once `report.jsonl` ships,
  `broken-noradids.ndjson` becomes derivable via
  `jq -rs 'map(.norad_id) | unique[] | select(. != null) | {noradId: .}' report.jsonl`.
  Consider deprecating in a future release (would need a one-version
  overlap period). Tracked as a follow-up — not bundled here because
  removing it would break existing downstream consumers.
- **Raw line bytes in JSONL.** The byte-faithful catalog stays in
  `broken/<stem>.broken.txt`. Consumers needing raw bytes have two
  recovery paths: (a) read `source_lines` and `sed -n 'X,Yp'` the
  original source file (fast, exact), or (b) parse the `.broken.txt`
  sidecar by entry index. If a future consumer needs raw bytes directly
  in JSONL, the safest design is a separate `raw_b64` field guarded by an
  opt-in CLI flag — additive, no schema bump. Not in v1.
- **Resolve cleaned-vs-reject publish ordering.** A pre-existing hazard
  (`pipeline.py:262-266`): `os.replace(cleaned_tmp, cleaned_path)` runs
  before `sink.finalize`, so a finalize failure publishes a cleaned file
  with incomplete reject artifacts. This spec doubles the surface area
  (adds JSONL finalize alongside `.broken.txt` finalize) but does not
  fix the underlying ordering. A follow-up should swap the order so
  cleaned publishes only after all reject artifacts finalize. Not
  bundled here because it is a wider-scope behavioral change affecting
  every existing `.broken.txt` test.

## 11. Deliberate departures from the issue sketch

Issue #9's schema sketch:

```json
{"file": "tle2022.txt", "source_lines": [12345, 12346], "rule_id": "TLE-CHK-001", "tier": "corrupt", "observed": "0", "expected": "3", "raw": "..."}
```

This spec departs in three places, each documented above:

| Sketch | Spec | Rationale |
|--------|------|-----------|
| `"tier": "corrupt"` | `"tier_attempted": "none"` (or `"tier-1"`, `"tier-2"`) | The sketch conflates outcome severity with attempted repair tier. `Diagnostic.tier_attempted` already exists in the codebase and names the repair tier that was tried before this diagnostic fired — that is the signal downstream wants. "corrupt" is the *category* of outcome (which is fully described by `rule_id` + the fact of being in `report.jsonl`). |
| `"raw": "..."` | _omitted_ | Per §2 non-goals. Raw bytes live in `.broken.txt`; consumers needing them have `file` + `source_lines` for a `sed -n 'X,Yp'` recovery, or can read the per-file sidecar. |
| _(no schema version)_ | `"schema_version": "1"` | Required for any structured artifact that downstream tooling will depend on. Adds 22 bytes per line; negligible against the average ~150-byte line. |
| _(no outcome field)_ | `"outcome": "quarantined"` | Forward compat. Today every line IS a quarantine and the field is constant; the moment we want to emit `"fixed"` entries (§10 follow-up) consumers can filter by `.outcome` without breaking on the prior "no field present" semantics. Adversarial review (Gemini) surfaced this. |

The rest of the sketch — `file`, `source_lines`, `rule_id`, `observed`,
`expected` — survives intact.
