"""Tests for run_planning.py — preflight, resume classification, and output scrub."""

import os

import pytest

from lintle import cli, pipeline, report, resume, run_planning


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
    actually kill a parallel run. ``run_identity`` defaults to the schema-v2
    shape used by ``main()`` (``{"max_quarantined": "0"}``)."""
    if run_identity is None:
        run_identity = {"max_quarantined": "0"}
    os.makedirs(out_dir, exist_ok=True)
    inputs = {p: resume.input_fingerprint(p) for p in src_paths}
    completed = {}
    for path in src_paths[:completed_count]:
        stats = pipeline.process_file(path, out_dir, "clean")
        cleaned_name = os.path.splitext(os.path.basename(path))[0] + ".cleaned.txt"
        sizes = {}
        cleaned_path = os.path.join(out_dir, "cleaned", cleaned_name)
        if os.path.exists(cleaned_path):
            sizes[cleaned_name] = os.path.getsize(cleaned_path)
        broken_name = os.path.splitext(os.path.basename(path))[0] + ".broken.txt"
        broken_path = os.path.join(out_dir, "broken", broken_name)
        if os.path.exists(broken_path):
            sizes[broken_name] = os.path.getsize(broken_path)
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
        first_cleaned = out_partial / "cleaned" / "tle2098.cleaned.txt"
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
        # report.jsonl and broken-noradids are timing-free — the golden anchors.
        assert (out_partial / "report.jsonl").read_bytes() == (
            out_full / "report.jsonl"
        ).read_bytes()
        assert (out_partial / "broken-noradids.ndjson").read_bytes() == (
            out_full / "broken-noradids.ndjson"
        ).read_bytes()
        for name in ("tle2098.cleaned.txt", "tle2099.cleaned.txt"):
            assert (out_partial / "cleaned" / name).read_bytes() == (
                out_full / "cleaned" / name
            ).read_bytes()
        assert _strip_generated(out_partial / "report.md") == _strip_generated(
            out_full / "report.md"
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
        assert (out / "cleaned" / "tle2098.cleaned.txt").exists()
        assert (out / "cleaned" / "tle2099.cleaned.txt").exists()

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
        first_cleaned = out_partial / "cleaned" / f"{first_stem}.cleaned.txt"
        mtime_before = first_cleaned.stat().st_mtime_ns

        # No --resume flag — auto-resume is the default in non-interactive mode.
        rc_auto = cli.main(
            ["clean", str(src), "--out-dir", str(out_partial), "--jobs", "1"]
        )

        assert rc_auto == rc_full
        # The already-completed file was skipped, not reprocessed.
        assert first_cleaned.stat().st_mtime_ns == mtime_before
        # report.jsonl output matches the full run.
        assert (out_partial / "report.jsonl").read_bytes() == (
            out_full / "report.jsonl"
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
        assert (out / "cleaned" / "tle2000.cleaned.txt").exists()
        assert (out / "cleaned" / "tle2001.cleaned.txt").exists()
        # A completed run deletes the checkpoint.
        assert not (out / resume.CHECKPOINT_NAME).exists()

        # Step 2 — remove tle2001.txt from the input set; its cleaned output is
        # now an orphan in the out-dir.
        (src / "tle2001.txt").unlink()
        assert (out / "cleaned" / "tle2001.cleaned.txt").exists()  # orphan present

        # Step 3 — fresh run (--no-resume) on the now-one-file dir.
        rc2 = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc2 == 0

        # The output for the surviving input must exist.
        assert (out / "cleaned" / "tle2000.cleaned.txt").exists()

        # The orphan from the prior run must be gone — spec §3.4 guarantees
        # the fresh run scrubs the whole cleaned/ tree before processing.
        assert not (out / "cleaned" / "tle2001.cleaned.txt").exists()


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
