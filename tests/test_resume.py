"""Tests for lintle.resume — the per-run `clean --resume` checkpoint (issue #56)."""

import os

import pytest

import lintle
from lintle import (
    BROKEN_DIRNAME,
    CLEANED_DIRNAME,
    CLEANED_SUFFIX,
    DATA_DIRNAME,
    FINDINGS_SUFFIX,
    SHARDS_DIRNAME,
    resume,
    stem,
)
from lintle.report import FileStats, summary_dict


def _write(path, data: bytes):
    with open(path, "wb") as handle:
        handle.write(data)


class TestInputFingerprint:
    def test_reports_size_mtime_ns_and_window_hashes(self, tmp_path):
        path = tmp_path / "tle.txt"
        _write(path, b"hello world\n")
        fp = resume.input_fingerprint(str(path))
        assert set(fp) == {
            "size",
            "mtime_ns",
            "ctime_ns",
            "inode",
            "head_sha256",
            "tail_sha256",
        }
        assert fp["size"] == len(b"hello world\n")
        assert isinstance(fp["mtime_ns"], int)
        # Below the window, head and tail hash the same full content.
        assert fp["head_sha256"] == fp["tail_sha256"]

    def test_includes_ctime_and_inode(self, tmp_path):
        f = tmp_path / "tle.txt"
        f.write_bytes(b"1 line\n2 line\n")
        fp = resume.input_fingerprint(str(f))
        assert set(fp) == {
            "size",
            "mtime_ns",
            "ctime_ns",
            "inode",
            "head_sha256",
            "tail_sha256",
        }
        st = os.stat(str(f))
        assert fp["ctime_ns"] == st.st_ctime_ns
        assert fp["inode"] == st.st_ino

    def test_distinguishes_files_by_head(self, tmp_path):
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        _write(a, b"alpha" + b"\x00" * 1000)
        _write(b, b"omega" + b"\x00" * 1000)
        assert (
            resume.input_fingerprint(str(a))["head_sha256"]
            != resume.input_fingerprint(str(b))["head_sha256"]
        )

    def test_detects_an_append_via_size_and_tail(self, tmp_path):
        path = tmp_path / "tle.txt"
        big = b"x" * (resume._HASH_WINDOW * 2)
        _write(path, big)
        before = resume.input_fingerprint(str(path))
        _write(path, big + b"appended line\n")
        after = resume.input_fingerprint(str(path))
        assert after["size"] != before["size"]
        assert after["tail_sha256"] != before["tail_sha256"]
        # The head window is unchanged by a pure append.
        assert after["head_sha256"] == before["head_sha256"]


