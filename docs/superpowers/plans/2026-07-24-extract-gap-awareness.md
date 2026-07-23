# Extract Gap Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lintle extract` warns when a satellite's history has temporal gaps or upstream-quarantined records, shows the gaps, and asks y/n before exporting.

**Architecture:** Untangle `_extract_one`'s copy-and-compute loop into a pure pass-1 `_analyze` (epochs → median spacing, reportable gaps, stats) and a dumb pass-2 `_copy_spans`. Between the passes: warn + confirm. Sidecar bumps to schema v2 with the gap fields. Spec: `docs/superpowers/specs/2026-07-24-extract-gap-awareness-design.md`.

**Tech Stack:** Python 3.14, stdlib only (`statistics.median` is the one new import). No new dependencies.

## Global Constraints

- Import wall: `extract.py` must never import `sgp4` or add clean-path imports — new imports are `statistics` (stdlib) and `REPORT_DIRNAME` from `lintle`.
- Sidecar stays byte-deterministic: sorted keys, 2-space indent, trailing LF, pure arithmetic, ASCII.
- Dataclasses: `@dataclasses.dataclass(slots=True, frozen=True)`.
- 3-way dispatch uses `match`, not `elif` chains.
- Gap rule: reportable iff delta > **10× median** inter-epoch spacing; analysis skipped below 3 records; sidecar/terminal list capped at the **10 largest**, shown chronologically.
- Non-TTY: warn + proceed. Decline is a skip, **not** an error (exit stays 0). No new CLI flags.
- All docstrings: concise one-paragraph style, no Args/Returns blocks.
- Verify before every commit: `uv run pytest tests/test_extract.py -q`, and before the final one the full chain `uv run pytest && uv run ruff check . && uv run ruff format --check .`
- Conventional commits.

---

### Task 0: Worktree

**Files:** none (setup only)

- [ ] **Step 1: Create the worktree and install deps**

```bash
cd /Users/andrei/Developer/lintle
git worktree add .worktrees/feature-extract-gap-awareness -b feature/extract-gap-awareness develop
cd .worktrees/feature-extract-gap-awareness
uv sync
ln -s ../../data data
```

Expected: worktree at `.worktrees/feature-extract-gap-awareness`, `uv sync` exits 0. All later tasks run inside this directory.

---

### Task 1: `Gap` / `HistoryStats` dataclasses + pure `_analyze`

**Files:**
- Modify: `src/lintle/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: existing `find_spans`, `_epoch_dt`, `element_set`, `_COPY_BLOCK`, `RECORD_BYTES`.
- Produces (later tasks rely on these exact names):
  - `Gap(start: datetime, end: datetime, days: float)` — frozen slots dataclass
  - `HistoryStats(count: int, first: datetime | None, last: datetime | None, elset_first: int | None, elset_last: int | None, largest_gap_days: float, largest_gap_at: datetime | None, median_spacing_days: float | None, gaps: tuple[Gap, ...], gap_count: int)` — frozen slots dataclass
  - `_analyze(spans: list[tuple[Path, int, int]]) -> HistoryStats`
  - Module constants `GAP_FACTOR = 10`, `GAPS_CAP = 10`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extract.py` (uses the existing `recs`/`write_import_tree` helpers):

