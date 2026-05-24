"""Tests for lintle.pipeline — streaming I/O, line pairing, file processing."""

import contextlib
import queue

import pytest

from lintle import pipeline
from lintle.diagnostics import RuleID


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
        assert records[0].diagnostic.rule_id == RuleID.ORPHAN_LINE
        assert records[0].diagnostic.note == "orphan line 1 at end of file"

    def test_two_line1s_orphan_the_first(self, tmp_path, line1):
        src = tmp_path / "in.txt"
        src.write_bytes((line1 + "\n" + line1 + "\n").encode("ascii"))
        records = list(pipeline.iter_records(str(src)))
        assert all(isinstance(r, pipeline.Orphan) for r in records)
        assert records[0].diagnostic.rule_id == RuleID.ORPHAN_LINE

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
        assert any(o.diagnostic.rule_id == RuleID.BAD_PREFIX for o in orphans)
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
        # With a queue, process_file streams record-count deltas to it; the
        # deltas sum to the exact record total — a partial trailing batch
        # included — so the caller can render an accurate live count.
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
        # Tuples are lifecycle events; ints are record-count deltas.
        deltas = [m for m in messages if isinstance(m, int)]
        assert sum(deltas) == 3

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
    """The constant-memory invariant: reject_exemplars stays bounded even on
    reject-heavy files, while the on-disk ``.broken.txt`` catalog is complete.
    """

    def test_exemplars_bounded_but_broken_catalog_is_complete(self, tmp_path):
        # Far more bad-prefix orphans than the in-memory exemplar bound — the
        # full catalog must reach disk; only the in-memory sample is capped.
        n = pipeline._EXEMPLAR_BOUND + 1500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        # Full counters reflect every reject…
        assert stats.quarantined_count == n
        assert stats.reject_counts.get(RuleID.BAD_PREFIX) == n
        # …but the in-memory exemplar buffer is capped at the bound.
        assert len(stats.reject_exemplars) == pipeline._EXEMPLAR_BOUND
        # The on-disk catalog header and trailing entry both reflect every
        # quarantined record — none were dropped due to the in-memory cap.
        broken = (out / "broken" / "tle2099.broken.txt").read_bytes()
        # Every yielded entry here is an orphan ("bad-prefix") — so the
        # paired/orphan split puts all `n` into orphan_entries, and the
        # denominator on the sidecar header is paired+orphan = n.
        assert f"# {n} quarantined of {n} entries".encode("ascii") in broken
        last = f"junk {n - 1:08d}".encode("ascii")
        assert last in broken

    def test_validate_mode_bounds_memory_too(self, tmp_path):
        # In validate mode no sidecar is written, but the in-memory exemplars
        # still cap so peak memory does not grow with reject count.
        n = pipeline._EXEMPLAR_BOUND + 500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.quarantined_count == n
        assert len(stats.reject_exemplars) == pipeline._EXEMPLAR_BOUND


class TestQuarantinedNoradIds:
    """The corpus-wide ``broken-noradids.csv`` feed: extract NORAD IDs from
    quarantined records' line 1, when that line is readable.
    """

    def test_extracts_id_from_record_reject(self, tmp_path, line1, line2):
        # A 2-line record with a wrong checksum gets quarantined; line 1 is
        # otherwise intact, so the catalog number must be recovered.
        src = tmp_path / "tle2099.txt"
        bad_line1 = line1[:68] + "9"
        src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_norad_ids == {5}  # canonical NORAD 00005

    def test_extracts_id_from_orphan_line1(self, tmp_path, line1):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n").encode("ascii"))  # lone line 1 at EOF

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids == {5}

    def test_orphan_line2_is_skipped(self, tmp_path, line2):
        # An orphan line 2 has no line 1 to read — the issue contract is
        # explicit: line 1 unrecoverable -> omit from the CSV.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids == set()

    def test_bad_prefix_orphan_is_skipped(self, tmp_path):
        # A line that doesn't start with "1 " or "2 " is unparseable as a
        # TLE line 1 — no NORAD ID is recoverable.
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"garbage line\n")

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.quarantined_count == 1
        assert stats.quarantined_norad_ids == set()

    def test_clean_records_do_not_populate_the_set(self, tmp_path, line1, line2):
        # Only quarantined records contribute — a fully clean file
        # produces an empty NORAD-ID set.
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

        assert stats.clean_count == 1
        assert stats.quarantined_norad_ids == set()
