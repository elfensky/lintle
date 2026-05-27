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
