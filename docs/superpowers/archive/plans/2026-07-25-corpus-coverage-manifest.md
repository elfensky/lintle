# Corpus Coverage: Histogram + Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add corpus-level coverage telemetry — a source-level epoch record-density histogram in `verify`, a per-satellite `manifest.jsonl` from `dedup`, and a cheap dedup→extract staleness fingerprint — on top of a shared pure `analyze_epochs()` reducer lifted from `extract`.

**Architecture:** Extract the pure history-reduction logic out of `extract._analyze` into a new I/O-free `history.py` (Task 1). Reuse that reducer to emit a one-row-per-satellite `manifest.jsonl` as a `dedup` byproduct (Task 2). Add a `Counter`-based month histogram to `verify`'s summary (Task 3). Add a structural cleaned-tree fingerprint that `dedup` records and `extract` checks (Task 4). Tasks are strictly ordered: 2 depends on 1; 3 and 4 are independent of each other but 4's helper is used by both dedup and extract.

**Tech Stack:** Python 3.14 · uv · `pytest` (`-n auto`) · `ruff` · stdlib only (`collections.Counter`, `statistics`, `json`, `datetime`) — no new runtime deps, `sgp4` stays walled out.

## Global Constraints

- **Python 3.14**, `src/lintle/` layout. Concise one-paragraph docstrings on every public module/function/class (no Args/Returns blocks).
- **No `sgp4` / `lintle.verify` import into the clean path.** New `history.py` is pure (no I/O, no sgp4) and may be imported by `extract` and `dedup` — both already on the auditor side. An import-graph test enforces the wall; do not break it.
- **Byte-deterministic unstyled output.** All new structured bytes (`manifest.jsonl`, `epoch_distribution` in `summary.{json,md}`, the fingerprint in `dedup summary.json`) MUST be deterministic: sorted keys or explicit sort order, ASCII, trailing LF, no wall-clock. Convert any `Counter` to `dict()` in sorted order at the output boundary.
- **Constant memory (Critical Rule #3).** No corpus file loaded whole. The manifest holds **one catalog's** epoch list at a time (bounded, ~hundreds of KB — the same bound `extract._analyze` already accepts).
- **One validator definition (Critical Rule #4).** No new validity path. Gap math lives once, in `history.analyze_epochs`; both `extract` and `dedup` call it.
- **Modern idioms:** `@dataclasses.dataclass(slots=True, frozen=True)` on dataclasses; `collections.Counter().update()` for tallies; `match` for 3+-way dispatch.
- Verify after every task: `uv run pytest && uv run ruff check . && uv run ruff format --check .`. Commit with conventional-commit prefixes on `develop` (this is a multi-file feature → **use a `feature/corpus-coverage` branch + worktree**, land via rebase-and-merge).

Constants already defined (reuse, do not re-declare): `CLEANED_DIRNAME="01-cleaned"`, `REPORT_DIRNAME="03-report"`, `DEDUP_DIRNAME="05-dedup"` (`lintle/__init__.py`). `GAP_FACTOR=10`, `GAPS_CAP=10` currently in `extract.py` — move to `history.py`.

---

### Task 1: Shared `history.py` reducer (lift from `extract._analyze`)

**Files:**
- Create: `src/lintle/history.py`
- Modify: `src/lintle/extract.py` (imports; `_analyze` becomes a thin wrapper; drop the moved defs)
- Test: `tests/test_history.py` (new); `tests/test_extract.py` (must keep passing unchanged)

**Interfaces:**
- Consumes: `lintle.verify.epoch.parse_epoch`, `lintle.verify.checks.element_set` (callers supply pre-parsed values; `history.py` itself imports only stdlib + `parse_epoch` for `_epoch_dt`).
- Produces:
  - `@dataclass(slots=True, frozen=True) Gap(start: datetime, end: datetime, days: float)`
  - `@dataclass(slots=True, frozen=True) HistoryStats(count, first, last, elset_first, elset_last, largest_gap_days, largest_gap_at, median_spacing_days, gaps, gap_count)` — field names/types identical to the current `extract.HistoryStats`.
  - `analyze_epochs(epochs: list[datetime], elsets: list[int | None]) -> HistoryStats` — pure.
  - `epoch_dt(line1: str) -> datetime` and `iso(dt: datetime) -> str` (renamed public forms of extract's `_epoch_dt`/`_iso`).
  - `GAP_FACTOR: int = 10`, `GAPS_CAP: int = 10`.

- [ ] **Step 1: Write the failing test** — `tests/test_history.py`:

```python
import datetime as dt
from lintle.history import analyze_epochs, Gap, HistoryStats

def _days(n):  # helper: build epochs n days apart from a fixed origin
    base = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    return [base + dt.timedelta(days=d) for d in n]

class TestAnalyzeEpochs:
    def test_uniform_cadence_no_gaps(self):
        hs = analyze_epochs(_days([0, 1, 2, 3, 4]), [1, 2, 3, 4, 5])
        assert hs.count == 5
        assert hs.gap_count == 0
        assert hs.median_spacing_days == 1.0
        assert hs.elset_first == 1 and hs.elset_last == 5

    def test_one_hole_is_one_gap(self):
        hs = analyze_epochs(_days([0, 1, 2, 42, 43]), [1, 2, 3, 4, 5])
        assert hs.gap_count == 1
        assert hs.gaps[0].days == 40.0

    def test_fewer_than_three_records_skips_analysis(self):
        hs = analyze_epochs(_days([0, 1]), [1, 2])
        assert hs.median_spacing_days is None
        assert hs.gap_count == 0            # the trivial-gapless footgun, asserted

    def test_empty(self):
        hs = analyze_epochs([], [])
        assert hs.count == 0 and hs.first is None and hs.gap_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lintle.history'`

- [ ] **Step 3: Write `src/lintle/history.py`** — move the pure logic verbatim from `extract.py` (lines ~129-221), turning the read loop's outputs into parameters:

```python
"""Pure history reduction shared by ``extract`` (per-satellite sidecar) and
``dedup`` (the corpus manifest): given a satellite's epoch datetimes and
element-set numbers in stream order, compute its ``HistoryStats`` — count,
span, median spacing, and reportable gaps. No I/O, no ``sgp4``; the single
definition of 'a gap' both callers share so their numbers cannot drift."""

import dataclasses
import datetime as _dt
import statistics

from lintle.verify.epoch import parse_epoch

GAP_FACTOR = 10
GAPS_CAP = 10


def epoch_dt(line1: str) -> _dt.datetime:
    """Record epoch as an aware UTC datetime — pure arithmetic from
    ``parse_epoch``'s ``(year, day_of_year)``, no wall clock."""
    year, day = parse_epoch(line1)
    return _dt.datetime(year, 1, 1, tzinfo=_dt.UTC) + _dt.timedelta(days=day - 1)


def iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclasses.dataclass(slots=True, frozen=True)
class Gap:
    """One reportable hole in a satellite's history."""

    start: _dt.datetime
    end: _dt.datetime
    days: float


@dataclasses.dataclass(slots=True, frozen=True)
class HistoryStats:
    """Reduction of one satellite's deduped span."""

    count: int
    first: _dt.datetime | None
    last: _dt.datetime | None
    elset_first: int | None
    elset_last: int | None
    largest_gap_days: float
    largest_gap_at: _dt.datetime | None
    median_spacing_days: float | None
    gaps: tuple[Gap, ...]
    gap_count: int


def analyze_epochs(
    epochs: list[_dt.datetime], elsets: list[int | None]
) -> HistoryStats:
    """Reduce one satellite's epoch/element-set lists (stream order) to its
    ``HistoryStats``. A gap is reportable when the inter-epoch delta exceeds
    ``GAP_FACTOR`` x the median spacing; the ``gaps`` tuple keeps the
    ``GAPS_CAP`` largest, chronologically, and ``gap_count`` is the total."""
    deltas = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(epochs, epochs[1:], strict=False)
    ]
    largest = max(deltas, default=0.0)
    largest_at = epochs[deltas.index(largest) + 1] if deltas else None
    median = statistics.median(deltas) if len(deltas) >= 2 else None
    reportable = [
        Gap(epochs[i], epochs[i + 1], d)
        for i, d in enumerate(deltas)
        if median and d > GAP_FACTOR * median
    ]
    top = sorted(reportable, key=lambda g: g.days, reverse=True)[:GAPS_CAP]
    return HistoryStats(
        count=len(epochs),
        first=epochs[0] if epochs else None,
        last=epochs[-1] if epochs else None,
        elset_first=elsets[0] if elsets else None,
        elset_last=elsets[-1] if elsets else None,
        largest_gap_days=largest,
        largest_gap_at=largest_at,
        median_spacing_days=median,
        gaps=tuple(sorted(top, key=lambda g: g.start)),
        gap_count=len(reportable),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_history.py -v`
Expected: PASS

- [ ] **Step 5: Refactor `extract.py` to consume `history.py`** — delete the moved defs (`Gap`, `HistoryStats`, `_epoch_dt`, `_iso`, `GAP_FACTOR`, `GAPS_CAP`, and the reduction tail of `_analyze`); import from `history`. `_analyze` keeps only the I/O loop and delegates:

```python
from lintle.history import GAPS_CAP, Gap, HistoryStats, analyze_epochs, epoch_dt, iso

def _analyze(spans: list[tuple[Path, int, int]]) -> HistoryStats:
    """Read ``spans`` (no writing), decode epochs + element sets, and delegate
    the reduction to ``history.analyze_epochs``. Holds one satellite's lists in
    memory — bounded, not a corpus file (Critical Rule #3 not in play)."""
    epochs: list = []
    elsets: list[int | None] = []
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                remaining -= len(block)
                for off in range(0, len(block), RECORD_BYTES):
                    line1 = block[off : off + 69].decode("ascii")
                    elsets.append(element_set(line1))
                    epochs.append(epoch_dt(line1))
    return analyze_epochs(epochs, elsets)
```

Update the two call sites of `_epoch_dt`/`_iso` elsewhere in `extract.py` to `epoch_dt`/`iso`.

- [ ] **Step 6: Run the full extract + history suites**

Run: `uv run pytest tests/test_extract.py tests/test_history.py -v`
Expected: PASS — the extract golden sidecar tests confirm byte-identical behaviour after the refactor.

- [ ] **Step 7: Lint + format + commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add src/lintle/history.py src/lintle/extract.py tests/test_history.py
git commit -m "refactor: lift extract history reduction into pure history.py"
```

---

### Task 2: `manifest.jsonl` as a `dedup` byproduct

**Files:**
- Modify: `src/lintle/dedup.py` (accumulate per-catalog rows in `run`'s main loop; add `MANIFEST_STEM`/`MANIFEST_SUFFIX`; a `_manifest_row` helper; update `_README`)
- Test: `tests/test_dedup.py`

**Interfaces:**
- Consumes: `history.HistoryStats`, `history.analyze_epochs`, `history.epoch_dt`, `history.iso` (Task 1); `checks.element_set`; `verify.epoch.parse_epoch`.
- Produces: `<out-dir>/05-dedup/manifest.jsonl` — one compact ASCII JSON object per catalog, catalog-ascending, trailing LF each. Fields: `norad_id:int, records:int, first_epoch:str, last_epoch:str, span_days:float, median_spacing_days:float|null, largest_gap_days:float, gap_count:int`.

- [ ] **Step 1: Write the failing test** — in `tests/test_dedup.py`, add a `TestManifest` class. Build a synthetic cleaned tree with two catalogs (one gap-free & well-sampled, one single-record) and run `dedup.run`, then assert the manifest:

```python
class TestManifest:
    def test_one_row_per_catalog_deterministic(self, tmp_path):
        # <build a cleaned tree: catalog 100 with 5 daily epochs,
        #  catalog 200 with 1 epoch — reuse this file's existing cleaned-tree fixture>
        out = str(tmp_path)
        _build_cleaned(out, {100: _daily(5), 200: _daily(1)})   # existing helper style
        dedup.run(out)
        manifest = (tmp_path / "05-dedup" / "manifest.jsonl").read_text("ascii")
        rows = [json.loads(l) for l in manifest.splitlines()]
        assert [r["norad_id"] for r in rows] == [100, 200]        # catalog-ascending
        assert rows[0]["records"] == 5 and rows[0]["gap_count"] == 0
        # trivial-gapless footgun stays visible: 1 record => gap_count 0 but records 1
        assert rows[1]["records"] == 1
        assert rows[1]["gap_count"] == 0
        assert rows[1]["median_spacing_days"] is None
        # byte-determinism: a second run produces identical bytes
        dedup.run(out)
        assert (tmp_path / "05-dedup" / "manifest.jsonl").read_text("ascii") == manifest
```

(If the test file lacks a cleaned-tree builder, reuse the fixture the existing dedup tests already use — grep `tests/test_dedup.py` for how it stages `01-cleaned`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dedup.py::TestManifest -v`
Expected: FAIL — no `manifest.jsonl` produced.

- [ ] **Step 3: Implement manifest accumulation in `dedup.py`.** Add near the other stem constants:

```python
MANIFEST_STEM = "manifest"
MANIFEST_SUFFIX = ".jsonl"
```

Add the row builder (pure):

```python
def _manifest_row(catalog: int, hs) -> bytes:
    """One compact ASCII JSON manifest row for a satellite — fixed key order so
    reruns are byte-identical. ``median_spacing_days`` is null for <3 records
    (the trivially-gapless case the row's ``records`` field lets a query exclude)."""
    span = (hs.last - hs.first).total_seconds() / 86400.0 if hs.count else 0.0
    row = {
        "norad_id": catalog,
        "records": hs.count,
        "first_epoch": history.iso(hs.first) if hs.first else None,
        "last_epoch": history.iso(hs.last) if hs.last else None,
        "span_days": round(span, 6),
        "median_spacing_days": (
            round(hs.median_spacing_days, 6)
            if hs.median_spacing_days is not None else None
        ),
        "largest_gap_days": round(hs.largest_gap_days, 6),
        "gap_count": hs.gap_count,
    }
    return (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
```

In `run`, accumulate per catalog across the `_groups` loop and flush on the catalog boundary. Add a `ChunkedWriter`? **No** — the manifest is bounded by catalog count (~MB); write one plain durable file. Buffer rows in a `bytearray` and `fsutil.durable_write_text`/`durable_replace` once after the loop:

```python
from lintle import history
from lintle.verify.epoch import parse_epoch

# inside run(), alongside the imp/notes writers:
manifest = bytearray()
cur_cat: int | None = None
cur_epochs: list = []
cur_elsets: list[int | None] = []

def _flush():
    if cur_cat is not None:
        hs = history.analyze_epochs(cur_epochs, cur_elsets)
        manifest.extend(_manifest_row(cur_cat, hs))

for g in _groups(sorter.sorted_records()):
    imp.write_record(...); ...            # existing import/notes writes, unchanged
    if g.kept.catalog != cur_cat:
        _flush()
        cur_cat, cur_epochs, cur_elsets = g.kept.catalog, [], []
    cur_epochs.append(history.epoch_dt(g.kept.line1))
    cur_elsets.append(checks.element_set(g.kept.line1))
# after the loop, before writing summary:
_flush()
fsutil.durable_write_text(
    str(ddir / f"{MANIFEST_STEM}{MANIFEST_SUFFIX}"),
    manifest.decode("ascii"), encoding="ascii",
)
```

Since `nonlocal` closures over reassigned locals are awkward, implement `_flush` inline or hold state in a small `list`/`dict` — pick whichever reads cleanly; the deliverable is the accumulate-and-flush-on-boundary behaviour, memory-bounded to one catalog. Update `_README` to mention `manifest.jsonl`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dedup.py::TestManifest -v`
Expected: PASS

- [ ] **Step 5: Run the full dedup suite**

Run: `uv run pytest tests/test_dedup.py -v`
Expected: PASS (existing import/notes/summary behaviour unchanged).

- [ ] **Step 6: Lint + format + commit**

```bash
git add src/lintle/dedup.py tests/test_dedup.py
git commit -m "feat: emit per-satellite manifest.jsonl from dedup"
```

---

### Task 3: Epoch record-density histogram in `verify`

**Files:**
- Modify: `src/lintle/verify/__init__.py` (tally a `Counter` in the first pass; pass it to `sink.write`)
- Modify: `src/lintle/verify/report.py` (thread `epoch_distribution` through `write` / `render_summary_json` / `render_summary_md` as a separate top-level key)
- Test: `tests/test_verify.py`

**Interfaces:**
- Consumes: `history.epoch_dt` (or `verify.epoch.parse_epoch` directly — either works; use `parse_epoch` to avoid a `verify → history` edge if the import-graph test objects). Bin key `f"{year}-{month:02d}"`.
- Produces: a new top-level `epoch_distribution` object in `summary.json` (`{"YYYY-MM": count, ...}`, key-sorted) and a short section in `summary.md`. **Not** placed inside `checked` (which stays `dict[str, int]` scalar tallies).

- [ ] **Step 1: Write the failing test** — in `tests/test_verify.py`:

```python
class TestEpochHistogram:
    def test_summary_bins_records_by_month(self, tmp_path):
        # cleaned tree: 3 records in 2017-01, 0 in Feb/Mar, 2 in 2017-04
        out = str(tmp_path)
        _build_cleaned(out, {100: [(2017, 15), (2017, 16), (2017, 17),
                                   (2017, 91), (2017, 92)]})   # doy 91/92 ≈ Apr 1/2
        verify.run(out, source_dir=None)
        summary = json.loads((tmp_path / "04-verify" / "summary.json").read_text())
        hist = summary["epoch_distribution"]
        assert hist["2017-01"] == 3
        assert hist["2017-04"] == 2
        assert "2017-02" not in hist          # the hole reads as an absent bin
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verify.py::TestEpochHistogram -v`
Expected: FAIL — `KeyError: 'epoch_distribution'`.

- [ ] **Step 3: Tally the histogram in `verify/__init__.py`.** In `run`, before the per-file loop: `from collections import Counter` and `histogram: Counter[str] = Counter()`. Inside the loop, after a record survives `revalidate` (so only valid records are binned — a broken record has no trustworthy epoch):

```python
from lintle.verify.epoch import parse_epoch
# ... after `sorter.add(rec)`:
year, day = parse_epoch(rec.line1)
month = (_dt.datetime(year, 1, 1) + _dt.timedelta(days=day - 1)).month
histogram[f"{year}-{month:02d}"] += 1
```

Pass `epoch_distribution=dict(sorted(histogram.items()))` into `sink.write(...)`.

- [ ] **Step 4: Thread `epoch_distribution` through `report.py`.** Add an `epoch_distribution: dict[str, int]` parameter (default `{}`) to `SuspectSink.write`, `render_summary_json`, `render_summary_md`, `_summary_json_bytes`, `_summary_md_str`. In `_summary_json_bytes` add the key **outside** `checked`:

```python
doc = {..., "checked": dict(sorted(checked.items())),
       "epoch_distribution": dict(sorted(epoch_distribution.items())), ...}
```

In `_summary_md_str` append a short `### Epoch distribution` section listing `YYYY-MM  count` in key order (only if non-empty). Keep `checked` typed `dict[str, int]`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_verify.py::TestEpochHistogram -v`
Expected: PASS

- [ ] **Step 6: Run the full verify suite (golden summaries updated)**

Run: `uv run pytest tests/test_verify.py tests/test_verify_stream.py -v`
Expected: PASS — update any golden `summary.json`/`summary.md` fixtures to include the new key/section.

- [ ] **Step 7: Lint + format + commit**

```bash
git add src/lintle/verify/__init__.py src/lintle/verify/report.py tests/test_verify.py
git commit -m "feat: add epoch record-density histogram to verify summary"
```

---

### Task 4: Staleness fingerprint (dedup records, extract checks)

**Files:**
- Modify: `src/lintle/verify/records.py` (add pure `cleaned_fingerprint(out_dir) -> dict`)
- Modify: `src/lintle/dedup.py` (store fingerprint in `summary.json`)
- Modify: `src/lintle/extract.py` (compare live vs stored at run start; warn on mismatch)
- Test: `tests/test_extract.py`, `tests/test_dedup.py`

**Interfaces:**
- Produces: `cleaned_fingerprint(out_dir: str) -> dict` → `{"stems": [[stem, total_bytes], ...]}` sorted by stem, computed from `cleaned_stems` + per-chunk `st_size` (cheap `stat`, no reads). Stored under `dedup summary.json` key `cleaned_fingerprint`. `extract` recomputes it from the live tree and `term.warning`s (exit unchanged) on inequality.

- [ ] **Step 1: Write the failing test** — `cleaned_fingerprint` is stable and detects size change:

```python
# tests/test_dedup.py
class TestFingerprint:
    def test_fingerprint_in_summary_and_stable(self, tmp_path):
        out = str(tmp_path)
        _build_cleaned(out, {100: _daily(3)})
        dedup.run(out)
        summary = json.loads((tmp_path / "05-dedup" / "summary.json").read_text())
        assert "cleaned_fingerprint" in summary
        from lintle.verify.records import cleaned_fingerprint
        assert cleaned_fingerprint(out) == summary["cleaned_fingerprint"]
```

```python
# tests/test_extract.py
class TestStalenessWarning:
    def test_mismatch_warns_but_exits_zero(self, tmp_path, monkeypatch, capsys):
        out = str(tmp_path)
        _build_cleaned(out, {100: _daily(3)})
        dedup.run(out)
        # mutate cleaned/ after dedup so the fingerprint drifts
        _append_record(out, 100)                       # changes a chunk's size
        code = extract.run(out, [100], str(tmp_path / "ex"))
        assert code == 0                               # warn-and-proceed, never exit 2
        assert "stale" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dedup.py::TestFingerprint tests/test_extract.py::TestStalenessWarning -v`
Expected: FAIL — no `cleaned_fingerprint` key / no warning.

- [ ] **Step 3: Add `cleaned_fingerprint` to `records.py`:**

```python
def cleaned_fingerprint(out_dir: str) -> dict:
    """A cheap structural fingerprint of ``01-cleaned`` — each cleaned stem and
    its total chunk-byte size (``stat`` only, no reads). Lets a downstream run
    detect that ``cleaned/`` changed since a ``dedup`` run without re-hashing the
    ~30 GB corpus; staleness, not bit-rot, is the threat this guards."""
    from lintle import CLEANED_DIRNAME, chunking, stem as _stem
    cdir = Path(out_dir) / CLEANED_DIRNAME
    stems = cleaned_stems(out_dir)
    fp = []
    for s in stems:
        # sum sizes of the stem's chunk set; reuse ChunkedReader.chunk_paths()
        paths = chunking.ChunkedReader(cdir, s, ".txt").chunk_paths()  # confirm suffix
        fp.append([s, sum(p.stat().st_size for p in paths)])
    return {"stems": sorted(fp)}
```

(Grep `records.iter_file` / `cleaned_stems` for the exact cleaned chunk stem+suffix convention and match it — do not guess the suffix.)

- [ ] **Step 4: Store it in `dedup.run`'s summary** — add to the `summary` dict:

```python
"cleaned_fingerprint": records.cleaned_fingerprint(out_dir),
```

- [ ] **Step 5: Check it in `extract.run`** — after `_import_chunks(out_dir)` and reading the dedup summary, compare:

```python
stored = json.loads((Path(out_dir) / DEDUP_DIRNAME / "summary.json")
                     .read_text("ascii")).get("cleaned_fingerprint")
if stored is not None and stored != records.cleaned_fingerprint(out_dir):
    term.warning(
        f"{CLEANED_DIRNAME} changed since the last dedup run — extract results "
        "may be stale; re-run 'lintle dedup'."
    )
```

Exit code path is untouched (warning only).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_dedup.py::TestFingerprint tests/test_extract.py::TestStalenessWarning -v`
Expected: PASS

- [ ] **Step 7: Full suite + lint + format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/lintle/verify/records.py src/lintle/dedup.py src/lintle/extract.py tests/test_dedup.py tests/test_extract.py
git commit -m "feat: warn extract when cleaned tree drifted since dedup"
```

---

### Task 5: Docs + CHANGELOG

**Files:**
- Modify: `ARCHITECTURE.md` (note `history.py` as a pure leaf; the manifest/histogram/fingerprint artifacts), `CLAUDE.md` project-layout block (add `history.py`), `CHANGELOG.md` (unreleased notes), `README.md` if it enumerates outputs.

- [ ] **Step 1: Update `ARCHITECTURE.md`** — add `history.py` to the module list as a pure I/O-free reducer depended on by `extract` and `dedup`; document `05-dedup/manifest.jsonl`, verify's `epoch_distribution`, and the `cleaned_fingerprint`.
- [ ] **Step 2: Update `CLAUDE.md`** project-layout tree with `history.py`.
- [ ] **Step 3: Add `CHANGELOG.md`** entries (collected under the next release when cut).
- [ ] **Step 4: Verify no code changed** (`uv run pytest` still green) and commit:

```bash
git add ARCHITECTURE.md CLAUDE.md CHANGELOG.md README.md
git commit -m "docs: document history.py, manifest, epoch histogram, fingerprint"
```

---

## Self-Review

**Spec coverage:** §1 histogram → Task 3. §2 manifest (+ footgun, single-file, jq/shuf note) → Task 2 (footgun asserted; `shuf`/`jq` is doc-level, no code — correct). §3 fingerprint (cheap, warn-only) → Task 4. §4 shared reducer → Task 1. Naming ("epoch distribution", not "coverage") → Task 3 field name + Task 5 docs. Import-graph wall → Global Constraints + Task 1/3 notes. All covered.

**Placeholder scan:** Two deliberate "grep to confirm the exact convention" notes (cleaned chunk suffix in Task 4 Step 3; cleaned-tree fixture in Task 2 Step 1) — these are *verification* instructions against real code, not unfilled logic; the surrounding code is complete. No TBD/TODO/"handle edge cases".

**Type consistency:** `HistoryStats`/`Gap` field names identical across Task 1 definition and Task 2/extract consumers. `analyze_epochs(epochs, elsets)` signature consistent Task 1↔2. `epoch_distribution: dict[str,int]` consistent across Task 3 renderer changes. `cleaned_fingerprint(out_dir) -> dict` consistent Task 4 producer/consumers. `checked` stays `dict[str,int]` (histogram is a sibling key) — the report.py wrinkle is handled explicitly.
