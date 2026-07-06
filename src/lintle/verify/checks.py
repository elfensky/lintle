"""The exhaustive, sgp4-free verify checks (goals 1 and 3).

- **Re-validate** (goal 3): every cleaned pair must still pass ``tle.validate_record``.
- **Contradiction** (goal 3): no two records share ``(catalog, epoch)`` yet carry
  different element bytes.
- **Source byte-diff** (goal 1): every cleaned line must be a *sanctioned* edit of
  a real source line — the sanctioned set is exactly the five edge tidy-ups
  ``repair.py`` may apply. Any change to an interior column is corruption. This
  re-implements the sanctioned transform independently (it never calls
  ``repair.py``) so a repair bug can't hide behind its own output."""

from collections.abc import Iterator

from lintle import tle
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, VrfyRule

# Source lines buffered to resync across a run of quarantined (dropped) source
# records. A gap larger than this reports ORIGIN_MISSING (soft) rather than a
# false interior-mutation. ponytail: fixed window; raise it if real corpora show
# quarantine runs longer than this between two accepted records.
_RESYNC_WINDOW = 4096


def revalidate(rec: CleanedRecord) -> Suspect | None:
    """A cleaned record that no longer validates is a hard suspect — cleaning is
    supposed to emit only perfect records, so a failure here means corruption or
    a cleaning bug. Reuses the ONE validator; never a second definition."""
    errors = tle.validate_record(rec.line1, rec.line2)
    if errors:
        return Suspect(
            VrfyRule.REVALIDATE_FAIL,
            rec.catalog,
            rec.epoch_key,
            rec.src_file,
            rec.index,
            f"cleaned record fails validation: {errors[0]}",
        )
    return None


def find_conflicts(sorted_records: Iterator[CleanedRecord]) -> Iterator[Suspect]:
    """Over a stream sorted by ``(catalog, epoch_key)``, flag any group that
    holds more than one distinct element-byte pair for the same satellite and
    epoch — a flat contradiction (at least one is wrong). Exact duplicates are
    harmless and not flagged. Constant memory: only the current group's distinct
    pairs are held."""
    group_key: tuple[int, float] | None = None
    seen: set[tuple[str, str]] = set()
    for rec in sorted_records:
        key = (rec.catalog, rec.epoch_key)
        if key != group_key:
            group_key = key
            seen = set()
        pair = (rec.line1, rec.line2)
        if pair not in seen:
            if seen:  # a different pair already exists for this (catalog, epoch)
                yield Suspect(
                    VrfyRule.EPOCH_CONFLICT,
                    rec.catalog,
                    rec.epoch_key,
                    rec.src_file,
                    rec.index,
                    f"catalog {rec.catalog} shares epoch {rec.epoch_key} with "
                    "different element bytes",
                )
            seen.add(pair)


def sanctioned_reduce(src_line: str) -> str:
    """Undo the sanctioned edge repairs, each at most once, in ``repair.py``'s
    order (CRLF, leading trim, trailing trim, one trailing backslash), leaving
    the interior untouched. The result is the 68/69-char 'core' a clean origin
    reduces to."""
    s = src_line
    if s.endswith("\r"):
        s = s[:-1]
    s = s.lstrip(" \t")
    s = s.rstrip(" \t")
    if s.endswith("\\"):
        s = s[:-1]
    return s


def sanctioned_match(src_line: str, clean_line: str) -> bool:
    """True iff ``clean_line`` is a sanctioned repair of ``src_line``: the
    reduced core equals it, or (missing-checksum case) the 68-char core plus the
    recomputed column-69 digit equals it."""
    core = sanctioned_reduce(src_line)
    if core == clean_line:
        return True
    return len(core) == 68 and clean_line == core + str(tle.compute_checksum(core))


def _anchor(line1: str) -> tuple[int, str] | None:
    """A cheap resync identity for a line 1: ``(catalog, epoch-columns)``. Used
    to tell 'this record was corrupted in an interior column' (anchor found,
    bytes differ) from 'no origin here at all'."""
    catalog = tle.extract_norad_id(line1)
    if catalog is None:
        return None
    return (catalog, line1[18:32])


class SourceAligner:
    """Streams one source file alongside a cleaned file's records, matching each
    cleaned record to its source origin through a bounded forward window. Common
    case (next pair matches immediately) is O(1) per record; the window is only
    scanned when quarantined records intervene."""

    def __init__(self, source_path: str) -> None:
        self._fh = open(source_path, encoding="ascii", errors="replace")  # noqa: SIM115
        self._buf: list[str] = []

    def _refill(self) -> None:
        while len(self._buf) < _RESYNC_WINDOW:
            line = self._fh.readline()
            if not line:
                break
            self._buf.append(line.rstrip("\n"))

    def check(self, rec: CleanedRecord) -> Suspect | None:
        """Locate ``rec``'s source origin and classify it: clean match (None),
        interior mutation (hard), or no origin in window (soft)."""
        self._refill()
        rec_anchor: tuple[int, str] | None = None
        anchor_at: int | None = None
        for i in range(len(self._buf) - 1):
            if sanctioned_match(self._buf[i], rec.line1) and sanctioned_match(
                self._buf[i + 1], rec.line2
            ):
                del self._buf[: i + 2]
                return None
            if anchor_at is None:
                if rec_anchor is None:
                    rec_anchor = _anchor(rec.line1)
                if rec_anchor is not None and _anchor(self._buf[i]) == rec_anchor:
                    anchor_at = i
        if anchor_at is not None:
            del self._buf[: anchor_at + 2]
            return Suspect(
                VrfyRule.INTERIOR_MUT,
                rec.catalog,
                rec.epoch_key,
                rec.src_file,
                rec.index,
                "cleaned record differs from its source in a non-edge column",
            )
        if self._buf:
            del self._buf[:1]  # keep scanning forward
        return Suspect(
            VrfyRule.ORIGIN_MISSING,
            rec.catalog,
            rec.epoch_key,
            rec.src_file,
            rec.index,
            f"no source origin within the {_RESYNC_WINDOW}-line resync window",
        )

    def close(self) -> None:
        self._fh.close()
