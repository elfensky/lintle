"""Quarantine threshold parsing and exit-code policy."""


def parse_quarantine_threshold(raw):
    """Parse a ``--max-quarantined`` value into ``(mode, threshold)``.

    A bare integer (e.g. ``"100"``) is an absolute record count and returns
    ``("count", int)``. A value with a trailing ``%`` (e.g. ``"1%"`` or
    ``"1.5%"``) is a percentage of routed records and returns
    ``("pct", float)``; the percentage must lie in ``0..100``. Surrounding
    whitespace is tolerated. Raises :class:`ValueError` on malformed input
    or out-of-range values; the message for a negative count preserves the
    exact substring asserted by the legacy issue-#13 integration test.
    """
    raw = raw.strip()
    if raw.endswith("%"):
        body = raw[:-1].strip()
        try:
            pct = float(body)
        except ValueError:
            raise ValueError(f"--max-quarantined: invalid percentage {raw!r}") from None
        if not (0.0 <= pct <= 100.0):
            raise ValueError(
                f"--max-quarantined percentage must be in 0..100 (got {raw!r})"
            )
        return ("pct", pct)
    try:
        count = int(raw)
    except ValueError:
        raise ValueError(f"--max-quarantined: invalid value {raw!r}") from None
    if count < 0:
        raise ValueError(f"--max-quarantined must be >= 0 (got {count})")
    return ("count", count)


def quarantine_exit_code(all_stats, threshold_mode, quarantine_threshold):
    """Return the quality-gate exit code for completed file stats.

    ``0`` means the run stayed at or below ``--max-quarantined``; ``1`` means
    the threshold was exceeded. Operational failures remain owned by the CLI
    caller and are intentionally outside this pure policy helper.
    """
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    if threshold_mode == "count":
        return 1 if total_quarantined > quarantine_threshold else 0
    # Rate mode: cross-multiplied (`100*q > p*r`) to avoid divide-by-zero on
    # an empty corpus and float drift at the boundary. See design §3.
    total_routed = sum(s.clean_count + s.quarantined_count for s in all_stats)
    if 100 * total_quarantined > quarantine_threshold * total_routed:
        return 1
    return 0
