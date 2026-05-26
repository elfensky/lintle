"""Pure-data fixtures for ``lintle explain`` — examples, citations, notes.

One ``RuleExplain`` per :class:`~lintle.diagnostics.RuleID` and one
``FixExplain`` per :class:`~lintle.categories.FixClass`. The examples here are
the *same objects* the explain renderer prints AND the test suite validates
against the live ``tle.py`` / ``repair.py`` — so an example cannot drift from
real validator behaviour without a test going red (see ``tests/test_explain``).

This module is data only: no rendering, no I/O. The renderer lives in
``explain.py``. The import-time guards below make coverage and namespace
disjointness structural — the explain feature cannot ship with a missing entry
or an ambiguous tag.
"""

import dataclasses
import enum

from lintle.categories import FixClass
from lintle.diagnostics import RepairTier, RuleID

# A canonical, known-good record (NORAD 00005). Reused as the "good" example
# and as the base every "bad" example is derived from by a single defect.
_L1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"  # noqa: E501 — a TLE line is a fixed 69-column record
_L2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"  # noqa: E501 — a TLE line is a fixed 69-column record
# A second valid line 2 carrying a DIFFERENT catalog number (09999) with its
# checksum recomputed for that change — pairs with _L1 to trigger a catalog
# mismatch between two otherwise-perfect lines.
_L2_OTHER_CATALOG = (
    "2 09999  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413668"  # noqa: E501 — a TLE line is a fixed 69-column record
)


class VerifyKind(enum.Enum):
    """How a rule's example is reproduced — which layer classifies it. The
    test suite dispatches on this; the renderer uses it to label the example.
    """

    LINE = enum.auto()  # a single line, classified by repair.repair_line
    PAIRING = enum.auto()  # unpaired/garbage input lines, classified by iter_records
    RECORD = enum.auto()  # a line-1/line-2 pair, classified by process_record
    NONE = enum.auto()  # no reproducible example (internal-error safety net)


@dataclasses.dataclass(frozen=True, slots=True)
class RuleExplain:
    """Explain metadata for one rejection rule. ``good_lines``/``bad_lines``
    are input lines; ``verify`` says how they reproduce. ``tier_note`` is the
    one field not machine-verified — it describes which repair tier is tried
    before the rule fires.
    """

    rule_id: RuleID
    verify: VerifyKind
    good_lines: tuple[str, ...]
    bad_lines: tuple[str, ...]
    lineno: int | None
    column_range: tuple[int, int] | None
    citation: str
    tier_note: str

    def __post_init__(self):
        if self.verify is VerifyKind.LINE:
            if self.lineno not in (1, 2) or len(self.bad_lines) != 1:
                raise ValueError(f"{self.rule_id}: LINE needs lineno + one bad line")
        elif self.verify is VerifyKind.RECORD:
            if len(self.good_lines) != 2 or len(self.bad_lines) != 2:
                raise ValueError(f"{self.rule_id}: RECORD needs two-line good+bad")
        elif self.verify is VerifyKind.PAIRING:
            if not self.bad_lines:
                raise ValueError(f"{self.rule_id}: PAIRING needs at least one bad line")
        elif self.verify is VerifyKind.NONE and (self.good_lines or self.bad_lines):
            raise ValueError(f"{self.rule_id}: NONE must carry no examples")


@dataclasses.dataclass(frozen=True, slots=True)
class FixExplain:
    """Explain metadata for one repair tag. ``before`` is an input line with a
    single defect; ``after`` is the committed line the repairer produces.
    """

    fix_class: FixClass
    before: str
    after: str
    lineno: int
    tier: RepairTier
    safety_note: str
    citation: str


