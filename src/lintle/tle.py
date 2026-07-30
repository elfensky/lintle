"""Core TLE validation: the single definition of a "perfect" record.

Pure functions only — no I/O. Column references use 1-indexed TLE column
numbers in prose; Python slices below are 0-indexed.
"""

from typing import Literal

LINE_LENGTH = 69

# The closed set of FieldError kinds — repair routes on these (#106), so the
# vocabulary is a typed contract, not free-form prose.
FieldErrorKind = Literal["length", "column", "semantic", "checksum", "catalog"]

# Only these ASCII characters count as TLE digits. str.isdigit() also accepts
# non-ASCII Unicode digits (e.g. '²', '٣') — which int() may then reject or
# silently misread — so every digit test goes through this explicit set.
_DIGIT = "0123456789"

# Precomputed per-character checksum contribution: ASCII digit → its value,
# '-' → 1, everything else → 0. Built once at import time so the hot-path
# loop body is a single dict lookup rather than a membership test + int().
# Non-ASCII codepoints are absent from the table and default to 0 via .get().
_CHECKSUM_CONTRIB: dict[str, int] = {str(d): d for d in range(10)}
_CHECKSUM_CONTRIB["-"] = 1


def is_ascii_digits(field: str) -> bool:
    """True if ``field`` is non-empty and every character is an ASCII digit —
    the TLE digit rule. The one digit test shared by every consumer, because
    ``str.isdigit()`` also accepts non-ASCII Unicode digits (e.g. ``'²'``,
    ``'٣'``) that ``int()`` may reject or silently misread."""
    return bool(field) and all(c in _DIGIT for c in field)


def compute_checksum(line: str) -> int:
    """Return the mod-10 TLE checksum of the first 68 characters of ``line``.

    Each ASCII digit adds its value, each ``-`` adds 1, every other character
    (letters, spaces, ``.``, ``+``, and any non-ASCII char) adds 0. The result
    is ``sum % 10``.
    """
    return sum(_CHECKSUM_CONTRIB.get(c, 0) for c in line[:68]) % 10


# --- Column-layout rules -------------------------------------------------
# Slices below are 0-indexed half-open ranges into the 68-character body.

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


class FieldError(str):
    """A validation error that is also its own human-readable message.

    Subclasses ``str`` so every consumer that treats a validator error as text
    — substring tests, ``"; ".join(...)``, f-string interpolation — keeps
    working byte-for-byte, while :mod:`repair` reads the structured fields to
    route on the error *kind* (not by grepping prose, #106) and to populate
    ``report.jsonl``'s ``column_range``/``observed``/``expected`` for column and
    semantic findings (#120). ``kind`` is a :data:`FieldErrorKind` —
    ``"length"``/``"column"``/``"semantic"``/``"checksum"``/``"catalog"``.
    ``column_range`` is a 1-indexed
    inclusive ``(low, high)`` span (or ``None``). Instances are only ever *read*
    — never sliced or concatenated — so the str-subclass caveat that operations
    return a plain ``str`` never bites.
    """

    __slots__ = ("kind", "column_range", "observed", "expected")

    def __new__(
        cls,
        message,
        *,
        kind: FieldErrorKind,
        column_range=None,
        observed=None,
        expected=None,
    ):
        self = super().__new__(cls, message)
        self.kind = kind
        self.column_range = column_range
        self.observed = observed
        self.expected = expected
        return self


def _check_columns(body: str, lineno: int) -> list[FieldError]:
    """Validate the fixed-position column layout of a 68-character ``body``.

    ``lineno`` is 1 or 2. Returns a list of :class:`FieldError` (each also its
    own prose string); an empty list means the column layout is valid.
    """
    if len(body) != 68:
        return [
            FieldError(
                f"body length {len(body)}, expected 68 columns",
                kind="length",
                observed=str(len(body)),
                expected="68",
            )
        ]
    chars, fields = _LINE_SPEC[lineno]
    errors = []
    for idx, allowed, desc in chars:
        if body[idx] not in allowed:
            errors.append(
                FieldError(
                    f"column {idx + 1} ({desc}): got {body[idx]!r}, "
                    f"expected one of {allowed!r}",
                    kind="column",
                    column_range=(idx + 1, idx + 1),
                    observed=body[idx],
                    expected=allowed,
                )
            )
    for start, end, allowed, desc in fields:
        if any(c not in allowed for c in body[start:end]):
            # A multi-char field violation is "some char in this span is outside
            # the allowed set" — there is no single expected *value*, only a
            # charset constraint, which the prose note carries in full. Leaving
            # `expected` null is more honest than a charset truncated to 16 chars
            # (e.g. the 37-char alnum-space set) in report.jsonl. `observed` still
            # carries the offending substring; `column_range` the span.
            errors.append(
                FieldError(
                    f"columns {start + 1}-{end} ({desc}): "
                    f"contains a character outside {allowed!r}",
                    kind="column",
                    column_range=(start + 1, end),
                    observed=body[start:end],
                )
            )
    return errors