class TestCheckpointRoundTrip:
    def test_write_then_load_returns_equal_payload(self, tmp_path):
        ckpt = resume.build_checkpoint(
            inputs={
                "data/source/tle.txt": {
                    "size": 1,
                    "mtime_ns": 2,
                    "head_sha256": "a",
                    "tail_sha256": "b",
                }
            },
            completed={},
            run_identity={},
        )
        resume.write_checkpoint(str(tmp_path), ckpt)
        assert resume.load_checkpoint(str(tmp_path)) == ckpt

    def test_build_checkpoint_pins_schema_and_version(self, tmp_path):
        ckpt = resume.build_checkpoint(inputs={}, completed={}, run_identity={})
        assert ckpt["schema_version"] == resume.SCHEMA_VERSION
        assert ckpt["lintle_version"] == lintle.__version__

    def test_write_is_atomic_no_partial_left_behind(self, tmp_path):
        resume.write_checkpoint(
            str(tmp_path),
            resume.build_checkpoint(inputs={}, completed={}, run_identity={}),
        )
        leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".partial")]
        assert leftovers == []

    def test_load_missing_returns_none(self, tmp_path):
        assert resume.load_checkpoint(str(tmp_path)) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        with open(tmp_path / resume.CHECKPOINT_NAME, "w") as handle:
            handle.write("{ not valid json")
        assert resume.load_checkpoint(str(tmp_path)) is None

    def test_load_invalid_utf8_returns_none(self, tmp_path):
        # Issue #92: a .clean-state.json with invalid-UTF-8 bytes must return
        # None, not raise UnicodeDecodeError.
        (tmp_path / resume.CHECKPOINT_NAME).write_bytes(b"\xff\xfe")
        assert resume.load_checkpoint(str(tmp_path)) is None

    def test_load_non_dict_json_returns_none(self, tmp_path):
        # Issue #91 dict-guard: a valid JSON array/string/null is not a usable
        # checkpoint — must return None so callers see "no checkpoint".
        for doc in ("[]", '"hello"', "42", "null"):
            (tmp_path / resume.CHECKPOINT_NAME).write_text(doc, encoding="utf-8")
            assert resume.load_checkpoint(str(tmp_path)) is None

    def test_delete_removes_checkpoint(self, tmp_path):
        resume.write_checkpoint(
            str(tmp_path),
            resume.build_checkpoint(inputs={}, completed={}, run_identity={}),
        )
        resume.delete_checkpoint(str(tmp_path))
        assert resume.load_checkpoint(str(tmp_path)) is None

    def test_delete_is_a_noop_when_absent(self, tmp_path):
        resume.delete_checkpoint(str(tmp_path))  # must not raise


def _fp(size=10, mtime_ns=100, ctime_ns=200, inode=1, head="h", tail="t"):
    return {
        "size": size,
        "mtime_ns": mtime_ns,
        "ctime_ns": ctime_ns,
        "inode": inode,
        "head_sha256": head,
        "tail_sha256": tail,
    }


def _ckpt(inputs):
    return {
        "schema_version": resume.SCHEMA_VERSION,
        "lintle_version": lintle.__version__,
        "inputs": inputs,
        "completed": {},
    }


class TestBuildCheckpoint:
    def test_schema_version_is_3_and_records_run_identity(self):
        ckpt = resume.build_checkpoint(
            inputs={"a.txt": {"size": 1}},
            completed={"a.txt": {"summary": {"clean_count": 3}, "outputs": {}}},
            run_identity={"args": []},
        )
        assert ckpt["schema_version"] == 3
        assert ckpt["run_identity"] == {"args": []}
        assert ckpt["completed"]["a.txt"]["summary"]["clean_count"] == 3


