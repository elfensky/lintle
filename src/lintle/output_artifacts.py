"""Run-level artifacts written after a successful clean dispatch."""

import dataclasses
import os

from lintle import cli_progress, report, report_writers


@dataclasses.dataclass(frozen=True)
class CleanArtifacts:
    """Paths published by the clean-run finalization step."""

    report_path: str | None = None
    noradids_path: str | None = None
    findings_path: str | None = None


def write_clean_artifacts(out_dir, all_stats):
    """Write the corpus-wide artifacts for a completed ``clean`` run."""
    if not all_stats:
        return CleanArtifacts()
    with cli_progress.status("finalizing report…"):
        report_path = os.path.join(out_dir, "report.md")
        report.write_run_report(report_path, all_stats)
        noradids_path = os.path.join(out_dir, "broken-noradids.ndjson")
        report_writers.write_broken_noradids_ndjson(noradids_path, all_stats)
        findings_path = os.path.join(out_dir, "report.jsonl")
        report_writers.concat_findings_shards(out_dir, findings_path, all_stats)
    return CleanArtifacts(
        report_path=report_path,
        noradids_path=noradids_path,
        findings_path=findings_path,
    )
