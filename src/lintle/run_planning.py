"""Clean-run preflight planning and resume resolution."""

import dataclasses

from lintle import report, resume, term


@dataclasses.dataclass
class RunPlan:
    """The resolved pre-flight plan for a ``clean``/``validate`` run."""

    files_to_process: list[str] = dataclasses.field(default_factory=list)
    reused_stats: list[report.FileStats] = dataclasses.field(default_factory=list)
    inputs: dict[str, object] = dataclasses.field(default_factory=dict)
    completed: dict[str, object] = dataclasses.field(default_factory=dict)
    run_identity: dict[str, object] = dataclasses.field(default_factory=dict)
    exit_code: int | None = None


def resolve_clean_plan(
    args,
    files,
    file_sizes,
    *,
    check_disk_space,
    is_interactive,
    prompt_yes_no,
    run_started_stamp,
    scrub_outputs,
):
    """Resolve disk-space, resume, and fresh-run state for ``clean``."""
    disk_status = check_disk_space(args.out_dir, sum(file_sizes.values()))
    if disk_status is not None:
        severity, msg = disk_status
        term.emit(severity, msg)
        if severity is term.Severity.ERROR:
            return RunPlan(exit_code=2)

    inputs = {path: resume.input_fingerprint(path) for path in files}
    run_identity = {"max_quarantined": args.max_quarantined}

    classification = resume.classify_checkpoint(args.out_dir, inputs, run_identity)
    decision = resume.resolve_resume_action(
        classification,
        resume=args.resume,
        no_resume=args.no_resume,
        interactive=is_interactive(),
        prompt=prompt_yes_no,
    )
    if decision.action is resume.ResumeAction.ABORT:
        term.error(decision.message)
        return RunPlan(exit_code=decision.exit_code)
    if decision.action is resume.ResumeAction.RESUME:
        checkpoint = classification.checkpoint
        completed = dict(checkpoint["completed"])
        # Integrity re-verification: drop any completed entry whose outputs are
        # missing or truncated, so they are reprocessed.
        for bad_path in resume.verify_completed_outputs(completed, args.out_dir):
            completed.pop(bad_path, None)
        reused_stats = [
            report.stats_from_summary(e["summary"]) for e in completed.values()
        ]
        files_to_process = [f for f in files if f not in completed]
        term.note(
            f"resuming: {len(completed)}/{len(files)} files already complete, "
            f"processing {len(files_to_process)}"
            " — pass --no-resume for a fresh run"
        )
        return RunPlan(
            files_to_process=files_to_process,
            reused_stats=reused_stats,
            inputs=inputs,
            completed=completed,
            run_identity=run_identity,
        )

    # FRESH: archive any checkpoint, then scrub output trees so no orphans linger.
    resume.archive_checkpoint(args.out_dir, timestamp=run_started_stamp())
    scrub_outputs(args.out_dir)
    return RunPlan(
        files_to_process=files,
        inputs=inputs,
        run_identity=run_identity,
    )
