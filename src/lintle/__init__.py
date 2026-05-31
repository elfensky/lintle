"""lintle — validator and cleaner for Two-Line Element (TLE) corpus files."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("lintle")
except PackageNotFoundError:  # source checkout that was never installed
    __version__ = "0.0.0+local"


def stem(filename):
    """Return a filename without its trailing ``.txt`` extension
    (``"tle2022.txt"`` -> ``"tle2022"``); names without it are returned unchanged.
    """
    return filename[:-4] if filename.endswith(".txt") else filename