```python
class TestAnalyze:
    """_analyze: pure pass-1 history stats — median spacing + reportable gaps."""

    def _hs(self, tmp_path, *days):
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        return extract._analyze(extract.find_spans(str(out), 100))

    def test_uniform_cadence_no_gaps(self, tmp_path):
        hs = self._hs(tmp_path, *[1.0 + i for i in range(10)])
        assert hs.count == 10
        assert hs.median_spacing_days == 1.0
        assert hs.gaps == () and hs.gap_count == 0
        assert hs.largest_gap_days == 1.0

    def test_one_hole_is_one_gap(self, tmp_path):
        # daily cadence days 1-10, then a 40-day hole to day 50
        hs = self._hs(tmp_path, *[1.0 + i for i in range(10)], 50.0, 51.0, 52.0)
        assert hs.median_spacing_days == 1.0
        assert hs.gap_count == 1 and len(hs.gaps) == 1
        gap = hs.gaps[0]
        assert gap.days == 40.0
        assert gap.start == extract._epoch_dt(l1(100, day=10.0))
        assert gap.end == extract._epoch_dt(l1(100, day=50.0))
        assert hs.largest_gap_days == 40.0 and hs.largest_gap_at == gap.end

    def test_under_three_records_skips_analysis(self, tmp_path):
        hs = self._hs(tmp_path, 1.0, 2.5)
        assert hs.count == 2
        assert hs.median_spacing_days is None
        assert hs.gaps == () and hs.gap_count == 0
        assert hs.largest_gap_days == 1.5  # largest gap still tracked

    def test_cap_keeps_ten_largest_chronological(self, tmp_path):
        # 12 runs of 3 daily records, 28-day holes between runs: 11 reportable
        days = [r * 30 + s for r in range(12) for s in (1.0, 2.0, 3.0)]
        hs = self._hs(tmp_path, *days)
        assert hs.gap_count == 11 and len(hs.gaps) == 10
        starts = [g.start for g in hs.gaps]
        assert starts == sorted(starts)  # chronological
        assert all(g.days == 28.0 for g in hs.gaps)

    def test_stats_match_extract_one(self, tmp_path):
        hs = self._hs(tmp_path, 1.0, 2.5, 10.0)
        assert hs.count == 3
        assert hs.elset_first == 1 and hs.elset_last == 1
        assert hs.first == extract._epoch_dt(l1(100, day=1.0))
        assert hs.last == extract._epoch_dt(l1(100, day=10.0))
        assert hs.largest_gap_days == 7.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract.py::TestAnalyze -q`
Expected: FAIL — `AttributeError: module 'lintle.extract' has no attribute '_analyze'`

- [ ] **Step 3: Implement**

In `src/lintle/extract.py`, add to the imports block:

```python
import dataclasses
import statistics
```

After the `_epoch_dt`/`_iso` helpers, add:

```python
# A gap is reportable when the inter-epoch delta exceeds GAP_FACTOR x the
# satellite's own median spacing; the report keeps the GAPS_CAP largest.
GAP_FACTOR = 10
GAPS_CAP = 10


@dataclasses.dataclass(slots=True, frozen=True)
class Gap:
    """One reportable hole in a satellite's history."""

    start: _dt.datetime
    end: _dt.datetime
    days: float


@dataclasses.dataclass(slots=True, frozen=True)
class HistoryStats:
    """Pass-1 analysis of one satellite's deduped span — everything the
    sidecar and the warn/confirm flow need, computed before a byte is
    exported."""

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


def _analyze(spans: list[tuple[Path, int, int]]) -> HistoryStats:
    """Read ``spans`` (no writing) and compute the history stats. Holds one
    satellite's epoch list in memory — bounded (tens of thousands of records,
    ~hundreds of KB worst case), not a corpus file, so Critical Rule #3's
    streaming mandate is not in play."""
    epochs: list[_dt.datetime] = []
    elset_first = elset_last = None
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                remaining -= len(block)
                for off in range(0, len(block), RECORD_BYTES):
                    line1 = block[off : off + 69].decode("ascii")
                    if not epochs:
                        elset_first = element_set(line1)
                    elset_last = element_set(line1)
                    epochs.append(_epoch_dt(line1))
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
        elset_first=elset_first,
        elset_last=elset_last,
        largest_gap_days=largest,
        largest_gap_at=largest_at,
        median_spacing_days=median,
        gaps=tuple(sorted(top, key=lambda g: g.start)),
        gap_count=len(reportable),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -q`
