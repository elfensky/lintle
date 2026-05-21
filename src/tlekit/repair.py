"""Speculative, validated repair of raw TLE lines and records.

Every fix is applied and then confirmed by ``tle`` validation; a fix is
committed only if the result passes. Pure functions — no I/O.
"""

from tlekit import tle

RECONSTRUCTED_CHECKSUM = "reconstructed-checksum"


def repair_line(raw, lineno):
    """Attempt to repair one raw line into a valid 69-character TLE line.

    ``raw`` is the bytes of a single line WITHOUT its ``\\n`` terminator
    (a trailing ``\\r`` may remain). ``lineno`` is 1 or 2.

    Returns ``(clean_line, fixes, error, category)``:
      * success -> ``(str, list[str], None, None)``
      * failure -> ``(None, list[str], str, str)`` where ``category`` is a
        short tag for summary aggregation.
    """
    fixes = []

    try:
        line = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, fixes, "line contains a non-ASCII byte", "non-ascii"

    # Fix order is fixed (spec §6.6).
    if line.endswith("\r"):
        line = line[:-1]
        fixes.append("crlf")
    lstripped = line.lstrip(" \t")
    if lstripped != line:
        line = lstripped
        fixes.append("leading-trim")
    rstripped = line.rstrip(" \t")
    if rstripped != line:
        line = rstripped
        fixes.append("trailing-ws")
    if line.endswith("\\"):
        line = line[:-1]
        fixes.append("trailing-backslash")

    # Build a 69-character candidate.
    if len(line) == tle.LINE_LENGTH:
        candidate = line
    elif len(line) == 68:
        body_errors = tle.validate_body(line, lineno)
        if body_errors:
            return (
                None,
                fixes,
                "68-char line; columns 1-68 fail layout/semantic checks "
                "(interior character missing): " + "; ".join(body_errors),
                "interior-char-missing",
            )
        candidate = line + str(tle.compute_checksum(line))
        fixes.append(RECONSTRUCTED_CHECKSUM)
    else:
        return (
            None,
            fixes,
            f"line length {len(line)} after normalization, expected 68 or 69",
            "wrong-length",
        )

    # Single full re-validation of the final candidate (spec §4.1, §6.6).
    errors = tle.validate_line(candidate, lineno)
    if errors:
        category = "checksum-mismatch" if any(
            "checksum" in e for e in errors
        ) else "invalid-columns"
        return None, fixes, "; ".join(errors), category

    return candidate, fixes, None, None
