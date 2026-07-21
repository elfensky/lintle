"""Process-pool dispatch for clean runs.

``run_workers`` returns a 5-tuple:
``(all_stats, failed_files, interrupted, interrupted_signo, operational_error)``.
``failed_files`` is a ``list[tuple[str, str]]`` of ``(path, error_string)``
pairs, one entry per file whose worker raised an exception.  The path is the
full input path as submitted to the executor; the error string is ``str(exc)``
from the caught exception.  Callers that only need the truthiness check (is
the list non-empty?) are unaffected by the shape change.
"""

import concurrent.futures
import multiprocessing
import signal

from lintle import cli_progress, pipeline, process_control, resume, run_planning, term


def run_workers(config: run_planning.CleanConfig, files, plan, jobs, console, sizes):
    """Dispatch ``plan.files_to_process`` across a worker pool."""
    all_stats = list(plan.reused_stats)
    failed_files = []
    interrupted = False
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

        # Save ALL three previous handlers before installing the traps so the
        # finally block can restore them on every exit path — success,
        # KeyboardInterrupt, or unexpected operational error. Issue #100: the
        # previous code only saved prev_term/prev_hup; the KI branch then set
        # SIGINT to SIG_IGN but never restored it, leaving it ignored after an
        # interrupted run.
        prev_int = signal.signal(signal.SIGINT, signal.getsignal(signal.SIGINT))
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
                already_done=len(plan.completed),
            ) as progress:
                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        stats = future.result()
                    except Exception as exc:
                        progress.file_failed(path, exc)
                        failed_files.append((path, str(exc)))
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
                            plan.completed[path] = resume.CompletedEntry.from_stats(
                                config.out_dir, stats
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
            # Issue #100: restore ALL three handlers — including SIGINT — on
            # every exit path (success, KI, or operational error). Previously
            # only SIGTERM and SIGHUP were restored; SIGINT was left as SIG_IGN
            # after an interrupted run.
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGHUP, prev_hup)
    return all_stats, failed_files, interrupted, interrupted_signo, operational_error
