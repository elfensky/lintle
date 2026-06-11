"""Run-level artifacts written after a successful clean dispatch."""

import dataclasses
from pathlib import Path

from lintle import cli_progress, report, report_writers, term


@dataclasses.dataclass(frozen=True)
class CleanArtifacts:
    """Paths published by the clean-run finalization step."""

    report_path: str | None = None
    report_json_path: str | None = None
    noradids_path: str | None = None
    findings_path: str | None = None


def write_clean_artifacts(out_dir, all_stats, envelope, failed_files=None):
    """Write the corpus-wide artifacts for a completed ``clean`` run: the
    Markdown ``report.md``, the machine ``report.json`` (the byte-identical twin
    of the ``--report json`` stdout envelope), the ``broken-noradids.ndjson``,
    and the concatenated ``report.jsonl``. All are committed in one place so a
    successful run leaves a stable artifact set. ``failed_files`` is forwarded
    to ``write_run_report`` so the ``## Failures`` section appears in report.md
    when any input files could not be processed (issue #83)."""
    if not all_stats:
        return CleanArtifacts()
    with cli_progress.status("finalizing report…"):
        out = Path(out_dir)
        report_path = str(out / "report.md")
        report.write_run_report(report_path, all_stats, failed_files=failed_files)
        report_json_path = str(out / "report.json")
        report.write_run_json(report_json_path, envelope)
        noradids_path = str(out / "broken-noradids.ndjson")
        report_writers.write_broken_noradids_ndjson(noradids_path, all_stats)
        findings_path = str(out / "report.jsonl")
        missing = report_writers.concat_findings_shards(
            out_dir, findings_path, all_stats
        )
    # Warn about any shard that was missing but had quarantined records — the
    # gap is surfaced here (stderr ephemera) rather than inside concat_findings_shards
    # so the function stays return-value-only and cycle-free (issue #117).
    for src_name in missing:
        term.warning(
            f"findings shard missing for {src_name!r}: its quarantine records are "
            "absent from report.jsonl — re-run with --resume to regenerate it."
        )
    return CleanArtifacts(
        report_path=report_path,
        report_json_path=report_json_path,
        noradids_path=noradids_path,
        findings_path=findings_path,
    )
