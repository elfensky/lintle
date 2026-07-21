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
# `lintle clean` groups all its output under <out-dir>/data/ (the cleaned TLE
# corpus + broken sidecars + report), keeping the out-dir root to one dir per
# pipeline step — data/ (clean), verify/, dedup/. cleaned/broken/report are
# subdirs of data/; .shards/ and the checkpoint stay at the root as transient
# machinery, not step output.
DATA_DIRNAME = "data"
CLEANED_DIRNAME = "cleaned"
BROKEN_DIRNAME = "broken"
REPORT_DIRNAME = "report"
SHARDS_DIRNAME = ".shards"


def stem(filename):
    """Return a filename without its trailing ``.txt`` extension
    (``"tle2022.txt"`` -> ``"tle2022"``); names without it are returned unchanged.
    """
    return filename[:-4] if filename.endswith(".txt") else filename
