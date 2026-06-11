"""Process-pool dispatch for validate and clean runs."""

import concurrent.futures
import multiprocessing
import signal

from lintle import cli_progress, pipeline, process_control, report, resume, term


def run_workers(args, files, plan, jobs, console, sizes):
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

        prev_term = signal.signal(signal.SIGTERM, _raise_interrupt)
        prev_hup = signal.signal(signal.SIGHUP, _raise_interrupt)
        try:
            futures = {
                executor.submit(
                    pipeline.process_file,
                    path,
                    args.out_dir,
                    args.command,
                    progress_queue,
                    reconstruct_checksum=args.reconstruct_checksum,
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
                        failed_files.append(path)
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
                            if args.command == "clean":
                                plan.completed[path] = {
                                    "summary": report.summary_dict(stats),
                                    "outputs": resume.output_sizes(args.out_dir, stats),
                                }
                                resume.write_checkpoint(
                                    args.out_dir,
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
            signal.signal(signal.SIGINT, signal.SIG_IGN)
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
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGHUP, prev_hup)
    return all_stats, failed_files, interrupted, interrupted_signo, operational_error
