"""Core TLE validation: the single definition of a "perfect" record.

Pure functions only — no I/O. Column references use 1-indexed TLE column
numbers in prose; Python slices below are 0-indexed.
"""

LINE_LENGTH = 69


def compute_checksum(line):
    """Return the mod-10 TLE checksum of the first 68 characters of ``line``.

    Each digit adds its value, each ``-`` adds 1, every other character
    (letters, spaces, ``.``, ``+``) adds 0. The result is ``sum % 10``.
    """
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10
