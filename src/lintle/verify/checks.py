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
from pathlib import Path

from lintle import repair, tle
from lintle.verify.grouping import record_key
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, VerifyRule

# Memory bound for the source read buffer: at most this many source lines are
# held at once. It is NOT a search-distance limit — ``check`` slides this buffer
# forward without bound (see below), so a quarantine run of ANY length between
# two accepted records is crossed. (A fixed *search* window here was the 44M-false-
# suspect / 31h desync cascade: real corpora have quarantine runs of 20k+ lines,
# far past any fixed cap.)
_RESYNC_WINDOW = 4096


def revalidate(rec: CleanedRecord) -> Suspect | None:
    """A cleaned record that no longer validates is a hard suspect — cleaning is
    supposed to emit only perfect records, so a failure here means corruption or
    a cleaning bug. Reuses the ONE validator; never a second definition."""
    errors = tle.validate_record(rec.line1, rec.line2)
    if errors:
        return Suspect(
            VerifyRule.REVALIDATE_FAIL,
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


def _is_clash(
    by_elset: dict[int | None, tuple], elset: int | None, state: tuple
) -> bool:
    """The #158 clash predicate — the one implementation both
    :func:`find_conflicts` (per record) and :func:`has_epoch_clash` (per group)
    route through: True iff ``elset`` was already seen in this
    ``(catalog, epoch)`` group with a *different* orbital state. Records
    ``state`` as ``elset``'s first-seen orbit on the way (setdefault)."""
    return by_elset.setdefault(elset, state) != state


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
        key = record_key(rec)
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
        if _is_clash(by_elset, elset, state):
            conflicts.append(
                Suspect(
                    VerifyRule.EPOCH_CONFLICT,
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
        states.add(state)
    return conflicts, reissues, dup_epoch_catalogs


def has_epoch_clash(records: Iterable[CleanedRecord]) -> bool:
    """True iff some element-set among these same-``(catalog, epoch)`` records names
    more than one orbital state — the #158 definition of a genuine contradiction.
    Routes through the same :func:`_is_clash` predicate as :func:`find_conflicts`,
    so ``verify`` and ``dedup`` agree on what a same-epoch clash is by
    construction, not by hand-synced twins. A *different* element-set with a
    different orbit is a benign refined re-issue, not a clash."""
    by_elset: dict[int | None, tuple] = {}
    return any(
        _is_clash(by_elset, element_set(r.line1), orbital_state(r.line1, r.line2))
        for r in records
    )


def sanctioned_reduce(src_line: str) -> str:
    """Undo the sanctioned edge repairs, leaving the interior untouched. The
    result is the 68/69-char 'core' a clean origin reduces to.

    Delegates to ``repair.normalize_edges`` rather than restating its rules:
    this reduction must track ``repair_line`` exactly or the aligner stops
    recognising a cleaned line as an edit of its origin and reports every later
    record in the file as ``ORIGIN_MISSING``. A hand-kept mirror drifted the
    moment the strip sequence learned to repeat."""
    return repair.normalize_edges(src_line)[0]


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


def _is_quarantined_shadow(line1: str, line2: str) -> bool:
    """True iff this source pair, after the sanctioned edge reduction, is NOT a
    valid record — i.e. one clean would have QUARANTINED. Such a pair can share a
    cleaned record's anchor (``(catalog, epoch-columns)``) yet is never that
    record's origin (an origin is a valid, accepted record), so the aligner skips
    it rather than reporting a false interior mutation. The real corpus needs
    this: tle2020 carries each satellite twice at one epoch — a ``+``-signed
    68-char *missing-checksum* copy clean drops, and the real space-signed 69-char
    copy it keeps — and both share the anchor. A genuine interior mutation is
    unaffected: its origin is a valid record, so this returns False and the
    mutation is still flagged."""
    return bool(tle.validate_record(sanctioned_reduce(line1), sanctioned_reduce(line2)))


class SourceAligner:
    """Streams one source file alongside a cleaned file's records, matching each
    cleaned record to its source origin. Every cleaned record is a *sanctioned
    edit* of a real source pair, so its origin always exists ahead in the source;
    the aligner slides a memory-bounded read buffer forward — across quarantine
    runs of ANY length — until it finds that origin. Common case (next pair matches
    immediately) is O(1) per record, and total work is O(source lines) even when
    thousands of quarantined records separate two accepted ones."""

    def __init__(self, source_path: str | None) -> None:
        # None -> a null object: feed()/close() are unconditionally callable and
        # do nothing, so callers never carry an `if aligner is not None` guard.
        self._fh = (
            open(source_path, encoding="ascii", errors="replace")  # noqa: SIM115
            if source_path is not None
            else None
        )
        self._buf: list[str] = []

    @classmethod
    def open(cls, source_dir: str | None, file_stem: str) -> SourceAligner:
        """Build the aligner for one cleaned stem: active when
        ``<source_dir>/<file_stem>.txt`` exists, inert otherwise (no source dir,
        or the stem has no source file). The existence check lives here so the
        caller constructs, feeds, and closes unconditionally."""
        if source_dir is not None:
            src_path = Path(source_dir) / (file_stem + ".txt")
            if src_path.is_file():
                return cls(str(src_path))
        return cls(None)

    @property
    def active(self) -> bool:
        """True iff a real source file backs this aligner (inert ones swallow
        every ``feed`` — used by the caller only for missing-source counting)."""
        return self._fh is not None

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

    def feed(self, rec: CleanedRecord, *, revalidated: bool = True) -> Suspect | None:
        """Locate ``rec``'s source origin and classify it: clean match (None),
        interior mutation (hard), or no origin through EOF (soft). Feed EVERY
        record in on-disk order — the skip policy lives here, not in the caller:
        an inert aligner or a revalidate-failed record (``revalidated=False``)
        returns None *without consuming source lines*, preserving the
        forward-only buffer invariant the 5bd4026/6cdb340 bug class depended on
        the caller to uphold. Scans forward without bound: when a buffer holds
        neither a byte-match nor an anchor for ``rec``, every line in it is a
        quarantined record BEFORE ``rec``'s origin (cleaned records are in
        source order), so the whole buffer is dropped and the scan continues,
        crossing a quarantine run of any length."""
        if self._fh is None or not revalidated:
            return None
        rec_anchor: tuple[int, str] | None = None
        anchor_computed = False  # defer _anchor(rec.line1) off the O(1) happy path
        while True:
            self._refill()
            anchor_at: int | None = None
            for i in range(len(self._buf) - 1):
                if sanctioned_match(self._buf[i], rec.line1) and sanctioned_match(
                    self._buf[i + 1], rec.line2
                ):
                    del self._buf[: i + 2]
                    return None
                if anchor_at is None:
                    if not anchor_computed:
                        rec_anchor = _anchor(rec.line1)
                        anchor_computed = True
                    if rec_anchor is not None and _anchor(self._buf[i]) == rec_anchor:
                        anchor_at = i
            if anchor_at is not None:
                shadow = _is_quarantined_shadow(
                    self._buf[anchor_at], self._buf[anchor_at + 1]
                )
                del self._buf[: anchor_at + 2]
                if shadow:
                    # A dropped duplicate sharing rec's anchor, not its origin.
                    # Consume it and keep scanning forward for the real origin.
                    continue
                return Suspect(
                    VerifyRule.INTERIOR_MUT,
                    rec.catalog,
                    rec.epoch_key,
                    rec.src_file,
                    rec.index,
                    "cleaned record differs from its source in a non-edge column",
                )
            if len(self._buf) <= 1:
                # EOF reached with no origin found (a cleaned record with no source
                # origin should not happen; kept as a soft signal, not a crash).
                return Suspect(
                    VerifyRule.ORIGIN_MISSING,
                    rec.catalog,
                    rec.epoch_key,
                    rec.src_file,
                    rec.index,
                    "no source origin found through end of source file",
                )
            # Drop the scanned window, keeping the last line so a pair straddling
            # the buffer boundary is still checked on the next refill.
            del self._buf[: len(self._buf) - 1]

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