Expected: all pass (new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): pure pass-1 _analyze — median spacing + reportable gaps"
```

---

### Task 2: Quarantine-flag loader

**Files:**
- Modify: `src/lintle/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `REPORT_DIRNAME` (`"03-report"`) from `lintle`.
- Produces: `_quarantined_ids(out_dir: str) -> set[int] | None` — the NORAD IDs in `<out_dir>/03-report/broken-noradids.ndjson`; `None` when the file is absent or unreadable (unknown ≠ false).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extract.py`:

```python
class TestQuarantinedIds:
    def _write_ndjson(self, tmp_path, text):
        rdir = tmp_path / "03-report"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "broken-noradids.ndjson").write_text(text, encoding="ascii")

    def test_present_ids(self, tmp_path):
        self._write_ndjson(tmp_path, '{"noradId":100}\n{"noradId":200}\n')
        assert extract._quarantined_ids(str(tmp_path)) == {100, 200}

    def test_missing_file_is_unknown(self, tmp_path):
        assert extract._quarantined_ids(str(tmp_path)) is None

    def test_malformed_file_is_unknown_with_warning(self, tmp_path, capsys):
        self._write_ndjson(tmp_path, "not json\n")
        assert extract._quarantined_ids(str(tmp_path)) is None
        assert "broken-noradids" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract.py::TestQuarantinedIds -q`
Expected: FAIL — `AttributeError: ... no attribute '_quarantined_ids'`

- [ ] **Step 3: Implement**

In `src/lintle/extract.py`, change the package import line to:

```python
from lintle import REPORT_DIRNAME, fsutil, term
```

Add after `_import_chunks`:

```python
def _quarantined_ids(out_dir: str) -> set[int] | None:
    """NORAD IDs quarantined during clean, from the run report's
    ``broken-noradids.ndjson`` — ``None`` (unknown, not false) when the report
    is absent or unreadable, e.g. a pruned tree."""
    path = Path(out_dir) / REPORT_DIRNAME / "broken-noradids.ndjson"
    if not path.is_file():
        return None
    try:
        return {
            json.loads(line)["noradId"]
            for line in path.read_text(encoding="ascii").splitlines()
            if line
        }
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        term.warning(f"unreadable {path.name} — quarantine info unavailable")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): quarantine flag from broken-noradids.ndjson (None = unknown)"
```

---

### Task 3: Two-pass `_extract_one` + sidecar v2

**Files:**
- Modify: `src/lintle/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: Task 1's `HistoryStats`/`_analyze`, Task 2's `_quarantined_ids`.
- Produces:
  - `_copy_spans(spans: list[tuple[Path, int, int]], out) -> None` — verbatim byte copy of spans into open binary file `out`, no stats
  - `_sidecar(out_dir: str, catalog: int, hs: HistoryStats, had_quarantined: bool | None) -> str` — new signature (replaces the old 7-arg one)
  - `_extract_one(out_dir: str, catalog: int, dest: Path, quarantined: set[int] | None) -> bool` — new 4th parameter
  - Sidecar `schema_version` becomes `"2"`; new keys `median_spacing_days`, `gap_count`, `gaps`, `had_quarantined_records`

- [ ] **Step 1: Update the tests (failing first)**

In `tests/test_extract.py`:

In `TestRun.test_writes_txt_and_json`, change the schema assertion and add the v2 keys — replace `assert meta["schema_version"] == "1"` with:

```python
        assert meta["schema_version"] == "2"
        assert meta["median_spacing_days"] == 4.5  # deltas [1.5, 7.5]
        assert meta["gap_count"] == 0 and meta["gaps"] == []
        assert meta["had_quarantined_records"] is None
```

In `TestRun.test_single_record_satellite_null_rate`, append:

```python
        assert meta["median_spacing_days"] is None
        assert meta["gap_count"] == 0 and meta["gaps"] == []
```

Replace the `expected` string in `TestRun.test_sidecar_bytes_golden` wholesale with:

```python
        expected = (
            "{\n"
            '  "element_set_first": 1,\n'
            '  "element_set_last": 1,\n'
            '  "first_epoch": "2020-01-01T00:00:00Z",\n'
            '  "gap_count": 0,\n'
            '  "gaps": [],\n'
            '  "had_quarantined_records": null,\n'
            '  "largest_gap_at": "2020-01-02T12:00:00Z",\n'
            '  "largest_gap_days": 1.5,\n'
            '  "last_epoch": "2020-01-02T12:00:00Z",\n'
            '  "mean_records_per_day": 1.333333,\n'
            '  "median_spacing_days": null,\n'
            '  "norad_id": 100,\n'
            '  "records": 2,\n'
            '  "schema_version": "2",\n'
            '  "source": {\n'
            '    "dedup_records_written": 2,\n'
            '    "dedup_schema_version": "1",\n'
            f'    "out_dir": "{out}"\n'
            "  },\n"
            '  "span_days": 1.5\n'
            "}\n"
        )
```

Append a new test class:

```python
class TestSidecarV2:
    def test_quarantine_flag_true_and_false(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0), (200, 1.0)), 10)
        rdir = out / "03-report"
        rdir.mkdir()
        (rdir / "broken-noradids.ndjson").write_text(
            '{"noradId":100}\n', encoding="ascii"
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100, 200], str(dest)) == 0
        meta100 = json.loads((dest / "100.json").read_text(encoding="ascii"))
        meta200 = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta100["had_quarantined_records"] is True
        assert meta200["had_quarantined_records"] is False

    def test_gap_fields_in_sidecar(self, tmp_path):
        days = [1.0 + i for i in range(10)] + [50.0, 51.0, 52.0]
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        meta = json.loads((dest / "100.json").read_text(encoding="ascii"))
        assert meta["median_spacing_days"] == 1.0
        assert meta["gap_count"] == 1
        assert meta["gaps"] == [
            {"days": 40.0, "end": "2020-02-19T00:00:00Z", "start": "2020-01-10T00:00:00Z"}
        ]
```

(`test_quarantine_flag_true_and_false` passes because the pytest process is non-TTY: the flow warns and proceeds — the prompt paths get their own tests in Task 4.)

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `uv run pytest tests/test_extract.py -q`
Expected: FAIL — golden mismatch (`schema_version "1"`), missing v2 keys.

- [ ] **Step 3: Implement**

In `src/lintle/extract.py`, replace `_sidecar` with:

```python
def _sidecar(
    out_dir: str, catalog: int, hs: HistoryStats, had_quarantined: bool | None
) -> str:
    """The ``<id>.json`` document (sorted keys, 2-space indent, trailing LF —
    the house deterministic-JSON shape). Schema v2 adds the gap-awareness
    fields; ``had_quarantined`` is tri-state (None = clean report absent)."""
    span = (hs.last - hs.first).total_seconds() / 86400.0
    summary_path = Path(out_dir) / DEDUP_DIRNAME / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="ascii"))
    doc = {
        "schema_version": "2",
        "norad_id": catalog,
        "records": hs.count,
        "first_epoch": _iso(hs.first),
        "last_epoch": _iso(hs.last),
        "span_days": round(span, 6),
        "mean_records_per_day": round(hs.count / span, 6) if span else None,
        "largest_gap_days": round(hs.largest_gap_days, 6),
        "largest_gap_at": (
            _iso(hs.largest_gap_at) if hs.largest_gap_at is not None else None
        ),
        "median_spacing_days": (
            round(hs.median_spacing_days, 6)
            if hs.median_spacing_days is not None
            else None
        ),
        "gap_count": hs.gap_count,
        "gaps": [
            {"start": _iso(g.start), "end": _iso(g.end), "days": round(g.days, 6)}
            for g in hs.gaps
        ],
        "had_quarantined_records": had_quarantined,
        "element_set_first": hs.elset_first,
        "element_set_last": hs.elset_last,
        "source": {
            "out_dir": str(Path(out_dir)),
            "dedup_records_written": summary.get("records_written"),
            "dedup_schema_version": summary.get("schema_version"),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"
```

