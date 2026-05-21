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


# --- Column-layout rules -------------------------------------------------
# Slices below are 0-indexed half-open ranges into the 68-character body.

_DIGIT = "0123456789"
_DIGIT_SPACE = "0123456789 "
_SIGN = " +-"
_EXP_SIGN = "+-"
_ALNUM_SPACE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

# Single-character positions: (index, allowed_chars, description).
_LINE1_CHARS = [
    (0, "1", "line number"),
    (1, " ", "column 2 separator"),
    (7, "UCS", "classification"),
    (8, " ", "column 9 separator"),
    (17, " ", "column 18 separator"),
    (23, ".", "epoch decimal point"),
    (32, " ", "column 33 separator"),
    (33, _SIGN, "first-derivative sign"),
    (34, ".", "first-derivative decimal point"),
    (43, " ", "column 44 separator"),
    (44, _SIGN, "second-derivative mantissa sign"),
    (50, _EXP_SIGN, "second-derivative exponent sign"),
    (52, " ", "column 53 separator"),
    (53, _SIGN, "B* mantissa sign"),
    (59, _EXP_SIGN, "B* exponent sign"),
    (61, " ", "column 62 separator"),
    (63, " ", "column 64 separator"),
]
# Multi-character fields: (start, end, allowed_chars, description).
_LINE1_FIELDS = [
    (2, 7, _ALNUM_SPACE, "satellite catalog number"),
    (9, 17, _ALNUM_SPACE, "international designator"),
    (18, 20, _DIGIT, "epoch year"),
    (20, 23, _DIGIT_SPACE, "epoch day-of-year"),
    (24, 32, _DIGIT, "epoch fraction"),
    (35, 43, _DIGIT, "first-derivative digits"),
    (45, 50, _DIGIT, "second-derivative mantissa"),
    (51, 52, _DIGIT, "second-derivative exponent"),
    (54, 59, _DIGIT, "B* mantissa"),
    (60, 61, _DIGIT, "B* exponent"),
    (62, 63, _DIGIT, "ephemeris type"),
    (64, 68, _DIGIT_SPACE, "element set number"),
]
_LINE2_CHARS = [
    (0, "2", "line number"),
    (1, " ", "column 2 separator"),
    (7, " ", "column 8 separator"),
    (11, ".", "inclination decimal point"),
    (16, " ", "column 17 separator"),
    (20, ".", "RAAN decimal point"),
    (25, " ", "column 26 separator"),
    (33, " ", "column 34 separator"),
    (37, ".", "argument-of-perigee decimal point"),
    (42, " ", "column 43 separator"),
    (46, ".", "mean-anomaly decimal point"),
    (51, " ", "column 52 separator"),
    (54, ".", "mean-motion decimal point"),
]
_LINE2_FIELDS = [
    (2, 7, _ALNUM_SPACE, "satellite catalog number"),
    (8, 11, _DIGIT_SPACE, "inclination integer part"),
    (12, 16, _DIGIT, "inclination fraction"),
    (17, 20, _DIGIT_SPACE, "RAAN integer part"),
    (21, 25, _DIGIT, "RAAN fraction"),
    (26, 33, _DIGIT, "eccentricity"),
    (34, 37, _DIGIT_SPACE, "argument-of-perigee integer part"),
    (38, 42, _DIGIT, "argument-of-perigee fraction"),
    (43, 46, _DIGIT_SPACE, "mean-anomaly integer part"),
    (47, 51, _DIGIT, "mean-anomaly fraction"),
    (52, 54, _DIGIT_SPACE, "mean-motion integer part"),
    (55, 63, _DIGIT, "mean-motion fraction"),
    (63, 68, _DIGIT_SPACE, "revolution number"),
]
_LINE_SPEC = {1: (_LINE1_CHARS, _LINE1_FIELDS), 2: (_LINE2_CHARS, _LINE2_FIELDS)}


def _check_columns(body, lineno):
    """Validate the fixed-position column layout of a 68-character ``body``.

    ``lineno`` is 1 or 2. Returns a list of human-readable error strings;
    an empty list means the column layout is valid.
    """
    if len(body) != 68:
        return [f"body length {len(body)}, expected 68 columns"]
    chars, fields = _LINE_SPEC[lineno]
    errors = []
    for idx, allowed, desc in chars:
        if body[idx] not in allowed:
            errors.append(
                f"column {idx + 1} ({desc}): got {body[idx]!r}, "
                f"expected one of {allowed!r}"
            )
    for start, end, allowed, desc in fields:
        if any(c not in allowed for c in body[start:end]):
            errors.append(
                f"columns {start + 1}-{end} ({desc}): "
                f"contains a character outside {allowed!r}"
            )
    return errors


def _check_semantics(body, lineno):
    """Validate that numeric fields fall in their physically valid ranges.

    Assumes ``body`` already passed ``_check_columns`` for ``lineno``.
    Returns a list of error strings; empty means valid.
    """
    errors = []
    try:
        if lineno == 1:
            day = float(body[20:23] + "." + body[24:32])
            if not 0.0 < day < 367.0:
                errors.append(f"epoch day-of-year {day} outside (0, 367)")
        else:
            inc = float(body[8:16])
            if not 0.0 <= inc <= 180.0:
                errors.append(f"inclination {inc} outside [0, 180]")
            raan = float(body[17:25])
            if not 0.0 <= raan < 360.0:
                errors.append(f"RAAN {raan} outside [0, 360)")
            ecc = int(body[26:33]) / 1e7
            if not 0.0 <= ecc < 1.0:
                errors.append(f"eccentricity {ecc} outside [0, 1)")
            argp = float(body[34:42])
            if not 0.0 <= argp < 360.0:
                errors.append(f"argument of perigee {argp} outside [0, 360)")
            mean_anom = float(body[43:51])
            if not 0.0 <= mean_anom < 360.0:
                errors.append(f"mean anomaly {mean_anom} outside [0, 360)")
            mean_motion = float(body[52:63])
            if mean_motion <= 0.0:
                errors.append(f"mean motion {mean_motion} is not strictly positive")
    except ValueError:
        errors.append("a numeric field could not be parsed for semantic checks")
    return errors


def validate_body(body, lineno):
    """Validate columns 1-68 of a TLE line: column layout then semantics.

    ``lineno`` is 1 or 2. Returns a list of error strings (empty = valid).
    The checksum (column 69) is intentionally NOT checked here — see
    ``validate_line``. Semantics are only checked if the column layout is
    sound, so callers get the more fundamental error first.
    """
    errors = _check_columns(body, lineno)
    if errors:
        return errors
    return _check_semantics(body, lineno)
