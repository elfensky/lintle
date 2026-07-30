"""Signal and worker-shutdown helpers for CLI runs."""

import signal


def signal_exit_code(signo):
    """Conventional 128 + signal number: 130 SIGINT, 143 SIGTERM, 129 SIGHUP."""
    return 128 + int(signo)


def format_cancel_message(*, done, total):
    """Return the operator-facing cancellation/resume guidance."""
    # Resume granularity is a whole file: the checkpoint is written only as
    # files complete, so the file interrupted mid-stream is never resumable and
    # always restarts. With nothing completed there is no checkpoint at all, so
    # the re-run simply starts over.
    if done == 0:
        return (
            f"interrupted — workers stopped (0/{total} files done).\n"
            "No file finished, so nothing was checkpointed — re-running starts "
            "over from the beginning.\n"
            "Resume only skips fully-completed files; a file interrupted "
            "mid-stream always restarts."
        )
    return (
        f"interrupted — workers stopped ({done}/{total} files done).\n"
        f"Re-run the same command (same --out-dir) to skip the {done} completed "
        "file(s) and finish the rest; inputs must be unchanged. The file "
        "interrupted mid-stream restarts.\n"
        "Pass --no-resume to start over."
    )


def ignore_sigint():
    """Worker-process initializer: ignore Ctrl-C in the worker."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