Replace `_extract_one` with (docstring change + two-pass body; the atomic
cleanup block is unchanged):

```python
def _copy_spans(spans: list[tuple[Path, int, int]], out) -> None:
    """Pass 2: verbatim byte copy of ``spans`` into the open binary file
    ``out`` — no decoding, no stats (pass 1 already has them)."""
    for chunk, lo, hi in spans:
        with open(chunk, "rb") as fh:
            fh.seek(lo * RECORD_BYTES)
            remaining = (hi - lo) * RECORD_BYTES
            while remaining:
                block = fh.read(min(_COPY_BLOCK, remaining))
                out.write(block)
                remaining -= len(block)


def _extract_one(
    out_dir: str, catalog: int, dest: Path, quarantined: set[int] | None
) -> bool:
    """Extract one satellite in two passes: analyze the span read-only, then
    stream its byte range verbatim to ``<dest>/<id>.txt`` (durable
    temp-then-rename) and write the stats sidecar. ``<id>.txt`` + ``<id>.json``
    are one atomic unit: a failure anywhere in the txt-stream + txt-commit +
    sidecar-write sequence leaves nothing behind for this run's attempted
    output, and pre-existing files from an earlier successful run are never
    touched. False if the catalog has no records."""
    spans = find_spans(out_dir, catalog)
    if not spans:
        return False
    hs = _analyze(spans)
    had_quarantined = None if quarantined is None else catalog in quarantined
    txt = dest / f"{catalog}.txt"
    tmp = str(txt) + fsutil.PARTIAL_SUFFIX
    sidecar_partial = str(dest / f"{catalog}.json") + fsutil.PARTIAL_SUFFIX
    committed = False
    try:
        with open(tmp, "wb") as out:
            _copy_spans(spans, out)
        fsutil.durable_replace(tmp, str(txt))
        committed = True
        fsutil.durable_write_text(
            str(dest / f"{catalog}.json"),
            _sidecar(out_dir, catalog, hs, had_quarantined),
            encoding="ascii",
        )
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        Path(sidecar_partial).unlink(missing_ok=True)
        if committed:
            Path(txt).unlink(missing_ok=True)
            Path(dest / f"{catalog}.json").unlink(missing_ok=True)
        raise
    return True
```

In `run()`, load the set once and thread it through — after the
`_import_chunks(out_dir)` line add:

```python
    quarantined = _quarantined_ids(out_dir)
```

and change the call site to:

```python
            found = _extract_one(out_dir, catalog, dest_dir, quarantined)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -q`
Expected: all pass — including the untouched atomicity tests
(`test_failure_cleans_partial_and_continues` now fails during pass-1
`_analyze`, before the tmp exists; the assertions still hold).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): two-pass analyze+copy; sidecar v2 gap + quarantine fields"
```

---

### Task 4: Warn + confirm flow

**Files:**
- Modify: `src/lintle/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: Task 3's `_extract_one(out_dir, catalog, dest, quarantined)`, `HistoryStats`, `term.is_interactive`, `term.prompt_yes_no`, `term.warning`, `term.note`.
- Produces:
  - `_warn_and_confirm(catalog: int, hs: HistoryStats, had_quarantined: bool | None) -> bool` — emits the warnings; True = proceed (always True when non-interactive; `prompt_yes_no` returning `None` also proceeds, per the `default=True` contract)
  - `_extract_one` return type changes `bool` → `str`: `"written" | "declined" | "absent"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extract.py`:

