"""Tests for lintle extract — per-satellite history from dedup/import chunks."""

import json

import pytest

from lintle import extract
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
        assert [(s[0].name, s[1], s[2]) for s in extract.find_spans(str(out), 300)] == [
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
