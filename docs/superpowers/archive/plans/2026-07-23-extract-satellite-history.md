# `lintle extract` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lintle extract <noradID>…` writes `<id>.txt` (a satellite's complete deduped TLE history) + `<id>.json` (stats sidecar) by binary-searching the sorted fixed-width `dedup/import.*` chunk set.

**Architecture:** One new read-only leaf `src/lintle/extract.py` (cli → extract → {chunking, fsutil, term, dedup constants, verify.records/epoch/checks parsers}), lazily imported by `cli`'s dispatch. No index artifact: every import record is exactly 140 bytes and the stream is sorted by `(catalog, epoch)`, so span location is seek + bisect. Spec: `docs/superpowers/archive/specs/2026-07-23-extract-satellite-history-design.md`.

**Tech Stack:** Python 3.14 stdlib only (no new deps). pytest. Existing helpers: `ChunkedReader.chunk_paths()`, `fsutil.durable_replace`/`durable_write_text`/`PARTIAL_SUFFIX`, `verify.records.catalog_of`, `verify.epoch.parse_epoch`, `verify.checks.element_set`.

## Global Constraints

- Worktree feature branch `feature/extract-satellite-history` off `develop`; small conventional commits (`feat:`/`test:`/`docs:`); land via rebase-and-merge PR.
- Never import `sgp4` or touch the clean path; `cli` imports `extract` lazily inside its dispatch arm (like `verify`/`dedup`).
- Byte-deterministic outputs: `<id>.txt` is a verbatim slice; `<id>.json` via `json.dumps(..., indent=2, sort_keys=True)` + trailing `\n`, committed with `fsutil` durable writes. No wall-clock anywhere.
- Constant memory: search buffers one 140-byte record; the copy streams in 1 MiB blocks.
- Correctness over recovery: a chunk whose size is not a multiple of 140 → operational error exit 2, nothing written.
- Style: 88-col ruff, one-paragraph docstrings, `dataclass(slots=True, frozen=True)` where used.
- Verify before each commit: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

---

### Task 1: Span location — `extract.py` core search

**Files:**
- Create: `src/lintle/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `chunking.ChunkedReader(directory, stem, suffix).chunk_paths() -> list[Path]`; `dedup.DEDUP_DIRNAME/IMPORT_STEM/IMPORT_SUFFIX`; `verify.records.catalog_of(line1: str) -> int | None`.
- Produces: `RECORD_BYTES = 140`; `ExtractError(RuntimeError)`; `find_spans(out_dir: str, catalog: int) -> list[tuple[Path, int, int]]` — per-chunk `(path, start_record_index, end_record_index)` half-open ranges, `[]` if the catalog is absent; `_import_chunks(out_dir: str) -> list[Path]` (raises `ExtractError` on empty set or `% 140` violation).

- [ ] **Step 1: Write the failing tests** — `tests/test_extract.py`:

```python
"""Tests for lintle extract — per-satellite history from dedup/import chunks."""

import json

import pytest

from lintle import cli, extract
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX


def l1(cat: int, yy: int = 20, day: float = 100.0, elset: int = 1) -> str:
    """A 69-char line 1 with real catalog/epoch/elset columns (checksum fake —
    extract never revalidates; it slices verbatim)."""
    epoch = f"{yy:02d}{day:012.8f}"  # YYDDD.DDDDDDDD -> cols 19-32
    base = f"1 {cat:5d}U 58002B   {epoch}  .00000023  00000-0  28098-4 0 {elset:4d}"
    return (base + "0" * 69)[:69]


def l2(cat: int) -> str:
    base = f"2 {cat:5d} 034.2682 348.7242 1859667 331.7664  19.3264 10.82419157413"
    return (base + "0" * 69)[:69]


