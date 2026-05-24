"""Speculative, validated repair of raw TLE lines and records.

Every fix is applied and then confirmed by ``tle`` validation; a fix is
committed only if the result passes. Pure functions — no I/O.
"""

import dataclasses

from lintle import tle
from lintle.categories import FixClass
from lintle.diagnostics import Diagnostic, RepairTier, RuleID, diagnostic


def repair_line(raw, lineno, source_line_no=None):
    """Attempt to repair one raw line into a valid 69-character TLE line.

    ``raw`` is the bytes of a single line WITHOUT its ``\\n`` terminator
    (a trailing ``\\r`` may remain). ``lineno`` is 1 or 2 (the TLE position
    within a record). ``source_line_no`` is the 1-indexed file line that
    populates a failure's :class:`Diagnostic`; ``None`` falls back to ``0``.

    Returns ``(clean_line, fixes, diagnostic_or_None)``:
      * success -> ``(str, list[FixClass], None)``
      * failure -> ``(None, list[FixClass], Diagnostic)``
    """
    fixes = []
    src = (source_line_no if source_line_no is not None else 0,)

    try:
        line = raw.decode("ascii")
    except UnicodeDecodeError:
        return (
            None,
            fixes,
            diagnostic(
                RuleID.NON_ASCII_BYTE,
                source_line_nos=src,
                note="line contains a non-ASCII byte",
            ),
        )

    # Fix order is fixed (spec §6.6).
    if line.endswith("\r"):
        line = line[:-1]
        fixes.append(FixClass.CRLF)
    lstripped = line.lstrip(" \t")
    if lstripped != line:
        line = lstripped
        fixes.append(FixClass.LEADING_TRIM)
    rstripped = line.rstrip(" \t")
    if rstripped != line:
        line = rstripped
        fixes.append(FixClass.TRAILING_WS)
    if line.endswith("\\"):
        line = line[:-1]
        fixes.append(FixClass.TRAILING_BACKSLASH)

    # Build a 69-character candidate.
    if len(line) == tle.LINE_LENGTH:
        candidate = line
    elif len(line) == 68:
        body_errors = tle.validate_body(line, lineno)
        if body_errors:
            return (
                None,
                fixes,
                diagnostic(
                    RuleID.INTERIOR_CHAR_MISSING,
                    source_line_nos=src,
                    tier_attempted=RepairTier.NORMALIZATION,
                    note="; ".join(body_errors),
                ),
            )
        candidate = line + str(tle.compute_checksum(line))
        fixes.append(FixClass.RECONSTRUCTED_CHECKSUM)
    else:
        return (
            None,
            fixes,
            diagnostic(
                RuleID.LINE_LENGTH,
                source_line_nos=src,
                tier_attempted=RepairTier.NORMALIZATION,
                observed=str(len(line)),
                expected="68 or 69",
            ),
        )

    # Single full re-validation of the final candidate (spec §4.1, §6.6).
    errors = tle.validate_line(candidate, lineno)
    if errors:
        tier = (
            RepairTier.CHECKSUM_RECONSTRUCT
            if FixClass.RECONSTRUCTED_CHECKSUM in fixes
            else RepairTier.NORMALIZATION
        )
        if any("checksum" in e for e in errors):
            observed = candidate[68] if len(candidate) > 68 else ""
            expected = str(tle.compute_checksum(candidate))
            return (
                None,
                fixes,
                diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=src,
                    tier_attempted=tier,
                    column_range=(69, 69),
                    observed=observed,
                    expected=expected,
                ),
            )
        return (
            None,
            fixes,
            diagnostic(
                RuleID.INVALID_COLUMN_LAYOUT,
                source_line_nos=src,
                tier_attempted=tier,
                note="; ".join(errors),
            ),
        )

    return candidate, fixes, None


@dataclasses.dataclass
class Accepted:
    """A record that is valid after repair. ``fixes`` lists the fix-class
    tags applied across both lines (e.g. ``FixClass.TRAILING_BACKSLASH``).
    """

    line1: str
    line2: str
    fixes: list[FixClass]


@dataclasses.dataclass
class Rejected:
    """A record routed to quarantine. ``raw_lines`` preserves the original
    bytes for byte-faithful sidecar output. ``primary`` is the headline
    :class:`Diagnostic` used for aggregation in ``stats.reject_counts`` and
    as the visible diagnosis in ``report.md``; ``related`` carries any
    supporting diagnostics — when both lines of a record fail, the first
    is primary and the second is related.
    """

    raw_lines: list
    source_lines: list
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()


def process_record(raw_line1, src1, raw_line2, src2):
    """Repair and validate a paired record.

    ``raw_line1``/``raw_line2`` are line bytes (no ``\\n``); ``src1``/``src2``
    are their 1-indexed source line numbers. Returns ``Accepted`` or
    ``Rejected``.
    """
    line1, fixes1, diag1 = repair_line(raw_line1, 1, src1)
    line2, fixes2, diag2 = repair_line(raw_line2, 2, src2)

    if diag1 or diag2:
        if diag1 and diag2:
            primary, related = diag1, (diag2,)
        else:
            primary = diag1 if diag1 else diag2
            related = ()
        return Rejected([raw_line1, raw_line2], [src1, src2], primary, related)

    record_errors = tle.validate_record(line1, line2)
    if record_errors:
        return Rejected(
            [raw_line1, raw_line2],
            [src1, src2],
            diagnostic(
                RuleID.CATALOG_MISMATCH,
                source_line_nos=(src1, src2),
                tier_attempted=RepairTier.NORMALIZATION,
                note="; ".join(record_errors),
            ),
        )

    return Accepted(line1, line2, fixes1 + fixes2)
