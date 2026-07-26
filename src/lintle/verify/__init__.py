"""``lintle verify`` — post-run correctness auditing of a clean run's output.

Runs the exhaustive, sgp4-free checks (Increment 1): every cleaned record still
validates (goal 3), no ``(catalog, epoch)`` contradictions (goal 3), and — when
the original source is available — every cleaned line is a *sanctioned* edit of a
real source line, never an interior mutation (goal 1). The opt-in ``--orbit`` pass
(Increment 2, goal 2) adds sampled ``sgp4`` orbit-consistency, lazily imported
from ``orbit.py`` so the default checks stay ``sgp4``-free.

The verifier is a pure *consumer*: it reads ``<out-dir>/01-cleaned`` and the
source tree, and writes only under ``<out-dir>/04-verify``. It reuses
``tle.py`` for every validity judgment and never re-defines what a valid TLE
is."""

import datetime as _dt
from collections import Counter
from pathlib import Path

from lintle import CLEANED_DIRNAME, cli_progress, term
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.verify import checks, grouping, records
from lintle.verify import report as vreport
from lintle.verify.epoch import parse_epoch
from lintle.verify.report import SuspectSink


def run(
    out_dir: str,
    source_dir: str | None,
    *,
    orbit: bool = False,
    sample: int | None = None,
    all_sats: bool = False,
    sensitivity: str = "sensitive",
    chunk_records: int = CHUNK_RECORDS_DEFAULT,
) -> int:
    """Verify a clean run's ``<out-dir>`` output. ``source_dir`` enables the
    goal-1 byte-diff (skipped, with a note, when ``None``). ``orbit`` runs the
    opt-in sampled sgp4 orbit-consistency pass (goal 2) over ``sample`` satellites
    (``all_sats`` for the full sweep); ``sensitivity`` (``sensitive``/``strict``,
    #3) scales its outlier threshold. Returns the process exit code: 0 clean, 1
    hard suspects found, 2 operational error (no cleaned output)."""
    stems = records.cleaned_stems(out_dir)
    if not stems:
        cleaned_dir = Path(out_dir) / CLEANED_DIRNAME
        term.error(
            f"no cleaned output found under {cleaned_dir!s}.\n"
            "  run 'lintle clean' first, or point at its --out-dir."
        )
        return 2

    # Phase 1 — discovery. The stat-only fingerprint the staleness check already
    # computes doubles as the roster's name -> size map, so what is announced and
    # what is then streamed cannot disagree.
    stem_sizes = dict(records.cleaned_fingerprint(out_dir)["stems"])
    cli_progress.render_roster(term.stderr_console, stem_sizes)

    sink = SuspectSink()  # external-sorts suspects to disk (#156): flat peak memory
    sorter = grouping.ExternalSorter()
    population: set[int] = set()  # distinct catalogs, for the orbit sample
    n_records = 0
    missing_source = 0
    records_by_stem: Counter[str] = Counter()  # phase-3 rows
    histogram: Counter[str] = Counter()  # epoch record density, YYYY-MM -> count

    with cli_progress.phase_bar("verifying", len(stems)) as progress:
        for file_stem in stems:
            progress(description=f"verifying {file_stem}")
            # Per-stem, not corpus-cumulative: the count sits next to one stem's
            # name, so a running corpus total there would read as that stem's.
            file_records = 0
            # Null-object seam: always constructed, inert when the stem has no
            # source — no `is not None` guards, no caller-side skip contract.
            aligner = checks.SourceAligner.open(source_dir, file_stem)
            if source_dir is not None and not aligner.active:
                missing_source += 1
            try:
                for rec in records.iter_file(out_dir, file_stem):
                    n_records += 1
                    file_records += 1
                    # Refresh the record counter sparsely — one `update` per
                    # record would cost more than the checks themselves.
                    if file_records % 100_000 == 0:
                        progress(
                            description=(
                                f"verifying {file_stem} — {file_records:,} records"
                            )
                        )
                    bad = checks.revalidate(rec)
                    if bad is not None:
                        sink.add(bad)
                        # keys untrustworthy — the aligner no-ops without consuming
                        # source lines (its buffer invariant, internalized).
                        aligner.feed(rec, revalidated=False)
                        continue  # and don't sort it either
                    sorter.add(rec)
                    # Only records that survive revalidate are binned — a broken
                    # record has no trustworthy epoch (informational, sgp4-free).
                    year, day = parse_epoch(rec.line1)
                    month = (
                        _dt.datetime(year, 1, 1) + _dt.timedelta(days=day - 1)
                    ).month
                    histogram[f"{year}-{month:02d}"] += 1
                    if orbit and rec.catalog != -1:
                        population.add(rec.catalog)
                    mutated = aligner.feed(rec)
                    if mutated is not None:
                        sink.add(mutated)
            finally:
                aligner.close()
            records_by_stem[file_stem] = file_records
            progress(advance=1)

    # Contradiction pass over the fully sorted stream (goal 3b): same-epoch
    # re-issues are counted (a census); only a same-element-set clash is hard.
    # Under --orbit, also collect the dup-epoch catalogs for the #2 sample stratum.
    with cli_progress.status("sorting records and checking contradictions..."):
        conflicts, epoch_reissues, dup_epoch_catalogs = checks.find_conflicts(
            sorter.sorted_records(), orbit=orbit
        )
    sink.add_all(conflicts)

    checked = {
        "files": len(stems),
        "records": n_records,
        "source_diff": "skipped" if source_dir is None else "on",
        "missing_source_files": missing_source,
        "epoch_reissues": epoch_reissues,
    }

    if orbit:
        # Lazy import keeps the default (non-orbit) verify path sgp4-free.
        from lintle.verify import orbit as orbit_pass

        orbit_census = orbit_pass.run_orbit_pass(
            out_dir,
            stems,
            population,
            sink,
            sample=sample,
            all_sats=all_sats,
            sensitivity=orbit_pass.TIERS[sensitivity],
            oversample=dup_epoch_catalogs,
        )
        checked.update(orbit_census)

    with cli_progress.status("writing 04-verify..."):
        vdir = sink.write(
            out_dir,
            checked=checked,
            epoch_distribution=dict(sorted(histogram.items())),
            chunk_records=chunk_records,
        )

    # Phase 3 — results: the per-stem breakdown the verdict line summarises.
    vreport.render_results(
        stem_sizes, records_by_stem, sink, console=term.stderr_console
    )

    code = sink.exit_code
    hard = sink.hard
    soft = sink.total - sink.hard
    verdict = f"{hard} hard, {soft} soft suspect(s) across {n_records} records"
    if code:
        term.error(f"verify: FAIL — {verdict}\n  see {vdir / 'summary.md'!s}")
    else:
        term.note(f"verify: PASS — {verdict}\n  see {vdir / 'summary.md'!s}")
    return code
