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

from lintle import CLEANED_DIRNAME, cli_progress, summary, term
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.verify import checks, grouping, records
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

    # One live table for the whole run: every stem has a row before any work
    # starts (so the first frame is the roster), each row fills in as its stem
    # streams, and the finished table is the results view. The stat-only
    # fingerprint supplies the sizes, so what is announced and what is then
    # streamed cannot disagree.
    stem_sizes = dict(records.cleaned_fingerprint(out_dir)["stems"])
    sink = SuspectSink()  # external-sorts suspects to disk (#156): flat peak memory
    sorter = grouping.ExternalSorter()
    population: set[int] = set()  # distinct catalogs, for the orbit sample
    n_records = 0
    missing_source = 0
    histogram: Counter[str] = Counter()  # epoch record density, YYYY-MM -> count

    with cli_progress.UnitTable(
        stems,
        ("#", "file", "size", "progress", "records", "hard", "soft"),
        console=term.stderr_console,
        drop={"medium": ("size",), "narrow": ("size", "progress")},
    ) as table:
        for file_stem in stems:
            table.start(file_stem)
            size = stem_sizes.get(file_stem, 0)
            # Per-stem, not corpus-cumulative: the count sits in one stem's row,
            # so a running corpus total there would read as that stem's.
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
                    # Refresh sparsely — one update per record would cost more
                    # than the checks themselves.
                    if file_records % 50_000 == 0:
                        table.update(
                            file_stem,
                            **_row_cells(file_records, size, sink, file_stem),
                        )
                        table.totals(records=f"{n_records:,}")
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
            table.finish(
                file_stem,
                size=summary.format_size(size),
                progress=cli_progress.bar(size, size),
                records=f"{file_records:,}",
                hard=f"{sink.hard_by_stem[file_stem]:,}",
                soft=f"{sink.soft_by_stem[file_stem]:,}",
            )
            table.totals(
                size=summary.format_size(sum(stem_sizes.values())),
                records=f"{n_records:,}",
                hard=f"{sink.hard:,}",
                soft=f"{sink.total - sink.hard:,}",
            )

        code, verdict, vdir = _finish_run(
            out_dir,
            table,
            sink=sink,
            sorter=sorter,
            stems=stems,
            stem_sizes=stem_sizes,
            population=population,
            histogram=histogram,
            n_records=n_records,
            missing_source=missing_source,
            source_dir=source_dir,
            orbit=orbit,
            sample=sample,
            all_sats=all_sats,
            sensitivity=sensitivity,
            chunk_records=chunk_records,
        )
    # After the table closes, so the verdict is the last line rather than a
    # print above a live region.
    if code:
        term.error(f"verify: FAIL — {verdict}\n  see {vdir / 'summary.md'!s}")
    else:
        term.note(f"verify: PASS — {verdict}\n  see {vdir / 'summary.md'!s}")
    return code


def _row_cells(file_records, size, sink, stem):
    """The cells of a stem's row mid-stream. Progress is the share of the stem's
    stat'd bytes its records account for: every cleaned record is exactly two
    69-column lines plus newlines, so the count converts to bytes exactly.
    Clamped, because a truncated final chunk would otherwise read past 100%."""
    seen = min(file_records * _RECORD_BYTES, size)
    return {
        "size": summary.format_size(size),
        "progress": cli_progress.bar(seen, size),
        "records": f"{file_records:,}",
        "hard": f"{sink.hard_by_stem[stem]:,}",
        "soft": f"{sink.soft_by_stem[stem]:,}",
    }


_RECORD_BYTES = 140  # two 69-column lines + two newlines


def _counted(stream, table, total):
    """Yield the sorted stream, reporting how much of it the contradiction pass
    has consumed. This is the run's long tail — an external merge over every
    record, minutes of it on a corpus — and without a counter the stage is
    indistinguishable from a hang. Every record passes through here and the
    total is already known, so the fraction is exact, not an estimate."""
    table.phase("sorting records and checking contradictions…")
    for seen, rec in enumerate(stream, start=1):
        # Sparse, like every other counter here: the merge yields far faster
        # than a terminal can redraw.
        if seen % 250_000 == 0:
            done = f"{seen:,}/{total:,}" if total else f"{seen:,}"
            pct = f" ({int(100 * seen / total)}%)" if total else ""
            table.phase(f"sorting and checking contradictions — {done}{pct}")
        yield rec


def _finish_run(
    out_dir,
    table,
    *,
    sink,
    sorter,
    stems,
    stem_sizes,
    population,
    histogram,
    n_records,
    missing_source,
    source_dir,
    orbit,
    sample,
    all_sats,
    sensitivity,
    chunk_records,
):
    """Run the stages that follow the per-stem stream — the contradiction pass,
    the optional orbit pass, and the write — reporting each through the live
    table's summary label rather than a spinner, so the table stays put and the
    per-stem suspect columns are complete before the frame freezes. Returns the
    exit code, the verdict text, and the output directory — the caller prints
    the verdict once the table has closed, so it lands under the results rather
    than above the live region."""
    # Contradiction pass over the fully sorted stream (goal 3b): same-epoch
    # re-issues are counted (a census); only a same-element-set clash is hard.
    # Under --orbit, also collect the dup-epoch catalogs for the #2 sample stratum.
    conflicts, epoch_reissues, dup_epoch_catalogs = checks.find_conflicts(
        _counted(sorter.sorted_records(), table, n_records), orbit=orbit
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

        table.phase("propagating sampled orbits…")
        orbit_census = orbit_pass.run_orbit_pass(
            out_dir,
            stems,
            population,
            sink,
            table=table,
            sample=sample,
            all_sats=all_sats,
            sensitivity=orbit_pass.TIERS[sensitivity],
            oversample=dup_epoch_catalogs,
        )
        checked.update(orbit_census)

    table.phase("writing 04-verify…")
    vdir = sink.write(
        out_dir,
        checked=checked,
        epoch_distribution=dict(sorted(histogram.items())),
        chunk_records=chunk_records,
    )

    # The suspect columns are only final now: the contradiction and orbit passes
    # attribute findings to stems after the stream. Rewrite every row's counts,
    # then hand the summary row back its files-done label.
    for stem in stems:
        table.update(
            stem,
            hard=f"{sink.hard_by_stem[stem]:,}",
            soft=f"{sink.soft_by_stem[stem]:,}",
        )
    table.totals(hard=f"{sink.hard:,}", soft=f"{sink.total - sink.hard:,}")
    table.phase(None)

    hard = sink.hard
    soft = sink.total - sink.hard
    verdict = f"{hard} hard, {soft} soft suspect(s) across {n_records} records"
    return sink.exit_code, verdict, vdir
