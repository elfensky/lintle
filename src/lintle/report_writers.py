"""Structured-file writers: the ``.broken.txt`` sidecar, ``report.jsonl``
findings shards, the corpus ``broken-noradids.ndjson``, and shard concat."""

import contextlib
import json
from collections import Counter
from pathlib import Path

from lintle import (
    BROKEN_SUFFIX,
    FINDINGS_SUFFIX,
    SHARDS_DIRNAME,
    __version__,
    chunking,
    fsutil,
    stem,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.diagnostics import Diagnostic
from lintle.report import (
    PER_RULE_EXEMPLAR_BOUND,
    FileSample,
    FileStats,
    QuarantineEntry,
    format_diagnostic,
    utc_stamp,
)


def _diagnostic_to_nested(diag: Diagnostic) -> dict[str, object]:
    """Render one :class:`Diagnostic` as a JSON-ready dict (issue #9).

    Used both inside the ``related`` array and as the body of the
    envelope shape produced by :func:`entry_to_jsonl_dict`. ``StrEnum``
    values coerce to their stable wire token (``"TLE-CHK-001"``,
    ``"tier-1"``); tuples become lists (JSON has no tuple type); the
    empty-string default of ``Diagnostic.note`` coerces to JSON ``null``
    so consumers see uniform ``null``-vs-string semantics across the
    three optional string fields.
    """
    return {
        "rule_id": diag.rule_id.value,
        "source_lines": list(diag.source_line_nos),
        "tier_attempted": diag.tier_attempted.value,
        "column_range": list(diag.column_range) if diag.column_range else None,
        "observed": diag.observed,
        "expected": diag.expected,
        "note": diag.note or None,
    }


def entry_to_jsonl_dict(
    entry: QuarantineEntry, *, file: str, norad_id: int | None
) -> dict[str, object]:
    """Render one :class:`QuarantineEntry` as a single ``report.jsonl`` line dict.

    Envelope shape carries ``schema_version`` (``"1"`` for this spec),
    ``outcome`` (always ``"quarantined"`` in v1; reserved for future
    ``"fixed"`` emission), the per-file ``file`` basename, the
    ``norad_id`` decoded at quarantine time, and the primary
    :class:`Diagnostic`'s nested fields spread inline. Secondary
    diagnostics fold into the ``related`` array, each rendered through
    :func:`_diagnostic_to_nested` so the envelope fields appear exactly
    once per line. See spec §4.1 for the field contract.
    """
    nested = _diagnostic_to_nested(entry.primary)
    return {
        "schema_version": "1",
        "outcome": "quarantined",
        "file": file,
        "norad_id": norad_id,
        **nested,
        "related": [_diagnostic_to_nested(d) for d in entry.related],
    }


def _render_entry(index: int, entry: QuarantineEntry) -> bytes:
    """Render one :class:`QuarantineEntry` as the bytes it occupies in ``.broken.txt``.

    Header line cites the primary diagnostic; any related diagnostics fold
    onto indented continuation lines (``    and: ...``). The original raw
    lines follow verbatim — byte-faithful quarantine.
    """
    if len(entry.source_lines) == 2:
        location = f"source lines {entry.source_lines[0]}-{entry.source_lines[1]}"
    else:
        location = f"source line {entry.source_lines[0]}"
    head = f"[{index}] {location} - {format_diagnostic(entry.primary)}\n"
    chunks = [head.encode("ascii", errors="replace")]
    for extra in entry.related:
        chunks.append(
            f"    and: {format_diagnostic(extra)}\n".encode("ascii", errors="replace")
        )
    for raw in entry.raw_lines:
        chunks.append(raw)
        chunks.append(b"\n")
    chunks.append(b"\n")
    return b"".join(chunks)


def _render_header(src_name: str) -> bytes:
    """Render the two-line ASCII header of a ``.broken.txt`` sidecar.

    The header carries only source + provenance, so it is fully known at open
    time and can lead the first chunk of the chunked ``<stem>.NNNNN.broken.txt``
    set (the per-file ``N quarantined of M entries`` counts moved out — they are
    already carried by ``report.json``/``report.md`` — because a count known only
    at end-of-stream cannot lead a chunk that commits as soon as it fills).
    """
    timestamp = utc_stamp()
    return (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | lintle {__version__}\n\n"
    ).encode("ascii", errors="replace")


class BrokenFileWriter:
    """Streaming writer for the chunked ``.broken.txt`` quarantine sidecar.

    Writes the header into the first chunk, then streams each rendered entry as
    one unit through a :class:`chunking.ChunkedWriter`, rolling to
    ``<stem>.NNNNN.broken.txt`` every ``units_per_chunk`` entries. Constant
    memory; each chunk is atomically-durably committed the instant it fills.
    ``directory`` is the ``broken/`` output dir; the filename set is derived from
    ``stem(src_name)``. Use as a context manager so an interrupted run abandons
    the whole set (:meth:`chunking.ChunkedWriter.discard_all`) rather than
    leaving a partial one. Concatenating the set in index order reproduces the
    old single-file bytes (minus the moved count line).
    """

    def __init__(
        self,
        directory: str,
        src_name: str,
        units_per_chunk: int = CHUNK_RECORDS_DEFAULT,
    ) -> None:
        self.directory = directory
        self.src_name = src_name
        self._units = units_per_chunk
        self._writer = None
        self._entry_count = 0
        self._completed = False

    def __enter__(self):
        self._writer = chunking.ChunkedWriter(
            self.directory, stem(self.src_name), BROKEN_SUFFIX, self._units
        )
        self._writer.__enter__()
        self._writer.write_raw(_render_header(self.src_name))
        return self

    def write_entry(self, entry: QuarantineEntry) -> None:
        """Append one ``QuarantineEntry`` to the sidecar, byte-faithfully, as one
        chunk unit."""
        self._entry_count += 1
        self._writer.write(_render_entry(self._entry_count, entry))

    def finalize(self) -> None:
        """Commit the final chunk."""
        if self._writer is not None:
            self._writer.close()
        self._completed = True

    def __exit__(self, exc_type, exc, tb):
        # On any non-finalized exit, abandon the whole chunk set so an
        # interrupted run leaves no partial sidecar behind.
        if self._writer is not None and not self._completed:
            self._writer.discard_all()
        return False


class JsonlFindingsWriter:
    """Streaming writer for one file's structured-findings shard (issue #9).

    Each :meth:`write_entry` call emits one line:
    ``json.dumps(payload, separators=(",", ":"), sort_keys=True,
    ensure_ascii=False) + "\\n"`` — compact (no whitespace), key-sorted
    (so byte output is deterministic across Python dict-iteration changes
    and future refactors), and UTF-8. The underlying file is opened with
    ``encoding="utf-8"`` and ``newline="\\n"`` so the artifact is
    byte-deterministic across platforms (Windows would otherwise
    translate ``\\n`` → ``\\r\\n``). Writes go to a ``.partial`` next to
    the destination; :meth:`finalize` atomically renames it into place.
    Use as a context manager so an interrupted run leaves no debris.
    """

    def __init__(self, path: str, src_name: str) -> None:
        self.path = path
        self.src_name = src_name
        self._partial = path + fsutil.PARTIAL_SUFFIX
        self._handle = None
        self._completed = False

    def __enter__(self):
        self._handle = open(self._partial, "w", encoding="utf-8", newline="\n")
        return self

    def write_entry(self, entry: QuarantineEntry) -> None:
        """Append one ``QuarantineEntry`` to the shard as a JSON line."""
        payload = entry_to_jsonl_dict(
            entry, file=self.src_name, norad_id=entry.norad_id
        )
        line = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
        self._handle.write(line)
        self._handle.write("\n")

    def finalize(self) -> None:
        """Close the partial and atomically, durably rename into place."""
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        fsutil.durable_replace(self._partial, self.path)
        self._completed = True

    def __exit__(self, exc_type, exc, tb):
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        # On any non-finalized exit, discard the partial so an interrupted
        # run leaves no debris.
        if not self._completed:
            with contextlib.suppress(OSError):
                Path(self._partial).unlink()
        return False


class QuarantineSink:
    """File-scoped quarantine sink: bounded sample + optional streaming sidecar.

    Single mutation entry point :meth:`add` enforces the per-rule cap by
    construction — there is no other path into the sample. In ``clean``
    mode the sink owns a :class:`BrokenFileWriter` and streams every
    entry byte-faithfully to ``.broken.txt`` as it arrives; in ``validate``
    mode the writer is absent and the sink is purely in-memory. When
    ``jsonl_path`` is set (issue #9, clean mode) the sink also owns a
    :class:`JsonlFindingsWriter` and emits one structured-findings line
    per entry to a per-file shard. On :meth:`finalize` the sink produces
    an immutable :class:`FileSample` and is sealed — any subsequent
    :meth:`add` raises ``RuntimeError`` so misuse surfaces loudly.

    ``__enter__`` uses an :class:`contextlib.ExitStack` to enter each
    sub-writer in turn (issue #104): if the second writer's ``__enter__``
    raises, the stack unwinds the first writer's ``__exit__`` so no handle
    or partial file is leaked. On success the stack is transferred to
    ``self._stack`` so ``__exit__`` closes everything in reverse order.
    """

    def __init__(
        self,
        *,
        cap: int = PER_RULE_EXEMPLAR_BOUND,
        broken_path: str | None = None,
        src_name: str | None = None,
        jsonl_path: str | None = None,
        chunk_records: int = CHUNK_RECORDS_DEFAULT,
    ) -> None:
        self._cap = cap
        self._buckets = {}
        # Per-rule running count of entries dropped because the bucket was
        # at cap. Surfaced on the finalized FileSample as ``dropped_count``
        # so consumers can show "5 of 1,000" without recomputing (issue #46).
        self._dropped = Counter()
        self._writer = None
        self._jsonl_writer = None
        self._stack = None
        self._finalized = False
        self._sample = None
        if broken_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when broken_path is set")
            # broken_path is the conventional single-file path; the chunked writer
            # derives its <stem>.NNNNN.broken.txt set from the parent dir + stem.
            self._writer = BrokenFileWriter(
                str(Path(broken_path).parent), src_name, chunk_records
            )
        if jsonl_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when jsonl_path is set")
            self._jsonl_writer = JsonlFindingsWriter(jsonl_path, src_name)

    def __enter__(self):
        # Use an ExitStack so a failure mid-__enter__ (e.g. the jsonl writer's
        # open fails after the broken writer is already open) unwinds already-
        # entered writers cleanly — no leaked handles or .partial debris.
        with contextlib.ExitStack() as stack:
            if self._writer is not None:
                stack.enter_context(self._writer)
            if self._jsonl_writer is not None:
                stack.enter_context(self._jsonl_writer)
            # Transfer ownership: pop_all() returns a new stack that now owns
            # the cleanup; assigning it to self._stack means __exit__ will
            # close it later. If anything above raised, the ``with`` block's
            # own cleanup fires first (unwinding whatever was entered).
            self._stack = stack.pop_all()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._stack is not None:
            self._stack.__exit__(exc_type, exc, tb)
        return False

    def add(self, entry: QuarantineEntry) -> None:
        """Record one quarantined entry. Silently drops past cap.

        Drops are not data loss: the operator-visible totals live in
        ``stats.quarantine_counts`` (incremented in the pipeline before this
        call), and the byte-faithful catalog reaches disk via the writer
        regardless of the in-memory cap. Likewise the structured JSONL
        shard receives every entry — the cap governs only the in-memory
        sample. Raises ``RuntimeError`` if the sink has already been
        finalized.
        """
        if self._finalized:
            raise RuntimeError("sink already finalized; cannot add new entries")
        rule_id = entry.primary.rule_id
        bucket = self._buckets.get(rule_id)
        if bucket is None:
            bucket = []
            self._buckets[rule_id] = bucket
        if len(bucket) < self._cap:
            bucket.append(entry)
        else:
            # Cap reached for this rule — track the drop (issue #46) so
            # the finalized sample can surface "K dropped" alongside the
            # bounded examples without consumers recomputing it.
            self._dropped[rule_id] += 1
        if self._writer is not None:
            self._writer.write_entry(entry)
        if self._jsonl_writer is not None:
            self._jsonl_writer.write_entry(entry)

    def finalize(self) -> FileSample:
        """Seal the sink and return the immutable :class:`FileSample`.

        The sink builds the
        ``FileSample`` directly rather than going through
        ``FileSample.from_bounded`` because the sink IS the invariant
        boundary — every bucket here is already capped by construction,
        so re-validation would be busy-work. ``from_bounded`` stays
        strict for test fixtures and any future external construction.
        Idempotent: a second call returns the cached sample.
        """
        if self._finalized:
            return self._sample
        if self._writer is not None:
            self._writer.finalize()
        if self._jsonl_writer is not None:
            self._jsonl_writer.finalize()
        self._sample = FileSample(
            buckets={rid: tuple(items) for rid, items in self._buckets.items()},
            cap=self._cap,
            dropped_count=dict(self._dropped),
        )
        self._finalized = True
        return self._sample


def write_broken_file(
    directory: str,
    src_name: str,
    stats: FileStats,
    units_per_chunk: int = CHUNK_RECORDS_DEFAULT,
) -> None:
    """Write the chunked ``.broken.txt`` sidecar set into ``directory`` from a
    populated ``FileStats``.

    Thin wrapper that flattens ``stats.quarantine_sample.buckets`` (a per-rule
    immutable mapping built by :class:`QuarantineSink`) and sorts by
    ``source_lines[0]`` so the rendered sidecar matches production
    encounter order. Produces ``<stem>.NNNNN.broken.txt`` under ``directory``.
    Suitable for tests and small-corpus paths where the sampled set fits in
    memory; production cleaning streams entries through :class:`QuarantineSink`
    (and its owned :class:`BrokenFileWriter`) directly so memory stays bounded.
    """
    with BrokenFileWriter(directory, src_name, units_per_chunk) as writer:
        flattened = [
            entry
            for bucket in stats.quarantine_sample.buckets.values()
            for entry in bucket
        ]
        flattened.sort(key=lambda e: e.source_lines[0])
        for entry in flattened:
            writer.write_entry(entry)
        writer.finalize()


def aggregate_broken_norad_ids(all_stats: list[FileStats]) -> list[int]:
    """Return the sorted, deduplicated NORAD IDs quarantined corpus-wide."""
    ids = set()
    for stats in all_stats:
        ids |= set(stats.quarantined_norad_ids.counts)
    return sorted(ids)


def format_broken_noradids_ndjson(all_stats: list[FileStats]) -> str:
    """Render the corpus-wide quarantined-NORAD-ID NDJSON as a string.

    One ``{"noradId": N}`` object per line, deduplicated across every
    processed file and sorted ascending so diffs across runs are
    deterministic. NDJSON has no header; an empty string is returned
    when no records were quarantined. The minimal one-field shape is
    deliberately additive — downstream consumers ignore unknown fields,
    so later releases can extend each record without breaking compat.
    """
    lines = [
        json.dumps({"noradId": nid}, separators=(",", ":"))
        for nid in aggregate_broken_norad_ids(all_stats)
    ]
    return "".join(line + "\n" for line in lines)


def write_broken_noradids_ndjson(path: str, all_stats: list[FileStats]) -> None:
    """Write the corpus-wide ``broken-noradids.ndjson`` to ``path``.

    Thin wrapper around ``format_broken_noradids_ndjson`` that pins LF
    line endings so the artifact is byte-deterministic across platforms.
    Written atomically and durably via tmp + :func:`fsutil.durable_replace`
    (issue #58).
    """
    fsutil.durable_write_text(
        path, format_broken_noradids_ndjson(all_stats), encoding="ascii"
    )


def shard_path(out_dir: str, src_name: str) -> Path:
    """Return the per-file findings-shard path
    ``<out_dir>/.shards/<stem>.findings.jsonl``. The single place that
    expression is built, so the pipeline's write side and this module's read
    side (``concat_findings_shards``) can never drift (issue #119); the dirname
    and suffix themselves come from the naming-convention authority in
    ``lintle.__init__``."""
    return Path(out_dir) / SHARDS_DIRNAME / (stem(src_name) + FINDINGS_SUFFIX)


def concat_findings_shards(
    out_dir: str,
    dest_path: str,
    all_stats: list[FileStats],
    chunk_records: int = CHUNK_RECORDS_DEFAULT,
) -> list[str]:
    """Concatenate per-file findings shards into the corpus chunked
    ``report.NNNNN.jsonl`` set.

    Per-worker shards live in ``<out_dir>/.shards/<stem>.findings.jsonl``,
    written by each worker's ``QuarantineSink`` (issue #9). We walk
    ``all_stats`` (already sorted by ``src_name`` in ``cli.py`` before this
    call) so the concatenated order is alphabetical by source filename —
    deterministic and matching ``report.md``'s per-file table. A single
    :class:`chunking.ChunkedWriter` spans the whole ``all_stats`` loop — the
    1M-line chunk boundary does not align with per-stem shard boundaries, so one
    chunk may hold the tail of one shard and the head of the next; each shard is
    read line by line (a finding = one unit) so no record is split across a
    chunk. Each chunk is committed atomically (issue #58). Always creates at
    least the ``.00001`` chunk even when every shard is empty or missing,
    matching the "artifact always present after a successful clean" contract.

    Returns the list of source filenames (``stats.src_name``) whose shard was
    missing but had a non-zero ``quarantined_count`` — a gap the caller should
    surface as a warning (issue #117). An empty list means no gap.

    This function only **reads** the shards — it does not remove ``.shards``.
    Shard cleanup is the caller's responsibility, tied to the resume-checkpoint
    lifecycle (issue #56): ``.shards`` and ``.clean-state.json`` are both
    in-progress run state and must be removed together, only on a fully
    successful run, so an interrupted or failed run keeps its shards and a
    later ``--resume`` can re-read them to rebuild a complete ``report.jsonl``.
    """
    dest = Path(dest_path)
    missing_nonempty: list[str] = []
    with chunking.ChunkedWriter(
        str(dest.parent), dest.stem, dest.suffix, chunk_records
    ) as out:
        for stats in all_stats:
            shard = shard_path(out_dir, stats.src_name)
            if not shard.exists():
                # Worker crashed before finalize, validate-mode worker, or
                # an out-of-band cleanup removed it. When the file had
                # quarantined records the gap is noteworthy — report it so the
                # caller can warn; otherwise it is silent (issue #117).
                if stats.quarantined_count:
                    missing_nonempty.append(stats.src_name)
                continue
            with open(shard, "rb") as src:
                for line in src:
                    out.write(line)  # one JSONL finding = one chunk unit
    return missing_nonempty
