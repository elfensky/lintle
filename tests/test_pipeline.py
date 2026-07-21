"""Tests for lintle.pipeline — streaming I/O, line pairing, file processing."""

import contextlib
import json
import queue

import pytest

from lintle import pipeline, report, report_writers
from lintle.categories import FixClass
from lintle.diagnostics import RuleID


class TestProgressQueue:
    """process_file's progress-queue protocol (issue #53 §6)."""

    def test_emits_unified_progress_messages(self, tmp_path, line1, line2):
        # With a queue, process_file emits FileStarted, then FileProgress
        # deltas, then FileEnded.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        q = queue.Queue()

        pipeline.process_file(str(src), str(out), "clean", q, progress_every=1)

        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        assert msgs[0] == pipeline.FileStarted("tle2099.txt")
        assert msgs[-1] == pipeline.FileEnded("tle2099.txt")
        progress = [m for m in msgs if isinstance(m, pipeline.FileProgress)]
        assert progress, "expected at least one progress message"
        assert all(
            p.name == "tle2099.txt" and p.bytes_delta > 0 and p.records_delta > 0
            for p in progress
        )
        assert sum(p.records_delta for p in progress) == 1  # one record processed
        assert sum(p.bytes_delta for p in progress) == src.stat().st_size  # bytes

    def test_byte_deltas_sum_to_st_size_with_dropped_lines(
        self, tmp_path, line1, line2
    ):
        # Byte deltas track the true file offset, so dropped blank/CR-only
        # separator lines and a missing final newline are all accounted for:
        # the reported bytes still sum to st_size exactly (issue #53).
        src = tmp_path / "tle2099.txt"
        # Blank + CR-only separators between records, no trailing newline.
        src.write_bytes(
            (line1 + "\n\n\r\n" + line2 + "\n" + line1 + "\n\n" + line2).encode("ascii")
        )
        out = tmp_path / "out"
        q = queue.Queue()

        pipeline.process_file(str(src), str(out), "clean", q, progress_every=1)

        progress = []
        while not q.empty():
            msg = q.get_nowait()
            if isinstance(msg, pipeline.FileProgress):
                progress.append(msg)

        assert sum(m.bytes_delta for m in progress) == src.stat().st_size
        assert sum(m.records_delta for m in progress) == 2  # two records processed


