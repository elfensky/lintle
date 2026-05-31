"""Tests for lintle.pipeline — streaming I/O, line pairing, file processing."""

import contextlib
import json
import queue

import pytest

from lintle import pipeline, report, report_writers
from lintle.diagnostics import RuleID


class TestProgressQueue:
    """process_file's progress-queue protocol (issue #53 §6)."""

    def test_emits_unified_progress_messages(self, tmp_path, line1, line2):
        # With a queue, process_file emits start, then
        # ("progress", name, bytes_delta, records_delta), then end.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        q = queue.Queue()

        pipeline.process_file(str(src), str(out), "clean", q, progress_every=1)

        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        assert msgs[0] == ("start", "tle2099.txt")
        assert msgs[-1] == ("end", "tle2099.txt")
        progress = [m for m in msgs if m[0] == "progress"]
        assert progress, "expected at least one progress message"
        assert all(
            name == "tle2099.txt" and b > 0 and r > 0
            for (_kind, name, b, r) in progress
        )
        assert sum(m[3] for m in progress) == 1  # one record processed
        assert sum(m[2] for m in progress) == src.stat().st_size  # bytes == file

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
            if msg[0] == "progress":
                progress.append(msg)

        assert sum(m[2] for m in progress) == src.stat().st_size
        assert sum(m[3] for m in progress) == 2  # two records processed


class TestIterRecords:
    def test_pairs_simple_records(self, tmp_path, line1, line2):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert len(records) == 1
        assert isinstance(records[0], pipeline.RecordCandidate)
        assert records[0].src1 == 1 and records[0].src2 == 2

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

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.paired_records == 2
        assert stats.orphan_entries == 0
        assert stats.input_lines_seen == 4
        assert stats.clean_count == 2
        assert stats.quarantined_count == 0
        cleaned = (out / "cleaned" / "tle2099.cleaned.txt").read_text()
        assert cleaned == line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
        assert (out / "broken" / "tle2099.broken.txt").exists()

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
        assert stats.reject_counts.get(RuleID.ORPHAN_LINE) == 1

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
        assert stats.reject_counts.get(RuleID.CHECKSUM_MISMATCH) == 1
        broken_bytes = (out / "broken" / "tle2099.broken.txt").read_bytes()
        assert b"TLE-CHK-001" in broken_bytes

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

        monkeypatch.setattr(pipeline.repair, "process_record", boom)
        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.reject_counts.get(RuleID.INTERNAL_ERROR) == 1

    def test_clean_run_leaves_no_temp_file(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean")
        assert not list(out.rglob("*.partial"))  # temp file was renamed away
        # The published cleaned file is world-readable, not owner-only (0600).
        cleaned = out / "cleaned" / "tle2099.cleaned.txt"
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
        # With a queue, process_file streams ("progress", name, bytes, records)
        # deltas; the record deltas sum to the exact total — a partial trailing
        # batch included — so the caller can render an accurate live count.
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
        # ("progress", name, bytes_delta, records_delta): record deltas sum to
        # the exact total (one flush of 2 records + a trailing flush of 1).
        record_deltas = [m[3] for m in messages if m[0] == "progress"]
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
        events = [m for m in messages if isinstance(m, tuple)]
        assert ("start", "tle2099.txt") in events
        assert ("end", "tle2099.txt") in events
        # Start must precede end so the display's active set is transiently
        # populated rather than instantly cancelled.
        assert events.index(("start", "tle2099.txt")) < events.index(
            ("end", "tle2099.txt")
        )

    def test_end_event_emitted_even_on_failure(self, tmp_path):
        # A failing open would otherwise leave 'start' lingering forever in
        # the display's active set — emit 'end' from a finally so cleanup
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
        events = [m for m in messages if isinstance(m, tuple)]
        assert ("end", "missing.txt") in events

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


class TestStreamingRejects:
    """The constant-memory invariant: each ``RuleID`` bucket in
    ``stats.reject_sample.buckets`` stays bounded even on reject-heavy
    files, while the on-disk ``.broken.txt`` catalog is complete. The
    bound is now enforced structurally by :class:`report_writers.RejectSink`
    (issue #19) — these tests exercise the invariant end-to-end through
    ``process_file``.
    """

    def test_exemplars_bucketed_per_rule_with_complete_broken_catalog(self, tmp_path):
        # Far more bad-prefix orphans than the per-rule exemplar bound —
        # the full catalog must reach disk; only the in-memory bucket caps.
        n = report._PER_RULE_EXEMPLAR_BOUND + 1500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        # Full counters reflect every reject…
        assert stats.quarantined_count == n
        assert stats.reject_counts.get(RuleID.BAD_PREFIX) == n
        # …but the in-memory bucket for that rule is capped at the bound.
        assert (
            len(stats.reject_sample.buckets[RuleID.BAD_PREFIX])
            == report._PER_RULE_EXEMPLAR_BOUND
        )
        # The on-disk catalog header and trailing entry both reflect every
        # quarantined record — none were dropped due to the in-memory cap.
        broken = (out / "broken" / "tle2099.broken.txt").read_bytes()
        assert f"# {n} quarantined of {n} entries".encode("ascii") in broken
        last = f"junk {n - 1:08d}".encode("ascii")
        assert last in broken

    def test_validate_mode_bucket_caps_per_rule(self, tmp_path):
        # In validate mode no sidecar is written, but each per-rule bucket
        # still caps so peak memory does not grow with reject count.
        n = report._PER_RULE_EXEMPLAR_BOUND + 500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.quarantined_count == n
        assert (
            len(stats.reject_sample.buckets[RuleID.BAD_PREFIX])
            == report._PER_RULE_EXEMPLAR_BOUND
        )

    def test_rare_rules_preserved_under_skew(self, tmp_path):
        # Feed 1000 bad-prefix rejects then a smaller batch of a different
        # rule. With per-rule buckets, both appear in stats.reject_sample.
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
        assert RuleID.BAD_PREFIX in stats.reject_sample.buckets
        assert RuleID.ORPHAN_LINE in stats.reject_sample.buckets
        # The rare rule has all its occurrences (well under the cap).
        assert len(stats.reject_sample.buckets[RuleID.ORPHAN_LINE]) == few

    def test_internal_error_rule_bucketed_like_data_defects(
        self, tmp_path, monkeypatch
    ):
        # Force ``repair.process_record`` to raise so every paired record
        # lands in RuleID.INTERNAL_ERROR. With many more rejects than the
        # cap, the bucket caps just like a data-defect rule.
        n = report._PER_RULE_EXEMPLAR_BOUND + 5
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

        monkeypatch.setattr(repair, "process_record", _boom)

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.reject_counts.get(RuleID.INTERNAL_ERROR) == n
        assert (
            len(stats.reject_sample.buckets[RuleID.INTERNAL_ERROR])
            == report._PER_RULE_EXEMPLAR_BOUND
        )


class TestQuarantinedNoradIds:
    """The corpus-wide ``broken-noradids.ndjson`` feed (and the per-NORAD
    breakdown in ``report.md``): extract NORAD IDs from quarantined records'
    line 1, when that line is readable, and bucket them by rule ID.
    """

    def test_extracts_id_from_record_reject(self, tmp_path, line1, line2):
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

    def test_multiple_rejects_for_same_id_accrue_per_rule(self, tmp_path, line1, line2):
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
