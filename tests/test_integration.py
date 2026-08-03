"""End-to-end pipeline tests: golden output, idempotence, re-validation."""

import json

from lintle import (
    BROKEN_DIRNAME,
    CLEANED_DIRNAME,
    EXTRACT_DIRNAME,
    REPORT_DIRNAME,
    cli,
    pipeline,
    tle,
)
from lintle.categories import FixClass
from lintle.chunking import ChunkedReader


class TestEndToEnd:
    def test_golden_mixed_file(self, tmp_path, line1, line2):
        # Record A: clean. Record B: checksumless line 1 + backslash, checksumless
        # line 2 (both repairable). Record C: line 1 with a wrong checksum (bad).
        bad_line1 = line1[:68] + "9"
        src = tmp_path / "tle2099.txt"
        src.write_bytes(
            (
                line1
                + "\n"
                + line2
                + "\n"
                + line1[:68]
                + "\\\n"
                + line2[:68]
                + "\n"
                + bad_line1
                + "\n"
                + line2
                + "\n"
            ).encode("ascii")
        )
        out = tmp_path / "out"

        # Record B is checksumless on both lines; reconstruction is opt-in (#82).
        stats = pipeline.process_file(
            str(src), str(out), "clean", reconstruct_checksum=True
        )

        assert stats.paired_records == 3
        assert stats.orphan_entries == 0
        assert stats.input_lines_seen == 6
        assert stats.clean_count == 2
        assert stats.quarantined_count == 1
        assert stats.fix_counts.get(FixClass.RECONSTRUCTED_CHECKSUM) == 2
        assert stats.fix_counts.get(FixClass.TRAILING_BACKSLASH) == 1

        cleaned = (out / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt").read_bytes()
        assert cleaned == (
            line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
        ).encode("ascii")
        broken = (out / BROKEN_DIRNAME / "tle2099.00001.broken.txt").read_bytes()
        assert b"TLE-CHK-001" in broken
        assert bad_line1.encode("ascii") in broken

    def test_clean_is_idempotent(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))

        out1 = tmp_path / "out1"
        pipeline.process_file(str(src), str(out1), "clean", reconstruct_checksum=True)
        cleaned1 = out1 / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt"

        # Re-clean the cleaned output. stem("tle2099.00001.cleaned.txt") ==
        # "tle2099.00001.cleaned", so the re-clean's single chunk lands at
        # "tle2099.00001.cleaned.00001.cleaned.txt". The cleaned record is
        # already 69 chars, so no reconstruction is needed — idempotence must
        # hold even with the flag off.
        out2 = tmp_path / "out2"
        stats2 = pipeline.process_file(str(cleaned1), str(out2), "clean")
        cleaned2 = out2 / CLEANED_DIRNAME / "tle2099.00001.cleaned.00001.cleaned.txt"

        assert cleaned1.read_bytes() == cleaned2.read_bytes()
        # Idempotence (spec §8): re-cleaning applies zero fixes and zero quarantines.
        assert stats2.fix_counts == {}
        assert stats2.quarantined_count == 0

    def test_cleaned_output_revalidates_as_perfect(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean", reconstruct_checksum=True)

        stats = pipeline.process_file(
            str(out / CLEANED_DIRNAME / "tle2099.00001.cleaned.txt"),
            str(tmp_path / "verify"),
            "validate",
        )
        assert stats.clean_count == 1
        assert stats.quarantined_count == 0

    def test_alpha5_quarantine_reaches_the_extract_sidecar(
        self, tmp_path, line1, line2
    ):
        # #206, the whole chain in one test: an Alpha-5 satellite (E8493 ->
        # 148493) with one clean record and one quarantined (wrong checksum)
        # one. clean must list the satellite in broken-noradids.ndjson, and
        # extract must then read that file back as had_quarantined_records
        # true. While extract_norad_id refused Alpha-5 the id never reached the
        # ndjson, so the sidecar reported a confident `false` for a satellite
        # whose records WERE quarantined — a silent wrong answer.
        def fix(line: str) -> str:
            return line[:68] + str(tle.compute_checksum(line))

        a1, a2 = fix("1 E8493" + line1[7:]), fix("2 E8493" + line2[7:])
        clean_epoch = fix(a1[:20] + "180" + a1[23:])
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes(
            f"{a1[:68]}9\n{a2}\n{clean_epoch}\n{a2}\n".encode("ascii")
        )
        out = tmp_path / "out"

        assert cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"]) == 1
        ndjson = (out / REPORT_DIRNAME / "broken-noradids.ndjson").read_text("ascii")
        assert ndjson == '{"noradId":148493}\n'

        assert cli.main(["dedup", str(out)]) == 0
        assert cli.main(["extract", "E8493", "--out-dir", str(out)]) == 0
        sidecar = json.loads(
            (out / EXTRACT_DIRNAME / "148493.json").read_text(encoding="ascii")
        )
        assert sidecar["had_quarantined_records"] is True

    def test_every_cleaned_line_passes_validate_line(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\n" + line2[:68] + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean", reconstruct_checksum=True)

        reader = ChunkedReader(out / CLEANED_DIRNAME, "tle2099", ".cleaned.txt")
        lines = [line.decode("ascii") for line in reader.iter_lines()]
        assert tle.validate_line(lines[0], 1) == []
        assert tle.validate_line(lines[1], 2) == []
