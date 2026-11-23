"""Tests for clean_run.join_exit_code — the pure five-source exit-code join."""

import dataclasses
import signal

from lintle import clean_run


@dataclasses.dataclass(slots=True, frozen=True)
class _Stats:
    clean_count: int = 0
    quarantined_count: int = 0


class TestJoinExitCode:
    """clean_run.join_exit_code — one table of exit-code precedence."""

    def test_plan_verdict_wins_outright(self):
        assert clean_run.join_exit_code(plan_exit_code=0) == 0
        assert clean_run.join_exit_code(plan_exit_code=2) == 2

    def test_operational_error_is_exit_2_and_beats_the_gate(self):
        assert clean_run.join_exit_code(operational_error=OSError("ENOSPC")) == 2

    def test_interrupt_maps_to_128_plus_signo(self):
        assert clean_run.join_exit_code(interrupted_signo=signal.SIGINT) == 130
        assert clean_run.join_exit_code(interrupted_signo=signal.SIGTERM) == 143

    def test_failed_file_is_exit_2_before_the_quarantine_gate(self):
        stats = [_Stats(clean_count=9)]
        code = clean_run.join_exit_code(
            failed_files=[("tle1.txt", "boom")], all_stats=stats
        )
        assert code == 2

    def test_quarantine_gate_decides_last(self):
        dirty = [_Stats(clean_count=5, quarantined_count=1)]
        assert clean_run.join_exit_code(all_stats=[_Stats(clean_count=5)]) == 0
        assert clean_run.join_exit_code(all_stats=dirty) == 1
        assert clean_run.join_exit_code(all_stats=dirty, quarantine_threshold=1) == 0
