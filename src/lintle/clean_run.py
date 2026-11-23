"""The ``lintle clean`` orchestration: input discovery, preflight planning,
worker-pool dispatch, run finalization, and the one five-source exit-code join
(:func:`join_exit_code`, pure and table-testable). Extracted from ``cli.py`` so
the clean tail has a name of its own — ``cli._dispatch``'s clean arm is now a
thin :func:`run` call — while argparse, config precedence, and the non-clean
subcommands stay behind in ``cli``."""

import contextlib
import os
import shutil
import time
import traceback
from pathlib import Path

from lintle import (
    BROKEN_SUFFIX,
    CLEANED_SUFFIX,
    SHARDS_DIRNAME,
    cli_progress,
    fsutil,
    output_artifacts,
    process_control,
    report,
    resume,
    run_planning,
    summary,
    term,
    thresholds,
    worker_pool,
)

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"


def discover_paths(path):
    """Expand ``path``: a directory becomes its sorted ``tle*.txt`` regular
    files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool output, and
    excluding dangling symlinks and directories even when named ``tle*.txt``);
    a file is returned as a single-element list. A nonexistent entry yields
    ``[]`` — callers should validate inputs with :func:`input_path_error` first.
    """
    directory = Path(path)
    if directory.is_dir():
        return [
            str(directory / name)
            for name in sorted(entry.name for entry in directory.iterdir())
            if (
                name.startswith("tle")
                and name.endswith(".txt")
                and not name.endswith(CLEANED_SUFFIX)
                and not name.endswith(BROKEN_SUFFIX)
                and (directory / name).is_file()  # excludes dangling symlinks + dirs
            )
        ]
    if Path(path).is_file():
        return [path]
    return []


def input_path_error(path, using_default):
    """Return a user-facing error string if ``path`` does not exist, else
    ``None``. ``using_default`` tailors the message for the case where the
    user passed nothing and the default (``data/source``) is what's missing.

    Readability is *not* checked here — :func:`os.access` consults the
    POSIX mode bits only and is a false-negative on filesystems that grant
    read through ACLs (NFSv4, SMB, FUSE). The authoritative answer is
    whatever the worker's :func:`open` returns; a genuine permission error
    surfaces through the per-file failure path in :func:`run` with the
    same exit code 2.
    """
    if Path(path).exists():
        return None
    if using_default:
        return (
            f"default input directory {_DEFAULT_SOURCE!r} does not exist.\n"
            f"  pass a file or directory on the command line,\n"
            f"  or create {_DEFAULT_SOURCE}/ and put your tle*.txt files there.\n"
            f"  run 'lintle --help' for usage and examples."
        )
    return f"no such file or directory: {path!r}"


def resolve_jobs(explicit, cpu_count, n_files):
    """Resolve the worker count for a run. An explicit ``--jobs`` is the user's
    deliberate choice and is returned unchanged; otherwise default to one fewer
    than the CPU count — reserving a core for the OS during a long run — capped
    at the number of files and floored at one."""
    if explicit is not None:
        return explicit
    return max(1, min((cpu_count or 1) - 1, n_files))


def join_exit_code(
    *,
    plan_exit_code=None,
    operational_error=None,
    interrupted_signo=None,
    failed_files=(),
    all_stats=(),
    threshold_mode="count",
    quarantine_threshold=0,
):
    """Join the five exit-code sources of a clean run into one process exit
    code, in precedence order: a preflight plan verdict wins outright; a
    parent-side operational error is exit 2; an interrupt maps to 128+signo;
    a file that failed to process is exit 2 (spec §2.7 / §10 — exit 1 stays
    reserved for the quality gate); otherwise the quarantine threshold gate
    decides 0/1. Pure — every side effect (error text, artifact writes) stays
    with the callers in :func:`run` and :func:`_finalize_run`."""
    if plan_exit_code is not None:
        return plan_exit_code
    if operational_error is not None:
        return 2
    if interrupted_signo is not None:
        return process_control.signal_exit_code(interrupted_signo)
    if failed_files:
        return 2
    return thresholds.quarantine_exit_code(
        all_stats, threshold_mode, quarantine_threshold
    )