class TestValidateRunIdentity:
    def _ckpt(self, **over):
        base = dict(
            schema_version=resume.SCHEMA_VERSION,
            lintle_version=lintle.__version__,
            run_identity={"args": []},
            inputs={"a.txt": {"size": 1}},
            completed={},
        )
        base.update(over)
        return base

    def test_passes_when_everything_matches(self):
        ck = self._ckpt()
        result = resume.validate_run_identity(ck, {"a.txt": {"size": 1}}, {"args": []})
        assert result is None

    def test_refuses_on_run_identity_drift(self):
        ck = self._ckpt()
        reason = resume.validate_run_identity(
            ck, {"a.txt": {"size": 1}}, {"args": ["--x"]}
        )
        assert reason and "configuration" in reason.lower()

    def test_refuses_on_schema_bump(self):
        ck = self._ckpt(schema_version=1)
        assert resume.validate_run_identity(ck, {"a.txt": {"size": 1}}, {"args": []})

    def test_identical_inputs_pass(self):
        inputs = {"a.txt": _fp(), "b.txt": _fp(size=20)}
        ckpt = _ckpt(inputs)
        ckpt["run_identity"] = {"args": []}
        assert resume.validate_run_identity(ckpt, dict(inputs), {"args": []}) is None

    def test_unknown_schema_refused(self):
        ckpt = _ckpt({"a.txt": _fp()})
        ckpt["schema_version"] = resume.SCHEMA_VERSION + 99
        ckpt["run_identity"] = {"args": []}
        reason = resume.validate_run_identity(ckpt, {"a.txt": _fp()}, {"args": []})
        assert reason and "schema" in reason.lower()

    def test_version_mismatch_refused(self):
        ckpt = _ckpt({"a.txt": _fp()})
        ckpt["lintle_version"] = "0.0.0-other"
        ckpt["run_identity"] = {"args": []}
        reason = resume.validate_run_identity(ckpt, {"a.txt": _fp()}, {"args": []})
        assert reason and "version" in reason.lower()
        assert "0.0.0-other" in reason and lintle.__version__ in reason

    def test_added_file_refused(self):
        ckpt = _ckpt({"a.txt": _fp()})
        ckpt["run_identity"] = {"args": []}
        reason = resume.validate_run_identity(
            ckpt, {"a.txt": _fp(), "new.txt": _fp()}, {"args": []}
        )
        assert reason and "new.txt" in reason

    def test_removed_file_refused(self):
        ckpt = _ckpt({"a.txt": _fp(), "gone.txt": _fp()})
        ckpt["run_identity"] = {"args": []}
        reason = resume.validate_run_identity(ckpt, {"a.txt": _fp()}, {"args": []})
        assert reason and "gone.txt" in reason

    @pytest.mark.parametrize(
        "field", ["size", "mtime_ns", "ctime_ns", "inode", "head_sha256", "tail_sha256"]
    )
    def test_changed_identity_refused(self, field):
        ckpt = _ckpt({"a.txt": _fp()})
        ckpt["run_identity"] = {"args": []}
        changed = _fp()
        int_fields = {"size", "mtime_ns", "ctime_ns", "inode"}
        changed[field] = 999 if field in int_fields else "CHANGED"
        reason = resume.validate_run_identity(ckpt, {"a.txt": changed}, {"args": []})
        assert reason and "a.txt" in reason


