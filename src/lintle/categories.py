"""Canonical taxonomy for successful repairs — the :class:`FixClass` enum.

``FixClass`` is the single source of truth for the short tags that appear in
``Accepted.fixes``, ``stats.fix_counts``, and the ``report.md`` "Fixes
applied" table. Members are :class:`enum.StrEnum` so they compare equal to
and serialize as their hyphenated string values.

Rejection taxonomy lives in :mod:`lintle.diagnostics` as the stable
:class:`~lintle.diagnostics.RuleID` enum (``TLE-COL-001``, ``TLE-CHK-001``,
…) — replacing the former ``RejectCategory`` enum removed in v0.3.0.
"""

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
