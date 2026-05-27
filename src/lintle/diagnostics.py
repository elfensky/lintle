"""Stable rule-ID registry and the structured ``Diagnostic`` dataclass.

Pure data, zero logic — no validation predicates live here. ``tle.py`` remains
the single source of truth for "what makes a TLE line perfect" (the *one
validator definition* rule from CLAUDE.md). This module just labels what
``tle.py`` already finds.

Three concepts:

* :class:`RuleID` — a stable :class:`enum.StrEnum`. The string value is the
  *public contract* (``"TLE-CHK-001"``); it appears in ``report.md``, the
  ``.broken.txt`` sidecar, and any future JSON output. **Never reuse, never
  recycle.** Retired IDs stay in the enum, annotated in source.
* :class:`RuleSpec` + :data:`RULES` — out-of-band metadata for each rule
  (family, short title, version introduced, deprecation chain). Kept off the
  enum members so policy questions don't force enum imports across the
  codebase.
* :class:`Diagnostic` — the structured rejection unit, built via
  :func:`diagnostic`. ``frozen`` + ``slots`` keep it hashable and cheap at
  millions-per-run scale; the helper bounds free-text fields so corrupt input
  cannot blow constant memory.
"""

import dataclasses
import enum


class RuleID(enum.StrEnum):
    """Stable diagnostic identifiers. The string value is forever; the member
    name is a Pythonic alias. Family prefixes are semantic: ``COL`` for column
    / layout, ``CHK`` for checksum, ``PAIR`` for line pairing, ``SEM`` for
    semantic ranges (reserved), ``INT`` for internal cleaner failure.
    """

    # TLE-COL-* — column / layout (physical line shape)
    LINE_LENGTH = "TLE-COL-001"
    INTERIOR_CHAR_MISSING = "TLE-COL-002"
    NON_ASCII_BYTE = "TLE-COL-003"
    INVALID_COLUMN_LAYOUT = "TLE-COL-004"

    # TLE-CHK-* — checksum (mod-10 digit at column 69)
    CHECKSUM_MISMATCH = "TLE-CHK-001"

    # TLE-PAIR-* — line-1 / line-2 pairing
    ORPHAN_LINE = "TLE-PAIR-001"
    BAD_PREFIX = "TLE-PAIR-002"
    CATALOG_MISMATCH = "TLE-PAIR-003"

    # TLE-SEM-* — semantic ranges (RESERVED; no rule emitted yet)
    # The first semantic-range rule, when added, takes TLE-SEM-001.

    # TLE-INT-* — internal (cleaner itself failed on this record)
    INTERNAL_ERROR = "TLE-INT-001"


class RepairTier(enum.StrEnum):
    """Which repair tier was attempted before a diagnostic fired.

    A tier-2 checksum-reconstruct that still failed is a stronger corruption
    signal than a record rejected at first read; consumers can downgrade trust
    accordingly.
    """

    NONE = "none"  # rejected without any repair attempt
    NORMALIZATION = "tier-1"  # CRLF / whitespace / trailing backslash
    CHECKSUM_RECONSTRUCT = "tier-2"  # missing-checksum reconstruction


@dataclasses.dataclass(frozen=True, slots=True)
class RuleSpec:
    """Metadata about one rule. Lives in :data:`RULES`, keyed by :class:`RuleID`.

    ``deprecated_for`` lists rules that supersede this one — empty for active
    rules; non-empty if the rule was split or merged into others. Retired IDs
    remain readable by downstream parsers indefinitely.
    """

    rule_id: "RuleID"
    family: str
    short_title: str
    introduced: str
    deprecated_for: tuple["RuleID", ...] = ()


RULES: dict[RuleID, RuleSpec] = {
    RuleID.LINE_LENGTH: RuleSpec(
        RuleID.LINE_LENGTH,
        "COL",
        "line length after normalization is not 69 columns",
        "0.3.0",
    ),
    RuleID.INTERIOR_CHAR_MISSING: RuleSpec(
        RuleID.INTERIOR_CHAR_MISSING,
        "COL",
        "68-char line where columns 1-68 fail layout — interior character missing",
        "0.3.0",
    ),
    RuleID.NON_ASCII_BYTE: RuleSpec(
        RuleID.NON_ASCII_BYTE,
        "COL",
        "line contains a non-ASCII byte",
        "0.3.0",
    ),
    RuleID.INVALID_COLUMN_LAYOUT: RuleSpec(
        RuleID.INVALID_COLUMN_LAYOUT,
        "COL",
        "column layout or format check failed",
        "0.3.0",
    ),
    RuleID.CHECKSUM_MISMATCH: RuleSpec(
        RuleID.CHECKSUM_MISMATCH,
        "CHK",
        "mod-10 checksum digit at column 69 does not match",
        "0.3.0",
    ),
    RuleID.ORPHAN_LINE: RuleSpec(
        RuleID.ORPHAN_LINE,
        "PAIR",
        "line with no matching partner — orphan line 1 or line 2",
        "0.3.0",
    ),
    RuleID.BAD_PREFIX: RuleSpec(
        RuleID.BAD_PREFIX,
        "PAIR",
        "line does not start with '1 ' or '2 '",
        "0.3.0",
    ),
    RuleID.CATALOG_MISMATCH: RuleSpec(
        RuleID.CATALOG_MISMATCH,
        "PAIR",
        "paired line 1 and line 2 disagree on NORAD catalog ID",
        "0.3.0",
    ),
    RuleID.INTERNAL_ERROR: RuleSpec(
        RuleID.INTERNAL_ERROR,
        "INT",
        "cleaner itself raised an exception on this record",
        "0.3.0",
    ),
}


