"""The exhaustive, sgp4-free verify checks (goals 1 and 3).

- **Re-validate** (goal 3): every cleaned pair must still pass ``tle.validate_record``.
- **Contradiction** (goal 3): no two records share ``(catalog, epoch)`` yet carry
  different element bytes.
- **Source byte-diff** (goal 1): every cleaned line must be a *sanctioned* edit of
  a real source line — the sanctioned set is exactly the five edge tidy-ups
  ``repair.py`` may apply. Any change to an interior column is corruption. This
  re-implements the sanctioned transform independently (it never calls
  ``repair.py``) so a repair bug can't hide behind its own output."""

from collections.abc import Iterable, Iterator

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


# The physical orbital fields, parsed to numeric *values* for the contradiction
# check. Comparing values (not raw bytes) makes the comparison independent of
# the many valid ASCII encodings space-track emits for one number — a leading
# space vs an explicit "+" on a signed field, "00000-0" vs "+00000+0" for zero.
# Two records contradict only if a parsed orbital value genuinely differs; admin
# fields (element-set number, revolution number, checksums, ephemeris type) are
# simply absent from the tuple, so their re-issue churn is ignored. A raw-byte
# mask instead flags every such re-issue — ~1.4% of the real corpus, ~3.2M bogus
# hard suspects (issue #154), the classic noisy-verifier failure. Column slices
# are 0-indexed and kept in lockstep with tle.py's semantic check.
_L1_ORBITAL = 64  # line-1 orbital-slice width — used only by the parse fallback
_L2_ORBITAL = 63  # line-2 orbital-slice width — used only by the parse fallback


def _decimal_exp(field: str) -> float:
    """Decode a TLE 'modified exponential' field (2nd-derivative mean motion, B*
    drag): 8 chars ``SNNNNN±E`` with an implied leading decimal on the 5-digit
    mantissa → ``±0.NNNNN × 10**±E``."""
    sign = -1 if field[0] == "-" else 1
    return sign * int(field[1:6]) * 10.0 ** (int(field[6:8]) - 5)


def orbital_state(line1: str, line2: str) -> tuple:
    """The physical orbit as parsed numeric values — encoding-independent, admin
    fields excluded. Falls back to the raw masked bytes if any field cannot be
    parsed, so an unparseable oddity can never silently collapse two genuinely
    different orbits into one. Shared with ``dedup`` so both agree, byte-for-byte,
    on when two records carry 'the same orbit'."""
    try:
        return (
            float(line1[33:43]),  # 1st-derivative mean motion (ndot)
            _decimal_exp(line1[44:52]),  # 2nd-derivative mean motion (nddot)
            _decimal_exp(line1[53:61]),  # B* drag
            float(line2[8:16]),  # inclination
            float(line2[17:25]),  # RAAN
            int(line2[26:33]),  # eccentricity (implied leading '.')
            float(line2[34:42]),  # argument of perigee
            float(line2[43:51]),  # mean anomaly
            float(line2[52:63]),  # mean motion
        )
    except ValueError, IndexError:
        return (line1[:_L1_ORBITAL], line2[:_L2_ORBITAL])


def element_set(line1: str) -> int | None:
    """The element-set number (line-1 cols 65-68) as an int, tolerant of space
    padding; ``None`` if unparseable. Each re-issue increments it, so it tells a
    benign re-issue (a new number) from an integrity clash (one number, two
    orbits), and gives ``dedup`` its 'keep the latest' key."""
    try:
        return int(line1[64:68])
    except ValueError:
        return None


def find_conflicts(
    sorted_records: Iterator[CleanedRecord],
    orbit: bool = False,
) -> tuple[list[Suspect], int, set[int]]:
    """Over a stream sorted by ``(catalog, epoch_key)``, classify records that
    share a ``(catalog, epoch)`` but carry a different orbital state, keyed on the
    element-set number:

    - **different element-set → a benign re-issue.** Space-track republishes
      successive *refined* solutions per epoch, each a new element set; the
      faithful archive keeps them all and ``dedup`` keeps the latest. These are
      *counted* into a census, never a per-record finding — at ~0.16% of a 232 M
      corpus that would bury the real soft findings (#147/#158).
    - **same element-set, different orbit → a hard ``VRFY-EPOCH-CONFLICT``** — a
      genuine integrity clash, since one element-set names exactly one orbit.

    When ``orbit`` is set, also collects the catalog of every ``(catalog, epoch)``
    group carrying **≥ 2 records** into ``dup_epoch_catalogs`` — the #2 stratified
    oversampling stratum. This keys purely on the group boundary (any dup-epoch
    group, including exact-duplicate and admin-only re-issues), independent of the
    re-issue/clash branch; it is gated behind ``orbit`` so the default sgp4-free
    path never pays for a set the orbit pass alone consumes.

    Returns ``(hard_conflicts, reissue_count, dup_epoch_catalogs)``. Constant memory
    over the stream: only the current group's element-set→orbital-state map is held;
    ``dup_epoch_catalogs`` is O(distinct dup-epoch catalogs), the same catalog-scale
    budget as the sample sets, not corpus-record-scale."""
    conflicts: list[Suspect] = []
    reissues = 0
    dup_epoch_catalogs: set[int] = set()
    group_key: tuple[int, float] | None = None
    by_elset: dict[int | None, tuple] = {}
    states: set[tuple] = set()
    for rec in sorted_records:
        key = (rec.catalog, rec.epoch_key)
        if key != group_key:
            group_key = key
            by_elset = {}
            states = set()
        elif orbit:
            # a second (or later) record in this (catalog, epoch) group -> a
            # dup-epoch group; collect its catalog for the oversampling stratum.
            dup_epoch_catalogs.add(rec.catalog)
        state = orbital_state(rec.line1, rec.line2)
        elset = element_set(rec.line1)
        if by_elset.get(elset, state) != state:
            # this element-set already appeared with a different orbit — a clash
            conflicts.append(
                Suspect(
                    VrfyRule.EPOCH_CONFLICT,
                    rec.catalog,
                    rec.epoch_key,
                    rec.src_file,
                    rec.index,
                    f"catalog {rec.catalog} shares epoch {rec.epoch_key} and "
                    f"element-set {elset} with a different orbital state",
                )
            )
        elif states and state not in states:
            reissues += 1  # a new orbit under a new element-set — a re-issue
        by_elset.setdefault(elset, state)
        states.add(state)
    return conflicts, reissues, dup_epoch_catalogs


def has_epoch_clash(records: Iterable[CleanedRecord]) -> bool:
    """True iff some element-set among these same-``(catalog, epoch)`` records names
    more than one orbital state — the #158 definition of a genuine contradiction,
    shared with :func:`find_conflicts` so ``verify`` and ``dedup`` agree on what a
    same-epoch clash is. A *different* element-set with a different orbit is a
    benign refined re-issue (space-track's successive solution), not a clash — the
    distinction :func:`find_conflicts` draws per record, expressed here as one
    boolean over a materialised group (bounded: a handful of re-issues)."""
    by_elset: dict[int | None, tuple] = {}
    for r in records:
        state = orbital_state(r.line1, r.line2)
        if by_elset.setdefault(element_set(r.line1), state) != state:
            return True
    return False


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
            stripped = line.rstrip("\n")
            # Skip blank source lines: never part of a TLE record (clean's
            # pairing skips them too), and an interposed blank would break the
            # adjacent line-1/line-2 pair match below -> false INTERIOR_MUT (#155).
            if stripped.strip():
                self._buf.append(stripped)

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
