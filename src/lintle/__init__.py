"""lintle — validator and cleaner for Two-Line Element (TLE) corpus files."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("lintle")
except PackageNotFoundError:  # source checkout that was never installed
    __version__ = "0.0.0+local"

# Output filename suffixes and directory names — the single source of truth for
# the naming convention shared by pipeline._clean_output_paths, resume.output_sizes,
# cli.discover_paths, and report_writers.concat_findings_shards. Changing one here
# propagates everywhere; no consumer re-encodes the convention.
CLEANED_SUFFIX = ".cleaned.txt"
BROKEN_SUFFIX = ".broken.txt"
FINDINGS_SUFFIX = ".findings.jsonl"
# `lintle` lays out its out-dir as one flat level of directories, numbered in
# pipeline order so the directory listing itself documents the order of
# operations: 01-cleaned (clean), 02-broken (clean), 03-report (clean),
# 04-verify (verify), 05-dedup (dedup), 06-extract (extract). .shards/ and the
# checkpoint stay at the root as transient machinery, not step output.
CLEANED_DIRNAME = "01-cleaned"
BROKEN_DIRNAME = "02-broken"
REPORT_DIRNAME = "03-report"
VERIFY_DIRNAME = "04-verify"
DEDUP_DIRNAME = "05-dedup"
EXTRACT_DIRNAME = "06-extract"
SHARDS_DIRNAME = ".shards"


def stem(filename):
    """Return a filename without its trailing ``.txt`` extension
    (``"tle2022.txt"`` -> ``"tle2022"``); names without it are returned unchanged.
    """
    return filename[:-4] if filename.endswith(".txt") else filename