class TestValidateCompletedBlock:
    """Issue #91(b): validate_run_identity must gate the ``completed`` block shape
    so corrupt entries never reach resolve_clean_plan's unguarded indexing.
    """

    def _valid_ckpt(self):
        return {
            "schema_version": resume.SCHEMA_VERSION,
            "lintle_version": __import__("lintle").__version__,
            "run_identity": {},
            "inputs": {},
            "completed": {},
        }

    def test_missing_completed_key_is_corrupt(self):
        ck = self._valid_ckpt()
        del ck["completed"]
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None and "completed" in reason.lower()

    def test_completed_not_a_dict_is_corrupt(self):
        ck = self._valid_ckpt()
        ck["completed"] = []
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None and "completed" in reason.lower()

    def test_entry_missing_summary_is_corrupt(self):
        ck = self._valid_ckpt()
        ck["completed"] = {"a.txt": {"outputs": {}}}  # no "summary" key
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None

    def test_entry_missing_outputs_is_corrupt(self):
        ck = self._valid_ckpt()
        ck["completed"] = {"a.txt": {"summary": {}}}  # no "outputs" key
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None

    def test_entry_summary_not_dict_is_corrupt(self):
        ck = self._valid_ckpt()
        ck["completed"] = {"a.txt": {"summary": "bad", "outputs": {}}}
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None

    def test_entry_outputs_not_dict_is_corrupt(self):
        ck = self._valid_ckpt()
        ck["completed"] = {"a.txt": {"summary": {}, "outputs": "bad"}}
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is not None

    def test_well_formed_completed_passes(self):
        ck = self._valid_ckpt()
        ck["completed"] = {
            "a.txt": {"summary": {"clean_count": 1}, "outputs": {"a.cleaned.txt": 99}}
        }
        reason = resume.validate_run_identity(ck, {}, {})
        assert reason is None

    def test_classify_treats_corrupt_completed_as_corrupt(self, tmp_path):
        # End-to-end: a checkpoint that passes schema_version/lintle_version/
        # run_identity/inputs but has a corrupt completed block → CORRUPT status,
        # so the ABORT path is taken instead of KeyError in resolve_clean_plan.
        ck = self._valid_ckpt()
        ck["completed"] = {"a.txt": {"outputs": {}}}  # missing summary
        resume.write_checkpoint(str(tmp_path), ck)
        # Manually overwrite with the corrupt completed (write_checkpoint would
        # reject via build_checkpoint, so write raw).
        import json

        (tmp_path / resume.CHECKPOINT_NAME).write_text(
            json.dumps(ck, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        c = resume.classify_checkpoint(str(tmp_path), {}, {})
        assert c.status is resume.CheckpointStatus.CORRUPT


class TestClassifyCheckpoint:
    def test_absent(self, tmp_path):
        c = resume.classify_checkpoint(
            str(tmp_path), {"a.txt": {"size": 1}}, {"args": []}
        )
        assert c.status is resume.CheckpointStatus.ABSENT

    def test_corrupt(self, tmp_path):
        (tmp_path / resume.CHECKPOINT_NAME).write_text("{not json")
        c = resume.classify_checkpoint(
            str(tmp_path), {"a.txt": {"size": 1}}, {"args": []}
        )
        assert c.status is resume.CheckpointStatus.CORRUPT

    def test_valid(self, tmp_path):
        ck = resume.build_checkpoint(
            inputs={"a.txt": {"size": 1}}, completed={}, run_identity={"args": []}
        )
        resume.write_checkpoint(str(tmp_path), ck)
        c = resume.classify_checkpoint(
            str(tmp_path), {"a.txt": {"size": 1}}, {"args": []}
        )
        assert c.status is resume.CheckpointStatus.VALID
        assert c.checkpoint is not None

    def test_stale(self, tmp_path):
        ck = resume.build_checkpoint(
            inputs={"a.txt": {"size": 1}}, completed={}, run_identity={"args": []}
        )
        resume.write_checkpoint(str(tmp_path), ck)
        c = resume.classify_checkpoint(
            str(tmp_path), {"a.txt": {"size": 2}}, {"args": []}
        )
        assert c.status is resume.CheckpointStatus.STALE
        assert "changed" in c.reason


class TestVerifyCompletedOutputs:
    def _completed(self, name, cleaned_size):
        return {
            name: {
                "summary": {"src_name": name},
                "outputs": {stem(name) + ".cleaned.txt": cleaned_size},
            }
        }

    def test_intact_outputs_are_trusted(self, tmp_path):
        out = tmp_path
        (out / DATA_DIRNAME / "cleaned").mkdir(parents=True)
        (out / DATA_DIRNAME / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 100)
        assert (
            resume.verify_completed_outputs(
                self._completed("tle2099.txt", 100), str(out)
            )
            == []
        )

    def test_missing_output_flags_reprocess(self, tmp_path):
        assert resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(tmp_path)
        ) == ["tle2099.txt"]

    def test_truncated_output_flags_reprocess(self, tmp_path):
        out = tmp_path
        (out / DATA_DIRNAME / "cleaned").mkdir(parents=True)
        (out / DATA_DIRNAME / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 7)
        assert resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(out)
        ) == ["tle2099.txt"]

    def test_locates_output_without_suffix_inference(self, tmp_path):
        # The output dir is found by searching the known output trees, not by
        # inferring it from the filename suffix — so a name that doesn't end in
        # ``.cleaned.txt`` is still located if it exists. (Old suffix-routing
        # would look in broken/, miss it, and falsely flag a reprocess.)
        out = tmp_path
        (out / DATA_DIRNAME / "cleaned").mkdir(parents=True)
        (out / DATA_DIRNAME / "cleaned" / "weird.name").write_bytes(b"x" * 50)
        completed = {"in.txt": {"summary": {}, "outputs": {"weird.name": 50}}}
        assert resume.verify_completed_outputs(completed, str(out)) == []


