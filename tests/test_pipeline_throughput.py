"""End-to-end throughput regression test for ``pipeline.process_file``.

Streams hundreds of thousands of synthetic TLE records through the real
pipeline and gates on two signals: a within-run *stability* check that fires
on GC pauses, IO hiccups, and hangs; and an opt-in *baseline* check that
fires on absolute throughput regression versus a per-machine reference.

Opt-in only — gated by ``@pytest.mark.slow`` and excluded from the default
``pytest`` invocation by ``pyproject.toml``'s ``addopts``. Run explicitly
with ``uv run pytest -m slow -s`` (the ``-s`` lets the throughput line
print). The current CI workflow runs ``uv run pytest`` with no ``-m``, so
this test never fires there — that is deliberate, since a throughput test
on a noisy shared runner is a flake factory.

Six design decisions, recorded so future readers see the rationale:

1. *Synthetic records.* We vary the satellite number on the conftest
   ``CANONICAL_LINE*`` constants and recompute the checksum via
   ``tle.compute_checksum``, then stream the result to a tempfile so
   ``process_file`` exercises its real on-disk reader. No dependence on the
   real ``data/source/`` corpus, and the records pass ``tle.py`` validation.

2. *Hybrid baseline.* A stored baseline lets us catch absolute regressions
   ("everything is now 40 % slower"); a within-run stability check catches
   the noise the stored baseline cannot ("one of three runs hit a GC pause").
   Either gate failing fails the test. The baseline file is per-machine and
   git-ignored — hardware variance makes a shared committed baseline lie.

3. *In one file.* Helpers live alongside the test rather than in
   ``tests/_throughput_helpers.py`` because they exist only to serve this one
   test; pulling them out is premature abstraction.

4. *CI gating via marker.* ``addopts = "-m 'not slow'"`` keeps the default
   suite untouched. No nightly job in this PR — scope creep belongs in a
   follow-up issue.

5. *Severe regression* = either (a) the slowest timed run is more than
   ``MAX_RUN_SLOWDOWN`` slower than the timed-runs median, or (b) the median
   throughput is below ``MIN_BASELINE_FRACTION`` of the stored baseline.
   30 % is well above realistic single-host noise.

6. *Determinism* = fixed RNG seed, fixed record count, fixed mutation rule.
   Same machine and Python should reproduce records/sec within the envelope.

Update the baseline after an intentional perf change::

    LINTLE_UPDATE_BASELINE=1 uv run pytest -m slow

That writes ``tests/.throughput_baseline.json`` (git-ignored) keyed by the
current Python major.minor version.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import pytest

from lintle import pipeline, tle
from tests.conftest import CANONICAL_LINE1, CANONICAL_LINE2

# How many records to stream per timed run. Sized to give a few seconds of
# work on a developer laptop — small enough that the full 5-run cycle is
# under ~30 seconds, large enough that timing noise stays well below the
# 30 % gate. Don't change this casually: the stored baseline is keyed only
# by Python version, not by record count, so a different count invalidates
# any baseline set against the previous count.
RECORD_COUNT = 200_000

# Each invocation: WARMUP_RUNS discarded, then TIMED_RUNS measured. Warmup
# absorbs cold disk caches, the cost of first-touching the cleaned-output
# directory, and any pytest-coverage instrumentation overhead that decays
# after the first call into the module under test.
WARMUP_RUNS = 2
TIMED_RUNS = 3

# Stability gate: no single timed run may take more than this multiple of
# the timed-runs median. 1.43 ≈ 1 / 0.70, i.e. throughput must stay above
# 70 % of the median throughput across timed runs.
MAX_RUN_SLOWDOWN = 1.43

# Baseline gate: median throughput must reach this fraction of the stored
# baseline records-per-second. 0.70 means a 30 % regression fails the test.
MIN_BASELINE_FRACTION = 0.70

# Deterministic seed for the synthetic-record stream.
RNG_SEED = 0xC0DE

# Where the per-machine baseline lives. Git-ignored.
BASELINE_PATH = Path(__file__).parent / ".throughput_baseline.json"

# Env-var switch to record a fresh baseline instead of asserting against it.
UPDATE_BASELINE_ENV = "LINTLE_UPDATE_BASELINE"


def _python_key() -> str:
    """Return ``"3.11"`` etc. — baselines are keyed by major.minor only."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _iter_synthetic_records(count: int, seed: int = RNG_SEED):
    """Yield ``count`` ``(line1, line2)`` pairs derived from the conftest canon.

    Each pair preserves the canonical 69-column layout and re-passes the
    ``tle.compute_checksum`` mod-10 rule. Only the 5-digit satellite-number
    field (cols 3–7, 0-indexed slice ``[2:7]``) varies; everything else is
    held constant so the pipeline's CPU work per record stays representative.
    """
    # A linear congruential generator inline keeps the per-record overhead
    # tiny and avoids ``random.Random`` attribute lookups in the hot loop.
    # Values mod 100_000 land safely inside the 5-digit satnum field.
    state = seed & 0xFFFFFFFF
    for _ in range(count):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        satnum = state % 100_000
        sat_field = f"{satnum:05d}"
        body1 = CANONICAL_LINE1[:2] + sat_field + CANONICAL_LINE1[7:68]
        body2 = CANONICAL_LINE2[:2] + sat_field + CANONICAL_LINE2[7:68]
        l1 = body1 + str(tle.compute_checksum(body1))
        l2 = body2 + str(tle.compute_checksum(body2))
        yield l1, l2


