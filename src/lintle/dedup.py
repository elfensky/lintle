"""``lintle dedup`` — emit a de-duplicated 'latest re-issue only' import list.

Space-track republishes the *same* orbit at the *same* epoch with only a bumped
element-set (or revolution) number; the faithful ``01-cleaned/`` archive keeps
every copy by design. ``dedup`` reads that archive (never mutating it) and
writes a single ingest-ready ``import.txt`` under ``<out-dir>/05-dedup``: one card per
``(catalog, epoch)``, keeping the latest re-issue (highest element-set number).

Re-issues — a new element-set at the same epoch, whether an identical or a refined
orbit — collapse to the latest (verify's #158 rule: a new element-set is a benign
successive solution, not a contradiction). A *genuine* contradiction — one
element-set naming two different orbits — is never resolved in silence: the latest
is still written, but the group is flagged in ``notes.jsonl`` and the run exits
non-zero so a human reviews it. When a ``verify`` run's
``suspects.jsonl`` is present, every hard suspect is excluded from the import list
first. Constant memory: records stream through ``verify``'s external sort and only
one ``(catalog, epoch)`` group is held at a time. Output bytes are deterministic.

``dedup`` shares ``verify.checks.orbital_state`` / ``element_set`` so the two
passes agree, byte-for-byte, on 'same orbit' and 'which is latest'."""

