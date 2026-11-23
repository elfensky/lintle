"""Tests for lintle.clean_run — the pure five-source exit-code join and the
``run(args)`` orchestration called directly, not through ``cli.main``."""

import dataclasses
import signal
from fractions import Fraction

import pytest

from lintle import CLEANED_DIRNAME, SHARDS_DIRNAME, clean_run, cli, resume


@dataclasses.dataclass(slots=True, frozen=True)
class _Stats:
    clean_count: int = 0
    quarantined_count: int = 0


_CLEAN = [_Stats(clean_count=5)]
_DIRTY = [_Stats(clean_count=5, quarantined_count=1)]  # 1 of 6 routed ≈ 16.7%
_FAILED = [("tle1.txt", "boom")]
_OS_ERROR = OSError("ENOSPC")

# id -> (join_exit_code kwargs, expected exit code). Precedence: plan verdict →
# operational error (2) → interrupt (128+signo) → failed file (2) → quarantine gate.
_JOIN_TABLE = {
    # Each source on its own.
    "nothing-ran": ({}, 0),
    "gate-pass": ({"all_stats": _CLEAN}, 0),
    "gate-exceeded": ({"all_stats": _DIRTY}, 1),
    "gate-at-count-threshold": ({"all_stats": _DIRTY, "quarantine_threshold": 1}, 0),
    "gate-pct-exceeded": (
        {
            "all_stats": _DIRTY,
            "threshold_mode": "pct",
            "quarantine_threshold": Fraction(10),
        },
        1,
    ),
    "gate-pct-within": (
        {
            "all_stats": _DIRTY,
            "threshold_mode": "pct",
            "quarantine_threshold": Fraction(20),
        },
        0,
    ),
    "failed-file": ({"failed_files": _FAILED, "all_stats": _CLEAN}, 2),
    "interrupt-sigint": ({"interrupted_signo": signal.SIGINT}, 130),
    "interrupt-sigterm": ({"interrupted_signo": signal.SIGTERM}, 143),
    "interrupt-sighup": ({"interrupted_signo": signal.SIGHUP}, 129),
    "operational-error": ({"operational_error": _OS_ERROR}, 2),
    "plan-verdict-0": ({"plan_exit_code": 0}, 0),
    "plan-verdict-2": ({"plan_exit_code": 2}, 2),
    # Combinations where precedence decides the code.
    "failed-file-beats-gate": ({"failed_files": _FAILED, "all_stats": _DIRTY}, 2),
    "interrupt-beats-failed-file-and-gate": (
        {
            "interrupted_signo": signal.SIGTERM,
            "failed_files": _FAILED,
            "all_stats": _DIRTY,
        },
        143,
    ),
    # worker_pool reports an operational error with interrupted=True too.
    "operational-error-beats-interrupt": (
        {"operational_error": _OS_ERROR, "interrupted_signo": signal.SIGTERM},
        2,
    ),
    "plan-verdict-beats-everything": (
        {
            "plan_exit_code": 0,
            "operational_error": _OS_ERROR,
            "interrupted_signo": signal.SIGTERM,
            "failed_files": _FAILED,
            "all_stats": _DIRTY,
        },
        0,
    ),
}


class TestJoinExitCode:
    """clean_run.join_exit_code — one table of exit-code precedence."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"), _JOIN_TABLE.values(), ids=_JOIN_TABLE.keys()
    )
    def test_join(self, kwargs, expected):
        assert clean_run.join_exit_code(**kwargs) == expected


class TestRun:
    """clean_run.run — one clean run from parsed args to exit code."""

    @pytest.mark.parametrize(
        ("bad_checksum", "expected"), [(False, 0), (True, 1)], ids=["clean", "gate"]
    )
    def test_run_returns_exit_code_and_tears_down_run_state(
        self, tmp_path, line1, line2, bad_checksum, expected
    ):
        src = tmp_path / "src"
        src.mkdir()
        if bad_checksum:
            line1 = line1[:68] + "9"  # quarantined, so the gate fails the run
        (src / "tle2099.txt").write_bytes(f"{line1}\n{line2}\n".encode("ascii"))
        out = tmp_path / "out"
        args = cli.build_parser().parse_args(
            ["clean", str(src), "--out-dir", str(out), "--jobs", "1"]
        )

        assert clean_run.run(args) == expected
        assert (out / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt").exists()
        # No failed file: the checkpoint and the shards are torn down together.
        assert resume.load_checkpoint(str(out)) is None
        assert not (out / SHARDS_DIRNAME).exists()
