"""Canonical taxonomies for cleaner outcomes — fix classes and reject categories.

Each enum is the single source of truth for the short tags that appear in
``stats.fix_counts`` / ``stats.reject_categories``, the ``.broken.txt``
sidecar header, and ``report.md``. Members are :class:`enum.StrEnum` so
they compare equal to and serialize as their hyphenated string values —
existing on-disk output stays byte-identical.
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


class RejectCategory(enum.StrEnum):
    """Tags for records or lines routed to quarantine instead of cleaned output.

    Each member names one reason a record could not be safely repaired.
    They are stored on ``Rejected.category`` / ``Orphan.category`` and
    tallied in ``FileStats.reject_categories``.
    """

    NON_ASCII = "non-ascii"
    INTERIOR_CHAR_MISSING = "interior-char-missing"
    WRONG_LENGTH = "wrong-length"
    CHECKSUM_MISMATCH = "checksum-mismatch"
    INVALID_COLUMNS = "invalid-columns"
    CATALOG_MISMATCH = "catalog-mismatch"
    ORPHAN_LINE = "orphan-line"
    BAD_PREFIX = "bad-prefix"
    INTERNAL_ERROR = "internal-error"