def _semantic(
    message: str,
    column_range: tuple[int, int],
    observed: object,
    expected: str,
) -> FieldError:
    """Build a ``kind="semantic"`` :class:`FieldError` for an out-of-range field."""
    return FieldError(
        message,
        kind="semantic",
        column_range=column_range,
        observed=str(observed),
        expected=expected,
    )


def _check_semantics(body: str, lineno: int) -> list[FieldError]:
    """Validate that numeric fields fall in their physically valid ranges.

    Assumes ``body`` already passed ``_check_columns`` for ``lineno``.
    Returns a list of :class:`FieldError`; empty means valid.
    """
    errors = []
    try:
        if lineno == 1:
            # (0, 367) with no leap-year logic is deliberate: space-track
            # ships real rollover records (day 366.x in a non-leap year, day
            # 0.x). Normalizing them across year boundaries is lintle.epoch's
            # job — tightening this bound would redefine "perfect" (Critical
            # Rule #4) and newly quarantine those records.
            day = float(body[20:23] + "." + body[24:32])
            if not 0.0 < day < 367.0:
                errors.append(
                    _semantic(
                        f"epoch day-of-year {day} outside (0, 367)",
                        (21, 32),
                        day,
                        "(0, 367)",
                    )
                )
        else:
            inc = float(body[8:16])
            if not 0.0 <= inc <= 180.0:
                errors.append(
                    _semantic(
                        f"inclination {inc} outside [0, 180]", (9, 16), inc, "[0, 180]"
                    )
                )
            raan = float(body[17:25])
            if not 0.0 <= raan < 360.0:
                errors.append(
                    _semantic(
                        f"RAAN {raan} outside [0, 360)", (18, 25), raan, "[0, 360)"
                    )
                )
            ecc = int(body[26:33]) / 1e7
            if not 0.0 <= ecc < 1.0:
                errors.append(
                    _semantic(
                        f"eccentricity {ecc} outside [0, 1)", (27, 33), ecc, "[0, 1)"
                    )
                )
            argp = float(body[34:42])
            if not 0.0 <= argp < 360.0:
                errors.append(
                    _semantic(
                        f"argument of perigee {argp} outside [0, 360)",
                        (35, 42),
                        argp,
                        "[0, 360)",
                    )
                )
            mean_anom = float(body[43:51])
            if not 0.0 <= mean_anom < 360.0:
                errors.append(
                    _semantic(
                        f"mean anomaly {mean_anom} outside [0, 360)",
                        (44, 51),
                        mean_anom,
                        "[0, 360)",
                    )
                )
            mean_motion = float(body[52:63])
            if mean_motion <= 0.0:
                errors.append(
                    _semantic(
                        f"mean motion {mean_motion} is not strictly positive",
                        (53, 63),
                        mean_motion,
                        "> 0",
                    )
                )
    except ValueError:
        errors.append(
            FieldError(
                "a numeric field could not be parsed for semantic checks",
                kind="semantic",
            )
        )
    return errors


def validate_body(body: str, lineno: int) -> list[FieldError]:
    """Validate columns 1-68 of a TLE line: column layout then semantics.

    ``lineno`` is 1 or 2. Returns a list of :class:`FieldError` (empty = valid).
    The checksum (column 69) is intentionally NOT checked here — see
    ``validate_line``. Semantics are only checked if the column layout is
    sound, so callers get the more fundamental error first.
    """
    errors = _check_columns(body, lineno)
    if errors:
        return errors
    return _check_semantics(body, lineno)


