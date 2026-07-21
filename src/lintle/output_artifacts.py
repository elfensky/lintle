"""Run-level artifacts written after a successful clean dispatch."""

from pathlib import Path

from lintle import cli_progress, report, report_writers, term
from lintle.chunking import CHUNK_RECORDS_DEFAULT


def write_clean_artifacts(
    out_dir,
    all_stats,
    envelope,
    failed_files=None,
    chunk_records=CHUNK_RECORDS_DEFAULT,
):
    """Write the corpus-wide artifacts for a completed ``clean`` run: the
    Markdown ``report.md``, the machine ``report.json`` (the byte-identical twin
    of the ``--report json`` stdout envelope), the ``broken-noradids.ndjson``,
    and the concatenated ``report.jsonl``. All are committed in one place so a
    successful run leaves a stable artifact set. ``failed_files`` is forwarded
    to ``write_run_report`` so the ``## Failures`` section appears in report.md
    when any input files could not be processed (issue #83). Always called with
    a non-empty ``all_stats`` (the caller guards with ``if all_stats:``).
    """
    with cli_progress.status("finalizing report…"):
        out = Path(out_dir)
        report.write_run_report(
            str(out / "report.md"), all_stats, failed_files=failed_files
        )
        report.write_run_json(str(out / "report.json"), envelope)
        report_writers.write_broken_noradids_ndjson(
            str(out / "broken-noradids.ndjson"), all_stats
        )
        missing = report_writers.concat_findings_shards(
            out_dir, str(out / "report.jsonl"), all_stats, chunk_records
        )
    # Warn about any shard that was missing but had quarantined records — the
    # gap is surfaced here (stderr ephemera) rather than inside concat_findings_shards
    # so the function stays return-value-only and cycle-free (issue #117).
    for src_name in missing:
        term.warning(
            f"findings shard missing for {src_name!r}: its quarantine records are "
            "absent from report.jsonl — re-run with --resume to regenerate it."
        )
