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


def _hold_lock_forever(out_dir, ready):
    """Child-process target: acquire the out-dir lock, signal, then block so the
    parent can observe a live hold and (after kill) the kernel's auto-release."""
    import time

    from lintle import fsutil as _fsutil

    with _fsutil.out_dir_lock(out_dir):
        ready.set()
        time.sleep(30)


class TestOutDirLock:
    def test_acquires_releases_and_keeps_the_file(self, tmp_path):
        lock = os.path.join(str(tmp_path), ".clean.lock")
        with fsutil.out_dir_lock(str(tmp_path)):
            assert os.path.exists(lock)
        # The file is intentionally NOT unlinked on release: flock binds to the
        # inode, so unlinking would let a racing opener lock an orphaned inode.
        assert os.path.exists(lock)
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # re-acquire succeeds — the kernel dropped the flock on close

    def test_refuses_while_a_live_holder_has_it(self, tmp_path):
        # flock binds to the open file description, so even a second acquire in
        # this same process (a distinct os.open) is denied while the first holds
        # — the same mechanism that excludes two separate processes.
        with fsutil.out_dir_lock(str(tmp_path)):  # noqa: SIM117
            with pytest.raises(fsutil.LockHeldError):
                with fsutil.out_dir_lock(str(tmp_path)):
                    pass

    def test_reclaims_a_stale_lock_file_with_no_live_holder(self, tmp_path):
        # Issues #87/#99: liveness is the kernel's flock, not the recorded
        # identity. A leftover lock file from a crashed/rebooted run holds no
        # flock, so it is acquired no matter what it names — even a currently
        # LIVE pid (PID reuse), a different host (post-reboot), or corrupt bytes.
        lock = os.path.join(str(tmp_path), ".clean.lock")
        for payload in (
            f'{{"host": "{fsutil._host_id()}", "pid": {os.getpid()}, "started": "x"}}',
            '{"host": "some-other-host-xyz", "pid": 999999999, "started": "x"}',
            "not even json",
        ):
            with open(lock, "w") as h:
                h.write(payload)
            with fsutil.out_dir_lock(str(tmp_path)):
                pass  # reclaimed cleanly — no LockHeldError, no crash

    def test_invalid_utf8_lock_does_not_crash(self, tmp_path):
        # Issue #92: invalid-UTF-8 bytes in .clean.lock must never surface as a
        # UnicodeDecodeError. With no live holder the lock is simply reclaimed.
        (tmp_path / fsutil.LOCK_NAME).write_bytes(b"\xff\xfe")
        with fsutil.out_dir_lock(str(tmp_path)):
            pass

    def test_a_refused_run_does_not_disturb_the_holder(self, tmp_path):
        # Issue #87 (blind release): a refused run must not delete or release the
        # live holder's lock. Hold it, get refused twice, and confirm the holder
        # keeps exclusive possession throughout.
        with fsutil.out_dir_lock(str(tmp_path)):  # holder A  # noqa: SIM117
            for _ in range(2):
                with (
                    pytest.raises(fsutil.LockHeldError),
                    fsutil.out_dir_lock(str(tmp_path)),
                ):
                    pass
        with fsutil.out_dir_lock(str(tmp_path)):
            pass  # A released — next run acquires

    def test_lockheld_message_names_the_file_and_escape_hatch(self, tmp_path):
        # Issue #99: name the lock file and the manual-removal escape hatch so an
        # operator facing a (truly) stuck lock isn't left guessing.
        with fsutil.out_dir_lock(str(tmp_path)):  # noqa: SIM117
            with pytest.raises(fsutil.LockHeldError) as excinfo:
                with fsutil.out_dir_lock(str(tmp_path)):
                    pass
        msg = str(excinfo.value)
        assert fsutil.LOCK_NAME in msg and "remove" in msg

    def test_auto_releases_when_the_holding_process_is_killed(self, tmp_path):
        # The crux of the redesign (#87/#99): the lock IS the kernel's flock on
        # the holder's fd, so a holder that is SIGKILLed — no clean release, no
        # unlink — frees it automatically. No stale-lock reclaim, no wedge.
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        ready = ctx.Event()
        proc = ctx.Process(target=_hold_lock_forever, args=(str(tmp_path), ready))
        proc.start()
        try:
            assert ready.wait(timeout=15), "child never acquired the lock"
            with (  # live child holds it
                pytest.raises(fsutil.LockHeldError),
                fsutil.out_dir_lock(str(tmp_path)),
            ):
                pass
            proc.kill()  # SIGKILL: no chance to clean up — mimics a crash
            proc.join(timeout=15)
            with fsutil.out_dir_lock(str(tmp_path)):
                pass  # kernel dropped the dead holder's flock — we acquire freely
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=15)


