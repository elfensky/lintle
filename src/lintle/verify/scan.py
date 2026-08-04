"""One driver for the cleaned-tree scan that ``verify`` and ``dedup`` both open
with. The two commands differ only in what they do per record and which columns
they tally; everything around that — the "no cleaned output" error, the stat-only
fingerprint, the live ``UnitTable``, the sparse row refresh, and the per-stem
finish/totals — was byte-identical in both. It lives here rather than in
``cli_progress`` because it is about *cleaned records*, not about tables:
``RECORD_BYTES`` and the fingerprint are cleaned-tree facts.

The scan is a context manager that keeps the table **live** after the record
loop, because both callers do their real work there (``verify`` runs the
contradiction and orbit passes; ``dedup`` collapses and writes the import set)
and report it through the same table's summary label."""

import contextlib
import dataclasses
from collections.abc import Callable, Iterator
from pathlib import Path

from lintle import CLEANED_DIRNAME, cli_progress, summary, term
from lintle.verify import records
from lintle.verify.records import CleanedRecord

# A cleaned record is exactly two 69-column lines plus their newlines, so a
# record count converts to a byte offset exactly. One definition: verify, dedup
# and the progress math all read the same number.
RECORD_BYTES = 140

# Rows refresh every N records — one update per record would cost more than the
# checks themselves on a corpus-scale stem.
REFRESH_EVERY = 50_000


def cleaned_stems_or_error(out_dir: str) -> list[str] | None:
    """The cleaned chunk-set stems under ``<out-dir>/01-cleaned``, or ``None``
    after printing the shared "run clean first" error — the caller turns that
    into exit 2. One spelling of the message for every cleaned-tree consumer."""
    stems = records.cleaned_stems(out_dir)
    if not stems:
        term.error(
            f"no cleaned output found under {Path(out_dir) / CLEANED_DIRNAME!s}.\n"
            "  run 'lintle clean' first, or point at its --out-dir."
        )
        return None
    return stems


@dataclasses.dataclass(slots=True)
class Scan:
    """A live scan of the cleaned tree: the open ``table``, the ``stems`` and
    their stat'd ``stem_sizes``, the whole ``fingerprint`` (handed back so a
    caller that also stores it never stat-walks the tree twice), and the running
    ``n_records``. Drive it with :meth:`units` and :meth:`stream`."""

    out_dir: str
    table: cli_progress.UnitTable
    stems: list[str]
    stem_sizes: dict[str, int]
    fingerprint: dict
    cells: Callable[[str, int], dict[str, str]]
    totals: Callable[[], dict[str, str]]
    n_records: int = 0
    _file_records: int = 0

    def units(self) -> Iterator[tuple[str, int]]:
        """Yield ``(stem, size)`` per stem, opening that stem's row before the
        caller's body and closing it — plus the corpus totals — after. The
        caller owns any per-stem resource in between (``verify``'s source
        aligner), so its own ``try``/``finally`` stays where it can see it."""
        for stem in self.stems:
            self.table.start(stem)
            size = self.stem_sizes.get(stem, 0)
            # Per-stem, not corpus-cumulative: a running corpus total sitting in
            # one stem's row would read as that stem's own count.
            self._file_records = 0
            yield stem, size
            self.table.finish(
                stem,
                size=summary.format_size(size),
                progress=cli_progress.bar(size, size),
                records=f"{self._file_records:,}",
                **self.cells(stem, self._file_records),
            )
            self.table.totals(
                size=summary.format_size(sum(self.stem_sizes.values())),
                records=f"{self.n_records:,}",
                **self.totals(),
            )

    def stream(self, stem: str) -> Iterator[CleanedRecord]:
        """Yield one stem's cleaned records, counting them and refreshing the
        row every :data:`REFRESH_EVERY`."""
        for rec in records.iter_file(self.out_dir, stem):
            self.n_records += 1
            self._file_records += 1
            if self._file_records % REFRESH_EVERY == 0:
                size = self.stem_sizes.get(stem, 0)
                self.table.update(
                    stem,
                    size=summary.format_size(size),
                    # Records convert to a byte offset exactly; `bar` already
                    # clamps `completed`, so no second clamp here.
                    progress=cli_progress.bar(self._file_records * RECORD_BYTES, size),
                    records=f"{self._file_records:,}",
                    **self.cells(stem, self._file_records),
                )
                self.table.totals(records=f"{self.n_records:,}")
            yield rec


@contextlib.contextmanager
def scan_cleaned(
    out_dir: str,
    stems: list[str],
    *,
    columns: tuple[str, ...],
    cells: Callable[[str, int], dict[str, str]],
    totals: Callable[[], dict[str, str]],
) -> Iterator[Scan]:
    """Open the live scan over ``stems``. ``columns`` names the table's columns;
    ``cells(stem, file_records)`` supplies the caller-specific row cells (the
    driver already fills ``size``/``progress``/``records``) and ``totals()`` the
    caller-specific summary-row cells. The table stays live for the duration of
    the ``with`` block — post-loop work belongs inside it, so its stage labels
    land in the summary row instead of printing above a live region."""
    # The stat-only fingerprint supplies the sizes, so what the roster announces
    # and what is then streamed cannot disagree. Computed once and handed back
    # on the Scan: dedup also stores it in summary.json, and used to stat-walk
    # the whole cleaned tree a second time to get it.
    fingerprint = records.cleaned_fingerprint(out_dir)
    with cli_progress.UnitTable(
        stems,
        columns,
        console=term.stderr_console,
        drop={"medium": ("size",), "narrow": ("size", "progress")},
    ) as table:
        yield Scan(
            out_dir=out_dir,
            table=table,
            stems=stems,
            stem_sizes=dict(fingerprint["stems"]),
            fingerprint=fingerprint,
            cells=cells,
            totals=totals,
        )
