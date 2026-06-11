"""Tests for process_control.py — signal/shutdown helpers and clean-run signal traps."""

import signal

import pytest

from lintle import cli, process_control, worker_pool


class TestShutdownHelpers:
    def test_ignore_sigint_sets_handler_to_ignore(self):
        original = signal.getsignal(signal.SIGINT)
        try:
            process_control.ignore_sigint()
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, original)

    def test_terminate_workers_terminates_every_process(self):
        class _FakeProc:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        class _FakeExecutor:
            def __init__(self, processes):
                self._processes = processes

        procs = {1: _FakeProc(), 2: _FakeProc()}
        process_control.terminate_workers(_FakeExecutor(procs))
        assert all(proc.terminated for proc in procs.values())

    def test_terminate_workers_falls_back_to_shutdown_when_processes_missing(self):
        # If a future CPython removes or renames the private `_processes`
        # attribute, we must still stop the pool — fall back to the public
        # shutdown(cancel_futures=True) API instead of silently no-op'ing.
        class _NoPrivateExecutor:
            def __init__(self):
                self.shutdown_kwargs = None

            @property
            def _processes(self):
                raise AttributeError("simulated CPython API change")

            def shutdown(self, **kwargs):
                self.shutdown_kwargs = kwargs

        executor = _NoPrivateExecutor()
        process_control.terminate_workers(executor)
        assert executor.shutdown_kwargs == {"cancel_futures": True}

    def test_terminate_workers_warns_to_stderr_when_processes_missing(self, capsys):
        # The fallback path is observable — print a one-line note so the
        # operator knows shutdown took the slow path (waits for in-flight
        # tasks to cancel) rather than the immediate-terminate path.
        class _NoPrivateExecutor:
            @property
            def _processes(self):
                raise AttributeError

            def shutdown(self, **kwargs):
                pass

        process_control.terminate_workers(_NoPrivateExecutor())
        err = capsys.readouterr().err
        assert "_processes" in err


class TestSignalHandling:
    def test_cancel_message_some_done_skips_completed_not_continues(self):
        # With some files completed, the re-run skips them and reprocesses the
        # rest; the file interrupted mid-stream restarts. The message must not
        # promise to "continue where it stopped" — resume has no intra-file
        # granularity, and that wording read as a broken resume.
        msg = process_control.format_cancel_message(done=12, total=29)
        assert "12/29" in msg
        assert "--no-resume" in msg
        assert "same --out-dir" in msg
        assert "continue where it stopped" not in msg
        assert "restart" in msg.lower()

    def test_cancel_message_zero_done_says_it_restarts(self):
        # No file finished -> no checkpoint is written -> the re-run starts over
        # from the beginning. The message must say so rather than imply
        # resumable progress (the single-file Ctrl-C field report). It also must
        # not dangle --no-resume, since there is no checkpoint to ignore.
        msg = process_control.format_cancel_message(done=0, total=1)
        assert "0/1" in msg
        assert "continue where it stopped" not in msg
        assert "starts over" in msg.lower()

    def test_signal_exit_code(self):
        assert process_control.signal_exit_code(signal.SIGINT) == 130
        assert process_control.signal_exit_code(signal.SIGTERM) == 143

    def test_sigterm_sighup_traps_installed_and_raise(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # A clean run must trap SIGTERM and SIGHUP (not just SIGINT) so a
        # scheduler/preemption kill stops gracefully and exits 128+signo. We
        # don't deliver a real signal (flaky); we capture what main() registers
        # and confirm the installed trap raises KeyboardInterrupt — which the
        # executor's except-path converts to the signal exit code (§3.2).
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        registered = {}
        real_signal = signal.signal

        def recording_signal(signum, handler):
            registered.setdefault(signum, []).append(handler)
            return real_signal(signum, handler)

        monkeypatch.setattr(signal, "signal", recording_signal)
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0

        assert signal.SIGTERM in registered, "SIGTERM was never trapped"
        assert signal.SIGHUP in registered, "SIGHUP was never trapped"
        # The first handler installed for SIGTERM during the run is the trap;
        # invoking it must raise KeyboardInterrupt (the graceful-stop trigger).
        trap = registered[signal.SIGTERM][0]
        with pytest.raises(KeyboardInterrupt):
            trap(signal.SIGTERM, None)

    def test_sigint_restored_after_interrupted_run(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Issue #100: after an interrupted run (KeyboardInterrupt path),
        # SIGINT must be restored to what it was before run_workers was called.
        # The bug: the KI branch set SIGINT to SIG_IGN but the finally only
        # restored SIGTERM and SIGHUP — SIGINT stayed SIG_IGN after the run.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        before_sigint = signal.getsignal(signal.SIGINT)

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(worker_pool.concurrent.futures, "as_completed", _interrupt)
        # run_workers sets SIGINT to SIG_IGN in the KI branch; capture post-call
        # value before restoring so we can assert the fix took effect.
        try:
            rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        finally:
            pass  # check *before* restoring

        after_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, before_sigint)  # restore for test isolation

        assert after_sigint == before_sigint, (
            f"SIGINT was not restored after interrupted run: "
            f"expected {before_sigint!r}, got {after_sigint!r}"
        )
        assert rc == 130

    def test_signal_handlers_restored_after_normal_run(self, tmp_path, line1, line2):
        # Issue #100: after run_workers returns on the success path, all three
        # signal dispositions must be restored to pre-call values.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        before_sigint = signal.getsignal(signal.SIGINT)
        before_sigterm = signal.getsignal(signal.SIGTERM)
        before_sighup = signal.getsignal(signal.SIGHUP)

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0

        assert signal.getsignal(signal.SIGINT) == before_sigint, (
            "SIGINT handler was not restored after normal run"
        )
        assert signal.getsignal(signal.SIGTERM) == before_sigterm, (
            "SIGTERM handler was not restored after normal run"
        )
        assert signal.getsignal(signal.SIGHUP) == before_sighup, (
            "SIGHUP handler was not restored after normal run"
        )

    def test_all_signals_ignored_during_cleanup_window(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # Issue #100: at the start of the KI cleanup branch, all three signals
        # must be set to SIG_IGN so a second SIGTERM/SIGHUP during teardown
        # does not fire _raise_interrupt and abort cleanup mid-flight.
        # We verify this by observing which handlers are set during the cleanup.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        handlers_set: list[tuple[int, object]] = []
        real_signal = signal.signal

        def recording_signal(signum, handler):
            handlers_set.append((signum, handler))
            return real_signal(signum, handler)

        monkeypatch.setattr(signal, "signal", recording_signal)

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(worker_pool.concurrent.futures, "as_completed", _interrupt)

        original_sigint = signal.getsignal(signal.SIGINT)
        try:
            cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        finally:
            signal.signal(signal.SIGINT, original_sigint)

        # After the KI branch runs, SIG_IGN must have been set for all three
        # signals (in order to block a second signal during teardown).
        ign_set = {
            signum for signum, handler in handlers_set if handler is signal.SIG_IGN
        }
        assert signal.SIGINT in ign_set, "SIGINT not set to SIG_IGN during cleanup"
        assert signal.SIGTERM in ign_set, "SIGTERM not set to SIG_IGN during cleanup"
        assert signal.SIGHUP in ign_set, "SIGHUP not set to SIG_IGN during cleanup"
