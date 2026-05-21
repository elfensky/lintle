"""tlekit — validator and cleaner for Two-Line Element (TLE) corpus files."""

__version__ = "0.1.0"


def stem(filename):
    """Return a filename without its trailing ``.txt`` extension.

    ``"tle2022.txt"`` -> ``"tle2022"``; other names are returned unchanged.
    """
    return filename[:-4] if filename.endswith(".txt") else filename