import dataclasses
import datetime as _dt
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from lintle import (
    CLEANED_DIRNAME,
    DEDUP_DIRNAME,
    chunking,
    cli_progress,
    fsutil,
    history,
    summary,
    term,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.verify import checks, grouping, records
from lintle.verify.records import CleanedRecord
from lintle.verify.report import SUSPECTS_STEM, SUSPECTS_SUFFIX, VERIFY_DIRNAME

IMPORT_SUFFIX = ".txt"
IMPORT_STEM = "import"
NOTES_SUFFIX = ".jsonl"
NOTES_STEM = "notes"
MANIFEST_STEM = "manifest"
MANIFEST_SUFFIX = ".jsonl"
SUMMARY_NAME = "summary.json"
SCHEMA_VERSION = "2"

_README = """\
# 05-dedup — latest-re-issue-only import list

- `import.NNNNN.txt` — the de-duplicated ingest list: one card per
  (catalog, epoch), hard suspects excluded, re-issues collapsed to the
  latest element-set.
- `notes.NNNNN.jsonl` — one note per collapsed group (the kept and dropped
  cards, and whether it was a genuine same-epoch conflict), plus one
  `DEDUP-UNUSABLE-RECORD` note per record skipped as unimportable
  (corrupt catalog field, unparseable epoch, non-ASCII bytes).
- `manifest.jsonl` — one row per satellite (catalog-ascending): record
  count, epoch span, median spacing, and largest gap — see `history.py`.
- `summary.json` — dedup tallies and verdict.

Regenerate with `lintle dedup`.
"""


@dataclasses.dataclass(slots=True, frozen=True)
class Group:
    """One collapsed ``(catalog, epoch)`` group: the ``kept`` card (latest
    re-issue), the ``dropped`` duplicates, and whether the group holds a genuine
    same-element-set clash (one element-set, two orbits — kept-but-flagged)."""

    kept: CleanedRecord
    dropped: list[CleanedRecord]
    conflict: bool


def _elset_or_min(line1: str) -> int:
    """Element-set number as a sort key; an unparseable one sorts below every
    real number so a parseable re-issue always wins the 'latest' pick."""
    es = checks.element_set(line1)
    return es if es is not None else -1


def _collapse(group: list[CleanedRecord]) -> Group:
    """Keep the highest element-set (ties broken by source position for a
    deterministic pick); the rest are dropped. ``conflict`` iff the group holds a
    genuine same-epoch clash — one element-set naming two orbits — using verify's
    shared #158 predicate, so a *different* element-set with a refined orbit stays
    a benign re-issue rather than a false contradiction (#164). On a wrap
    (9999 -> 0001) the orbit is identical, so keeping the highest is still safe."""
    kept = max(group, key=lambda r: (_elset_or_min(r.line1), r.src_file, r.index))
    dropped = sorted(
        (r for r in group if r is not kept), key=lambda r: (r.src_file, r.index)
    )
    return Group(kept, dropped, checks.has_epoch_clash(group))


def _group_key(rec: CleanedRecord) -> tuple[int, float]:
    """Group key for :func:`grouping.grouped`: one group per ``(catalog, epoch)``."""
    return (rec.catalog, rec.epoch_key)


def _groups(sorted_records: Iterator[CleanedRecord]) -> Iterator[Group]:
    """Collapse a stream sorted by ``(catalog, epoch_key)`` group by group. Holds
    one group at a time — a handful of re-issues in validated ``cleaned/`` output.
    ponytail: a pathological giant group can't occur in validated cleaned records
    (each has a parseable, unique-ish key); a corrupt tree is ``verify``'s job."""
    for _, buf in grouping.grouped(sorted_records, key=_group_key):
        yield _collapse(buf)


def _load_hard_positions(out_dir: str) -> set[tuple[str, int]]:
    """The ``(src_file, index)`` of every hard suspect in a prior ``verify`` run's
    chunked ``suspects.NNNNN.jsonl`` set — excluded from the import list. Empty set
    when no verify run exists (dedup still collapses re-issues). Reads the whole
    chunk set via :class:`ChunkedReader`; reading a single fixed ``suspects.jsonl``
    would silently miss the chunked output and drop every exclusion. ponytail: the
    set is bounded by the hard-suspect count, the rare exception (~0 on healthy
    output), not the norm."""
    reader = chunking.ChunkedReader(
        Path(out_dir) / VERIFY_DIRNAME, SUSPECTS_STEM, SUSPECTS_SUFFIX
    )
    hard: set[tuple[str, int]] = set()
    for raw in reader.iter_lines():
        line = raw.decode("ascii").strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("severity") == "hard":
            hard.add((row["src_file"], row["index"]))
    return hard


def _card(rec: CleanedRecord) -> dict[str, object]:
    return {
        "src_file": rec.src_file,
        "index": rec.index,
        "element_set": checks.element_set(rec.line1),
    }


# notes.jsonl wire token for a record dedup cannot usably import. Telemetry,
# never an exit-code change — matching extract's warn-and-proceed philosophy.
UNUSABLE_RULE = "DEDUP-UNUSABLE-RECORD"


def _unusable_reason(g: Group, epoch: _dt.datetime | None) -> str | None:
    """Why the group's kept record cannot enter the import list, or ``None``
    when it can. The reader (``records._catalog_and_key``) is lenient *by
    design* — an unparseable line yields ``catalog == -1`` and flows on as "a
    finding, not a crash" — so the strict write seam must uphold the same
    policy: a corrupt catalog field, an unparseable epoch, or a U+FFFD from the
    reader's tolerant decode used to crash the run (or poison record 0 of the
    import set, failing every later ``extract``). Alpha-5 ids no longer land
    here — they decode to their integer value and import normally (#203)."""
    if g.kept.catalog < 0:
        return "unparseable catalog or epoch (a corrupt line)"
    if epoch is None:
        return "unparseable epoch"
    if not (g.kept.line1.isascii() and g.kept.line2.isascii()):
        return "non-ASCII bytes in a cleaned line"
    return None


def _unusable_note_bytes(g: Group, reason: str) -> bytes:
    """One compact ASCII JSON note for a skipped-unusable record — fixed key
    order, same byte-determinism contract as :func:`_note_bytes`."""
    note = {
        "schema_version": SCHEMA_VERSION,
        "rule": UNUSABLE_RULE,
        "catalog": g.kept.catalog,
        "src_file": g.kept.src_file,
        "index": g.kept.index,
        "detail": reason,
    }
    return (json.dumps(note, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _note_bytes(g: Group) -> bytes:
    """One compact ASCII JSON note for a collapsed group — fixed key order, so
    two runs over the same output produce identical bytes."""
    note = {
        "schema_version": SCHEMA_VERSION,
        "catalog": g.kept.catalog,
        "epoch_key": g.kept.epoch_key,
        "conflict": g.conflict,
        "kept": _card(g.kept),
        "dropped": [_card(r) for r in g.dropped],
    }
    return (json.dumps(note, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _manifest_row(catalog: int, hs: history.HistoryStats) -> bytes:
    """One compact ASCII JSON manifest row for a satellite — fixed key order so
    reruns are byte-identical. ``median_spacing_days`` is null for <3 records
    (the trivially-gapless case the row's ``records`` field lets a query
    exclude). Gap math comes solely from ``history.analyze_epochs`` — the one
    reduction ``extract`` shares, never recomputed here."""
    span = (hs.last - hs.first).total_seconds() / 86400.0 if hs.count else 0.0
    row = {
        "norad_id": catalog,
        "records": hs.count,
        "first_epoch": history.iso(hs.first) if hs.first else None,
        "last_epoch": history.iso(hs.last) if hs.last else None,
        "span_days": round(span, 6),
        "median_spacing_days": (
            round(hs.median_spacing_days, 6)
            if hs.median_spacing_days is not None
            else None
        ),
        "largest_gap_days": round(hs.largest_gap_days, 6),
        "gap_count": hs.gap_count,
    }
    return (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


class _ManifestBuilder:
    """Accumulates one satellite's epoch/element-set lists at a time and
    flushes a manifest row on each catalog boundary — memory-bounded to a
    single satellite's history (Critical Rule #3), never the whole corpus.
    ``flush`` must also be called once after the final ``add`` to emit the
    last catalog's row."""

    def __init__(self) -> None:
        self._catalog: int | None = None
        self._epochs: list[_dt.datetime] = []
        self._elsets: list[int | None] = []
        self.body = bytearray()
        # Satellites with < MIN_GAP_RECORDS records: gap analysis is
        # definitionally silent for them (one delta has no typical spacing),
        # so the summary carries the corpus-level count — the per-row signal
        # is already derivable from `records` + null median_spacing_days.
        self.gap_silent = 0

    def add(self, catalog: int, epoch: _dt.datetime, elset: int | None) -> None:
        if catalog != self._catalog:
            self.flush()
            self._catalog = catalog
            self._epochs = []
            self._elsets = []
        self._epochs.append(epoch)
        self._elsets.append(elset)

    def flush(self) -> None:
        if self._catalog is not None:
            hs = history.analyze_epochs(self._epochs, self._elsets)
            self.body.extend(_manifest_row(self._catalog, hs))
            if hs.count < history.MIN_GAP_RECORDS:
                self.gap_silent += 1


def run(out_dir: str, chunk_records: int = CHUNK_RECORDS_DEFAULT) -> int:
    """De-duplicate a clean run's ``<out-dir>/01-cleaned`` into the chunked
    ``<out-dir>/05-dedup/import.NNNNN.txt`` set (+ ``notes.NNNNN.jsonl`` and
    ``summary.json``). Returns the exit code: ``0`` clean, ``1`` genuine
    contradiction(s) arbitrated (review the notes), ``2`` operational error
    (no cleaned output)."""
    stems = records.cleaned_stems(out_dir)
    if not stems:
        cleaned_dir = Path(out_dir) / CLEANED_DIRNAME
        term.error(
            f"no cleaned output found under {cleaned_dir!s}.\n"
            "  run 'lintle clean' first, or point at its --out-dir."
        )
        return 2

    # One live table for the whole run — a row per stem before any work starts,
    # filled in as each streams. Sizes come from the same stat-only fingerprint
    # `verify` uses, so the roster and the stream it announces cannot disagree.
    stem_sizes = dict(records.cleaned_fingerprint(out_dir)["stems"])
    # Spinner: this streams and parses a prior verify run's whole suspects
    # chunk set — every severity, filtered after — before the table opens.
    with cli_progress.status("reading verify suspects…"):
        hard = _load_hard_positions(out_dir)
    sorter = grouping.ExternalSorter()
    n_read = n_excluded = 0
    excluded_by_stem: Counter[str] = Counter()
    table = cli_progress.UnitTable(
        stems,
        ("#", "file", "size", "progress", "records", "excluded"),
        console=term.stderr_console,
        drop={"medium": ("size",), "narrow": ("size", "progress")},
    )
    with table:
        for stem in stems:
            table.start(stem)
            size = stem_sizes.get(stem, 0)
            # Per-stem, like verify's: a corpus total in one stem's row would
            # read as that stem's own count.
            file_records = 0
            for rec in records.iter_file(out_dir, stem):
                n_read += 1
                file_records += 1
                # Sparse refresh — one update per record would dominate the loop.
                if file_records % 50_000 == 0:
                    table.update(
                        stem,
                        size=summary.format_size(size),
                        progress=_progress_bar(file_records, size),
                        records=f"{file_records:,}",
                        excluded=f"{excluded_by_stem[stem]:,}",
                    )
                    table.totals(records=f"{n_read:,}")
                if (rec.src_file, rec.index) in hard:
                    n_excluded += 1
                    excluded_by_stem[stem] += 1
                    continue
                sorter.add(rec)
            table.finish(
                stem,
                size=summary.format_size(size),
                progress=cli_progress.bar(size, size),
                records=f"{file_records:,}",
                excluded=f"{excluded_by_stem[stem]:,}",
            )
            table.totals(
                size=summary.format_size(sum(stem_sizes.values())),
                records=f"{n_read:,}",
                excluded=f"{n_excluded:,}",
            )

        code, verdict, ddir, n_conflicts = _write_import_set(
            out_dir, table, sorter, n_read, n_excluded, len(stems), chunk_records
        )
    # After the table closes, so the verdict lands under the results rather than
    # as a print above a live region.
    if code:
        term.error(
            f"dedup: {n_conflicts} genuine contradiction(s) arbitrated — review "
            f"{ddir / 'notes.*.jsonl'!s}\n  {verdict}"
        )
    else:
        term.note(f"dedup: PASS — {verdict}\n  see {ddir / 'import.*.txt'!s}")
    return code


def _progress_bar(records_seen, size):
    """A stem's progress bar, from the record count: a cleaned record is exactly
    two 69-column lines plus newlines, so the count converts to bytes exactly.
    Clamped — a truncated final chunk would read past 100%."""
    if not size:
        return ""
    return cli_progress.bar(records_seen * _RECORD_BYTES, size)


_RECORD_BYTES = 140  # two 69-column lines + two newlines


def _write_import_set(
    out_dir, table, sorter, n_read, n_excluded, n_stems, chunk_records
):
    """Collapse the sorted stream into the import set, reporting through the
    live table's summary label so the stage needs no spinner and no new line.
    Returns the exit code, the verdict text, the output directory, and the
    conflict count — the caller prints the verdict once the table has closed."""
    table.phase("collapsing re-issues and writing 05-dedup…")
    ddir = Path(out_dir) / DEDUP_DIRNAME
    ddir.mkdir(parents=True, exist_ok=True)
    n_written = n_dropped = n_collapsed = n_conflicts = n_unusable = 0
    manifest = _ManifestBuilder()
    # Stream both outputs in sorted (catalog, epoch) order into fixed-count chunk
    # sets — constant memory even when import is corpus-scale (28.7 GB). import is
    # a 2-line-record stream; notes is one JSON line per collapsed group. The
    # manifest accumulates one catalog's epochs/elsets at a time, flushing a row
    # on each catalog boundary (and once more below, for the final catalog).
    with (
        chunking.ChunkedWriter(
            str(ddir), IMPORT_STEM, IMPORT_SUFFIX, chunk_records
        ) as imp,
        chunking.ChunkedWriter(
            str(ddir), NOTES_STEM, NOTES_SUFFIX, chunk_records
        ) as notes,
    ):
        for g in _groups(sorter.sorted_records()):
            try:
                epoch = history.epoch_dt(g.kept.line1)
            except ValueError, IndexError:
                epoch = None
            if (reason := _unusable_reason(g, epoch)) is not None:
                # Skip-and-report: never a crash, never a norad_id:-1 manifest
                # row, never an import record extract's binary search chokes on.
                notes.write(_unusable_note_bytes(g, reason))
                n_unusable += 1
                continue
            imp.write_record(g.kept.line1.encode("ascii"), g.kept.line2.encode("ascii"))
            n_written += 1
            if n_written % 10_000 == 0:  # sparse refresh, as in the read loop
                table.phase(f"writing 05-dedup — {n_written:,} records…")
            if g.dropped:
                notes.write(_note_bytes(g))
                n_collapsed += 1
                n_dropped += len(g.dropped)
                if g.conflict:
                    n_conflicts += 1
            manifest.add(g.kept.catalog, epoch, checks.element_set(g.kept.line1))
    manifest.flush()
    fsutil.durable_write_text(
        str(ddir / f"{MANIFEST_STEM}{MANIFEST_SUFFIX}"),
        manifest.body.decode("ascii"),
        encoding="ascii",
    )

    code = 1 if n_conflicts else 0
    summary = {
        "schema_version": SCHEMA_VERSION,
        "cleaned_fingerprint": records.cleaned_fingerprint(out_dir),
        "cleaned_files": n_stems,
        "records_read": n_read,
        "excluded_hard_suspects": n_excluded,
        "records_written": n_written,
        "records_dropped": n_dropped,
        "groups_collapsed": n_collapsed,
        "conflicts_flagged": n_conflicts,
        "unusable_records": n_unusable,
        "gap_silent_satellites": manifest.gap_silent,
        "exit_code": code,
    }
    body = json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    # Structured artifact — commit through the one sanctioned durable path.
    fsutil.durable_write_text(str(ddir / SUMMARY_NAME), body, encoding="ascii")
    fsutil.durable_write_text(str(ddir / "README.md"), _README, encoding="utf-8")

    # The per-stem rows are already final; the group-level numbers belong to the
    # sorted stream, not to any one stem, so they close the run in the verdict.
    table.phase(None)

    verdict = (
        f"{n_written} records written, {n_dropped} re-issue duplicate(s) collapsed"
    )
    if n_unusable:
        verdict += f", {n_unusable} unusable record(s) skipped (see notes.*.jsonl)"
    return code, verdict, ddir, n_conflicts