class TestResolveResumeAction:
    def _c(self, status, reason=None):
        return resume.Classification(status, reason=reason)

    def call(
        self,
        status,
        *,
        resume_flag=False,
        no_resume=False,
        interactive=False,
        answer=True,
        reason="inputs changed",
    ):
        cls = self._c(status, reason=reason)
        prompt = lambda msg, *, default: answer  # noqa: E731
        return resume.resolve_resume_action(
            cls,
            resume=resume_flag,
            no_resume=no_resume,
            interactive=interactive,
            prompt=prompt,
        )

    def test_absent_default_is_fresh(self):
        d = self.call(resume.CheckpointStatus.ABSENT)
        assert d.action is resume.ResumeAction.FRESH

    def test_absent_with_resume_aborts(self):
        d = self.call(resume.CheckpointStatus.ABSENT, resume_flag=True)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 2

    def test_corrupt_default_aborts_no_resume_freshes(self):
        d_abort = self.call(resume.CheckpointStatus.CORRUPT)
        assert d_abort.action is resume.ResumeAction.ABORT
        d_fresh = self.call(resume.CheckpointStatus.CORRUPT, no_resume=True)
        assert d_fresh.action is resume.ResumeAction.FRESH

    def test_valid_flags(self):
        d_resume = self.call(resume.CheckpointStatus.VALID, resume_flag=True)
        assert d_resume.action is resume.ResumeAction.RESUME
        d_fresh = self.call(resume.CheckpointStatus.VALID, no_resume=True)
        assert d_fresh.action is resume.ResumeAction.FRESH

    def test_valid_non_interactive_auto_resumes(self):
        d = self.call(resume.CheckpointStatus.VALID, interactive=False)
        assert d.action is resume.ResumeAction.RESUME

    def test_valid_interactive_prompt(self):
        d_yes = self.call(resume.CheckpointStatus.VALID, interactive=True, answer=True)
        assert d_yes.action is resume.ResumeAction.RESUME
        d_no = self.call(resume.CheckpointStatus.VALID, interactive=True, answer=False)
        assert d_no.action is resume.ResumeAction.FRESH

    def test_valid_interactive_eof_aborts(self):
        d = self.call(resume.CheckpointStatus.VALID, interactive=True, answer=None)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 2

    def test_stale_no_resume_freshes_resume_aborts(self):
        d_fresh = self.call(resume.CheckpointStatus.STALE, no_resume=True)
        assert d_fresh.action is resume.ResumeAction.FRESH
        d_abort = self.call(resume.CheckpointStatus.STALE, resume_flag=True)
        assert d_abort.action is resume.ResumeAction.ABORT

    def test_stale_non_interactive_aborts(self):
        d = self.call(resume.CheckpointStatus.STALE, interactive=False)
        assert d.action is resume.ResumeAction.ABORT and d.exit_code == 2
        assert "--no-resume" in d.message

    def test_stale_interactive_prompt(self):
        d_yes = self.call(resume.CheckpointStatus.STALE, interactive=True, answer=True)
        assert d_yes.action is resume.ResumeAction.FRESH
        d_no = self.call(resume.CheckpointStatus.STALE, interactive=True, answer=False)
        assert d_no.action is resume.ResumeAction.ABORT