def write_import_tree(tmp_path, records, chunk_records=3):
    """Build a fake dedup import chunk set from (line1, line2) pairs, rolling
    every ``chunk_records`` records like ChunkedWriter would."""
    ddir = tmp_path / DEDUP_DIRNAME
    ddir.mkdir(parents=True, exist_ok=True)
    for idx in range((len(records) + chunk_records - 1) // chunk_records or 1):
        chunk = records[idx * chunk_records : (idx + 1) * chunk_records]
        path = ddir / f"{IMPORT_STEM}.{idx + 1:05d}{IMPORT_SUFFIX}"
        path.write_bytes(b"".join(f"{a}\n{b}\n".encode("ascii") for a, b in chunk))
    (ddir / "summary.json").write_text(
        json.dumps({"records_written": len(records), "schema_version": "1"}),
        encoding="ascii",
    )
    return tmp_path


def recs(*cats_epochs):
    """(catalog, day) pairs -> sorted record list, mirroring dedup's order."""
    return [(l1(c, day=d), l2(c)) for c, d in cats_epochs]


class TestFindSpans:
    def test_single_chunk_middle_catalog(self, tmp_path):
        out = write_import_tree(
            tmp_path, recs((100, 1.0), (200, 1.0), (200, 2.0), (300, 1.0)), 10
        )
        spans = extract.find_spans(str(out), 200)
        assert [(s[1], s[2]) for s in spans] == [(1, 3)]

    def test_absent_catalog_returns_empty(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0), (300, 1.0)), 10)
        assert extract.find_spans(str(out), 200) == []

    def test_run_straddles_chunk_seam(self, tmp_path):
        # chunk_records=2: [100, 200] [200, 200] [300, ...]
        out = write_import_tree(
            tmp_path,
            recs((100, 1.0), (200, 1.0), (200, 2.0), (200, 3.0), (300, 1.0)),
            2,
        )
        spans = extract.find_spans(str(out), 200)
        assert [(s[0].name, s[1], s[2]) for s in spans] == [
            (f"{IMPORT_STEM}.00001{IMPORT_SUFFIX}", 1, 2),
            (f"{IMPORT_STEM}.00002{IMPORT_SUFFIX}", 0, 2),
        ]

    def test_first_and_last_catalog_in_set(self, tmp_path):
        out = write_import_tree(
            tmp_path, recs((100, 1.0), (200, 1.0), (300, 1.0), (300, 2.0)), 2
        )
        assert [(s[1], s[2]) for s in extract.find_spans(str(out), 100)] == [(0, 1)]
        assert [(s[1], s[2]) for s in extract.find_spans(str(out), 300)] == [
            (0, 1),
            (0, 1),
        ] or [(s[0].name, s[1], s[2]) for s in extract.find_spans(str(out), 300)] == [
            (f"{IMPORT_STEM}.00002{IMPORT_SUFFIX}", 0, 2)
        ]

    def test_torn_chunk_is_operational_error(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        chunk = out / DEDUP_DIRNAME / f"{IMPORT_STEM}.00001{IMPORT_SUFFIX}"
        chunk.write_bytes(chunk.read_bytes() + b"x")  # 141 bytes — torn
        with pytest.raises(extract.ExtractError, match="not a multiple"):
            extract.find_spans(str(out), 100)

    def test_missing_dedup_tree_is_operational_error(self, tmp_path):
        with pytest.raises(extract.ExtractError, match="lintle dedup"):
            extract.find_spans(str(tmp_path), 100)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_extract.py -qn0` → FAIL: `lintle` has no attribute/module `extract`.

- [ ] **Step 3: Implement the search core** — `src/lintle/extract.py`:

```python
"""`lintle extract` — one satellite's complete deduped TLE history as
``<id>.txt`` + ``<id>.json``. A read-only consumer of a prior `dedup` run: the
``dedup/import.*`` chunk set holds only validated-perfect records (exactly 140
bytes each) globally sorted by ``(catalog, epoch)``, so each satellite is one
contiguous byte range found by pure binary search — the sorted fixed-width
stream *is* the index. Never imports sgp4; never touches the clean path."""

from pathlib import Path

from lintle import fsutil, term
from lintle.chunking import ChunkedReader
from lintle.dedup import DEDUP_DIRNAME, IMPORT_STEM, IMPORT_SUFFIX
from lintle.verify.checks import element_set
from lintle.verify.epoch import parse_epoch
from lintle.verify.records import catalog_of

RECORD_BYTES = 140  # two validated-perfect 69-char lines + two \n — guarded, not assumed


class ExtractError(RuntimeError):
    """Operational failure (missing/torn dedup tree) — cli maps this to exit 2."""


def _import_chunks(out_dir: str) -> list[Path]:
    """The dedup import chunk set, index-ordered, each verified to hold whole
    140-byte records (a torn chunk must never yield sliced records — correctness
    over recovery)."""
    ddir = Path(out_dir) / DEDUP_DIRNAME
    chunks = ChunkedReader(ddir, IMPORT_STEM, IMPORT_SUFFIX).chunk_paths()
    if not chunks:
        raise ExtractError(
            f"no dedup import set under {ddir}.\n"
            "  run 'lintle dedup' first, or point at its --out-dir."
        )
    for chunk in chunks:
        if chunk.stat().st_size % RECORD_BYTES:
            raise ExtractError(
                f"{chunk} size is not a multiple of {RECORD_BYTES} bytes — "
                "corrupted or foreign import chunk; re-run 'lintle dedup'."
            )
    return chunks


def _catalog_at(fh, index: int) -> int:
    """Catalog of record ``index`` in an open chunk (one 140-byte seek+read)."""
    fh.seek(index * RECORD_BYTES)
    line1 = fh.read(RECORD_BYTES)[:69].decode("ascii", errors="replace")
    cat = catalog_of(line1)
    if cat is None:
        raise ExtractError("unparseable catalog in import chunk — corrupted set")
    return cat


def find_spans(out_dir: str, catalog: int) -> list[tuple[Path, int, int]]:
    """Locate ``catalog``'s contiguous run as per-chunk half-open record-index
    ranges ``(chunk_path, lo, hi)`` — ``[]`` if absent. Bisects inside each
    candidate chunk; a run may straddle consecutive chunks (fixed-count rolls
    ignore catalog boundaries)."""
    spans: list[tuple[Path, int, int]] = []
    for chunk in _import_chunks(out_dir):
        n = chunk.stat().st_size // RECORD_BYTES
        if n == 0:
            continue
        with open(chunk, "rb") as fh:
            if _catalog_at(fh, 0) > catalog or _catalog_at(fh, n - 1) < catalog:
                continue
            lo = _bisect(fh, n, lambda c: c >= catalog)
            hi = _bisect(fh, n, lambda c: c > catalog)
        if hi > lo:
            spans.append((chunk, lo, hi))
    return spans


def _bisect(fh, n: int, pred) -> int:
    """First record index whose catalog satisfies ``pred`` (monotone over the
    sorted stream), or ``n`` if none does."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(_catalog_at(fh, mid)):
            hi = mid
        else:
            lo = mid + 1
    return lo
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_extract.py -qn0` → all `TestFindSpans` PASS. (`term`/`fsutil`/`element_set`/`parse_epoch` imports are used by Task 2 — if ruff flags them unused at this commit, add them in Task 2 instead.)

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): span location over the sorted fixed-width import set"
```

---

### Task 2: Extraction + sidecar — `extract.run`

**Files:**
- Modify: `src/lintle/extract.py` (append)
- Test: `tests/test_extract.py` (append)

**Interfaces:**
- Consumes: Task 1's `find_spans`/`_import_chunks`/`RECORD_BYTES`/`ExtractError`; `fsutil.PARTIAL_SUFFIX`, `fsutil.durable_replace(tmp, dest)`, `fsutil.durable_write_text(path, text)`; `verify.epoch.parse_epoch(line1) -> (year, day_of_year)`; `verify.checks.element_set(line1) -> int | None`.
- Produces: `run(out_dir: str, catalogs: list[int], dest: str) -> int` (0 = all extracted, 2 = any missing or operational error) — the function `cli` calls.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_extract.py`):

```python
class TestRun:
    def test_writes_txt_and_json(self, tmp_path, capsys):
        out = write_import_tree(
            tmp_path, recs((200, 1.0), (200, 2.5), (200, 10.0)), 2
        )
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [200], str(dest)) == 0
        txt = (dest / "200.txt").read_bytes()
        assert txt == b"".join(
            f"{a}\n{b}\n".encode("ascii") for a, b in recs((200, 1.0), (200, 2.5), (200, 10.0))
        )
        meta = json.loads((dest / "200.json").read_text(encoding="ascii"))
        assert meta["schema_version"] == "1"
        assert meta["norad_id"] == 200 and meta["records"] == 3
        assert meta["first_epoch"] == "2020-01-01T00:00:00Z"
        assert meta["last_epoch"] == "2020-01-10T00:00:00Z"
        assert meta["span_days"] == 9.0
        assert meta["largest_gap_days"] == 7.5
        assert meta["largest_gap_at"] == "2020-01-10T00:00:00Z"
        assert meta["source"]["dedup_records_written"] == 3

    def test_missing_id_partial_success_exit_2(self, tmp_path, capsys):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100, 424242], str(dest)) == 2
        assert (dest / "100.txt").exists()
        assert not (dest / "424242.txt").exists()
        assert "424242" in capsys.readouterr().err

    def test_single_record_satellite_null_rate(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        assert extract.run(str(out), [100], str(dest)) == 0
        meta = json.loads((dest / "100.json").read_text(encoding="ascii"))
        assert meta["records"] == 1 and meta["span_days"] == 0.0
        assert meta["mean_records_per_day"] is None
        assert meta["largest_gap_days"] == 0.0 and meta["largest_gap_at"] is None

    def test_no_partial_debris_on_success(self, tmp_path):
        out = write_import_tree(tmp_path, recs((100, 1.0)), 10)
        dest = tmp_path / "dest"
        dest.mkdir()
        extract.run(str(out), [100], str(dest))
        assert list(dest.glob("*.partial")) == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_extract.py::TestRun -qn0` → FAIL: no attribute `run`.

- [ ] **Step 3: Implement** (append to `src/lintle/extract.py`):

```python
import datetime as _dt
import json

_COPY_BLOCK = 1 << 20  # 1 MiB — constant-memory streaming copy


def _epoch_dt(line1: str) -> _dt.datetime:
    """Record epoch as an aware UTC datetime — pure arithmetic from
    ``parse_epoch``'s ``(year, day_of_year)``; no wall clock, so sidecar bytes
    stay deterministic."""
    year, day = parse_epoch(line1)
    return _dt.datetime(year, 1, 1, tzinfo=_dt.UTC) + _dt.timedelta(days=day - 1)


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _sidecar(out_dir: str, catalog: int, first, last, n, gap, gap_at) -> str:
    """The ``<id>.json`` document (sorted keys, 2-space indent, trailing LF —
    the house deterministic-JSON shape)."""
    span = (last - first).total_seconds() / 86400.0
    summary_path = Path(out_dir) / DEDUP_DIRNAME / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="ascii"))
    doc = {
        "schema_version": "1",
        "norad_id": catalog,
        "records": n["count"],
        "first_epoch": _iso(first),
        "last_epoch": _iso(last),
        "span_days": round(span, 6),
        "mean_records_per_day": round(n["count"] / span, 6) if span else None,
        "largest_gap_days": round(gap, 6),
        "largest_gap_at": _iso(gap_at) if gap_at is not None else None,
        "element_set_first": n["elset_first"],
        "element_set_last": n["elset_last"],
        "source": {
            "out_dir": str(Path(out_dir)),
            "dedup_records_written": summary.get("records_written"),
            "dedup_schema_version": summary.get("schema_version"),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _extract_one(out_dir: str, catalog: int, dest: Path) -> bool:
    """Extract one satellite: stream its byte range verbatim to
    ``<dest>/<id>.txt`` (durable temp-then-rename) and write the stats sidecar.
    False if the catalog has no records."""
    spans = find_spans(out_dir, catalog)
    if not spans:
        return False
    txt = dest / f"{catalog}.txt"
    tmp = str(txt) + fsutil.PARTIAL_SUFFIX
    first = last = gap_at = None
    prev = None
    gap = 0.0
    stats = {"count": 0, "elset_first": None, "elset_last": None}
    with open(tmp, "wb") as out:
        for chunk, lo, hi in spans:
            with open(chunk, "rb") as fh:
                fh.seek(lo * RECORD_BYTES)
                remaining = (hi - lo) * RECORD_BYTES
                while remaining:
                    block = fh.read(min(_COPY_BLOCK, remaining))
                    out.write(block)
                    remaining -= len(block)
                    # per-record stats over the block (records never split
                    # blocks: both are multiples of RECORD_BYTES)
                    for off in range(0, len(block), RECORD_BYTES):
                        line1 = block[off : off + 69].decode("ascii")
                        dt = _epoch_dt(line1)
                        if first is None:
                            first = dt
                            stats["elset_first"] = element_set(line1)
                        if prev is not None:
                            step = (dt - prev).total_seconds() / 86400.0
                            if step > gap:
                                gap, gap_at = step, dt
                        prev = last = dt
                        stats["elset_last"] = element_set(line1)
                        stats["count"] += 1
    fsutil.durable_replace(tmp, str(txt))
    fsutil.durable_write_text(
        str(dest / f"{catalog}.json"),
        _sidecar(out_dir, catalog, first, last, stats, gap, gap_at),
        encoding="ascii",
    )
    return True


def run(out_dir: str, catalogs: list[int], dest: str) -> int:
    """Extract each catalog's history into ``dest``. Exit 0 if every id was
    found; 2 if any was absent (the others are still written) or on an
    operational error (missing/torn dedup tree — nothing written for it)."""
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    missing = []
    for catalog in catalogs:
        if _extract_one(out_dir, catalog, dest_dir):
            term.note(f"wrote {dest_dir / f'{catalog}.txt'}")
        else:
            missing.append(catalog)
            term.error(f"no records for catalog {catalog} in {Path(out_dir) / DEDUP_DIRNAME}")
    return 2 if missing else 0
```

Move the two `import` lines (`datetime as _dt`, `json`) to the top of the file with the existing imports (ruff will demand it).

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_extract.py -qn0` → all PASS. Check `term.note` exists (it does — `term.py` exposes `error/warning/note`); adjust the missing-ID assertion if `note` writes to stderr too.

- [ ] **Step 5: Commit**

```bash
git add src/lintle/extract.py tests/test_extract.py
git commit -m "feat(extract): verbatim history slice + deterministic stats sidecar"
```

---

### Task 3: CLI wiring — subparser, dispatch, lock

**Files:**
- Modify: `src/lintle/cli.py` (add `_add_extract_subparser` beside `_add_dedup_subparser` ~line 385; add a `case "extract":` arm in the `match args.command` dispatch ~line 650)
- Test: `tests/test_extract.py` (append)

**Interfaces:**
- Consumes: Task 2's `extract.run(out_dir, catalogs, dest) -> int`; `cli._locked_postrun(out_dir, name, action)`; `_apply_config_paths` (out-dir config default — mirror how `dedup`'s `--out-dir`/positional gets its default).
- Produces: `lintle extract <id>… [--out-dir] [--dest]` end-to-end.

- [ ] **Step 1: Write the failing CLI tests** (append to `tests/test_extract.py`):

```python
class TestCli:
    def test_end_to_end(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((200, 1.0), (200, 2.0)), 10)
        dest = tmp_path / "dest"
        monkeypatch.chdir(tmp_path)
        rc = cli.main(
            ["extract", "200", "--out-dir", str(out), "--dest", str(dest)]
        )
        assert rc == 0
        assert (dest / "200.txt").exists() and (dest / "200.json").exists()

    def test_dest_defaults_to_cwd(self, tmp_path, monkeypatch):
        out = write_import_tree(tmp_path, recs((300, 1.0)), 10)
        workdir = tmp_path / "wd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        assert cli.main(["extract", "300", "--out-dir", str(out)]) == 0
        assert (workdir / "300.txt").exists()

    def test_missing_tree_exit_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["extract", "200", "--out-dir", str(tmp_path / "nope")])
        assert rc == 2
        assert "dedup" in capsys.readouterr().err

    def test_rejects_non_numeric_id(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            cli.main(["extract", "ISS", "--out-dir", str(tmp_path)])
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_extract.py::TestCli -qn0` → FAIL: argparse rejects unknown command `extract`.

- [ ] **Step 3: Implement.** In `cli.py`, next to `_add_dedup_subparser`:

```python
def _add_extract_subparser(subparsers):
    """Add the ``extract`` subparser: one satellite's complete deduped TLE
    history from a prior dedup run — ``<id>.txt`` (pure 2-line records,
    epoch-ascending) plus a ``<id>.json`` stats sidecar, per requested id."""
    extract_parser = subparsers.add_parser(
        "extract",
        help="extract one satellite's complete TLE history from a dedup run",
        description=(
            "Extract each NORAD id's complete deduped history from "
            "<out-dir>/dedup into <dest>/<id>.txt (pure TLE lines, "
            "epoch-ascending) and <dest>/<id>.json (stats). Read-only and "
            "local; requires a prior 'lintle dedup' run."
        ),
    )
    extract_parser.add_argument(
        "norad_ids",
        metavar="NORAD-ID",
        nargs="+",
        type=int,
        help="catalog number(s) to extract (1-99999)",
    )
    extract_parser.add_argument(
        "--out-dir",
        metavar="DIR",
        default="data/output",
        help="pipeline output tree holding dedup/ (default: %(default)s)",
    )
    extract_parser.add_argument(
        "--dest",
        metavar="DIR",
        default=".",
        help="where <id>.txt / <id>.json are written (default: cwd)",
    )
```

Register it in `build_parser` alongside the other `_add_*_subparser` calls, mirror the config-default handling `dedup` gets in `_apply_config_paths` (out-dir only — `--dest` is never config-defaulted), and add the dispatch arm next to `case "dedup":`:

```python
        case "extract":
            # Lazy for the wall: extract imports lintle.verify parsers.
            from lintle import extract as extract_mod

            return _locked_postrun(
                args.out_dir,
                "extract",
                lambda: extract_mod.run(args.out_dir, args.norad_ids, args.dest),
            )
```

- [ ] **Step 4: Run the file + the CLI suite** — `uv run pytest tests/test_extract.py tests/test_cli.py -qn0` → PASS (test_cli has help-text golden tests; update them if the new subcommand line changes `--help` output they pin).

- [ ] **Step 5: Commit**

```bash
git add src/lintle/cli.py tests/test_extract.py tests/test_cli.py
git commit -m "feat(cli): lintle extract subcommand — wiring, lock, config default"
```

---

### Task 4: Wall test, docs, changelog, full verification

**Files:**
- Modify: the import-graph test (find it: `grep -rn "import-graph\|ImportGuard" tests/` — likely `tests/test_integration.py::TestImportGuard`) — add `extract` to the read-only-consumer set alongside `dedup`.
- Modify: `ARCHITECTURE.md` §2 module map (one `extract.py` row + one dependency-flow line, patterned on `dedup.py`'s), `CLAUDE.md` project-layout tree (one line), `README.md` usage (one line), `CHANGELOG.md` `[Unreleased]` → `### Added`.
- Test: existing suites.

**Interfaces:**
- Consumes: everything prior. Produces: a landable branch.

- [ ] **Step 1: Extend the import-graph test** — assert the clean path (`pipeline`, `repair`, `tle`, cli-clean closure) does not import `lintle.extract`, and that `lintle.extract` never appears in `sgp4`-adjacent closures — copy the exact pattern the test uses for `dedup` (read it first; match its idiom, don't invent one).

- [ ] **Step 2: Docs.** `ARCHITECTURE.md` module-map row (after `dedup.py`):

```markdown
| `extract.py` | The `lintle extract` pass: reads a prior `dedup` run's sorted fixed-width `import.*` chunk set (140 bytes/record — guarded, never assumed) and binary-searches one catalog's contiguous run into `<dest>/<id>.txt` (verbatim byte slice, epoch-ascending) plus a deterministic `<id>.json` stats sidecar. Read-only, local, no index artifact; reuses `verify`'s `catalog_of`/`parse_epoch`/`element_set` so catalog and epoch keep one definition. |
```

`CHANGELOG.md` under `[Unreleased]`:

```markdown
### Added

- **`lintle extract <noradID>…`** — one satellite's complete deduped TLE
  history as pure `<id>.txt` (epoch-ascending 2-line records) plus a
  deterministic `<id>.json` stats sidecar (record count, epoch range, largest
  gap, elset range, provenance). Binary search over the sorted fixed-width
  `dedup/import` chunks — no index artifact, works on any existing dedup
  output. `--dest` picks the destination (default: cwd); missing ids exit 2.
```

- [ ] **Step 3: Full verification** — `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` → 100% pass, report actual output.

- [ ] **Step 4: Commit + land**

```bash
git add -A
git commit -m "docs(extract): architecture row, changelog, import-graph wall test"
git push -u origin feature/extract-satellite-history
gh pr create --base develop --title "feat: lintle extract — per-satellite TLE history" --body "Implements docs/superpowers/archive/specs/2026-07-23-extract-satellite-history-design.md"
```

- [ ] **Step 5: Acceptance on the real corpus** — from the main checkout: `~/Downloads/tle/.venv-0.10.3/... no — use the worktree: `uv run lintle extract 25544 20580 5 --out-dir ~/Downloads/tle/output --dest /tmp/sat-test` → expect `25544.txt` (ISS), `20580.txt` (Hubble), `5.txt` (Vanguard 1, space-padded catalog) + sidecars; spot-check `25544.json`'s epoch range spans the corpus years and every line in `25544.txt` is 69 chars.

---

## Self-review

- **Spec coverage:** CLI grammar/defaults → T3; 140-byte guard + binary search + straddle → T1; verbatim slice, durable writes, sidecar fields incl. null-span and provenance → T2; exit codes → T2/T3; lock → T3 (`_locked_postrun`); wall + one-definition parser reuse → T1/T4; docs/changelog → T4; non-goals need no tasks. Gap check: none found.
- **Placeholders:** none — every step carries code or an exact command.
- **Type consistency:** `find_spans -> list[tuple[Path, int, int]]` consumed as `(chunk, lo, hi)` in T2; `run(out_dir, catalogs, dest) -> int` consumed by T3's lambda; `ExtractError` raised in T1, surfaced via `_locked_postrun`'s operational-error backstop (it catches `Exception` → exit 2, which satisfies the spec's exit-2 contract without extract-specific cli handling).
