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
            "paths", nargs="*", default=[_DEFAULT_SOURCE],
            help=f"files or directories to process (default: {_DEFAULT_SOURCE})",
        )
        sub.add_argument(
            "--out-dir", default=_DEFAULT_OUTPUT,
            help=f"destination for cleaned/broken files (default: {_DEFAULT_OUTPUT})",
        )
        sub.add_argument(
            "--jobs", type=int, default=os.cpu_count() or 1,
            help="number of files to process in parallel",
        )
        sub.add_argument(
            "--report", choices=["text", "json"], default="text",
            help="summary output format",
        )
    return parser
