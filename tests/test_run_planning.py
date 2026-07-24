"""Tests for run_planning.py — preflight, resume classification, and output scrub."""

import os

import pytest

from lintle import (
    CLEANED_DIRNAME,
    REPORT_DIRNAME,
    cli,
    pipeline,
    report,
    resume,
    run_planning,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT


def _strip_generated(path):
    """Read a report.md, dropping the nondeterministic `- Generated:` line so
    two runs can be compared for structural equality."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.startswith("- Generated:")
    )


def _simulate_interrupted_clean(
    src_paths, out_dir, *, completed_count, run_identity=None
):
    """Leave `out_dir` looking exactly like a clean run interrupted partway:
    the first `completed_count` files fully processed (their cleaned/broken
    outputs and findings shards committed, as a worker leaves them — no
    end-of-run concat), plus a checkpoint that lists them complete with every
    input fingerprinted. Mirrors the real interrupted state without needing to
    actually kill a parallel run. ``run_identity`` defaults to the shape used
    by ``main()`` (``{"max_quarantined": "0", "reconstruct_checksum": False,
    "chunk_records": CHUNK_RECORDS_DEFAULT}``)."""
    if run_identity is None:
        run_identity = {
            "max_quarantined": "0",
            "reconstruct_checksum": False,
            "chunk_records": CHUNK_RECORDS_DEFAULT,
        }
    os.makedirs(out_dir, exist_ok=True)
    inputs = {p: resume.input_fingerprint(p) for p in src_paths}
    completed = {}
    for path in src_paths[:completed_count]:
        stats = pipeline.process_file(path, out_dir, "clean")
        sizes = resume.output_sizes(out_dir, stats)
        completed[path] = {"summary": report.summary_dict(stats), "outputs": sizes}
    resume.write_checkpoint(
        out_dir,
        resume.build_checkpoint(
            inputs=inputs, completed=completed, run_identity=run_identity
        ),
    )


class TestPreRunShardScrub:
    """Issue #9 spec §4.6 / §8.10: the pre-run shard-dir scrub removes
    any leftover ``.shards/`` from a prior aborted run so this run's
    ``report.jsonl`` cannot inherit stale entries.
    """

    def test_pre_run_scrub_purges_stale_shards(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        # The ownership marker is written by the first real run; preseed it here
        # to represent a prior run that left stale shard state behind (issue #93).
        (out / run_planning._OUTPUT_MARKER).write_text("")
        # Preseed a finalized shard from a previous run for a file that
        # the current run does NOT process.
        stale_shard_dir = out / ".shards"
        stale_shard_dir.mkdir()
        stale = stale_shard_dir / "tle1999.findings.jsonl"
        stale.write_text('{"bogus": "from-prior-run"}\n', encoding="utf-8")

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        # The scrub removed .shards/ before workers wrote anything;
        # the concat then runs and either removes the dir again or
        # leaves no trace either way.
        assert not stale.exists()

    def test_pre_run_scrub_purges_partials(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        # The ownership marker is written by the first real run; preseed it here
        # to represent a prior run that left partial shard state behind (issue #93).
        (out / run_planning._OUTPUT_MARKER).write_text("")
        stale_shard_dir = out / ".shards"
        stale_shard_dir.mkdir()
        stale = stale_shard_dir / "tle1999.findings.jsonl.partial"
        stale.write_text("incomplete-write\n", encoding="utf-8")

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert not stale.exists()


class TestResume:
    """Single-run resume for `clean --resume` (issue #56): checkpoint lifecycle,
    refuse-on-change validation, and a golden 'resume finishes the job' run."""

    def _two_file_src(self, tmp_path, line1, line2):
        # Each file: one clean pair + one wrong-checksum pair (quarantined),
        # so cleaned/, broken/, report.jsonl, and broken-noradids all exercise.
        src = tmp_path / "src"
        src.mkdir()
        bad1 = line1[:68] + "9"
        content = (line1 + "\n" + line2 + "\n" + bad1 + "\n" + line2 + "\n").encode(
            "ascii"
        )
        (src / "tle2098.txt").write_bytes(content)
        (src / "tle2099.txt").write_bytes(content)
        return src

    def test_completed_run_leaves_no_checkpoint(self, tmp_path, line1, line2):
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert not (out / resume.CHECKPOINT_NAME).exists()

    def test_resume_without_checkpoint_errors(self, tmp_path, line1, line2, capsys):
        # --resume (explicit force-resume) with no checkpoint: ABSENT + resume
        # → ABORT with exit_code=2 (operational error, not the quarantine gate).
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--resume", "--jobs", "1"]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "no interrupted run" in err.lower()

    def test_resume_finishes_and_matches_full_run(self, tmp_path, line1, line2):
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_full = tmp_path / "full"
        rc_full = cli.main(
            ["clean", str(src), "--out-dir", str(out_full), "--jobs", "1"]
        )

        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        first_cleaned = out_partial / CLEANED_DIRNAME / "tle2098.00001.cleaned.txt"
        mtime_before = first_cleaned.stat().st_mtime_ns

        rc_resume = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
            ]
        )

        assert rc_resume == rc_full
        # report.00001.jsonl and broken-noradids are timing-free — the golden
        # anchors. Small test corpora never exceed the default chunk boundary
        # (spec 2026-07-21-output-chunking-design), so exactly one chunk exists.
        report_partial = out_partial / REPORT_DIRNAME
        report_full = out_full / REPORT_DIRNAME
        assert (report_partial / "report.00001.jsonl").read_bytes() == (
            report_full / "report.00001.jsonl"
        ).read_bytes()
        assert (report_partial / "broken-noradids.ndjson").read_bytes() == (
            report_full / "broken-noradids.ndjson"
        ).read_bytes()
        for name in ("tle2098.00001.cleaned.txt", "tle2099.00001.cleaned.txt"):
            assert (out_partial / CLEANED_DIRNAME / name).read_bytes() == (
                out_full / CLEANED_DIRNAME / name
            ).read_bytes()
        assert _strip_generated(report_partial / "report.md") == _strip_generated(
            report_full / "report.md"
        )
        # The already-completed file was skipped, not reprocessed.
        assert first_cleaned.stat().st_mtime_ns == mtime_before
        # A fully successful resumed run tears down BOTH the checkpoint and the
        # findings shards (they are removed together only on success, #56).
        assert not (out_partial / resume.CHECKPOINT_NAME).exists()
        assert not (out_partial / ".shards").exists()

    def test_fresh_run_clears_stale_checkpoint(self, tmp_path, line1, line2):
        # --no-resume discards an incompatible checkpoint and starts fresh
        # (archives it, scrubs output trees, reprocesses everything).
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        resume.write_checkpoint(
            str(out),
            resume.build_checkpoint(
                inputs={
                    "old.txt": {
                        "size": 1,
                        "mtime_ns": 1,
                        "head_sha256": "x",
                        "tail_sha256": "y",
                    }
                },
                completed={},
                run_identity={},
            ),
        )
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc == 1  # quarantines present
        assert not (out / resume.CHECKPOINT_NAME).exists()
        assert (out / CLEANED_DIRNAME / "tle2098.00001.cleaned.txt").exists()
        assert (out / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt").exists()

    def test_resume_refuses_when_input_changed(self, tmp_path, line1, line2, capsys):
        # --resume (explicit force-resume) with a stale checkpoint (input changed)
        # → STALE + resume flag → ABORT with exit_code=2.
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        # The not-yet-processed input changes between "office" and "home".
        with open(src / "tle2099.txt", "ab") as handle:
            handle.write(b"1 extra junk line\n")

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
            ]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "input changed" in err.lower()
        assert "tle2099" in err
        # A refused resume leaves the checkpoint intact for an explicit restart.
        assert (out_partial / resume.CHECKPOINT_NAME).exists()

    def test_resume_refuses_when_reconstruct_flag_changed(
        self, tmp_path, line1, line2, capsys
    ):
        # The interrupted run used the default (reconstruct off); resuming with
        # --reconstruct-checksum changes which records are accepted, so the run
        # configuration no longer matches the checkpoint → STALE → refuse (#82).
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
                "--reconstruct-checksum",
            ]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "run configuration changed" in err.lower()
        # A refused resume leaves the checkpoint intact for an explicit restart.
        assert (out_partial / resume.CHECKPOINT_NAME).exists()

    def test_resume_refuses_when_chunk_records_changed(
        self, tmp_path, line1, line2, capsys
    ):
        # Debate golden test: the interrupted run used the default chunk size;
        # resuming with a different --chunk-records would mix chunk sizes within
        # one logical run (completed stems at the old size, redone stems at the
        # new), so run identity no longer matches the checkpoint → STALE → refuse.
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
                "--chunk-records",
                "500000",
            ]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "run configuration changed" in err.lower()
        assert (out_partial / resume.CHECKPOINT_NAME).exists()

    def test_interrupt_preserves_checkpoint_and_shards(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # An interrupted run must stay resumable: BOTH the checkpoint and the
        # findings shards survive (the shards so a later --resume rebuilds a
        # complete report.jsonl). Simulate the interrupt by raising
        # KeyboardInterrupt from the parent's checkpoint write after a file
        # commits — the same path a real Ctrl-C takes through the collect loop.
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        real_write = resume.write_checkpoint

        def interrupt_after_first(out_dir, checkpoint):
            real_write(out_dir, checkpoint)  # persist the first file's progress
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.resume, "write_checkpoint", interrupt_after_first)
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 130
        # The checkpoint written before the interrupt survives → resumable.
        assert resume.load_checkpoint(str(out)) is not None
        # The end-of-run shard scrub never ran (we returned 130 first), so the
        # completed file's shard is still on disk for --resume to reuse.
        assert (out / ".shards").exists()

    def test_resume_with_corrupt_checkpoint_reports_corruption(
        self, tmp_path, line1, line2, capsys
    ):
        # A present-but-corrupt checkpoint must not be reported as "not found".
        # CORRUPT + any flag (or no flag) → ABORT with exit_code=2 unless --no-resume.
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        (out / resume.CHECKPOINT_NAME).write_text("{ not valid json", encoding="utf-8")
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--resume", "--jobs", "1"]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "corrupt" in err.lower() or "unreadable" in err.lower()
        assert "no interrupted run" not in err.lower()


class TestScrubOutputs:
    def test_removes_output_trees(self, tmp_path):
        out = tmp_path
        for sub in ("cleaned", "broken", ".shards"):
            d = out / sub
            d.mkdir()
            (d / "stale.txt").write_text("old")
        run_planning.scrub_outputs(str(out))
        for sub in ("cleaned", "broken", ".shards"):
            assert not (out / sub).exists()

    def test_noop_on_empty_dir(self, tmp_path):
        run_planning.scrub_outputs(str(tmp_path))  # must not raise

    def test_scrub_removes_grouped_data_layout(self, tmp_path):
        out = tmp_path / "out"
        (out / "data" / "cleaned").mkdir(parents=True)
        (out / "data" / "cleaned" / "x.00001.cleaned.txt").write_text("stale")
        run_planning.scrub_outputs(str(out))
        assert not (out / "data").exists()

    def test_scrub_removes_numbered_dirs(self, tmp_path):
        out = tmp_path / "out"
        for d in ("01-cleaned", "02-broken", "03-report"):
            (out / d).mkdir(parents=True)
            (out / d / "stale.txt").write_text("stale")
        run_planning.scrub_outputs(str(out))
        for d in ("01-cleaned", "02-broken", "03-report"):
            assert not (out / d).exists()


class TestResumeWiring:
    """Task 11: --no-resume flag + decision-core wiring in main() (spec §2)."""

    def _make_src(self, tmp_path, line1, line2, n=2):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(n):
            (src / f"tle20{i:02d}.txt").write_bytes(
                (line1 + "\n" + line2 + "\n").encode()
            )
        return src

    def test_no_resume_and_resume_are_mutually_exclusive(
        self, tmp_path, line1, line2, capsys
    ):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        with pytest.raises(SystemExit) as exc:
            cli.main(
                [
                    "clean",
                    str(src),
                    "--out-dir",
                    str(out),
                    "--resume",
                    "--no-resume",
                    "--jobs",
                    "1",
                ]
            )
        assert exc.value.code == 2  # argparse usage error

    def test_default_run_with_no_checkpoint_is_fresh_and_succeeds(
        self, tmp_path, line1, line2
    ):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0
        # checkpoint deleted on success
        assert not (out / ".clean-state.json").exists()

    def test_auto_resume_when_checkpoint_valid(self, tmp_path, line1, line2):
        # DEFAULT behavior (no --resume flag, no --no-resume flag, non-interactive
        # environment — pytest has no TTY): a valid checkpoint is picked up
        # automatically and the completed files are skipped.
        src = self._make_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_full = tmp_path / "full"
        rc_full = cli.main(
            ["clean", str(src), "--out-dir", str(out_full), "--jobs", "1"]
        )

        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        # Note the mtime of the already-completed file so we can confirm it
        # was NOT reprocessed.
        first_name = os.path.basename(paths[0])
        first_stem = os.path.splitext(first_name)[0]
        first_cleaned = (
            out_partial / CLEANED_DIRNAME / f"{first_stem}.00001.cleaned.txt"
        )
        mtime_before = first_cleaned.stat().st_mtime_ns

        # No --resume flag — auto-resume is the default in non-interactive mode.
        rc_auto = cli.main(
            ["clean", str(src), "--out-dir", str(out_partial), "--jobs", "1"]
        )

        assert rc_auto == rc_full
        # The already-completed file was skipped, not reprocessed.
        assert first_cleaned.stat().st_mtime_ns == mtime_before
        # report.00001.jsonl output matches the full run.
        report_partial = out_partial / REPORT_DIRNAME
        report_full = out_full / REPORT_DIRNAME
        assert (report_partial / "report.00001.jsonl").read_bytes() == (
            report_full / "report.00001.jsonl"
        ).read_bytes()
        # A successful auto-resume tears down the checkpoint and shards.
        assert not (out_partial / resume.CHECKPOINT_NAME).exists()
        assert not (out_partial / ".shards").exists()


class TestFreshRunOrphanScrub:
    """Gap 1 (spec §3.4): a true-fresh run wipes cleaned/ + broken/ so outputs
    from a prior run whose input is no longer in the input set do not linger."""

    def test_no_resume_scrubs_orphaned_outputs(self, tmp_path, line1, line2):
        # Step 1 — run on two inputs; both cleaned outputs are written.
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        out = tmp_path / "out"

        rc1 = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc1 == 0
        assert (out / CLEANED_DIRNAME / "tle2000.00001.cleaned.txt").exists()
        assert (out / CLEANED_DIRNAME / "tle2001.00001.cleaned.txt").exists()
        # A completed run deletes the checkpoint.
        assert not (out / resume.CHECKPOINT_NAME).exists()

        # Step 2 — remove tle2001.txt from the input set; its cleaned output is
        # now an orphan in the out-dir.
        (src / "tle2001.txt").unlink()
        assert (
            out / CLEANED_DIRNAME / "tle2001.00001.cleaned.txt"
        ).exists()  # orphan present

        # Step 3 — fresh run (--no-resume) on the now-one-file dir.
        rc2 = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc2 == 0

        # The output for the surviving input must exist.
        assert (out / CLEANED_DIRNAME / "tle2000.00001.cleaned.txt").exists()

        # The orphan from the prior run must be gone — spec §3.4 guarantees
        # the fresh run scrubs the whole cleaned/ tree before processing.
        assert not (out / CLEANED_DIRNAME / "tle2001.00001.cleaned.txt").exists()


class TestStaleCheckpointNonInteractive:
    """Gap 2 (spec §2.3): STALE checkpoint + no flag + non-interactive mode
    must exit 2 and print the change reason plus a --no-resume hint.

    ``test_resume_refuses_when_input_changed`` in TestResume covers the
    STALE + *explicit --resume flag* cell.  This class covers the
    STALE + *no flag* + non-interactive cell — a different branch in
    resolve_resume_action — so both are independently tested."""

    def _make_stale_setup(self, tmp_path, line1, line2):
        """Return (src, out_partial) with a valid checkpoint whose fingerprint
        will not match after we mutate the input."""
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        return src, out_partial

    def test_stale_checkpoint_non_interactive_errors(
        self, tmp_path, line1, line2, capsys, monkeypatch
    ):
        # Ensure non-interactive mode regardless of the test runner environment.
        monkeypatch.setenv("CI", "true")

        src, out_partial = self._make_stale_setup(tmp_path, line1, line2)

        # Mutate tle2001.txt so its fingerprint no longer matches the checkpoint.
        with open(src / "tle2001.txt", "ab") as fh:
            fh.write(b"extra junk\n")

        # No --resume and no --no-resume: the default path.
        rc = cli.main(["clean", str(src), "--out-dir", str(out_partial), "--jobs", "1"])
        err = capsys.readouterr().err

        # spec §2.3: STALE + no flag + non-interactive → exit 2 + guidance.
        assert rc == 2
        # The error message must name the change reason.
        assert "input changed" in err.lower() or "cannot resume" in err.lower()
        # The guidance hint directs the operator to --no-resume.
        assert "--no-resume" in err
        # The checkpoint must survive (not silently discarded) so the operator
        # can inspect what changed before choosing to discard or investigate.
        assert (out_partial / resume.CHECKPOINT_NAME).exists()


# ---------------------------------------------------------------------------
# Issue #94 — disk-space guard runs at the right time with the right amount
# ---------------------------------------------------------------------------


class TestDiskGuardOrdering:
    """Issue #94: the disk-space guard must be charged against the REMAINING
    work (RESUME branch) or run AFTER scrub (FRESH branch) so a nearly-complete
    resume is not rejected for a tight disk that would comfortably hold the rest."""

    def _make_two_file_src(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        return src

    def test_resume_tight_disk_passes_when_remaining_fits(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Arrange: interrupt after file 1 of 2 is done.
        src = self._make_two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out = tmp_path / "out"
        _simulate_interrupted_clean(paths, str(out), completed_count=1)

        remaining_size = os.path.getsize(paths[1])

        # Disk is tight: only enough for 2× the REMAINING file (not 2× all).
        # Under the old code (charging 2× total before classification) this
        # would have been refused.  Under the new code it must proceed.
        class _Usage:
            free = remaining_size * 2 + 1  # just above the 2× remaining guard

        monkeypatch.setattr(run_planning.shutil, "disk_usage", lambda _: _Usage())

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        # The resume should complete successfully (all records clean → exit 0).
        assert rc == 0

    def test_resume_disk_error_returns_2(self, tmp_path, line1, line2, monkeypatch):
        # Even under the new ordering, if the remaining work won't fit → exit 2.
        src = self._make_two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out = tmp_path / "out"
        _simulate_interrupted_clean(paths, str(out), completed_count=1)

        class _Usage:
            free = 1  # far below 2× anything

        monkeypatch.setattr(run_planning.shutil, "disk_usage", lambda _: _Usage())

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 2

    def test_fresh_disk_guard_runs_after_scrub(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Arrange: run once, then do a fresh --no-resume run with a disk that
        # is tight relative to the old outputs but fine after scrub.
        src = self._make_two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        # Disk guard call counter — we want to verify it runs AFTER scrub.
        calls = []
        real_check = run_planning.check_disk_space

        def recording_check(out_dir, input_bytes):
            # Capture whether 01-cleaned/ still exists at the moment of the check.
            cleaned_exists = (tmp_path / "out" / CLEANED_DIRNAME).exists()
            calls.append({"cleaned_exists": cleaned_exists})
            return real_check(out_dir, input_bytes)

        monkeypatch.setattr(run_planning, "check_disk_space", recording_check)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--no-resume",
                "--jobs",
                "1",
            ]
        )

        assert rc == 0
        # The guard must have been called exactly once for the FRESH branch.
        assert len(calls) == 1
        # At the moment the guard ran, the prior cleaned/ tree must be gone
        # (i.e. scrub happened before the guard).
        assert not calls[0]["cleaned_exists"], (
            "disk guard ran before scrub: cleaned/ still present"
        )


# ---------------------------------------------------------------------------
# Issue #93 — scrub is gated by an ownership marker
# ---------------------------------------------------------------------------


class TestScrubOwnershipGate:
    """Issue #93: scrub_outputs must not silently destroy user-owned content.
    The out-dir is safe to scrub iff it is empty (modulo the lock file), already
    contains the ownership marker (.lintle-output), or already contains a lintle
    signal (checkpoint or stale-checkpoint archive)."""

    def test_refuses_non_lintle_dir_with_user_content(
        self, tmp_path, line1, line2, capsys
    ):
        # A directory with user content and no lintle ownership signal must be refused.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        out = tmp_path / "out"
        out.mkdir()
        # Place user-owned subdirectory named like a lintle output tree.
        (out / CLEANED_DIRNAME).mkdir()
        (out / CLEANED_DIRNAME / "my_data.txt").write_text("precious user data")

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        err = capsys.readouterr().err

        assert rc == 2
        assert (
            "refusing to scrub" in err.lower() or "not a lintle output" in err.lower()
        )
        # Precious user data must survive.
        assert (out / CLEANED_DIRNAME / "my_data.txt").exists()

    def test_refuses_user_file_sharing_checkpoint_prefix(
        self, tmp_path, line1, line2, capsys
    ):
        # A user file whose name merely STARTS WITH the checkpoint name (e.g.
        # ".clean-state.json.bak") is NOT a stale-checkpoint archive (those are
        # ".clean-state.json.stale-<ts>") and must not be mistaken for a lintle
        # ownership signal — else a user dir with such a file + a "cleaned/"
        # subdir would be wrongly scrubbed (#93 false positive).
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        (out / (resume.CHECKPOINT_NAME + ".bak")).write_text("user backup")
        (out / CLEANED_DIRNAME).mkdir()
        (out / CLEANED_DIRNAME / "my_data.txt").write_text("precious user data")

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 2
        assert "refusing to scrub" in capsys.readouterr().err.lower()
        assert (out / CLEANED_DIRNAME / "my_data.txt").exists()  # untouched

    def test_proceeds_on_empty_dir(self, tmp_path, line1, line2):
        # An empty out-dir (only the lock is present, held by us) must proceed.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        # Do NOT pre-create any content — cli.main creates and locks it.
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0

    def test_proceeds_with_marker_present(self, tmp_path, line1, line2):
        # An out-dir with the ownership marker proceeds even with user-named content.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        (out / run_planning._OUTPUT_MARKER).write_text("")
        (out / "cleaned").mkdir()
        (out / "cleaned" / "old_output.txt").write_text("prior run output")

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        # Old output must be scrubbed; a clean 1-file run succeeds.
        assert rc == 0
        assert (out / CLEANED_DIRNAME / "tle2000.00001.cleaned.txt").exists()
        assert not (out / "cleaned" / "old_output.txt").exists()

    def test_proceeds_with_checkpoint_signal(self, tmp_path, line1, line2):
        # An out-dir that contains a checkpoint (interrupted run) is lintle-owned.
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        out = tmp_path / "out"
        paths = cli.discover_paths(str(src))
        # Simulate an interrupted run (checkpoint present, partial outputs).
        _simulate_interrupted_clean(paths, str(out), completed_count=1)

        # A fresh --no-resume must see the checkpoint as a lintle-ownership signal
        # and proceed with the scrub, not refuse.
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc == 0

    def test_marker_written_on_first_fresh_run(self, tmp_path, line1, line2):
        # After a fresh run the marker must be present so subsequent runs
        # recognise the dir as lintle-owned without needing a checkpoint.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert (out / run_planning._OUTPUT_MARKER).exists()

    def test_proceeds_with_stale_checkpoint_archive(self, tmp_path, line1, line2):
        # A stale-checkpoint archive (.clean-state.json.stale-<timestamp>) is a
        # lintle-ownership signal — the dir must proceed, not be refused.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        # Plant a stale archive (as archive_checkpoint would leave it).
        stale_name = f"{resume.CHECKPOINT_NAME}.stale-20260101T000000Z"
        (out / stale_name).write_text("{}")

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Issue #102 — scrub removes prior run's report artifacts
# ---------------------------------------------------------------------------


class TestScrubClearsReportArtifacts:
    """Issue #102: a fresh run must remove the prior run's report artifacts
    (report.md, report.json, report.jsonl, broken-noradids.ndjson) during the
    FRESH scrub, so an interrupted fresh run does not leave a stale prior-run
    report that `lintle report` would render as current."""

    def _make_src(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        return src

    def test_scrub_removes_report_json(self, tmp_path, line1, line2):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        # First run: writes report artifacts.
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        report_dir = out / REPORT_DIRNAME
        assert (report_dir / "report.json").exists()

        # Plant a stale report.json so we can verify it is removed.
        stale_json = report_dir / "report.json"
        stale_json.write_text('{"schema_version":"3","stale":true}', encoding="utf-8")

        # scrub_outputs (called during a fresh run's FRESH branch) must remove it.
        run_planning.scrub_outputs(str(out))
        assert not stale_json.exists()

    def test_scrub_removes_all_report_artifacts(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        # Plant all four report artifacts.
        artifacts = (
            "report.md",
            "report.json",
            "report.jsonl",
            "broken-noradids.ndjson",
        )
        for name in artifacts:
            (out / name).write_text("stale", encoding="utf-8")

        run_planning.scrub_outputs(str(out))

        for name in artifacts:
            assert not (out / name).exists(), f"{name} should have been removed"

    def test_scrub_removes_legacy_report_chunks_only(self, tmp_path):
        # The legacy root-level report chunk set goes; a non-chunk bystander
        # that the old loose glob would have caught survives — the scrub now
        # shares ChunkedReader's anchored 5-digit parse (one naming authority).
        out = tmp_path / "out"
        out.mkdir()
        (out / "report.00001.jsonl").write_text("stale", encoding="utf-8")
        (out / "report.00002.jsonl").write_text("stale", encoding="utf-8")
        bystander = out / "report.backup.jsonl"
        bystander.write_text("keep me", encoding="utf-8")

        run_planning.scrub_outputs(str(out))

        assert not (out / "report.00001.jsonl").exists()
        assert not (out / "report.00002.jsonl").exists()
        assert bystander.exists()

    def test_fresh_run_does_not_show_stale_report(
        self, tmp_path, line1, line2, monkeypatch, capsys
    ):
        # After an interrupted fresh run the old report.json must be gone so
        # `lintle report` exits 2 (not found) rather than rendering the prior run.
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        # First run: writes report.json.
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert (out / REPORT_DIRNAME / "report.json").exists()

        # Simulate an "interrupted fresh run": scrub removes the old report.json
        # but the new run never finishes (we just call scrub_outputs directly
        # here to represent what would happen if workers were killed before
        # finalization).
        run_planning.scrub_outputs(str(out))

        # `lintle report` must now fail (no report.json) — not serve the stale one.
        capsys.readouterr()
        rc = cli.main(["report", str(out)])
        assert rc == 2  # "no run found"