class TestArchiveCheckpoint:
    def test_renames_with_stale_suffix(self, tmp_path):
        ck = resume.build_checkpoint(inputs={}, completed={}, run_identity={})
        resume.write_checkpoint(str(tmp_path), ck)
        archived = resume.archive_checkpoint(str(tmp_path), timestamp="20260530T0000Z")
        assert archived is not None
        assert not (tmp_path / resume.CHECKPOINT_NAME).exists()
        assert (tmp_path / archived).exists()
        assert archived.startswith(resume.CHECKPOINT_NAME + ".stale-")

    def test_noop_when_absent(self, tmp_path):
        assert resume.archive_checkpoint(str(tmp_path), timestamp="x") is None

    def test_prunes_old_stale_archives_keeping_newest_3(self, tmp_path):
        # Create 5 stale archives with sortable timestamps — only the newest 3
        # should survive after the next archive_checkpoint call.
        prefix = resume.CHECKPOINT_NAME + ".stale-"
        timestamps = [
            "20260101T000000Z",
            "20260102T000000Z",
            "20260103T000000Z",
            "20260104T000000Z",
            "20260105T000000Z",
        ]
        for ts in timestamps:
            (tmp_path / f"{prefix}{ts}").write_text("{}")
        # Write a fresh checkpoint and archive it — the call triggers pruning.
        ck = resume.build_checkpoint(inputs={}, completed={}, run_identity={})
        resume.write_checkpoint(str(tmp_path), ck)
        resume.archive_checkpoint(str(tmp_path), timestamp="20260106T000000Z")
        # After archiving there are 6 total; pruning must leave only the 3 newest.
        remaining = sorted(
            p.name for p in tmp_path.iterdir() if p.name.startswith(prefix)
        )
        assert len(remaining) == 3
        assert remaining == [
            f"{prefix}20260104T000000Z",
            f"{prefix}20260105T000000Z",
            f"{prefix}20260106T000000Z",
        ]

    def test_prunes_nothing_when_three_or_fewer(self, tmp_path):
        # Fewer than _STALE_ARCHIVE_KEEP archives — nothing removed.
        prefix = resume.CHECKPOINT_NAME + ".stale-"
        for ts in ("20260101T000000Z", "20260102T000000Z"):
            (tmp_path / f"{prefix}{ts}").write_text("{}")
        ck = resume.build_checkpoint(inputs={}, completed={}, run_identity={})
        resume.write_checkpoint(str(tmp_path), ck)
        resume.archive_checkpoint(str(tmp_path), timestamp="20260103T000000Z")
        remaining = [p.name for p in tmp_path.iterdir() if p.name.startswith(prefix)]
        assert len(remaining) == 3  # all three survive


# ---------------------------------------------------------------------------
# Issue #101 — output_sizes unconditional sidecar + shard recording
# ---------------------------------------------------------------------------


