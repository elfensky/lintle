"""Process-pool dispatch for validate and clean runs."""

from lintle import cli_progress, pipeline, report, resume, term


def run_workers(
    args,
    files,
    plan,
    jobs,
    console,
    sizes,
    *,
    futures_module,
    multiprocessing_module,
    signal_module,
    ignore_sigint,
    terminate_workers,
    format_cancel_message,
    output_sizes,
):
    """Dispatch ``plan.files_to_process`` across a worker pool."""
    all_stats = list(plan.reused_stats)
    failed_files = []
    interrupted = False
    interrupted_signo = signal_module.SIGINT
    with multiprocessing_module.Manager() as manager:
        progress_queue = manager.Queue()
        executor = futures_module.ProcessPoolExecutor(
            max_workers=jobs, initializer=ignore_sigint
        )
        caught = {"signo": signal_module.SIGINT}

        def _raise_interrupt(signo, _frame):
            caught["signo"] = signo
            raise KeyboardInterrupt

        prev_term = signal_module.signal(signal_module.SIGTERM, _raise_interrupt)
        prev_hup = signal_module.signal(signal_module.SIGHUP, _raise_interrupt)
        try:
            futures = {
                executor.submit(
                    pipeline.process_file,
                    path,
                    args.out_dir,
                    args.command,
                    progress_queue,
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
                for future in futures_module.as_completed(futures):
                    path = futures[future]
                    try:
                        stats = future.result()
                    except Exception as exc:
                        progress.file_failed(path, exc)
                        failed_files.append(path)
                    else:
                        all_stats.append(stats)
                        progress.file_done(stats)
                        if args.command == "clean":
                            plan.completed[path] = {
                                "summary": report.summary_dict(stats),
                                "outputs": output_sizes(args.out_dir, stats),
                            }
                            resume.write_checkpoint(
                                args.out_dir,
                                resume.build_checkpoint(
                                    inputs=plan.inputs,
                                    completed=plan.completed,
                                    run_identity=plan.run_identity,
                                ),
                            )
        except KeyboardInterrupt:
            signal_module.signal(signal_module.SIGINT, signal_module.SIG_IGN)
            interrupted = True
            interrupted_signo = caught["signo"]
            terminate_workers(executor)
            executor.shutdown(wait=False, cancel_futures=True)
            term.note(format_cancel_message(done=len(plan.completed), total=len(files)))
        else:
            executor.shutdown(wait=True)
        finally:
            signal_module.signal(signal_module.SIGTERM, prev_term)
            signal_module.signal(signal_module.SIGHUP, prev_hup)
    return all_stats, failed_files, interrupted, interrupted_signo
