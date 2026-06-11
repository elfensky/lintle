"""Structured-file writers: the ``.broken.txt`` sidecar, ``report.jsonl``
findings shards, the corpus ``broken-noradids.ndjson``, and shard concat."""

import contextlib
import datetime
import json
import shutil
from pathlib import Path

from lintle import __version__, fsutil, stem
from lintle.diagnostics import Diagnostic
from lintle.report import (
    PER_RULE_EXEMPLAR_BOUND,
    FileSample,
    FileStats,
    QuarantineEntry,
    format_diagnostic,
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


def _render_header(src_name: str, quarantined: int, entries: int) -> bytes:
    """Render the three-line ASCII header of a ``.broken.txt`` sidecar.

    ``entries`` is ``paired_records + orphan_entries`` — the count of things
    that became findings or clean output, which is the meaningful denominator
    for ``quarantined``.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | lintle {__version__}\n"
        f"# {quarantined} quarantined of {entries} entries\n\n"
    ).encode("ascii", errors="replace")


class BrokenFileWriter:
    """Streaming writer for the ``.broken.txt`` quarantine sidecar.

    Constant memory: each entry is rendered and flushed to a body temp file
    as ``write_entry`` is called. On ``finalize`` the body is stitched onto
    the now-known header (entry count + corpus total) and atomically renamed
    to the final path. Use as a context manager so an interrupted run never
    leaves a half-written sidecar behind.
    """

    def __init__(self, path: str, src_name: str) -> None:
        self.path = path
        self.src_name = src_name
        self._body_path = path + ".body.partial"
        self._final_partial = path + ".partial"
        self._handle = None
        self._entry_count = 0
        self._completed = False

    def __enter__(self):
        self._handle = open(self._body_path, "wb")
        return self

    def write_entry(self, entry: QuarantineEntry) -> None:
        """Append one ``QuarantineEntry`` to the sidecar body, byte-faithfully."""
        self._entry_count += 1
        self._handle.write(_render_entry(self._entry_count, entry))

    def finalize(self, *, entries: int) -> None:
        """Stitch header + body into the final path; atomic-rename in place.

        ``entries`` is the denominator shown in the header — pass
        ``paired_records + orphan_entries`` from the source file's stats.
        Keyword-only to match :meth:`QuarantineSink.finalize`, so the three
        streaming writers share one ``finalize`` calling convention.
        """
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        header = _render_header(self.src_name, self._entry_count, entries)
        with open(self._final_partial, "wb") as out, open(self._body_path, "rb") as src:
            out.write(header)
            shutil.copyfileobj(src, out, length=65536)
        with contextlib.suppress(OSError):
            Path(self._body_path).unlink()
        fsutil.durable_replace(self._final_partial, self.path)
        self._completed = True

    def __exit__(self, exc_type, exc, tb):
        # Always close the body handle; on any non-finalized exit, discard
        # the partials so an interrupted run leaves no debris.
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        if not self._completed:
            for partial in (self._body_path, self._final_partial):
                with contextlib.suppress(OSError):
                    Path(partial).unlink()
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
        self._partial = path + ".partial"
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
    """

    def __init__(
        self,
        *,
        cap: int = PER_RULE_EXEMPLAR_BOUND,
        broken_path: str | None = None,
        src_name: str | None = None,
        jsonl_path: str | None = None,
    ) -> None:
        self._cap = cap
        self._buckets = {}
        # Per-rule running count of entries dropped because the bucket was
        # at cap. Surfaced on the finalized FileSample as ``dropped_count``
        # so consumers can show "5 of 1,000" without recomputing (issue #46).
        self._dropped = {}
        self._writer = None
        self._jsonl_writer = None
        self._finalized = False
        self._sample = None
        if broken_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when broken_path is set")
            self._writer = BrokenFileWriter(broken_path, src_name)
        if jsonl_path is not None:
            if src_name is None:
                raise ValueError("src_name is required when jsonl_path is set")
            self._jsonl_writer = JsonlFindingsWriter(jsonl_path, src_name)

    def __enter__(self):
        if self._writer is not None:
            self._writer.__enter__()
        if self._jsonl_writer is not None:
            self._jsonl_writer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._writer is not None:
            self._writer.__exit__(exc_type, exc, tb)
        if self._jsonl_writer is not None:
            self._jsonl_writer.__exit__(exc_type, exc, tb)
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
            self._dropped[rule_id] = self._dropped.get(rule_id, 0) + 1
        if self._writer is not None:
            self._writer.write_entry(entry)
        if self._jsonl_writer is not None:
            self._jsonl_writer.write_entry(entry)

    def finalize(self, *, entries: int) -> FileSample:
        """Seal the sink and return the immutable :class:`FileSample`.

        ``entries`` is the denominator shown in the sidecar header
        (``paired_records + orphan_entries``). The sink builds the
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
            self._writer.finalize(entries=entries)
        if self._jsonl_writer is not None:
            self._jsonl_writer.finalize()
        self._sample = FileSample(
            buckets={rid: tuple(items) for rid, items in self._buckets.items()},
            cap=self._cap,
            dropped_count=dict(self._dropped),
        )
        self._finalized = True
        return self._sample


def write_broken_file(path: str, src_name: str, stats: FileStats) -> None:
    """Write the ``.broken.txt`` sidecar from a populated ``FileStats``.

    Thin wrapper that flattens ``stats.quarantine_sample.buckets`` (a per-rule
    immutable mapping built by :class:`QuarantineSink`) and sorts by
    ``source_lines[0]`` so the rendered sidecar matches production
    encounter order. Suitable for tests and small-corpus paths where the
    sampled set fits in memory; production cleaning streams entries
    through :class:`QuarantineSink` (and its owned :class:`BrokenFileWriter`)
    directly so memory stays bounded.
    """
    with BrokenFileWriter(path, src_name) as writer:
        flattened = [
            entry
            for bucket in stats.quarantine_sample.buckets.values()
            for entry in bucket
        ]
        flattened.sort(key=lambda e: e.source_lines[0])
        for entry in flattened:
            writer.write_entry(entry)
        writer.finalize(entries=stats.paired_records + stats.orphan_entries)


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


def concat_findings_shards(
    out_dir: str, dest_path: str, all_stats: list[FileStats]
) -> None:
    """Concatenate per-file findings shards into the corpus ``report.jsonl``.

    Per-worker shards live in ``<out_dir>/.shards/<stem>.findings.jsonl``,
    written by each worker's ``QuarantineSink`` (issue #9). We walk
    ``all_stats`` (already sorted by ``src_name`` in ``cli.py`` before this
    call) so the concatenated order is alphabetical by source filename —
    deterministic and matching ``report.md``'s per-file table. The
    destination is written via tmp + :func:`fsutil.durable_replace` for
    atomicity and power-loss durability (issue #58). Always
    creates the destination even when every shard is empty or missing,
    matching ``broken-noradids.ndjson``'s "artifact always present after
    successful clean" contract. Spec §4.6.

    This function only **reads** the shards — it does not remove ``.shards``.
    Shard cleanup is the caller's responsibility, tied to the resume-checkpoint
    lifecycle (issue #56): ``.shards`` and ``.clean-state.json`` are both
    in-progress run state and must be removed together, only on a fully
    successful run, so an interrupted or failed run keeps its shards and a
    later ``--resume`` can re-read them to rebuild a complete ``report.jsonl``.
    """
    shard_dir = Path(out_dir) / ".shards"
    tmp_path = dest_path + ".partial"
    with open(tmp_path, "wb") as out:
        for stats in all_stats:
            shard = shard_dir / (stem(stats.src_name) + ".findings.jsonl")
            if not shard.exists():
                # Worker crashed before finalize, validate-mode worker, or
                # an out-of-band cleanup removed it — skip silently.
                continue
            with open(shard, "rb") as src:
                shutil.copyfileobj(src, out, length=65536)
    fsutil.durable_replace(tmp_path, dest_path)