def _write_synthetic_file(path: Path, count: int) -> None:
    """Stream ``count`` synthetic records into ``path`` as a real TLE file."""
    with open(path, "wb") as fh:
        for l1, l2 in _iter_synthetic_records(count):
            fh.write(l1.encode("ascii"))
            fh.write(b"\n")
            fh.write(l2.encode("ascii"))
            fh.write(b"\n")


def _time_validate_run(src: Path, out_dir: Path) -> tuple[int, float]:
    """Run ``process_file`` in validate mode once; return (records, seconds).

    We use ``validate`` (not ``clean``) so the timing focuses on the CPU-bound
    parse-and-validate path without coupling the measurement to output-IO
    variance from writing N cleaned records back to disk.
    """
    t0 = time.perf_counter()
    stats = pipeline.process_file(str(src), str(out_dir), "validate")
    elapsed = time.perf_counter() - t0
    return stats.paired_records, elapsed


def _load_baseline() -> dict[str, dict[str, float]]:
    """Return the stored baseline mapping, or ``{}`` if no file exists yet."""
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _save_baseline(baseline: dict[str, dict[str, float]]) -> None:
    """Persist the baseline mapping as pretty JSON keyed by Python version."""
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class TestPipelineThroughput:
    @pytest.mark.slow
    def test_no_severe_throughput_regression(self, tmp_path: Path) -> None:
        """Stream synthetic records and gate on stability + optional baseline."""
        src = tmp_path / "throughput.txt"
        _write_synthetic_file(src, RECORD_COUNT)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        timings: list[float] = []
        for _ in range(WARMUP_RUNS + TIMED_RUNS):
            records, elapsed = _time_validate_run(src, out_dir)
            assert records == RECORD_COUNT, (
                f"pipeline parsed {records} records, generator emitted "
                f"{RECORD_COUNT} — the test workload is malformed"
            )
            timings.append(elapsed)

        timed = timings[WARMUP_RUNS:]
        timed_median = statistics.median(timed)
        slowest_timed = max(timed)
        median_rps = RECORD_COUNT / timed_median
        slowest_rps = RECORD_COUNT / slowest_timed
        slowdown_ratio = slowest_timed / timed_median

        py = _python_key()

        # Update-baseline mode: record and exit before asserting. The user
        # explicitly asked to overwrite, so the previous value is gone.
        if os.environ.get(UPDATE_BASELINE_ENV) == "1":
            baseline = _load_baseline()
            baseline[py] = {
                "records_per_second": round(median_rps, 1),
                "record_count": RECORD_COUNT,
                "set_on": time.strftime("%Y-%m-%d"),
            }
            _save_baseline(baseline)
            print(
                f"\n[throughput] baseline updated for Python {py}: "
                f"{median_rps:,.0f} rec/s at count={RECORD_COUNT}"
            )
            return

        # Stability gate — always on. Catches GC pauses, IO hiccups, hangs.
        assert slowdown_ratio <= MAX_RUN_SLOWDOWN, (
            f"throughput unstable: slowest timed run was "
            f"{slowdown_ratio:.2f}× slower than the timed-runs median "
            f"({slowest_rps:,.0f} vs {median_rps:,.0f} rec/s) — "
            f"threshold {MAX_RUN_SLOWDOWN:.2f}×."
        )

        # Baseline gate — only when a baseline exists for this Python.
        baseline = _load_baseline().get(py)
        baseline_msg = "no baseline (run with LINTLE_UPDATE_BASELINE=1 to set)"
        if baseline is not None:
            baseline_rps = baseline["records_per_second"]
            fraction = median_rps / baseline_rps
            baseline_msg = (
                f"baseline={baseline_rps:,.0f} rec/s "
                f"(set {baseline['set_on']}, fraction={fraction:.2%})"
            )
            assert fraction >= MIN_BASELINE_FRACTION, (
                f"throughput regressed: median {median_rps:,.0f} rec/s is "
                f"{fraction:.2%} of baseline {baseline_rps:,.0f} rec/s — "
                f"threshold {MIN_BASELINE_FRACTION:.0%}."
            )

        # Always print so a passing run still leaves a record. Visible with
        # ``pytest -m slow -s`` or in the captured-output section on failure.
        print(
            f"\n[throughput] py={py} count={RECORD_COUNT} "
            f"median={timed_median:.3f}s slowest={slowest_timed:.3f}s "
            f"median_rps={median_rps:,.0f} slowest_rps={slowest_rps:,.0f} "
            f"slowdown_ratio={slowdown_ratio:.2f}× "
            f"stability_threshold={MAX_RUN_SLOWDOWN:.2f}× "
            f"{baseline_msg}"
        )
