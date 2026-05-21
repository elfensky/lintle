"""Streaming I/O: read a file, pair lines into records, route them."""

import dataclasses


@dataclasses.dataclass
class RecordCandidate:
    """A line-1 / line-2 pair, with their 1-indexed source line numbers."""

    raw_line1: bytes
    raw_line2: bytes
    src1: int
    src2: int


@dataclasses.dataclass
class Orphan:
    """A line that could not be paired into a record."""

    raw_line: bytes
    src: int
    category: str
    reason: str


def iter_records(path):
    """Yield ``RecordCandidate`` / ``Orphan`` items streamed from ``path``.

    The file is read in binary so ``\\r`` and stray bytes are observed
    exactly. Blank and CR-only lines are dropped. Pairing is prefix-driven
    and resynchronises on every ``1 `` line, so one missing line cannot
    cascade into a run of mispaired records.
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2

    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.rstrip(b"\n")
            if line.rstrip(b"\r") == b"":
                continue  # blank or CR-only line — dropped

            prefix = line[:2]
            if prefix == b"1 ":
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by another line 1",
                    )
                held = (line, lineno)
            elif prefix == b"2 ":
                if held is not None:
                    yield RecordCandidate(held[0], line, held[1], lineno)
                    held = None
                else:
                    yield Orphan(
                        line, lineno, "orphan-line",
                        "orphan line 2: no preceding line 1",
                    )
            else:
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by a non-TLE line",
                    )
                    held = None
                yield Orphan(
                    line, lineno, "bad-prefix",
                    "line does not start with '1 ' or '2 '",
                )

    if held is not None:
        yield Orphan(
            held[0], held[1], "orphan-line", "orphan line 1 at end of file"
        )
