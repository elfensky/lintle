"""Run-level artifacts written after a successful clean dispatch."""

from pathlib import Path

from lintle import (
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
    ``report.NNNNN.jsonl`` set. Also drops a root ``README.md`` describing the
    output layout. All are committed in one place so a successful run leaves a
    stable artifact set. ``failed_files`` is forwarded to ``write_run_report`` so
    the ``## Failures`` section appears in report.md when any input files could
    not be processed (issue #83). Always called with a non-empty ``all_stats``
    (the caller guards with ``if all_stats:``).
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
for Two-Line Element (TLE) satellite-tracking files. Each pipeline step writes into
its own subdirectory, so it is clear which data is which.

Every record/line file is a **chunk set**: `<name>.NNNNN.<suffix>` split at
`--chunk-records` records each (default 1,000,000). Concatenating a set in index
order reproduces the single logical file — e.g.
`cat data/cleaned/tle2020.*.cleaned.txt`.

## `data/` — `lintle clean` output (the cleaned corpus)

- **`data/cleaned/`** — valid TLE records, ready to ingest. `<stem>.NNNNN.cleaned.txt`,
  one chunk set per input file.
- **`data/broken/`** — records that could not be *safely* repaired, quarantined
  byte-faithfully with the reason. `<stem>.NNNNN.broken.txt`.
- **`data/report/`** — the corpus-wide run report:
  - `report.md` — human-readable summary.
  - `report.json` — machine-readable run envelope (what `lintle report` renders).
  - `report.NNNNN.jsonl` — structured findings, one JSON object per line.
  - `broken-noradids.ndjson` — quarantined NORAD IDs, one `{{"noradId": N}}` per line.

## `verify/` — `lintle verify` output (independent audit of `data/cleaned/`)

- `suspects.NNNNN.jsonl` — flagged records (`hard` = must fix, `soft` = inconclusive).
- `summary.json` / `summary.md` — audit tallies and verdict.

## `dedup/` — `lintle dedup` output (latest-re-issue-only import list)

- `import.NNNNN.txt` — the de-duplicated ingest list (hard suspects excluded,
  re-issues collapsed to the latest).
- `notes.NNNNN.jsonl` — one note per collapsed group.
- `summary.json` — dedup tallies.

`verify/` and `dedup/` appear only after you run those steps.
"""


def write_layout_readme(out_dir):
    """Write a static ``README.md`` at the out-dir root explaining the per-step
    output layout, so a run is self-describing without external docs. Written via
    the durable-commit path; deterministic for a given lintle version."""
    fsutil.durable_write_text(
        str(Path(out_dir) / "README.md"), _LAYOUT_README, encoding="utf-8"
    )