```python
GAPPY_DAYS = [1.0 + i for i in range(10)] + [50.0, 51.0, 52.0]


def gappy_tree(tmp_path, cat=100):
    return write_import_tree(tmp_path, recs(*[(cat, d) for d in GAPPY_DAYS]), 10000)


class TestWarnConfirm:
    def test_non_tty_warns_and_proceeds(self, tmp_path, capsys):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()
        err = capsys.readouterr().err
        assert "1 gap" in err and "40.0 d" in err
        assert "2020-01-10" in err and "2020-02-19" in err

    def test_interactive_decline_skips_exit_0(self, tmp_path, monkeypatch, capsys):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(
            extract.term, "prompt_yes_no", lambda msg, *, default: False
        )
        assert extract.run(str(out), [100], str(dest)) == 0
        assert not (dest / "100.txt").exists()
        assert not (dest / "100.json").exists()
        assert "skipped 100" in capsys.readouterr().err

    def test_interactive_accept_writes(self, tmp_path, monkeypatch):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(
            extract.term, "prompt_yes_no", lambda msg, *, default: True
        )
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()

    def test_prompt_eof_proceeds(self, tmp_path, monkeypatch):
        out = gappy_tree(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)
        monkeypatch.setattr(
            extract.term, "prompt_yes_no", lambda msg, *, default: None
        )
        assert extract.run(str(out), [100], str(dest)) == 0
        assert (dest / "100.txt").exists()

    def test_clean_history_never_prompts(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(extract.term, "is_interactive", lambda: True)

        def boom(msg, *, default):
            raise AssertionError("prompted on a clean history")

        monkeypatch.setattr(extract.term, "prompt_yes_no", boom)
        assert extract.run(str(out), [100], str(dest)) == 0

    def test_quarantine_only_triggers_warning(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((100, 1.0), (100, 2.0)), 10)
        rdir = out / "03-report"
        rdir.mkdir()
        (rdir / "broken-noradids.ndjson").write_text(
            '{"noradId":100}\n', encoding="ascii"
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert "quarantined during clean" in capsys.readouterr().err

    def test_cap_prints_and_more_line(self, tmp_path, capsys):
        days = [r * 30 + s for r in range(12) for s in (1.0, 2.0, 3.0)]
        out = write_import_tree(tmp_path, recs(*[(100, d) for d in days]), 10000)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        assert "and 1 more" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract.py::TestWarnConfirm -q`
Expected: `test_non_tty_warns_and_proceeds`, decline/accept/EOF, quarantine-warning, and cap tests FAIL (no warnings emitted, no skip). `test_clean_history_never_prompts` may already pass — fine.

- [ ] **Step 3: Implement**

In `src/lintle/extract.py`, add before `_extract_one`:

```python
def _warn_and_confirm(
    catalog: int, hs: HistoryStats, had_quarantined: bool | None
) -> bool:
    """Report the gaps (the GAPS_CAP largest, chronologically) and the
    quarantine flag, then ask to continue. Non-interactive runs warn and
    proceed; Enter and an unusable answer (EOF) both take the default —
    proceed. Only an explicit "n" skips."""
    if hs.gap_count:
        term.warning(
            f"history for {catalog} has {hs.gap_count} gap(s) "
            f"(median spacing {hs.median_spacing_days:.2f} d):"
        )
        for g in hs.gaps:
            term.note(f"  {g.start.date()} → {g.end.date()}  ({g.days:.1f} d)")
        if hs.gap_count > len(hs.gaps):
            term.note(f"  …and {hs.gap_count - len(hs.gaps)} more")
    if had_quarantined:
        term.warning(
            f"records for {catalog} were quarantined during clean — gaps may "
            f"stem from that; see {REPORT_DIRNAME}/report.jsonl"
        )
    if not term.is_interactive():
        return True
    answer = term.prompt_yes_no(
        f"continue export of {catalog}? [Y/n] ", default=True
    )
    return answer is not False
```

In `_extract_one`: change the return type annotation to `-> str`, the
docstring's last sentence to
`Returns "written", "declined" (operator said no), or "absent" (no records).`,
and the body's early returns:

