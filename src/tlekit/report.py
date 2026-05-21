"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import dataclasses
import datetime

from tlekit import __version__, stem


@dataclasses.dataclass
class RejectEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful.
    """

    raw_lines: list
    source_lines: list
    reason: str


@dataclasses.dataclass
class FileStats:
    """Accumulated results for one processed source file."""

    src_name: str
    total_records: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_categories: dict = dataclasses.field(default_factory=dict)
    rejects: list = dataclasses.field(default_factory=list)


def write_broken_file(path, src_name, stats):
    """Write the byte-faithful ``.broken.txt`` quarantine sidecar.

    The header and per-record reason lines are UTF-8 (ASCII-compatible for
    pure-ASCII content); the quarantined-line payloads are copied as raw
    bytes, so the file may not be valid UTF-8.
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    header = (
        f"# {stem(src_name)}.broken.txt — quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | tlekit {__version__}\n"
        f"# {stats.quarantined_count} records quarantined "
        f"of {stats.total_records} total\n\n"
    )
    with open(path, "wb") as handle:
        handle.write(header.encode("utf-8"))
        for index, entry in enumerate(stats.rejects, start=1):
            if len(entry.source_lines) == 2:
                location = (
                    f"source lines {entry.source_lines[0]}-"
                    f"{entry.source_lines[1]}"
                )
            else:
                location = f"source line {entry.source_lines[0]}"
            handle.write(
                f"[{index}] {location} — reason: {entry.reason}\n".encode(
                    "utf-8", errors="replace"
                )
            )
            for raw in entry.raw_lines:
                handle.write(raw)
                handle.write(b"\n")
            handle.write(b"\n")
