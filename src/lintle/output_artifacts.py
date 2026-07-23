"""Run-level artifacts written after a successful clean dispatch."""

from pathlib import Path

from lintle import (
    BROKEN_DIRNAME,
    CLEANED_DIRNAME,
    REPORT_DIRNAME,
    __version__,
    cli_progress,
    fsutil,
    report,
    report_writers,
    term,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT


def write_clean_artifacts(
    out_dir,
    all_stats,
    envelope,
    failed_files=None,
    chunk_records=CHUNK_RECORDS_DEFAULT,
):
    """Write the corpus-wide artifacts for a completed ``clean`` run into
    ``<out-dir>/03-report/``: the Markdown ``report.md``, the machine
    ``report.json`` (the byte-identical twin of the ``--report json`` stdout
    envelope), the ``broken-noradids.ndjson``, and the concatenated
    ``report.NNNNN.jsonl`` set. Also drops the root ``README.md`` describing
    the output layout and a static README.md inside each of the three dirs
    ``clean`` owns (01-cleaned, 02-broken, 03-report). All are committed in one
    place so a successful run leaves a stable artifact set. ``failed_files`` is
    forwarded to ``write_run_report`` so the ``## Failures`` section appears in
    report.md when any input files could not be processed (issue #83). Always
    called with a non-empty ``all_stats`` (the caller guards with
    ``if all_stats:``).
    """
    with cli_progress.status("finalizing report…"):
        rdir = Path(out_dir) / REPORT_DIRNAME
        rdir.mkdir(parents=True, exist_ok=True)
        report.write_run_report(
            str(rdir / "report.md"), all_stats, failed_files=failed_files
        )
        report.write_run_json(str(rdir / "report.json"), envelope)
        report_writers.write_broken_noradids_ndjson(
            str(rdir / "broken-noradids.ndjson"), all_stats
        )
        missing = report_writers.concat_findings_shards(
            out_dir, str(rdir / "report.jsonl"), all_stats, chunk_records
        )
        write_layout_readme(out_dir)
        _write_step_readmes(out_dir)
    # Warn about any shard that was missing but had quarantined records — the
    # gap is surfaced here (stderr ephemera) rather than inside concat_findings_shards
    # so the function stays return-value-only and cycle-free (issue #117).
    for src_name in missing:
        term.warning(
            f"findings shard missing for {src_name!r}: its quarantine records are "
            "absent from report.jsonl — re-run with --resume to regenerate it."
        )


_LAYOUT_README = f"""\
# lintle output

This directory holds the output of **lintle** {__version__}, a validator/cleaner
for Two-Line Element (TLE) satellite-tracking files. Each pipeline step writes
into its own numbered subdirectory, in the order the steps run — the directory
listing itself documents the pipeline:

- **`01-cleaned/`** — `lintle clean`'s valid TLE records, ready to ingest.
- **`02-broken/`** — `lintle clean`'s quarantined records, byte-faithful with
  the reason each one failed.
- **`03-report/`** — `lintle clean`'s corpus-wide run report.
- **`04-verify/`** — `lintle verify`'s independent audit of `01-cleaned/`.
- **`05-dedup/`** — `lintle dedup`'s latest-re-issue-only import list.
- **`06-extract/`** — `lintle extract`'s per-satellite TLE history.

`04-verify/`, `05-dedup/`, and `06-extract/` appear only once you run those
steps; each populated directory carries its own `README.md` with the details.

Every record/line file in every step is a **chunk set**: `<name>.NNNNN.<suffix>`
split at `--chunk-records` records each (default 1,000,000). Concatenating a
set in index order reproduces the single logical file — e.g.
`cat 01-cleaned/tle2020.*.cleaned.txt`.

This whole tree is reproducible from the source corpus: if it looks stale or
wrong, delete it and regenerate rather than trying to patch it — there is no
migration tool between layout versions (established policy). Transient run
state — `.shards/`, `.clean-state.json`, `.clean.lock`, `.lintle-output` — stays
hidden at the root; it is pipeline machinery, not step output, so it is not
described here.
"""

_CLEANED_README = """\
# 01-cleaned — validated output

Every record here passed the full validator after repair (Critical Rule #1).
Files are `<stem>.NNNNN.cleaned.txt` chunk sets: concatenate a stem's chunks
in index order to reproduce the single-file form. Regenerate with
`lintle clean`.
"""

_BROKEN_README = """\
# 02-broken — quarantined records

Every record here failed validation and could not be *safely* repaired
(Critical Rule #2: correctness over recovery — a doubtful record is
quarantined, never guessed at). Files are `<stem>.NNNNN.broken.txt` chunk
sets, byte-faithful sidecars: each quarantined record is followed by the
rule tag(s) it failed. Run `lintle explain <rule>` for what any tag means.
Regenerate with `lintle clean`.
"""

_REPORT_README = """\
# 03-report — the corpus-wide run report

- `report.md` — human-readable summary of the run.
- `report.json` — the machine-readable run envelope; `lintle report` renders it.
- `report.NNNNN.jsonl` — structured per-record findings, one JSON object per
  line; this chunk set is `lintle diff`'s input for comparing two runs.
- `broken-noradids.ndjson` — the complete list of quarantined NORAD IDs, one
  `{"noradId": N}` per line — report.md's table is capped, this is the whole
  list.

Regenerate with `lintle clean`.
"""


def write_layout_readme(out_dir):
    """Write a static ``README.md`` at the out-dir root explaining the per-step
    output layout, so a run is self-describing without external docs. Written via
    the durable-commit path; deterministic for a given lintle version."""
    fsutil.durable_write_text(
        str(Path(out_dir) / "README.md"), _LAYOUT_README, encoding="utf-8"
    )


def _write_step_readmes(out_dir):
    """Write the static ``README.md`` inside each of the three dirs ``clean``
    owns — 01-cleaned, 02-broken, 03-report — so each is self-describing on its
    own, not just via the root README. Deterministic text, no counts or
    timestamps; committed via the durable-commit path."""
    for dirname, text in (
        (CLEANED_DIRNAME, _CLEANED_README),
        (BROKEN_DIRNAME, _BROKEN_README),
        (REPORT_DIRNAME, _REPORT_README),
    ):
        d = Path(out_dir) / dirname
        d.mkdir(parents=True, exist_ok=True)
        fsutil.durable_write_text(str(d / "README.md"), text, encoding="utf-8")
