"""Process-pool dispatch for clean runs.

``run_workers`` returns a :class:`WorkerRunResult`. ``failed_files`` is a
``list[tuple[str, str]]`` of ``(path, error_string)`` pairs, one entry per
file whose worker raised an exception — the path as submitted to the
executor, the error string ``str(exc)`` from the caught exception.
"""

import concurrent.futures
import dataclasses
import multiprocessing
import os
import signal

from lintle import (
    cli_progress,
    pipeline,
    process_control,
    report,
    resume,
    run_planning,
    term,
)


@dataclasses.dataclass(slots=True, frozen=True)
class WorkerRunResult:
    """Outcome of one pool dispatch: the collected per-file stats, the files
    whose workers raised, and how the run ended (interrupt signal or
    parent-side operational error)."""

    all_stats: list
    failed_files: list[tuple[str, str]]
    interrupted: bool
    interrupted_signo: int
    operational_error: Exception | None
    # True when the live table could not fit every row in the terminal and had
    # to window them — the caller then prints the complete static results table,
    # since the frame left on screen shows only the window.
    display_windowed: bool = False


def _failure_detail(exc: Exception) -> str:
    """The error string recorded for a failed file. Normally ``str(exc)``; with
    ``LINTLE_DEBUG`` set, the chained remote traceback (which the pool carries
    on ``__cause__``) is appended so the worker-side failure site isn't lost at
    the process boundary."""
    if os.environ.get("LINTLE_DEBUG") and exc.__cause__ is not None:
        return f"{exc}\n{exc.__cause__}"
    return str(exc)


def run_workers(
    config: run_planning.CleanConfig,
    files: list[str],
    plan: run_planning.RunPlan,
    jobs: int,
    console,
    sizes: dict[str, int],
) -> WorkerRunResult:
    """Dispatch ``plan.files_to_process`` across a worker pool."""
    all_stats = list(plan.reused_stats)
    failed_files = []
    interrupted = False
    display = None  # the live table, kept past its `with` for the window flag
    interrupted_signo = signal.SIGINT
    operational_error: Exception | None = None
    with multiprocessing.Manager() as manager:
        progress_queue = manager.Queue()
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=jobs, initializer=process_control.ignore_sigint
        )
        caught = {"signo": signal.SIGINT}

        def _raise_interrupt(signo, _frame):
            caught["signo"] = signo
            raise KeyboardInterrupt

        # Save ALL three previous handlers — including SIGINT — before
        # installing the traps so the finally block can restore them on every
        # exit path: success, KeyboardInterrupt, or operational error
        # (issue #100).
        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.signal(signal.SIGTERM, _raise_interrupt)
        prev_hup = signal.signal(signal.SIGHUP, _raise_interrupt)
        try:
            futures = {
                executor.submit(
                    pipeline.process_file,
                    path,
                    config.out_dir,
                    config.command,
                    progress_queue,
                    reconstruct_checksum=config.reconstruct_checksum,
                    chunk_records=config.chunk_records,
                ): path
                for path in plan.files_to_process
            }
            with cli_progress.ProgressDisplay(
                len(files),
                progress_queue,
                console,
                sizes,
                completed=plan.reused_stats,
            ) as progress:
                display = progress  # kept past the `with` for its window flag
                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        stats = future.result()
                    except Exception as exc:
                        progress.file_failed(path, exc)
                        failed_files.append((path, _failure_detail(exc)))
                    else:
                        # Guard the parent-side post-result bookkeeping so an
                        # unexpected OSError (e.g. ENOSPC from write_checkpoint)
                        # is treated as an operational error rather than escaping
                        # the pool dispatch with a raw traceback that would
                        # collide with exit-code 1 (the quality-gate meaning).
                        # Issue #89.
                        try:
                            all_stats.append(stats)
                            progress.file_done(stats)
                            plan.completed[path] = resume.CompletedEntry(
                                summary=report.summary_dict(stats),
                                outputs=resume.output_sizes(config.out_dir, stats),
                            ).as_dict()
                            resume.write_checkpoint(
                                config.out_dir,
                                resume.build_checkpoint(
                                    inputs=plan.inputs,
                                    completed=plan.completed,
                                    run_identity=plan.run_identity,
                                ),
                            )
                        except Exception as exc:
                            # Parent-side bookkeeping failure: tear the pool
                            # down via the KI path; mark as an operational
                            # error so cli.main returns 2, not 1.
                            operational_error = exc
                            raise KeyboardInterrupt from None
        except KeyboardInterrupt:
            # Issue #100: immediately silence all three signals so a second
            # signal arriving mid-teardown (e.g. a second SIGTERM) cannot fire
            # _raise_interrupt and abort cleanup with a fresh uncaught
            # KeyboardInterrupt.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            interrupted = True
            interrupted_signo = caught["signo"]
            process_control.terminate_workers(executor)
            executor.shutdown(wait=False, cancel_futures=True)
            if operational_error is None:
                term.note(
                    process_control.format_cancel_message(
                        done=len(plan.completed), total=len(files)
                    )
                )
        else:
            executor.shutdown(wait=True)
        finally:
            # Restore all three handlers saved above (issue #100).
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGHUP, prev_hup)
    return WorkerRunResult(
        all_stats=all_stats,
        failed_files=failed_files,
        interrupted=interrupted,
        interrupted_signo=interrupted_signo,
        operational_error=operational_error,
        # A windowed live table showed only part of the roster, so the caller
        # still owes the operator the complete results table.
        display_windowed=display is not None and display.windowed,
    )