def _finalize_run(
    args,
    all_stats,
    failed_files,
    *,
    run_started_iso,
    run_monotonic_start,
    threshold_mode,
    quarantine_threshold,
    resumed_names=frozenset(),
    print_results=True,
):
    """Finish a non-interrupted run: sort stats, write the clean-run artifacts,
    emit the text/JSON summary, tear down resumable state on success, and return
    the process exit code. Split out of :func:`run` so the orchestration there
    stays at the level of phases (check inputs -> plan -> dispatch -> finalize).
    ``resumed_names`` is the basenames carried over from a previous run, dimmed
    in the results table because their numbers came from that earlier run.
    ``print_results`` prints that table; the live display already ends on the
    same per-file numbers, so the caller suppresses it unless the table was
    windowed or never rendered."""
    all_stats.sort(key=lambda stats: stats.src_name)

    # Build the run envelope once, unconditionally — even an all-failed run
    # (empty all_stats) must emit a valid versioned object under --report json,
    # never ``null`` (``build_run_envelope([])`` returns a zeroed envelope). The
    # same object is persisted to report.json (when there are stats) and printed
    # to stdout under --report json — one object, no divergence.
    # Issue #20: top-level versioned envelope. Run wall-clock is the parent
    # process's monotonic delta, NOT the sum of per-file worker durations
    # (those are reported per-file under ``files[i].elapsed_seconds`` and
    # exceed parent wall-clock under ``--jobs N``).
    run_elapsed = time.monotonic() - run_monotonic_start
    envelope = report.build_run_envelope(
        all_stats,
        command=args.command,
        started_at=run_started_iso,
        elapsed_seconds=run_elapsed,
        failed_files=failed_files,
    )

    # A `clean` run writes a Markdown run report, the machine-readable
    # ``report.json`` (the byte-identical twin of the --report json stdout
    # envelope), a corpus-wide NDJSON of NORAD IDs quarantined anywhere, and
    # (issue #9) a corpus-wide ``report.jsonl`` of structured findings
    # concatenated from the per-worker shards. All are always written on a
    # successful clean run — empty when nothing was quarantined — so downstream
    # consumers see a stable artifact set. A spinner covers the silent
    # finalization (the per-worker shard concat dominates on a large corpus); a
    # no-op off a TTY, and after the progress block exits, so no Live nesting.
    if all_stats:
        output_artifacts.write_clean_artifacts(
            args.out_dir,
            all_stats,
            envelope,
            failed_files=failed_files,
            chunk_records=args.chunk_records,
        )

    if args.report == "json":
        print(report.render_run_json(envelope), end="")
    elif all_stats:
        # Phase 3 of the display: the per-file results table, then the human
        # aggregate panel. Both go to stderr (styled ephemera) — per-file detail
        # also lives durably in report.md. Off a TTY they degrade to plain ASCII.
        # Text-mode stdout stays empty so a pipe sees nothing the report.json
        # artifact doesn't already carry. This revisits the old "no per-file
        # dump" rule deliberately: that rule was about polluting *stdout*.
        if print_results:
            summary.render_files(
                envelope, console=term.stderr_console, resumed=resumed_names
            )
        summary.render(envelope, console=term.stderr_console, command_label="clean")

    # A fully successful clean run leaves no resumable state behind. The
    # checkpoint and the findings shards (`.shards`) are both in-progress run
    # state and are torn down together, here, ONLY on success: an interrupted
    # run already returned 130 above (keeping both), and a failed run keeps both
    # too, so the operator can fix the cause and `--resume` re-reads the
    # surviving shards to rebuild a complete `report.jsonl` (issue #56). The
    # `report.jsonl` was written from those shards by the report block above.
    if not failed_files:
        resume.delete_checkpoint(args.out_dir)
        shard_dir = Path(args.out_dir) / SHARDS_DIRNAME
        if shard_dir.exists():
            shutil.rmtree(shard_dir)

    # A file that could not be processed is an operational failure (exit 2);
    # otherwise the quarantine quality gate decides — one definition of the
    # precedence, in join_exit_code.
    return join_exit_code(
        failed_files=failed_files,
        all_stats=all_stats,
        threshold_mode=threshold_mode,
        quarantine_threshold=quarantine_threshold,
    )