# Fail fast at import time if a RuleID is added without a matching RuleSpec.
# Uses ``raise`` rather than ``assert`` so the guard survives ``python -O``
# (asserts are compiled out under optimization; this check must always run).
if set(RULES) != set(RuleID):
    raise RuntimeError(
        f"RULES mismatch: missing={set(RuleID) - set(RULES)} "
        f"extra={set(RULES) - set(RuleID)}"
    )


_OBSERVED_EXPECTED_MAX = 16
_NOTE_MAX = 80
# ASCII ellipsis (3 dots). The ``.broken.txt`` sidecar encodes diagnostics
# with ``str.encode("ascii", errors="replace")``, which would turn a
# Unicode U+2026 ellipsis into the literal ``?`` character — obscuring the
# truncation indicator. Using three ASCII dots keeps the marker readable
# on every platform without an encoding workaround.
_ELLIPSIS = "..."


@dataclasses.dataclass(frozen=True, slots=True)
class Diagnostic:
    """One structured rejection — the unit cited in reports and the sidecar.

    Construct via :func:`diagnostic`, which silently truncates oversized
    strings to the bounds below. Direct construction is supported (tests,
    deserialization) but is checked by ``__post_init__``: oversized
    ``observed``/``expected``/``note`` raises :class:`ValueError`, so the
    bound cannot be bypassed even when the helper is skipped.
    """

    rule_id: RuleID
    source_line_nos: tuple[int, ...]
    tier_attempted: RepairTier = RepairTier.NONE
    column_range: tuple[int, int] | None = None
    observed: str | None = None
    expected: str | None = None
    note: str = ""

    def __post_init__(self):
        if self.observed is not None and len(self.observed) > _OBSERVED_EXPECTED_MAX:
            raise ValueError(
                f"Diagnostic.observed exceeds {_OBSERVED_EXPECTED_MAX} chars "
                f"({len(self.observed)}); use diagnostic() to truncate"
            )
        if self.expected is not None and len(self.expected) > _OBSERVED_EXPECTED_MAX:
            raise ValueError(
                f"Diagnostic.expected exceeds {_OBSERVED_EXPECTED_MAX} chars "
                f"({len(self.expected)}); use diagnostic() to truncate"
            )
        if len(self.note) > _NOTE_MAX:
            raise ValueError(
                f"Diagnostic.note exceeds {_NOTE_MAX} chars "
                f"({len(self.note)}); use diagnostic() to truncate"
            )


def _bound(value: str | None, limit: int) -> str | None:
    """Truncate ``value`` to ``limit`` chars, appending ``...`` if cut.

    ``None`` passes through. The ellipsis is included *within* the limit so
    the returned string is never longer than ``limit``.
    """
    if value is None or len(value) <= limit:
        return value
    return value[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _sanitize_note(value: str) -> str:
    """Replace non-printable characters in a note with ``?``.

    Defense-in-depth: ``note`` content comes from validator error strings
    that flow into the ``.broken.txt`` sidecar verbatim. ASCII control
    characters and escape sequences in those strings could mis-render
    when an analyst views the sidecar with ``cat`` or ``less``. We strip
    them at construction time rather than at render time so the in-memory
    Diagnostic is always safe to print.
    """
    return "".join(c if c.isprintable() else "?" for c in value)


def diagnostic(
    rule_id: RuleID,
    *,
    source_line_nos: tuple[int, ...],
    tier_attempted: RepairTier = RepairTier.NONE,
    column_range: tuple[int, int] | None = None,
    observed: str | None = None,
    expected: str | None = None,
    note: str = "",
) -> Diagnostic:
    """Construct a :class:`Diagnostic` with size-bounded, sanitized strings.

    ``observed`` and ``expected`` cap at 16 chars; ``note`` is sanitized
    (non-printable chars replaced with ``?``) and capped at 80. A
    truncated value gets a trailing ``...``. Truncation and sanitization
    are silent — the goal is to keep memory, on-disk size, and terminal
    output safe regardless of input corruption, not to warn on contact.
    """
    return Diagnostic(
        rule_id=rule_id,
        source_line_nos=source_line_nos,
        tier_attempted=tier_attempted,
        column_range=column_range,
        observed=_bound(observed, _OBSERVED_EXPECTED_MAX),
        expected=_bound(expected, _OBSERVED_EXPECTED_MAX),
        note=_bound(_sanitize_note(note), _NOTE_MAX) or "",
    )
