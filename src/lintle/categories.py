"""Canonical taxonomy for successful repairs — the :class:`FixClass` enum.

``FixClass`` is the single source of truth for the short tags that appear in
``Accepted.fixes``, ``stats.fix_counts``, and the ``report.md`` "Fixes
applied" table. Members are :class:`enum.StrEnum` so they compare equal to
and serialize as their hyphenated string values.

:class:`FixSpec` + :data:`FIXES` give each tag a canonical one-line
definition — the repair-side mirror of ``RuleSpec``/``RULES`` in
:mod:`lintle.diagnostics`. Kept as pure data here so ``lintle explain`` can
single-source a fix's definition instead of re-describing it.

Quarantine taxonomy lives in :mod:`lintle.diagnostics` as the stable
:class:`~lintle.diagnostics.RuleID` enum (``TLE-COL-001``, ``TLE-CHK-001``,
…) — replacing the former category enum removed in v0.3.0.
"""

import dataclasses
import enum


class FixClass(enum.StrEnum):
    """Tags for speculative repairs that passed re-validation and committed.

    Each member names one transformation that, when applied, produced a
    line that the validator accepted. They are appended to
    ``Accepted.fixes`` and tallied in ``FileStats.fix_counts``.
    """

    CRLF = "crlf"
    LEADING_TRIM = "leading-trim"
    TRAILING_WS = "trailing-ws"
    TRAILING_BACKSLASH = "trailing-backslash"
    RECONSTRUCTED_CHECKSUM = "reconstructed-checksum"


@dataclasses.dataclass(frozen=True, slots=True)
class FixSpec:
    """Metadata about one repair tag. Lives in :data:`FIXES`, keyed by
    :class:`FixClass`. The repair-side counterpart to ``RuleSpec``.
    """

    fix_class: FixClass
    short_title: str
    introduced: str


FIXES: dict[FixClass, FixSpec] = {
    FixClass.CRLF: FixSpec(
        FixClass.CRLF,
        "stripped a trailing carriage return (CRLF line ending)",
        "0.3.0",
    ),
    FixClass.LEADING_TRIM: FixSpec(
        FixClass.LEADING_TRIM,
        "stripped leading spaces or tabs before column 1",
        "0.3.0",
    ),
    FixClass.TRAILING_WS: FixSpec(
        FixClass.TRAILING_WS,
        "stripped trailing spaces or tabs after column 69",
        "0.3.0",
    ),
    FixClass.TRAILING_BACKSLASH: FixSpec(
        FixClass.TRAILING_BACKSLASH,
        "stripped a stray trailing backslash",
        "0.3.0",
    ),
    FixClass.RECONSTRUCTED_CHECKSUM: FixSpec(
        FixClass.RECONSTRUCTED_CHECKSUM,
        "recomputed a missing column-69 checksum digit",
        "0.3.0",
    ),
}


# Fail fast at import time if a FixClass is added without a matching FixSpec —
# the repair-side mirror of the RULES guard in diagnostics.py. ``raise`` (not
# ``assert``) so the guard survives ``python -O``.
if set(FIXES) != set(FixClass):
    raise RuntimeError(
        f"FIXES mismatch: missing={set(FixClass) - set(FIXES)} "
        f"extra={set(FIXES) - set(FixClass)}"
    )