```python
    spans = find_spans(out_dir, catalog)
    if not spans:
        return "absent"
    hs = _analyze(spans)
    had_quarantined = None if quarantined is None else catalog in quarantined
    if (hs.gap_count or had_quarantined) and not _warn_and_confirm(
        catalog, hs, had_quarantined
    ):
        return "declined"
```

and the final `return True` → `return "written"`.

In `run()`, replace the found/missing branch with a `match` (3-way dispatch —
house style) and update the docstring's exit-code sentence to note that a
user-declined skip is not an error:

```python
    for catalog in catalogs:
        try:
            outcome = _extract_one(out_dir, catalog, dest_dir, quarantined)
        except Exception as exc:
            term.error(f"extraction failed for catalog {catalog}: {exc}")
            missing.append(catalog)
            continue
        match outcome:
            case "written":
                term.note(f"wrote {dest_dir / f'{catalog}.txt'}")
            case "declined":
                term.note(f"skipped {catalog}")
            case "absent":
                missing.append(catalog)
                term.error(
                    f"no records for catalog {catalog} in "
                    f"{Path(out_dir) / DEDUP_DIRNAME}"
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): warn on gaps/quarantine, y/n confirm before export"
```

---

### Task 5: Docs + full verification + PR

**Files:**
- Modify: `src/lintle/extract.py` (module docstring + `_README`), `ARCHITECTURE.md`, `CHANGELOG.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the in-code README and module docstring**

In `src/lintle/extract.py`, replace the `_README` sidecar bullet with:

```python
- `<id>.json` — a stats sidecar for that history (record count, epoch span,
  median spacing, reportable gaps, quarantine flag, element-set range).
```

and append to the module docstring's last line: `Warns — and, interactively,
confirms — before exporting a history with reportable gaps or
upstream-quarantined records.`

- [ ] **Step 2: Update ARCHITECTURE.md**

In the `extract.py` row of the module table (line ~116), after
"plus a deterministic `<id>.json` stats sidecar" insert:

```
(schema v2: median spacing, the 10 largest reportable gaps — delta > 10× the
satellite's median spacing — and a tri-state quarantine flag from
`03-report/broken-noradids.ndjson`); warns and, on a TTY, asks y/n before
exporting a gappy or quarantine-affected history (non-TTY: warn + proceed;
decline = skip, not an error)
```

- [ ] **Step 3: Update CHANGELOG.md**

Add under the unreleased notes (alongside the existing extract bullets):

```markdown
- `lintle extract` now warns when a satellite's history has reportable gaps
  (> 10× its median epoch spacing) or records quarantined during `clean`,
  shows the gaps, and — on a TTY — asks y/n before exporting (non-TTY runs
  warn and proceed; declining skips that satellite without an error). The
  `<id>.json` sidecar is schema v2: `median_spacing_days`, `gap_count`,
  `gaps`, `had_quarantined_records`.
```

- [ ] **Step 4: Full verification chain**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Expected: all pass. Report actual output; fix anything that fails before committing.

- [ ] **Step 5: Commit and open the PR**

```bash
git add src/lintle/extract.py ARCHITECTURE.md CHANGELOG.md
git commit -m "docs(extract): gap-awareness — architecture row, changelog, extract README"
git push -u origin feature/extract-gap-awareness
gh pr create --base develop --title "feat(extract): gap awareness — warn, show gaps, confirm before export" --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-07-24-extract-gap-awareness-design.md:
pass-1 analyze / pass-2 copy split, sidecar schema v2 (median spacing, 10
largest gaps, tri-state quarantine flag), warn + y/n confirm (non-TTY: warn +
proceed; decline = skip, exit 0).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then land via rebase-and-merge (`gh pr merge --rebase --delete-branch`) after review, and clean up the worktree:

```bash
cd /Users/andrei/Developer/lintle
git worktree remove .worktrees/feature-extract-gap-awareness
git branch -D feature/extract-gap-awareness
```