class TestIterRecords:
    def test_pairs_simple_records(self, tmp_path, line1, line2):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.RecordCandidate)
        assert records[0].src1 == 1 and records[0].src2 == 2

    def test_pairs_lines_with_leading_whitespace_preserving_raw_bytes(
        self, tmp_path, line1, line2
    ):
        # Leading whitespace must not block pairing: the prefix is matched on a
        # trimmed view, but the RAW bytes (whitespace intact) are carried so
        # repair_line trims and tags leading-trim (#88).
        src = tmp_path / "in.txt"
        src.write_bytes(("  " + line1 + "\n\t" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.RecordCandidate)
        assert records[0].raw_line1 == b"  " + line1.encode("ascii")
        assert records[0].raw_line2 == b"\t" + line2.encode("ascii")

    def test_blank_and_cr_only_lines_dropped(self, tmp_path, line1, line2):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n\n" + "\r\n" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.RecordCandidate)
        # Line numbers count skipped blank/CR lines: line 2 is at source line 4.
        assert records[0].src1 == 1 and records[0].src2 == 4

    def test_whitespace_only_line_dropped(self, tmp_path, line1, line2):
        # A line of spaces/tabs between records is blank — dropped, not
        # quarantined, and it must not orphan the surrounding record.
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n" + "   \t \n" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.RecordCandidate)

    def test_lone_line1_at_eof_is_orphaned(self, tmp_path, line1):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.Orphan)
        assert records[0].diag.rule_id == RuleID.ORPHAN_LINE
        assert records[0].diag.note == "orphan line 1 at end of file"

    def test_two_line1s_orphan_the_first(self, tmp_path, line1):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n" + line1 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert all(isinstance(r, pipeline.Orphan) for r in records)
        assert records[0].diag.rule_id == RuleID.ORPHAN_LINE

    def test_orphan_line2(self, tmp_path, line2):
        src = tmp_path / "in.txt"
        src.write_bytes((line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1 and isinstance(records[0], pipeline.Orphan)

    def test_bad_prefix_line(self, tmp_path, line1, line2):
        src = tmp_path / "in.txt"
        src.write_bytes(("garbage\n" + line1 + "\n" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        orphans = [r for r in records if isinstance(r, pipeline.Orphan)]
        assert any(o.diag.rule_id == RuleID.BAD_PREFIX for o in orphans)
        # The valid record after the garbage line still pairs.
        assert any(isinstance(r, pipeline.RecordCandidate) for r in records)


class TestProcessFile:
    def test_process_file_clean_mode(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        # One clean record, then one checksumless record (both repairable).
        src.write_bytes(
            (
                line1 + "\n" + line2 + "\n" + line1[:68] + "\n" + line2[:68] + "\n"
            ).encode("ascii")
        )
        out = tmp_path / "out"

        # The checksumless record is reconstructed only when opted in (#82).
        stats = pipeline.process_file(
            str(src), str(out), "clean", reconstruct_checksum=True
        )

        assert stats.paired_records == 2
        assert stats.orphan_entries == 0
        assert stats.input_lines_seen == 4
        assert stats.clean_count == 2
        assert stats.quarantined_count == 0
        cleaned = (out / "cleaned" / "tle2099.00001.cleaned.txt").read_text()
        assert cleaned == line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
        assert (out / "broken" / "tle2099.00001.broken.txt").exists()

    def test_leading_whitespace_record_pairs_and_repairs(self, tmp_path, line1, line2):
        # A record whose lines carry leading whitespace must pair and repair
        # via the documented leading-trim fix class — not quarantine as
        # BAD_PREFIX before repair can run (#88, ARCHITECTURE §4).
        src = tmp_path / "tle2099.txt"
        src.write_bytes(("  " + line1 + "\n" + " " + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.paired_records == 1
        assert stats.orphan_entries == 0
        assert stats.clean_count == 1
        assert stats.quarantined_count == 0
        assert stats.fix_counts.get(FixClass.LEADING_TRIM) == 2
        cleaned = (out / "cleaned" / "tle2099.00001.cleaned.txt").read_text()
        assert cleaned == line1 + "\n" + line2 + "\n"

    def test_orphan_does_not_count_as_paired_record(self, tmp_path, line1, line2):
        # One paired record, then an orphan line 1 at EOF. Orphans must not
        # contribute to ``paired_records`` (issue #5) — they live in
        # ``orphan_entries`` instead. ``quarantined_count`` still counts the
        # orphan since orphans are written to ``.broken.txt``.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n" + line1 + "\n").encode("ascii"))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.paired_records == 1
        assert stats.orphan_entries == 1
        assert stats.input_lines_seen == 3
        assert stats.clean_count == 1
        assert stats.quarantined_count == 1
        assert stats.quarantine_counts.get(RuleID.ORPHAN_LINE) == 1

    def test_input_lines_seen_counts_blank_lines(self, tmp_path, line1, line2):
        # ``input_lines_seen`` is the count of physical lines read — including
        # blanks that the pairing loop silently drops. With one blank line
        # between two records the count is 5 (line1, line2, blank, line1, line2).
        src = tmp_path / "tle2099.txt"
        src.write_bytes(
            (line1 + "\n" + line2 + "\n\n" + line1 + "\n" + line2 + "\n").encode(
                "ascii"
            )
        )
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.paired_records == 2
        assert stats.orphan_entries == 0
        assert stats.input_lines_seen == 5

    def test_process_file_quarantines_bad_record(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"  # 69 chars, wrong checksum
        src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantine_counts.get(RuleID.CHECKSUM_MISMATCH) == 1
        broken_bytes = (out / "broken" / "tle2099.00001.broken.txt").read_bytes()
        assert b"TLE-CHK-001" in broken_bytes

    def test_zero_cleaned_with_broken_still_writes_empty_cleaned_chunk(
        self, tmp_path, line1, line2
    ):
        # Debate golden test: a stem with 0 cleaned records but some broken ones
        # still gets exactly one empty cleaned/.00001.cleaned.txt chunk (a stream
        # is always a non-empty set on disk), and the concatenation of the cleaned
        # set is byte-empty (concat-identity for an empty stream). The content is
        # carried entirely by the broken set.
        from lintle import chunking

        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"  # 69 chars, wrong checksum → quarantined
        src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.clean_count == 0
        assert stats.quarantined_count == 1
        cleaned_chunk = out / "cleaned" / "tle2099.00001.cleaned.txt"
        assert cleaned_chunk.is_file()
        assert cleaned_chunk.read_bytes() == b""
        # Concat-identity: joining the cleaned chunk set reproduces the (empty)
        # single-file bytes.
        reader = chunking.ChunkedReader(out / "cleaned", "tle2099", ".cleaned.txt")
        joined = b"".join(p.read_bytes() for p in reader.chunk_paths())
        assert joined == b""
        # The broken set carries the quarantined record.
        assert (out / "broken" / "tle2099.00001.broken.txt").read_bytes() != b""

    def test_validate_mode_writes_nothing(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "validate")

        assert stats.clean_count == 1
        assert not out.exists()  # validate mode never creates the output dir

    def test_clean_mode_writes_jsonl_shard(self, tmp_path, line1, line2):
        # Issue #9: clean mode writes a per-file findings shard to
        # ``<out_dir>/.shards/<stem>.findings.jsonl`` alongside the
        # cleaned and broken outputs.
        bad_line2 = line2[:-1] + ("9" if line2[-1] != "9" else "0")
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + bad_line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        pipeline.process_file(str(src), str(out), "clean")

        shard = out / ".shards" / "tle2099.findings.jsonl"
        assert shard.exists()
        with open(shard, encoding="utf-8") as handle:
            lines = handle.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["schema_version"] == "1"
        assert parsed["outcome"] == "quarantined"
        assert parsed["file"] == "tle2099.txt"

    def test_validate_mode_skips_jsonl_shard(self, tmp_path, line1, line2):
        # Issue #9: validate mode emits no JSONL shard.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        pipeline.process_file(str(src), str(out), "validate")
        assert not out.exists()  # no .shards/, no nothing

    def test_jsonl_write_failure_after_counters_advance(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Issue #9 spec §8.7: if the JSONL writer raises mid-stream,
        # (a) the exception propagates, (b) the .partial is unlinked by
        # __exit__'s abnormal-exit branch, (c) no broken sidecar is
        # finalized — sink.finalize never runs because an exception
        # interrupted the record loop. The cleaned tmp is unlinked too.
        bad_line2 = line2[:-1] + ("9" if line2[-1] != "9" else "0")
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + bad_line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        def boom(self, entry):
            raise OSError("simulated jsonl write failure")

        monkeypatch.setattr(report_writers.JsonlFindingsWriter, "write_entry", boom)

        with pytest.raises(OSError, match="simulated jsonl write failure"):
            pipeline.process_file(str(src), str(out), "clean")

        # No partials linger after the abnormal-exit cleanup runs.
        assert not list(out.rglob("*.partial"))
        # No published cleaned file (the os.replace happens after the
        # record loop, which we never reached).
        assert not list(out.rglob("*.cleaned.txt"))

    def test_internal_error_is_quarantined_not_raised(
        self, tmp_path, line1, line2, monkeypatch
    ):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(pipeline.repair, "repair_record", boom)
        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantine_counts.get(RuleID.INTERNAL_ERROR) == 1

    def test_clean_run_leaves_no_temp_file(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean")
        assert not list(out.rglob("*.partial"))  # temp file was renamed away
        # The published cleaned file is world-readable, not owner-only (0600).
        cleaned = out / "cleaned" / "tle2099.00001.cleaned.txt"
        assert cleaned.stat().st_mode & 0o044  # group/other read bits set

    def test_failed_run_does_not_leak_temp_file(self, tmp_path):
        # A non-existent source makes iter_records raise when it opens the file.
        out = tmp_path / "out"
        with pytest.raises(OSError):
            pipeline.process_file(
                str(tmp_path / "does_not_exist.txt"), str(out), "clean"
            )
        assert out.exists()  # the output dir was created
        assert not list(out.rglob("*.partial"))  # but no partial temp file leaked

    def test_process_file_pushes_progress_to_queue(self, tmp_path, line1, line2):
        # With a queue, process_file streams FileProgress deltas; the record
        # deltas sum to the exact total — a partial trailing batch included —
        # so the caller can render an accurate live count.
        src = tmp_path / "tle2099.txt"
        src.write_bytes(
            ((line1 + "\n" + line2 + "\n") * 3).encode("ascii")
        )  # 3 records
        progress = queue.Queue()
        pipeline.process_file(
            str(src),
            str(tmp_path / "out"),
            "clean",
            progress_queue=progress,
            progress_every=2,
        )
        messages = []
        while not progress.empty():
            messages.append(progress.get_nowait())
        # FileProgress record deltas sum to the exact total (one flush of 2
        # records + a trailing flush of 1).
        record_deltas = [
            m.records_delta for m in messages if isinstance(m, pipeline.FileProgress)
        ]
        assert sum(record_deltas) == 3

    def test_process_file_emits_start_and_end_events(self, tmp_path, line1, line2):
        # The display needs to know which files are in flight to surface
        # filenames in its live line. process_file frames each run with
        # ('start', src_name) and ('end', src_name) on the same queue.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        progress = queue.Queue()
        pipeline.process_file(
            str(src),
            str(tmp_path / "out"),
            "clean",
            progress_queue=progress,
            progress_every=2,
        )
        messages = []
        while not progress.empty():
            messages.append(progress.get_nowait())
        events = [
            m
            for m in messages
            if isinstance(m, (pipeline.FileStarted, pipeline.FileEnded))
        ]
        assert pipeline.FileStarted("tle2099.txt") in events
        assert pipeline.FileEnded("tle2099.txt") in events
        # Start must precede end so the display's active set is transiently
        # populated rather than instantly cancelled.
        assert events.index(pipeline.FileStarted("tle2099.txt")) < events.index(
            pipeline.FileEnded("tle2099.txt")
        )

    def test_sink_enter_failure_does_not_leak_cleaned_partial(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Issue #104: if QuarantineSink.__enter__ (or a writer it enters)
        # raises, the cleaned .partial must not be left behind.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        def boom(self):
            raise OSError("simulated sink enter failure")

        monkeypatch.setattr(report_writers.QuarantineSink, "__enter__", boom)

        with pytest.raises(OSError, match="simulated sink enter failure"):
            pipeline.process_file(str(src), str(out), "clean")

        # No .partial should survive a failed __enter__.
        assert not list(out.rglob("*.partial"))

    def test_end_event_emitted_even_on_failure(self, tmp_path):
        # A failing open would otherwise leave FileStarted lingering forever in
        # the display's active set — emit FileEnded from a finally so cleanup
        # is unconditional.
        progress = queue.Queue()
        with contextlib.suppress(Exception):
            pipeline.process_file(
                str(tmp_path / "missing.txt"),
                str(tmp_path / "out"),
                "clean",
                progress_queue=progress,
                progress_every=2,
            )
        messages = []
        while not progress.empty():
            messages.append(progress.get_nowait())
        events = [m for m in messages if isinstance(m, pipeline.FileEnded)]
        assert pipeline.FileEnded("missing.txt") in events

    def test_progress_disabled_when_every_is_zero(self, tmp_path, line1, line2):
        # progress_every=0 disables reporting: nothing reaches the queue —
        # neither record deltas nor lifecycle events.
        src = tmp_path / "tle2099.txt"
        src.write_bytes(((line1 + "\n" + line2 + "\n") * 3).encode("ascii"))
        progress = queue.Queue()
        pipeline.process_file(
            str(src),
            str(tmp_path / "out"),
            "clean",
            progress_queue=progress,
            progress_every=0,
        )
        assert progress.empty()


class TestStreamingQuarantines:
    """The constant-memory invariant: each ``RuleID`` bucket in
    ``stats.quarantine_sample.buckets`` stays bounded even on quarantine-heavy
    files, while the on-disk ``.broken.txt`` catalog is complete. The
    bound is now enforced structurally by :class:`report_writers.QuarantineSink`
    (issue #19) — these tests exercise the invariant end-to-end through
    ``process_file``.
    """

    def test_exemplars_bucketed_per_rule_with_complete_broken_catalog(self, tmp_path):
        # Far more bad-prefix orphans than the per-rule exemplar bound —
        # the full catalog must reach disk; only the in-memory bucket caps.
        n = report.PER_RULE_EXEMPLAR_BOUND + 1500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        # Full counters reflect every quarantine…
        assert stats.quarantined_count == n
        assert stats.quarantine_counts.get(RuleID.BAD_PREFIX) == n
        # …but the in-memory bucket for that rule is capped at the bound.
        assert (
            len(stats.quarantine_sample.buckets[RuleID.BAD_PREFIX])
            == report.PER_RULE_EXEMPLAR_BOUND
        )
        # The on-disk catalog's trailing entry reflects every quarantined
        # record — none were dropped due to the in-memory cap.
        broken = (out / "broken" / "tle2099.00001.broken.txt").read_bytes()
        last = f"junk {n - 1:08d}".encode("ascii")
        assert last in broken

    def test_validate_mode_bucket_caps_per_rule(self, tmp_path):
        # In validate mode no sidecar is written, but each per-rule bucket
        # still caps so peak memory does not grow with quarantine count.
        n = report.PER_RULE_EXEMPLAR_BOUND + 500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.quarantined_count == n
        assert (
            len(stats.quarantine_sample.buckets[RuleID.BAD_PREFIX])
            == report.PER_RULE_EXEMPLAR_BOUND
        )

    def test_rare_rules_preserved_under_skew(self, tmp_path):
        # Feed 1000 bad-prefix quarantines then a smaller batch of a different
        # rule. With per-rule buckets, both appear in stats.quarantine_sample.
        many = 1000
        few = 3
        lines = [f"junk {i:08d}".encode("ascii") for i in range(many)]
        # Append a few orphan line-1 records (no following line-2): these
        # land in RuleID.ORPHAN_LINE, distinct from BAD_PREFIX.
        tle_line1 = (
            "1 {i:05d}U 24001A   24001.00000000  .00000000  00000-0  00000-0 0  0001"
        )
        lines.extend(tle_line1.format(i=i).encode("ascii") for i in range(few))
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(lines))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        # Both rules appear in the sample — the old flat buffer's failure
        # mode is gone.
        assert RuleID.BAD_PREFIX in stats.quarantine_sample.buckets
        assert RuleID.ORPHAN_LINE in stats.quarantine_sample.buckets
        # The rare rule has all its occurrences (well under the cap).
        assert len(stats.quarantine_sample.buckets[RuleID.ORPHAN_LINE]) == few

    def test_internal_error_rule_bucketed_like_data_defects(
        self, tmp_path, monkeypatch
    ):
        # Force ``repair.repair_record`` to raise so every paired record
        # lands in RuleID.INTERNAL_ERROR. With many more quarantines than the
        # cap, the bucket caps just like a data-defect rule.
        n = report.PER_RULE_EXEMPLAR_BOUND + 5
        line1_tmpl = (
            "1 {i:05d}U 24001A   24001.00000000  .00000000  00000-0  00000-0 0  0001"
        )
        line2_tmpl = (
            "2 {i:05d}  51.6000 000.0000 0001000   0.0000   0.0000 15.50000000000001"
        )
        lines = []
        for i in range(n):
            lines.append(line1_tmpl.format(i=i).encode("ascii"))
            lines.append(line2_tmpl.format(i=i).encode("ascii"))
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(lines))

        from lintle import repair

        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic per-record failure")

        monkeypatch.setattr(repair, "repair_record", _boom)

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.quarantine_counts.get(RuleID.INTERNAL_ERROR) == n
        assert (
            len(stats.quarantine_sample.buckets[RuleID.INTERNAL_ERROR])
            == report.PER_RULE_EXEMPLAR_BOUND
        )


class TestOversizedLine:
    """Issue #95 — a newline-free or CR-only multi-GB file must not be
    materialised as one giant bytes object. ``iter_records`` must cap line
    length at ``_MAX_LINE_BYTES`` so constant-memory is preserved."""

    def test_single_oversized_line_yields_one_orphan(self, tmp_path):
        # A file whose sole content is 10× the cap with no newline must
        # yield exactly one Orphan (LINE_LENGTH), not buffer the whole thing.
        cap = pipeline._MAX_LINE_BYTES
        payload = b"X" * (10 * cap)
        src = tmp_path / "huge.txt"
        src.write_bytes(payload)

        records = list(pipeline.iter_records(str(src)))

        assert len(records) == 1
        assert isinstance(records[0], pipeline.Orphan)
        assert records[0].diag.rule_id == RuleID.LINE_LENGTH
        assert "exceeds" in records[0].diag.note
        assert len(records[0].raw_line) <= cap

    def test_oversized_bytes_consumed_equals_file_size(self, tmp_path):
        # bytes_consumed must still reach st_size even when the line is
        # oversized and the remainder is discarded without being yielded.
        cap = pipeline._MAX_LINE_BYTES
        payload = b"Y" * (10 * cap)
        src = tmp_path / "huge.txt"
        src.write_bytes(payload)
        from lintle import report as rpt

        stats = rpt.FileStats(src_name="huge.txt")

        list(pipeline.iter_records(str(src), stats))

        assert stats.bytes_consumed == src.stat().st_size

    def test_oversized_counts_as_one_input_line(self, tmp_path):
        # input_lines_seen must be 1 for a file with one oversized line.
        cap = pipeline._MAX_LINE_BYTES
        src = tmp_path / "huge.txt"
        src.write_bytes(b"Z" * (5 * cap))
        from lintle import report as rpt

        stats = rpt.FileStats(src_name="huge.txt")

        list(pipeline.iter_records(str(src), stats))

        assert stats.input_lines_seen == 1

    def test_normal_lines_unchanged_around_oversized(self, tmp_path, line1, line2):
        # An oversized line sandwiched between two valid TLE records must
        # quarantine the oversized line but still pair the surrounding records.
        cap = pipeline._MAX_LINE_BYTES
        payload = (
            (line1 + "\n").encode("ascii")
            + (line2 + "\n").encode("ascii")
            + b"B" * (5 * cap)
            + b"\n"
            + (line1 + "\n").encode("ascii")
            + (line2 + "\n").encode("ascii")
        )
        src = tmp_path / "mixed.txt"
        src.write_bytes(payload)

        records = list(pipeline.iter_records(str(src)))

        pairs = [r for r in records if isinstance(r, pipeline.RecordCandidate)]
        orphans = [r for r in records if isinstance(r, pipeline.Orphan)]
        assert len(pairs) == 2
        assert len(orphans) == 1
        assert orphans[0].diag.rule_id == RuleID.LINE_LENGTH

    def test_oversized_between_l1_and_l2_orphans_all(self, tmp_path, line1, line2):
        # An oversized line BETWEEN a line-1 and its line-2 must break pairing:
        # line-1 and line-2 must NOT pair across the corruption (#95 — the
        # oversized branch flushes `held`). Distinct from the sandwich layout,
        # where `held` is already None at the oversized line.
        cap = pipeline._MAX_LINE_BYTES
        payload = (
            (line1 + "\n").encode("ascii")
            + b"X" * (5 * cap)
            + b"\n"
            + (line2 + "\n").encode("ascii")
        )
        src = tmp_path / "split.txt"
        src.write_bytes(payload)

        records = list(pipeline.iter_records(str(src)))

        pairs = [r for r in records if isinstance(r, pipeline.RecordCandidate)]
        orphans = [r for r in records if isinstance(r, pipeline.Orphan)]
        assert len(pairs) == 0  # must not pair across the oversized line
        assert len(orphans) == 3  # held line-1, the oversized line, the line-2
        assert [o.diag.rule_id for o in orphans] == [
            RuleID.ORPHAN_LINE,
            RuleID.LINE_LENGTH,
            RuleID.ORPHAN_LINE,
        ]

    def test_oversized_excerpt_bounded(self, tmp_path):
        # The raw_line on the emitted Orphan must not exceed _MAX_LINE_BYTES.
        cap = pipeline._MAX_LINE_BYTES
        src = tmp_path / "huge2.txt"
        src.write_bytes(b"A" * (20 * cap))

        records = list(pipeline.iter_records(str(src)))

        assert isinstance(records[0], pipeline.Orphan)
        assert len(records[0].raw_line) <= cap

    def test_normal_multiline_file_unchanged(self, tmp_path, line1, line2):
        # Introducing the cap must not alter byte handling of normal-length
        # lines. A two-record file must pair, and raw bytes must be identical.
        body = (line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n").encode(
            "ascii"
        )
        src = tmp_path / "normal.txt"
        src.write_bytes(body)

        records = list(pipeline.iter_records(str(src)))

        assert len(records) == 2
        assert all(isinstance(r, pipeline.RecordCandidate) for r in records)
        assert records[0].raw_line1 == line1.encode("ascii")
        assert records[0].raw_line2 == line2.encode("ascii")


class TestQuarantinedNoradIds:
    """The corpus-wide ``broken-noradids.ndjson`` feed (and the per-NORAD
    breakdown in ``report.md``): extract NORAD IDs from quarantined records'
    line 1, when that line is readable, and bucket them by rule ID.
    """

    def test_extracts_id_from_record_quarantine(self, tmp_path, line1, line2):
        # A 2-line record with a wrong checksum gets quarantined; line 1 is
        # otherwise intact, so the catalog number must be recovered.
        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"
        src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        # canonical NORAD 00005; the rule is checksum-mismatch since the
        # only defect was the tampered column-69 digit.
        assert stats.quarantined_norad_ids.counts == {5: {RuleID.CHECKSUM_MISMATCH: 1}}

    def test_extracts_id_from_orphan_line1(self, tmp_path, line1):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n").encode("ascii"))  # lone line 1 at EOF

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids.counts == {5: {RuleID.ORPHAN_LINE: 1}}

    def test_orphan_line2_is_skipped(self, tmp_path, line2):
        # An orphan line 2 has no line 1 to read — the issue contract is
        # explicit: line 1 unrecoverable -> omit from the map.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids.counts == {}

    def test_bad_prefix_orphan_is_skipped(self, tmp_path):
        # A line that doesn't start with "1 " or "2 " is unparseable as a
        # TLE line 1 — no NORAD ID is recoverable.
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"garbage line\n")

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids.counts == {}

    def test_clean_records_do_not_populate_the_map(self, tmp_path, line1, line2):
        # Only quarantined records contribute — a fully clean file
        # produces an empty per-NORAD map.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.clean_count == 1
        assert stats.quarantined_norad_ids.counts == {}

    def test_multiple_quarantines_for_same_id_accrue_per_rule(
        self, tmp_path, line1, line2
    ):
        # Two checksum-mismatched records for the same NORAD ID should
        # surface as one entry with a count of 2 under the same rule,
        # confirming the per-rule accumulator advances rather than
        # overwriting on each call.
        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"
        body = (bad_line1 + "\n" + line2 + "\n") * 2
        src.write_bytes(body.encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 2
        assert stats.quarantined_norad_ids.counts == {5: {RuleID.CHECKSUM_MISMATCH: 2}}

    def test_two_distinct_rules_for_same_id_accrue_independently(
        self, tmp_path, line1, line2
    ):
        # Same NORAD ID quarantined under two different rules in the same
        # file: each per-rule bucket must accumulate independently rather
        # than the second overwriting the first. Mix a paired record with
        # a tampered checksum (TLE-CHK-001) and a trailing lone line 1
        # (TLE-PAIR-001) — both surface NORAD 5.
        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"
        body = bad_line1 + "\n" + line2 + "\n" + line1 + "\n"
        src.write_bytes(body.encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 2
        assert stats.quarantined_norad_ids.counts == {
            5: {
                RuleID.CHECKSUM_MISMATCH: 1,
                RuleID.ORPHAN_LINE: 1,
            }
        }