def checksum_error(line: str) -> FieldError | None:
    """Return a ``kind="checksum"`` :class:`FieldError` if the column-69 checksum
    of a 69-char ``line`` is wrong or non-numeric, else ``None``. ``observed`` is
    the column-69 character and ``expected`` is the recomputed checksum digit (for
    both the non-digit and the numeric-mismatch case, matching the structured
    fields ``repair`` records).
    """
    actual = line[68]
    expected = compute_checksum(line)
    if actual not in _DIGIT:
        return FieldError(
            f"checksum column 69 is {actual!r}, not a digit",
            kind="checksum",
            column_range=(69, 69),
            observed=actual,
            expected=str(expected),
        )
    if int(actual) != expected:
        return FieldError(
            f"checksum mismatch: column 69 is {actual!r}, computed {expected}",
            kind="checksum",
            column_range=(69, 69),
            observed=actual,
            expected=str(expected),
        )
    return None


def validate_line(line: str, lineno: int) -> list[FieldError]:
    """Fully validate a single 69-character TLE line.

    ``lineno`` is 1 or 2. Returns a list of :class:`FieldError` (empty = valid):
    length, column layout, semantic ranges, and the column-69 checksum.
    """
    if len(line) != LINE_LENGTH:
        return [
            FieldError(
                f"line length {len(line)}, expected {LINE_LENGTH}",
                kind="length",
                observed=str(len(line)),
                expected=str(LINE_LENGTH),
            )
        ]
    errors = validate_body(line[:68], lineno)
    if errors:
        return errors
    err = checksum_error(line)
    return [err] if err else []


def extract_norad_id(line: str | bytes) -> int | None:
    """Return the 5-digit NORAD catalog ID from a TLE line 1, or ``None``.

    Reads columns 3-7 (the satellite catalog number) and parses them as a
    decimal integer. Used to recover a programmatic ID from quarantined
    records whose other fields may be corrupt. Returns ``None`` when the
    line does not start with the ``"1 "`` line-1 prefix, is too short to
    contain the field, contains a non-ASCII byte, or the field is not
    five decimal digits — Alpha-5 letter-prefixed IDs are deliberately
    excluded to keep the downstream contract a plain integer.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError:
            return None
    if len(line) < 7 or not line.startswith("1 "):
        return None
    field = line[2:7]
    if not is_ascii_digits(field):
        return None
    return int(field)


def validate_record(line1: str, line2: str) -> list[FieldError]:
    """Validate a paired TLE record: each line valid, and the satellite
    catalog numbers (columns 3-7) match. Returns a list of :class:`FieldError`
    (each also its own prose string, prefixed ``"line 1: "`` / ``"line 2: "``
    for the per-line errors). The catalog cross-check is delegated to
    :func:`validate_record_catalog` so that mismatch error is defined in one
    place. Test/oracle helper — ``repair`` uses ``validate_line`` plus the
    catalog fast path directly (#109).
    """
    errors = []
    for label, line, lineno in (("line 1", line1, 1), ("line 2", line2, 2)):
        for err in validate_line(line, lineno):
            errors.append(
                FieldError(
                    f"{label}: {err}",
                    kind=err.kind,
                    column_range=err.column_range,
                    observed=err.observed,
                    expected=err.expected,
                )
            )
    if not errors:
        errors.extend(validate_record_catalog(line1, line2))
    return errors


def validate_record_catalog(line1: str, line2: str) -> list[FieldError]:
    """Check only the catalog-number cross-match for two individually-valid lines.

    Assumes both ``line1`` and ``line2`` have already passed ``validate_line``
    for their respective line numbers. Returns the same error list that
    ``validate_record`` would return for two valid lines — i.e. either an empty
    list (catalog numbers match) or a single ``kind="catalog"`` mismatch
    :class:`FieldError` — without re-running per-line layout, semantics, or
    checksum validation. The record-level fast path for callers like
    ``repair_record`` where each line has already been individually validated.
    """
    if line1[2:7] != line2[2:7]:
        return [
            FieldError(
                f"catalog number mismatch: "
                f"line 1 {line1[2:7]!r} vs line 2 {line2[2:7]!r}",
                kind="catalog",
                column_range=(3, 7),
                observed=line1[2:7],
                expected=line2[2:7],
            )
        ]
    return []