RULE_EXPLAIN: dict[RuleID, RuleExplain] = {
    RuleID.LINE_LENGTH: RuleExplain(
        RuleID.LINE_LENGTH,
        VerifyKind.LINE,
        good_lines=(_L1,),
        bad_lines=(_L1[:60],),
        lineno=1,
        column_range=None,
        citation="tle.validate_line",
        tier_note="length is checked after tier-1 normalization (CRLF/whitespace).",
    ),
    RuleID.INTERIOR_CHAR_MISSING: RuleExplain(
        RuleID.INTERIOR_CHAR_MISSING,
        VerifyKind.LINE,
        good_lines=(_L1,),
        bad_lines=(_L1[:24] + "A" + _L1[25:68],),
        lineno=1,
        column_range=(25, 25),
        citation="tle.validate_body",
        tier_note="a 68-char line whose body fails layout cannot be completed; "
        "tier-1 normalization was attempted first.",
    ),
    RuleID.NON_ASCII_BYTE: RuleExplain(
        RuleID.NON_ASCII_BYTE,
        VerifyKind.LINE,
        good_lines=(_L1,),
        bad_lines=(_L1[:68] + "ñ",),
        lineno=1,
        column_range=None,
        citation="repair.repair_line",
        tier_note="rejected before any repair tier — the bytes are not ASCII.",
    ),
    RuleID.INVALID_COLUMN_LAYOUT: RuleExplain(
        RuleID.INVALID_COLUMN_LAYOUT,
        VerifyKind.LINE,
        good_lines=(_L1,),
        bad_lines=(_L1[:24] + "A" + _L1[25:],),
        lineno=1,
        column_range=(25, 25),
        citation="tle.validate_body",
        tier_note="tier-1 (or tier-2, if a checksum was reconstructed) was "
        "attempted before this fired.",
    ),
    RuleID.CHECKSUM_MISMATCH: RuleExplain(
        RuleID.CHECKSUM_MISMATCH,
        VerifyKind.LINE,
        good_lines=(_L1,),
        bad_lines=(_L1[:68] + "0",),
        lineno=1,
        column_range=(69, 69),
        citation="tle.checksum_error",
        tier_note="tier-1, or tier-2 if the column-69 digit was reconstructed, "
        "was attempted before this fired.",
    ),
    RuleID.ORPHAN_LINE: RuleExplain(
        RuleID.ORPHAN_LINE,
        VerifyKind.PAIRING,
        good_lines=(_L1, _L2),
        bad_lines=(_L1,),
        lineno=None,
        column_range=None,
        citation="pipeline.iter_records",
        tier_note="pairing precedes repair; no repair tier applies.",
    ),
    RuleID.BAD_PREFIX: RuleExplain(
        RuleID.BAD_PREFIX,
        VerifyKind.PAIRING,
        good_lines=(_L1, _L2),
        bad_lines=("this line does not start with '1 ' or '2 '",),
        lineno=None,
        column_range=(1, 2),
        citation="pipeline.iter_records",
        tier_note="pairing precedes repair; no repair tier applies.",
    ),
    RuleID.CATALOG_MISMATCH: RuleExplain(
        RuleID.CATALOG_MISMATCH,
        VerifyKind.RECORD,
        good_lines=(_L1, _L2),
        bad_lines=(_L1, _L2_OTHER_CATALOG),
        lineno=None,
        column_range=(3, 7),
        citation="tle.validate_record",
        tier_note="tier-1 (or tier-2, if either line's checksum was reconstructed) "
        "was attempted before this fired.",
    ),
    RuleID.INTERNAL_ERROR: RuleExplain(
        RuleID.INTERNAL_ERROR,
        VerifyKind.NONE,
        good_lines=(),
        bad_lines=(),
        lineno=None,
        column_range=None,
        citation="pipeline._run",
        tier_note="not applicable — this is the cleaner's own crash-safety net: "
        "if processing a record raises, the record is quarantined verbatim.",
    ),
}


FIX_EXPLAIN: dict[FixClass, FixExplain] = {
    FixClass.CRLF: FixExplain(
        FixClass.CRLF,
        before=_L1 + "\r",
        after=_L1,
        lineno=1,
        tier=RepairTier.NORMALIZATION,
        safety_note="A carriage return from a CRLF (Windows) line ending; "
        "stripping it cannot alter the 69 data columns.",
        citation="repair.repair_line",
    ),
    FixClass.LEADING_TRIM: FixExplain(
        FixClass.LEADING_TRIM,
        before="  " + _L1,
        after=_L1,
        lineno=1,
        tier=RepairTier.NORMALIZATION,
        safety_note="Leading spaces/tabs before column 1; removing them realigns "
        "the fixed-column record without touching any data character.",
        citation="repair.repair_line",
    ),
    FixClass.TRAILING_WS: FixExplain(
        FixClass.TRAILING_WS,
        before=_L1 + "   ",
        after=_L1,
        lineno=1,
        tier=RepairTier.NORMALIZATION,
        safety_note="Trailing spaces/tabs after column 69; they carry no data.",
        citation="repair.repair_line",
    ),
    FixClass.TRAILING_BACKSLASH: FixExplain(
        FixClass.TRAILING_BACKSLASH,
        before=_L1 + "\\",
        after=_L1,
        lineno=1,
        tier=RepairTier.NORMALIZATION,
        safety_note="A stray trailing backslash (a common export artifact); it is "
        "not part of the 69-column record.",
        citation="repair.repair_line",
    ),
    FixClass.RECONSTRUCTED_CHECKSUM: FixExplain(
        FixClass.RECONSTRUCTED_CHECKSUM,
        before=_L1[:68],
        after=_L1,
        lineno=1,
        tier=RepairTier.CHECKSUM_RECONSTRUCT,
        safety_note="The only sanctioned reconstruction. The column-69 checksum is "
        "a deterministic mod-10 function of columns 1-68 (tle.compute_checksum), so "
        "a missing digit is recomputed — never guessed — and the completed line is "
        "re-validated in full before it is committed. No data character is ever "
        "reconstructed. This is the weakest, most scrutinised repair tier (tier-2).",
        citation="repair.repair_line",
    ),
}


# Coverage guards — co-located with the data so a missing entry fails at the
# earliest possible import, mirroring the RULES/FIXES guards. ``raise`` (not
# ``assert``) survives ``python -O``.
if set(RULE_EXPLAIN) != set(RuleID):
    raise RuntimeError(
        f"RULE_EXPLAIN mismatch: missing={set(RuleID) - set(RULE_EXPLAIN)} "
        f"extra={set(RULE_EXPLAIN) - set(RuleID)}"
    )
if set(FIX_EXPLAIN) != set(FixClass):
    raise RuntimeError(
        f"FIX_EXPLAIN mismatch: missing={set(FixClass) - set(FIX_EXPLAIN)} "
        f"extra={set(FIX_EXPLAIN) - set(FixClass)}"
    )
# Namespace-disjointness guard: ``explain <TAG>`` accepts either vocabulary, so
# a RuleID value that equalled a FixClass value would make dispatch ambiguous.
_collisions = {r.value for r in RuleID} & {f.value for f in FixClass}
if _collisions:
    raise RuntimeError(f"RuleID/FixClass tag-value collision: {_collisions}")
