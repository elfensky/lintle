"""Speculative, validated repair of raw TLE lines and records.

Every fix is applied and then confirmed by ``tle`` validation; a fix is
committed only if the result passes. Pure functions — no I/O.
"""

import dataclasses

from lintle import tle
from lintle.categories import FixClass
from lintle.diagnostics import Diagnostic, RepairTier, RuleID, diagnostic


def repair_line(
    raw: bytes,
    lineno: int,
    source_line_no: int,
    *,
    reconstruct_checksum: bool = False,
) -> tuple[str, list[FixClass], None] | tuple[None, list[FixClass], Diagnostic]:
    """Attempt to repair one raw line into a valid 69-character TLE line.

    ``raw`` is the bytes of a single line WITHOUT its ``\\n`` terminator
    (a trailing ``\\r`` may remain). ``lineno`` is 1 or 2 (the TLE position
    within a record). ``source_line_no`` is the 1-indexed file line that
    populates a failure's :class:`Diagnostic` — required so provenance is
    never silently invented (no sentinel-line-0 in published output).

    ``reconstruct_checksum`` gates the tier-2 missing-checksum repair (issue
    #82). It defaults to ``False`` — a 68-char line whose body is otherwise
    valid is quarantined as a length error rather than having a checksum
    appended, because a dropped trailing *data* character is indistinguishable
    from a dropped checksum and reconstructing the latter would silently emit
    wrong-but-valid data (Critical Rule #2). Pass ``True`` (the CLI's
    ``--reconstruct-checksum``) to opt in to the deterministic recompute.

    Returns ``(clean_line, fixes, diagnostic_or_None)``:
      * success -> ``(str, list[FixClass], None)``
      * failure -> ``(None, list[FixClass], Diagnostic)``
    """
    fixes = []
    src = (source_line_no,)

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
        if not reconstruct_checksum:
            # Body is valid but the column-69 checksum is absent. By default we
            # quarantine rather than recompute it: a dropped trailing data
            # character looks identical to a dropped checksum, so appending one
            # could emit wrong-but-valid data (Critical Rule #2, issue #82).
            return (
                None,
                fixes,
                diagnostic(
                    RuleID.LINE_LENGTH,
                    source_line_nos=src,
                    tier_attempted=RepairTier.NORMALIZATION,
                    observed="68",
                    expected="69",
                    note="checksum absent; use --reconstruct-checksum to recompute",
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
        # Route on the validator's own error wording. Body (column/semantic)
        # errors fire before the checksum check in validate_line, so a record
        # with both a bad layout and a bad checksum stays INVALID_COLUMN_LAYOUT.
        # Do not reroute on checksum_error() alone — it reads only column 69 and
        # would misroute such a record to CHECKSUM_MISMATCH (a public RuleID).
        if any("checksum" in e for e in errors):
            diag = diagnostic(
                RuleID.CHECKSUM_MISMATCH,
                source_line_nos=src,
                tier_attempted=tier,
                column_range=(69, 69),
                # candidate is invariantly 69 chars here: the branch above
                # assigns it from either a length-69 line or a length-68 line
                # plus its recomputed checksum digit; every other length
                # returns early. So column 69 (index 68) always exists.
                observed=candidate[68],
                expected=str(tle.compute_checksum(candidate)),
            )
        else:
            diag = diagnostic(
                RuleID.INVALID_COLUMN_LAYOUT,
                source_line_nos=src,
                tier_attempted=tier,
                note="; ".join(errors),
            )
        return None, fixes, diag

    return candidate, fixes, None


@dataclasses.dataclass(slots=True)
class Accepted:
    """A record that is valid after repair. ``fixes`` lists the fix-class
    tags applied across both lines (e.g. ``FixClass.TRAILING_BACKSLASH``).
    """

    line1: str
    line2: str
    fixes: list[FixClass]


@dataclasses.dataclass(slots=True)
class Quarantined:
    """A record routed to quarantine. ``raw_lines`` preserves the original
    bytes for byte-faithful sidecar output. ``primary`` is the headline
    :class:`Diagnostic` used for aggregation in ``stats.quarantine_counts`` and
    as the visible diagnosis in ``report.md``; ``related`` carries any
    supporting diagnostics — when both lines of a record fail, the first
    is primary and the second is related.
    """

    raw_lines: list[bytes]
    source_lines: list[int]
    primary: Diagnostic
    related: tuple[Diagnostic, ...] = ()


def repair_record(
    raw_line1: bytes,
    src1: int,
    raw_line2: bytes,
    src2: int,
    *,
    reconstruct_checksum: bool = False,
) -> Accepted | Quarantined:
    """Repair and validate a paired record.

    ``raw_line1``/``raw_line2`` are line bytes (no ``\\n``); ``src1``/``src2``
    are their 1-indexed source line numbers. ``reconstruct_checksum`` is
    forwarded to :func:`repair_line` (issue #82; default off). Returns
    ``Accepted`` or ``Quarantined``.
    """
    line1, fixes1, diag1 = repair_line(
        raw_line1, 1, src1, reconstruct_checksum=reconstruct_checksum
    )
    line2, fixes2, diag2 = repair_line(
        raw_line2, 2, src2, reconstruct_checksum=reconstruct_checksum
    )

    if diag1 or diag2:
        if diag1 and diag2:
            primary, related = diag1, (diag2,)
        else:
            primary = diag1 if diag1 else diag2
            related = ()
        return Quarantined([raw_line1, raw_line2], [src1, src2], primary, related)

    record_errors = tle.validate_record_catalog(line1, line2)
    if record_errors:
        # Tier reflects the strongest repair attempted on EITHER line. A
        # CATALOG_MISMATCH after both lines survived checksum reconstruction
        # is a stronger corruption signal than one caught at first read; the
        # consumer should see tier-2 in that case, not tier-1.
        tier = (
            RepairTier.CHECKSUM_RECONSTRUCT
            if FixClass.RECONSTRUCTED_CHECKSUM in fixes1 + fixes2
            else RepairTier.NORMALIZATION
        )
        return Quarantined(
            [raw_line1, raw_line2],
            [src1, src2],
            diagnostic(
                RuleID.CATALOG_MISMATCH,
                source_line_nos=(src1, src2),
                tier_attempted=tier,
                note="; ".join(record_errors),
            ),
        )

    return Accepted(line1, line2, fixes1 + fixes2)
