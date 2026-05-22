"""Command-line interface: ``tle-clean validate`` and ``tle-clean clean``."""

import argparse
import concurrent.futures
import json
import os
import shutil
import sys

from tlekit import pipeline, report

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"


def discover_paths(paths):
    """Expand each entry in ``paths``: a directory becomes its sorted
    ``tle*.txt`` files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool
    output); a file is passed through unchanged.
    """
    result = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if (
                    name.startswith("tle")
                    and name.endswith(".txt")
                    and not name.endswith(".cleaned.txt")
                    and not name.endswith(".broken.txt")
                ):
                    result.append(os.path.join(path, name))
        else:
            result.append(path)
    return result


def build_parser():
    """Build the ``tle-clean`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="tle-clean",
        description="Validate and clean Two-Line Element (TLE) corpus files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("validate", "audit files and report defects (writes nothing)"),
        ("clean", "write cleaned files and quarantine sidecars"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "paths",
            nargs="*",
            default=[_DEFAULT_SOURCE],
            help=f"files or directories to process (default: {_DEFAULT_SOURCE})",
        )
        sub.add_argument(
            "--out-dir",
            default=_DEFAULT_OUTPUT,
            help=f"destination for cleaned/broken files (default: {_DEFAULT_OUTPUT})",
        )
        sub.add_argument(
            "--jobs",
            type=int,
            default=os.cpu_count() or 1,
            help="number of files to process in parallel",
        )
        sub.add_argument(
            "--report",
            choices=["text", "json"],
            default="text",
            help="summary output format",
        )
    return parser


def _check_disk_space(out_dir, files):
    """Return an error string if ``out_dir`` lacks room for cleaned +
    broken output (roughly twice the total input size), else ``None``.
    """
    needed = sum(os.path.getsize(f) for f in files) * 2
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        return (
            f"insufficient disk space in {out_dir}: "
            f"need ~{needed:,} bytes, have {free:,}"
        )
    return None


def main(argv=None):
    """Entry point for the ``tle-clean`` console script.

    Returns the process exit code: ``0`` = no records quarantined;
    ``1`` = at least one record quarantined; ``2`` = operational error
    (no input files, disk shortfall, or a file that failed to process).
    """
    args = build_parser().parse_args(argv)
    files = discover_paths(args.paths)
    if not files:
        print("no input files found", file=sys.stderr)
        return 2

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        disk_error = _check_disk_space(args.out_dir, files)
        if disk_error:
            print(disk_error, file=sys.stderr)
            return 2

    print(
        f"processing {len(files)} file(s) with {args.jobs} worker(s)...",
        file=sys.stderr,
        flush=True,
    )
    all_stats = []
    failed_files = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                pipeline.process_file, path, args.out_dir, args.command
            ): path
            for path in files
        }
        for done, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            path = futures[future]
            try:
                stats = future.result()
                all_stats.append(stats)
                print(
                    f"[{done}/{len(files)}] {stats.src_name} — "
                    f"{stats.clean_count:,} clean, "
                    f"{stats.quarantined_count:,} quarantined",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[{done}/{len(files)}] error processing {path}: {exc!r}",
                    file=sys.stderr,
                )
                failed_files.append(path)

    all_stats.sort(key=lambda stats: stats.src_name)

    # A `clean` run writes a Markdown run report to the out-dir root.
    report_path = None
    if args.command == "clean" and all_stats:
        report_path = os.path.join(args.out_dir, "report.md")
        report.write_run_report(report_path, all_stats)

    if args.report == "json":
        print(json.dumps([report.summary_dict(s) for s in all_stats], indent=2))
    else:
        for stats in all_stats:
            print(report.format_summary(stats))
            if args.command == "validate" and stats.rejects:
                print(report.format_reject_lines(stats))
        if report_path:
            print(f"\nrun report: {report_path}")

    # A file that could not be processed is an operational error (spec §10),
    # and that outranks the quarantined-record signal.
    if failed_files:
        return 2
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    return 1 if total_quarantined else 0
