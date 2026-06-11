"""Tests for lintle.fsutil — durable atomic file commit (issue #58)."""

import os
import stat
import sys

import pytest

from lintle import fsutil


class TestDurableReplace:
    def test_commits_tmp_content_to_dest_and_removes_tmp(self, tmp_path):
        tmp = tmp_path / "out.partial"
        dest = tmp_path / "out.final"
        tmp.write_bytes(b"cleaned tle data\n")
        fsutil.durable_replace(str(tmp), str(dest))
        assert dest.read_bytes() == b"cleaned tle data\n"
        assert not tmp.exists()

    def test_fsyncs_file_data_then_containing_directory(self, tmp_path, monkeypatch):
        # Record whether each fsync target is a directory, in call order. The
        # gap #58 closes is forgetting the directory fsync, so we assert both
        # happen: the regular file first (its data, before the rename), then the
        # containing directory (so the rename itself is durable).
        synced = []

        def record(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))

        monkeypatch.setattr(fsutil, "_fsync", record, raising=False)
        tmp = tmp_path / "out.partial"
        dest = tmp_path / "out.final"
        tmp.write_bytes(b"x")
        fsutil.durable_replace(str(tmp), str(dest))
        assert synced == [False, True]

    def test_overwrites_existing_dest(self, tmp_path):
        tmp = tmp_path / "out.partial"
        dest = tmp_path / "out.final"
        dest.write_bytes(b"stale")
        tmp.write_bytes(b"fresh")
        fsutil.durable_replace(str(tmp), str(dest))
        assert dest.read_bytes() == b"fresh"

    def test_handles_relative_dest_with_no_directory_component(
        self, tmp_path, monkeypatch
    ):
        # dest "out.final" has no dirname; the helper must fsync "." rather than
        # an empty path. Run from inside tmp_path so we don't touch the cwd.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "out.partial").write_bytes(b"data")
        fsutil.durable_replace("out.partial", "out.final")
        assert (tmp_path / "out.final").read_bytes() == b"data"

    def test_missing_tmp_raises_and_leaves_no_dest(self, tmp_path):
        dest = tmp_path / "out.final"
        with pytest.raises(FileNotFoundError):
            fsutil.durable_replace(str(tmp_path / "absent.partial"), str(dest))
        assert not dest.exists()

    def test_commits_on_non_macos_fsync_path(self, tmp_path, monkeypatch):
        # Force the os.fsync branch (Linux/other), exercised even on macOS CI.
        monkeypatch.setattr(fsutil, "_USE_FULLFSYNC", False)
        tmp = tmp_path / "out.partial"
        dest = tmp_path / "out.final"
        tmp.write_bytes(b"portable\n")
        fsutil.durable_replace(str(tmp), str(dest))
        assert dest.read_bytes() == b"portable\n"


class TestPlatformBarrier:
    def test_macos_selects_full_fsync(self):
        # The decision recorded in #58: macOS needs F_FULLFSYNC for real
        # power-loss durability; plain os.fsync is not a drive-cache barrier.
        if sys.platform == "darwin":
            assert fsutil._USE_FULLFSYNC is True
        else:
            assert fsutil._USE_FULLFSYNC is False


class TestOutDirLock:
    def test_acquires_and_releases(self, tmp_path):
        with fsutil.out_dir_lock(str(tmp_path)):
            assert os.path.exists(os.path.join(str(tmp_path), ".clean.lock"))
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # re-acquire succeeds after release

    def test_refuses_when_held_by_live_same_host(self, tmp_path):
        with fsutil.out_dir_lock(str(tmp_path)):  # noqa: SIM117
            with pytest.raises(fsutil.LockHeldError):
                with fsutil.out_dir_lock(str(tmp_path)):
                    pass

    def test_reclaims_dead_same_host_pid(self, tmp_path):
        lock = os.path.join(str(tmp_path), ".clean.lock")
        payload = f'{{"host": "{fsutil._host_id()}", "pid": 999999999, "started": "x"}}'
        with open(lock, "w") as h:
            h.write(payload)
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # reclaimed, no error

    def test_refuses_cross_host_even_if_pid_dead(self, tmp_path):
        lock = os.path.join(str(tmp_path), ".clean.lock")
        with open(lock, "w") as h:
            h.write('{"host": "some-other-host-xyz", "pid": 999999999, "started": "x"}')
        with pytest.raises(fsutil.LockHeldError):  # noqa: SIM117
            with fsutil.out_dir_lock(str(tmp_path)):
                pass

    def test_invalid_utf8_lock_treated_as_unreadable(self, tmp_path):
        # Issue #92: invalid-UTF-8 bytes in .clean.lock must not crash with a
        # UnicodeDecodeError traceback; the lock should be treated as unreadable
        # (same-host dead-PID reclaim path or LockHeldError, not AttributeError).
        lock = tmp_path / fsutil.LOCK_NAME
        lock.write_bytes(b"\xff\xfe")
        # We can't reclaim (not a dead same-host PID), so we expect LockHeldError
        # OR the lock to be quietly treated as corrupt and an error raised — but
        # crucially, NOT an unhandled UnicodeDecodeError.
        try:
            with fsutil.out_dir_lock(str(tmp_path)):
                pass
        except fsutil.LockHeldError:
            pass  # expected — corrupt lock treated as unrecognised holder
        # The key assertion: no UnicodeDecodeError was raised above.


class TestReadJsonOrNone:
    """``read_json_or_none`` returns a dict or None for any unreadable/malformed/
    non-dict payload — never raises OSError, json.JSONDecodeError, or UnicodeDecodeError.
    """

    def test_valid_json_object_returns_dict(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"k": 1}', encoding="utf-8")
        assert fsutil.read_json_or_none(p) == {"k": 1}

    def test_missing_file_returns_none(self, tmp_path):
        assert fsutil.read_json_or_none(tmp_path / "absent.json") is None

    def test_invalid_json_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert fsutil.read_json_or_none(p) is None

    def test_invalid_utf8_returns_none(self, tmp_path):
        # Issue #92: UnicodeDecodeError must be caught, not propagated.
        p = tmp_path / "bad.json"
        p.write_bytes(b"\xff\xfe")
        assert fsutil.read_json_or_none(p) is None

    def test_json_array_returns_none(self, tmp_path):
        # Issue #91 dict-guard: non-dict JSON (array/string/null/number) → None,
        # so callers get the safe "no usable data" default for any non-object payload.
        for doc in ('[]', '"hello"', '42', 'null'):
            p = tmp_path / "typed.json"
            p.write_text(doc, encoding="utf-8")
            assert fsutil.read_json_or_none(p) is None