class TestReadJsonOrNone:
    """``read_json_or_none`` returns a dict or None for any unreadable/malformed/
    non-dict payload — never raises OSError, json.JSONDecodeError, or
    UnicodeDecodeError.
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
        for doc in ("[]", '"hello"', "42", "null"):
            p = tmp_path / "typed.json"
            p.write_text(doc, encoding="utf-8")
            assert fsutil.read_json_or_none(p) is None


class TestFsyncFallback:
    """Issue #98 — _fsync must fall back to os.fsync when F_FULLFSYNC raises
    OSError (SMB/NFS/exFAT filesystems that do not implement F_FULLFSYNC).
    """

    def test_fullfsync_oserror_falls_back_to_os_fsync(self, tmp_path, monkeypatch):
        # Force _USE_FULLFSYNC True and reset the per-process latch so we
        # exercise the F_FULLFSYNC path even on Linux CI; monkeypatch
        # fcntl.fcntl to raise OSError (ENOTSUP); assert os.fsync is called
        # instead and _fsync does not raise.
        monkeypatch.setattr(fsutil, "_USE_FULLFSYNC", True)
        monkeypatch.setattr(fsutil, "_fullfsync_works", None)
        import fcntl as _fcntl

        # F_FULLFSYNC is macOS-only; add it so this forced-macOS-path test is
        # reachable on Linux CI, where the constant is otherwise absent and
        # `_fsync` would raise AttributeError before reaching the fallback.
        # fcntl.fcntl itself is monkeypatched below, so the value is inert.
        monkeypatch.setattr(_fcntl, "F_FULLFSYNC", 51, raising=False)

        called_fsync = []

        def raise_enotsup(fd, op):
            raise OSError(45, "Operation not supported")  # ENOTSUP

        monkeypatch.setattr(_fcntl, "fcntl", raise_enotsup)

        def spy_fsync(fd):
            called_fsync.append(fd)

        monkeypatch.setattr(os, "fsync", spy_fsync)

        # _fsync must not raise and must have called os.fsync as fallback.
        f = tmp_path / "test.bin"
        f.write_bytes(b"x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            fsutil._fsync(fd)
        finally:
            os.close(fd)
        assert called_fsync, "os.fsync fallback was not called"

    def test_fullfsync_success_does_not_call_os_fsync(self, tmp_path, monkeypatch):
        # When F_FULLFSYNC succeeds, os.fsync must NOT be called (no double-sync).
        monkeypatch.setattr(fsutil, "_USE_FULLFSYNC", True)
        monkeypatch.setattr(fsutil, "_fullfsync_works", None)
        import fcntl as _fcntl

        # F_FULLFSYNC is macOS-only; add it so this forced-macOS-path test is
        # reachable on Linux CI, where the constant is otherwise absent and
        # `_fsync` would raise AttributeError before reaching the fallback.
        # fcntl.fcntl itself is monkeypatched below, so the value is inert.
        monkeypatch.setattr(_fcntl, "F_FULLFSYNC", 51, raising=False)

        called_fsync = []
        called_fullfsync = []

        def fake_fcntl(fd, op):
            called_fullfsync.append(fd)

        monkeypatch.setattr(_fcntl, "fcntl", fake_fcntl)

        def spy_fsync(fd):
            called_fsync.append(fd)

        monkeypatch.setattr(os, "fsync", spy_fsync)

        f = tmp_path / "test.bin"
        f.write_bytes(b"x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            fsutil._fsync(fd)
        finally:
            os.close(fd)
        assert called_fullfsync, "F_FULLFSYNC was not called"
        assert not called_fsync, "os.fsync must not be called when F_FULLFSYNC succeeds"

    def test_durable_replace_survives_fullfsync_enotsup(self, tmp_path, monkeypatch):
        # End-to-end: durable_replace must succeed even when F_FULLFSYNC raises
        # OSError; the fallback path still commits the file.
        monkeypatch.setattr(fsutil, "_USE_FULLFSYNC", True)
        monkeypatch.setattr(fsutil, "_fullfsync_works", None)
        import fcntl as _fcntl

        # F_FULLFSYNC is macOS-only; add it so this forced-macOS-path test is
        # reachable on Linux CI, where the constant is otherwise absent and
        # `_fsync` would raise AttributeError before reaching the fallback.
        # fcntl.fcntl itself is monkeypatched below, so the value is inert.
        monkeypatch.setattr(_fcntl, "F_FULLFSYNC", 51, raising=False)

        def raise_enotsup(fd, op):
            raise OSError(45, "Operation not supported")

        monkeypatch.setattr(_fcntl, "fcntl", raise_enotsup)

        tmp = tmp_path / "data.partial"
        dest = tmp_path / "data.final"
        tmp.write_bytes(b"durable content\n")
        fsutil.durable_replace(str(tmp), str(dest))
        assert dest.read_bytes() == b"durable content\n"
        assert not tmp.exists()


class TestDurableWriteText:
    """Issue #85 — durable_write_text pins LF newlines and deduplicates the
    .partial / durable_replace boilerplate for bounded text-mode writers.
    """

    def test_writes_content_to_dest(self, tmp_path):
        dest = tmp_path / "report.md"
        fsutil.durable_write_text(str(dest), "hello\nworld\n")
        assert dest.read_text(encoding="utf-8") == "hello\nworld\n"

    def test_pins_lf_newlines_in_binary(self, tmp_path):
        # The key contract: text with \n must reach disk as \n, never \r\n.
        dest = tmp_path / "report.json"
        fsutil.durable_write_text(str(dest), "line1\nline2\n")
        raw = dest.read_bytes()
        assert b"\n" in raw
        assert b"\r\n" not in raw

    def test_no_partial_left_behind(self, tmp_path):
        dest = tmp_path / "out.md"
        fsutil.durable_write_text(str(dest), "content\n")
        leftovers = list(tmp_path.glob("*.partial"))
        assert leftovers == []

    def test_ascii_encoding_param(self, tmp_path):
        dest = tmp_path / "broken-noradids.ndjson"
        fsutil.durable_write_text(str(dest), '{"id":1}\n', encoding="ascii")
        assert dest.read_bytes() == b'{"id":1}\n'

    def test_overwrites_existing_dest(self, tmp_path):
        dest = tmp_path / "report.md"
        dest.write_bytes(b"stale content\n")
        fsutil.durable_write_text(str(dest), "fresh content\n")
        assert dest.read_text(encoding="utf-8") == "fresh content\n"

    def test_routes_through_durable_replace(self, tmp_path, monkeypatch):
        # Verify the commit is atomic: durable_replace is called, not a
        # direct rename or copy (so power-loss durability is guaranteed).
        calls = []
        real_durable_replace = fsutil.durable_replace

        def spy(tmp, dest):
            calls.append((tmp, dest))
            return real_durable_replace(tmp, dest)

        monkeypatch.setattr(fsutil, "durable_replace", spy)
        dest_path = str(tmp_path / "out.md")
        fsutil.durable_write_text(dest_path, "hello\n")
        assert len(calls) == 1
        tmp_used, dest_used = calls[0]
        assert tmp_used == dest_path + ".partial"
        assert dest_used == dest_path