def _debug_traceback() -> None:
    """Emit the full traceback to stderr when ``LINTLE_DEBUG`` is set — the
    opt-in escape hatch behind the clean single-line operational errors."""
    if os.environ.get("LINTLE_DEBUG"):
        term.error(traceback.format_exc())


def run(args):
    """Orchestrate one ``lintle clean`` run from parsed args to process exit
    code: validate the clean argument surface, discover and stat the inputs,
    take the out-dir lock, resolve the preflight plan, dispatch the worker
    pool, and finalize — with every exit path routed through
    :func:`join_exit_code`. ``args`` is the argparse namespace after
    ``cli._apply_config_paths`` has filled config-backed defaults."""
    # `args.path` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = args.path is None
    path = args.path if args.path is not None else _DEFAULT_SOURCE

    if args.jobs is not None and args.jobs < 1:
        term.error(f"--jobs must be >= 1 (got {args.jobs})")
        return 2

    try:
        threshold_mode, quarantine_threshold = thresholds.parse_quarantine_threshold(
            args.max_quarantined
        )
    except ValueError as exc:
        term.error(str(exc))
        return 2

    path_error = input_path_error(path, using_default=using_default)
    if path_error:
        term.error(path_error)
        return 2

    files = discover_paths(path)
    if not files:
        if Path(path).is_dir():
            term.error(
                f"no tle*.txt files found in {path!r}.\n"
                "  expected one or more files named tle*.txt "
                "(excluding *.cleaned.txt / *.broken.txt)."
            )
        else:
            term.error("no input files found")
        return 2

    # Stat every input exactly once — the single source of size truth shared by
    # the disk-space guard, the pre-run roster, and the live byte-bar
    # denominators, so those three readouts can never silently diverge. Ordered
    # by discovery so the roster lists files in a stable order.
    try:
        file_sizes = {p: Path(p).stat().st_size for p in files}
    except OSError as exc:
        term.error(f"cannot read input file: {exc}")
        return 2

    # ExitStack holds the out-dir lock for the clean run. Closed in the finally
    # block below — so every exit path (LockHeldError aside, which returns
    # before entering the try, leaving the stack empty) releases the lock.
    _lock_stack = contextlib.ExitStack()

    try:
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        term.error(f"cannot create output directory {args.out_dir!r}: {exc}")
        return 2

    try:
        _lock_stack.enter_context(
            fsutil.out_dir_lock(args.out_dir, started=resume.run_started_stamp())
        )
    except fsutil.LockHeldError as exc:
        # Stack is still empty — no lock to release.
        term.error(str(exc))
        return 2
    except OSError as exc:
        # A non-contention error acquiring the lock (e.g. ENOLCK, a read-only
        # out-dir). out_dir_lock has already closed its fd; surface it as a clean
        # exit 2 rather than an unhandled traceback.
        term.error(f"cannot lock output directory {args.out_dir!r}: {exc}")
        return 2

    # The try/finally guarantees _lock_stack.close() runs on every exit path
    # that reaches here: disk-error return, ABORT return, interrupt return,
    # failed-files return, and normal success — so the advisory flock is always
    # released (the kernel would drop it on process death regardless; the file
    # itself is intentionally left in place — see out_dir_lock).
    try:
        # Build the typed config snapshot from args ONCE so the two leaves
        # (resolve_clean_plan, run_workers) receive named, statically-typed
        # fields rather than a raw argparse Namespace.  A flag rename that
        # breaks from_args surfaces at this single construction site rather
        # than mid-run as an AttributeError inside a leaf (issue #121).
        config = run_planning.CleanConfig.from_args(args)
        try:
            plan = run_planning.resolve_clean_plan(config, files, file_sizes)
        except OSError as exc:
            term.error(f"preflight error: {exc}")
            return 2
        if plan.exit_code is not None:
            return join_exit_code(plan_exit_code=plan.exit_code)
        # Resolve the worker count now that files_to_process is final: an
        # explicit --jobs is honoured as-is; the default is CPU count - 1,
        # capped at the file count and floored at one (issue #53 §2.3).
        jobs = resolve_jobs(
            # process_cpu_count honours CPU affinity (taskset, cgroup quota,
            # a scheduler pinning) where cpu_count reports the whole machine;
            # identical on macOS, right on a constrained Linux box.
            config.jobs,
            os.process_cpu_count(),
            len(plan.files_to_process),
        )

        # The shared rich Console on stderr (term.stderr_console) drives both the
        # roster and the live progress block; off a TTY each degrades to plain
        # text. Byte-bar denominators come from os.stat (issue #53 §2.1/§2.2) —
        # no pre-read of the corpus.
        console = term.stderr_console
        # Every discovered file, in input order — not just the ones left to do.
        # On a resumed run the carried-over files still get rows (filled in from
        # the checkpoint), so the live table is the whole corpus and its
        # "N/M files" count matches the rows under it.
        sizes = {Path(p).name: file_sizes[p] for p in files}
        # On a TTY the live table opens on the same rows and fills them in, so
        # printing a separate roster first would be the same list twice. Off a
        # TTY there is no live table, and this static roster is the only
        # discovery view a piped run gets.
        if not console.is_terminal:
            cli_progress.render_roster(
                console, {p: file_sizes[p] for p in plan.files_to_process}
            )

        if not plan.reused_stats:
            term.note(
                f"processing {len(plan.files_to_process)} file(s) "
                f"with {jobs} worker(s)..."
            )
        # Run-level timing for the v1 envelope (issue #20). The wall-clock
        # start captures NOW (just before worker dispatch); the corresponding
        # stop happens after the executor drains. Two separate measurements:
        # ``run_started_iso`` is an ISO 8601 UTC string for human-readable
        # ``run.timestamp``; ``run_monotonic_start`` feeds the elapsed_seconds
        # subtraction using a monotonic clock so NTP jitter mid-run cannot
        # produce a negative duration. Per spec §4, this aggregate is
        # intentionally NOT the sum of per-file worker durations.
        run_started_iso = report.utc_stamp()
        run_monotonic_start = time.monotonic()

        result = worker_pool.run_workers(config, files, plan, jobs, console, sizes)

        if result.operational_error is not None:
            # Issue #89: parent-side bookkeeping failure (e.g. ENOSPC from
            # write_checkpoint). The pool has already been torn down via the
            # KI path; surface a clean error and return 2 (operational error).
            # Exit 1 is reserved exclusively for the quarantine quality gate.
            term.error(
                f"run aborted due to an operational error: {result.operational_error}\n"
                "  if some files completed, re-run with --resume to finish."
            )
            return join_exit_code(operational_error=result.operational_error)

        if result.interrupted:
            return join_exit_code(interrupted_signo=result.interrupted_signo)

        return _finalize_run(
            args,
            result.all_stats,
            result.failed_files,
            run_started_iso=run_started_iso,
            run_monotonic_start=run_monotonic_start,
            threshold_mode=threshold_mode,
            quarantine_threshold=quarantine_threshold,
            resumed_names=frozenset(s.src_name for s in plan.reused_stats),
            # The live table already ended on the per-file results, unless it
            # had to window them away or never ran at all (off a TTY).
            print_results=result.display_windowed or not console.is_terminal,
        )
    except Exception as exc:
        # Issue #89: catch-all backstop — any unhandled exception in the clean
        # orchestration (that isn't a KeyboardInterrupt, which propagates
        # naturally) maps to a clean exit-2 operational error so exit 1 stays
        # unambiguous as the quality-gate signal. A brief resume hint tells the
        # operator that partial progress may be recoverable.
        term.error(
            f"unexpected error during clean: {exc}\n"
            "  if some files completed, re-run with --resume to finish."
        )
        _debug_traceback()
        return 2
    finally:
        _lock_stack.close()