class TestOutputSizes:
    """output_sizes records every cleaned/broken *chunk* (by chunk basename)
    unconditionally (issue #101a) plus the findings shard (issue #117 / #101b),
    regardless of quarantine count — so a resume integrity check covers the whole
    chunk set, not a stale single-file name."""

    def _make_stats(self, src_name, quarantined_count=0):
        return FileStats(src_name=src_name, quarantined_count=quarantined_count)

    def _write(self, path, data=b"content"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_records_each_cleaned_chunk(self, tmp_path):
        st = self._make_stats("tle2099.txt")
        self._write(
            tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt",
            b"x" * 200,
        )
        self._write(
            tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.00002.cleaned.txt",
            b"y" * 50,
        )
        sizes = resume.output_sizes(str(tmp_path), st)
        assert sizes["tle2099.00001.cleaned.txt"] == 200
        assert sizes["tle2099.00002.cleaned.txt"] == 50

    def test_records_broken_chunk_even_with_zero_quarantines(self, tmp_path):
        # Issue #101a: sidecar always written (header-only when nothing is
        # quarantined), so its chunk set must always be recorded.
        st = self._make_stats("tle2099.txt", quarantined_count=0)
        self._write(
            tmp_path / DATA_DIRNAME / BROKEN_DIRNAME / "tle2099.00001.broken.txt",
            b"# header\n",
        )
        sizes = resume.output_sizes(str(tmp_path), st)
        assert "tle2099.00001.broken.txt" in sizes

    def test_records_findings_shard(self, tmp_path):
        # Issue #117: the shard (still a single intermediate file) must be recorded
        # so a missing shard on resume triggers reprocessing.
        st = self._make_stats("tle2099.txt")
        shard = tmp_path / SHARDS_DIRNAME / ("tle2099" + FINDINGS_SUFFIX)
        self._write(shard, b'{"outcome":"quarantined"}\n')
        sizes = resume.output_sizes(str(tmp_path), st)
        assert "tle2099" + FINDINGS_SUFFIX in sizes
        assert sizes["tle2099" + FINDINGS_SUFFIX] == len(b'{"outcome":"quarantined"}\n')

    def test_absent_outputs_not_recorded(self, tmp_path):
        # When nothing exists (e.g. validate mode) the sizes dict is empty —
        # no KeyError, no OSError.
        st = self._make_stats("tle2099.txt")
        sizes = resume.output_sizes(str(tmp_path), st)
        assert sizes == {}


class TestVerifyCompletedOutputsWithShard:
    """verify_completed_outputs must flag reprocessing when the shard is missing
    or truncated (issue #117)."""

    def _completed_with_shard(self, src_name, cleaned_size, shard_size):
        file_stem = stem(src_name)
        return {
            src_name: {
                "summary": {"src_name": src_name},
                "outputs": {
                    file_stem + CLEANED_SUFFIX: cleaned_size,
                    file_stem + FINDINGS_SUFFIX: shard_size,
                },
            }
        }

    def test_intact_shard_not_flagged(self, tmp_path):
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME).mkdir(parents=True)
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.cleaned.txt").write_bytes(
            b"x" * 100
        )
        (tmp_path / SHARDS_DIRNAME).mkdir()
        (tmp_path / SHARDS_DIRNAME / "tle2099.findings.jsonl").write_bytes(b"y" * 50)
        assert (
            resume.verify_completed_outputs(
                self._completed_with_shard("tle2099.txt", 100, 50), str(tmp_path)
            )
            == []
        )

    def test_missing_shard_flags_reprocess(self, tmp_path):
        # Shard deleted out-of-band → file should be reprocessed.
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME).mkdir(parents=True)
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.cleaned.txt").write_bytes(
            b"x" * 100
        )
        # No shard directory / shard file created.
        assert resume.verify_completed_outputs(
            self._completed_with_shard("tle2099.txt", 100, 50), str(tmp_path)
        ) == ["tle2099.txt"]

    def test_truncated_shard_flags_reprocess(self, tmp_path):
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME).mkdir(parents=True)
        (tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.cleaned.txt").write_bytes(
            b"x" * 100
        )
        (tmp_path / SHARDS_DIRNAME).mkdir()
        # Write only 10 bytes, but checkpoint says 50.
        (tmp_path / SHARDS_DIRNAME / "tle2099.findings.jsonl").write_bytes(b"y" * 10)
        assert resume.verify_completed_outputs(
            self._completed_with_shard("tle2099.txt", 100, 50), str(tmp_path)
        ) == ["tle2099.txt"]


# ---------------------------------------------------------------------------
# Issue #118 — CompletedEntry round-trip and verify_completed_outputs wiring
# ---------------------------------------------------------------------------


