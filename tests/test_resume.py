"""Tests for lintle.resume — the per-run `clean --resume` checkpoint (issue #56)."""

import os

import pytest

import lintle
from lintle import resume, stem


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
    def test_schema_version_is_2_and_records_run_identity(self):
        ckpt = resume.build_checkpoint(
            inputs={"a.txt": {"size": 1}},
            completed={"a.txt": {"summary": {"clean_count": 3}, "outputs": {}}},
            run_identity={"args": []},
        )
        assert ckpt["schema_version"] == 2
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
        (out / "cleaned").mkdir()
        (out / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 100)
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
        (out / "cleaned").mkdir()
        (out / "cleaned" / "tle2099.cleaned.txt").write_bytes(b"x" * 7)
        assert resume.verify_completed_outputs(
            self._completed("tle2099.txt", 100), str(out)
        ) == ["tle2099.txt"]


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
