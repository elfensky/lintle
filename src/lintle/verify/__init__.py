"""``lintle verify`` — post-run correctness auditing of a clean run's output.

Runs the exhaustive, sgp4-free checks (Increment 1): every cleaned record still
validates (goal 3), no ``(catalog, epoch)`` contradictions (goal 3), and — when
the original source is available — every cleaned line is a *sanctioned* edit of a
real source line, never an interior mutation (goal 1). Physics-based orbit
consistency (goal 2) is a later increment; this module never imports ``sgp4``.

The verifier is a pure *consumer*: it reads ``<out-dir>/cleaned`` and the source
tree, and writes only under ``<out-dir>/verify``. It reuses ``tle.py`` for every
validity judgment and never re-defines what a valid TLE is."""

from pathlib import Path

from lintle import term
from lintle.verify import checks, grouping, records
from lintle.verify.report import exit_code, write_reports


def run_verify(out_dir: str, source_dir: str | None) -> int:
    """Verify a clean run's ``<out-dir>`` output. ``source_dir`` enables the
    goal-1 byte-diff (skipped, with a note, when ``None``). Returns the process
    exit code: 0 clean, 1 hard suspects found, 2 operational error (no cleaned
    output)."""
    stems = records.cleaned_stems(out_dir)
    if not stems:
        term.error(
            f"no cleaned output found under {Path(out_dir) / 'cleaned'!s}.\n"
            "  run 'lintle clean' first, or point at its --out-dir."
        )
        return 2

    suspects = []
    sorter = grouping.ExternalSorter()
    n_records = 0
    missing_source = 0

    for file_stem in stems:
        aligner = None
        if source_dir is not None:
            src_path = Path(source_dir) / (file_stem + ".txt")
            if src_path.is_file():
                aligner = checks.SourceAligner(str(src_path))
            else:
                missing_source += 1
        try:
            for rec in records.iter_file(out_dir, file_stem):
                n_records += 1
                bad = checks.revalidate(rec)
                if bad is not None:
                    suspects.append(bad)
                    continue  # keys untrustworthy — don't sort or byte-diff it
                sorter.add(rec)
                if aligner is not None:
                    mutated = aligner.check(rec)
                    if mutated is not None:
                        suspects.append(mutated)
        finally:
            if aligner is not None:
                aligner.close()

    # Contradiction pass over the fully sorted stream (goal 3b): same-epoch
    # re-issues are counted (a census); only a same-element-set clash is hard.
    conflicts, epoch_reissues = checks.find_conflicts(sorter.sorted_records())
    suspects.extend(conflicts)

    checked = {
        "files": len(stems),
        "records": n_records,
        "source_diff": "skipped" if source_dir is None else "on",
        "missing_source_files": missing_source,
        "epoch_reissues": epoch_reissues,
    }
    vdir = write_reports(out_dir, suspects, checked=checked)

    code = exit_code(suspects)
    hard = sum(1 for s in suspects if s.severity == "hard")
    soft = len(suspects) - hard
    verdict = f"{hard} hard, {soft} soft suspect(s) across {n_records} records"
    if code:
        term.error(f"verify: FAIL — {verdict}\n  see {vdir / 'summary.md'!s}")
    else:
        term.note(f"verify: PASS — {verdict}\n  see {vdir / 'summary.md'!s}")
    return code