class TestCompletedEntryRoundTrip:
    """CompletedEntry → as_dict → verify_completed_outputs round-trip
    (issue #118): the typed record must produce a dict shape that
    verify_completed_outputs accepts and actually inspects on disk. Entries
    are built the way worker_pool builds them — summary_dict + output_sizes —
    since resume deliberately owns no report import."""

    def _entry(self, out_dir, st):
        return resume.CompletedEntry(
            summary=summary_dict(st), outputs=resume.output_sizes(out_dir, st)
        )

    def _write_output(self, root, dirname, name, data=b"x" * 50):
        d = root / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)

    def test_as_dict_has_summary_and_outputs_keys(self, tmp_path):
        # The wire shape must match the checkpoint contract exactly.
        entry = resume.CompletedEntry(summary={"src_name": "tle2099.txt"}, outputs={})
        d = entry.as_dict()
        assert set(d) == {"summary", "outputs"}
        assert d["summary"] == {"src_name": "tle2099.txt"}
        assert d["outputs"] == {}

    def test_built_entry_is_valid(self, tmp_path):
        # The worker_pool construction recipe must produce an entry with a
        # summary dict and an outputs dict; the summary must include src_name.
        st = FileStats(src_name="tle2099.txt")
        self._write_output(
            tmp_path, f"{DATA_DIRNAME}/{CLEANED_DIRNAME}", "tle2099.00001.cleaned.txt"
        )
        entry = self._entry(str(tmp_path), st)
        assert isinstance(entry.summary, dict)
        assert isinstance(entry.outputs, dict)
        assert entry.summary.get("src_name") == "tle2099.txt"
        # The cleaned chunk was present — it must be in outputs.
        assert "tle2099.00001.cleaned.txt" in entry.outputs

    def test_round_trip_verify_passes_intact_outputs(self, tmp_path):
        # The dict produced by as_dict() must satisfy verify_completed_outputs
        # when all named output files are on disk at the recorded size.
        st = FileStats(src_name="tle2099.txt")
        data = b"y" * 80
        self._write_output(
            tmp_path,
            f"{DATA_DIRNAME}/{CLEANED_DIRNAME}",
            "tle2099.00001.cleaned.txt",
            data,
        )
        entry = self._entry(str(tmp_path), st)
        completed = {"tle2099.txt": entry.as_dict()}
        assert resume.verify_completed_outputs(completed, str(tmp_path)) == []

    def test_round_trip_verify_flags_missing_output(self, tmp_path):
        # If the output file is absent after as_dict() is serialised, the
        # round-trip must flag it for reprocessing.
        st = FileStats(src_name="tle2099.txt")
        data = b"z" * 60
        self._write_output(
            tmp_path,
            f"{DATA_DIRNAME}/{CLEANED_DIRNAME}",
            "tle2099.00001.cleaned.txt",
            data,
        )
        entry = self._entry(str(tmp_path), st)
        # Remove the output chunk to simulate a post-completion corruption.
        (
            tmp_path / DATA_DIRNAME / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt"
        ).unlink()
        completed = {"tle2099.txt": entry.as_dict()}
        assert resume.verify_completed_outputs(completed, str(tmp_path)) == [
            "tle2099.txt"
        ]

    def test_checkpoint_bytes_unchanged_after_refactor(self, tmp_path):
        # Byte-determinism check (issue #118): the JSON bytes produced by
        # build_checkpoint when using CompletedEntry.as_dict() must be
        # identical to those produced by the pre-refactor inline dict.
        import json

        summary = {"src_name": "tle2099.txt", "clean_count": 5, "quarantined_count": 1}
        outputs = {"tle2099.cleaned.txt": 200, "tle2099.broken.txt": 50}

        # Pre-refactor inline dict (the old write path).
        pre_refactor_entry = {"summary": summary, "outputs": outputs}
        # Post-refactor typed path.
        post_refactor_entry = resume.CompletedEntry(
            summary=summary, outputs=outputs
        ).as_dict()

        completed = {"data/source/tle2099.txt": pre_refactor_entry}
        completed_new = {"data/source/tle2099.txt": post_refactor_entry}

        ckpt_old = resume.build_checkpoint(
            inputs={}, completed=completed, run_identity={}
        )
        ckpt_new = resume.build_checkpoint(
            inputs={}, completed=completed_new, run_identity={}
        )

        # json.dumps with sort_keys=True must produce identical bytes.
        old_bytes = json.dumps(ckpt_old, separators=(",", ":"), sort_keys=True)
        new_bytes = json.dumps(ckpt_new, separators=(",", ":"), sort_keys=True)
        assert old_bytes == new_bytes
