"""End-to-end pipeline tests: golden output, idempotence, re-validation."""

from lintle import pipeline, tle
from lintle.categories import FixClass


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

        stats = pipeline.process_file(str(src), str(out), "clean")

        assert stats.paired_records == 3
        assert stats.orphan_entries == 0
        assert stats.input_lines_seen == 6
        assert stats.clean_count == 2
        assert stats.quarantined_count == 1
        assert stats.fix_counts.get(FixClass.RECONSTRUCTED_CHECKSUM) == 2
        assert stats.fix_counts.get(FixClass.TRAILING_BACKSLASH) == 1

        cleaned = (out / "cleaned" / "tle2099.cleaned.txt").read_bytes()
        assert cleaned == (
            line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
        ).encode("ascii")
        broken = (out / "broken" / "tle2099.broken.txt").read_bytes()
        assert b"TLE-CHK-001" in broken
        assert bad_line1.encode("ascii") in broken

    def test_clean_is_idempotent(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))

        out1 = tmp_path / "out1"
        pipeline.process_file(str(src), str(out1), "clean")
        cleaned1 = out1 / "cleaned" / "tle2099.cleaned.txt"

        # Re-clean the cleaned output. stem("tle2099.cleaned.txt") == "tle2099.cleaned".
        out2 = tmp_path / "out2"
        stats2 = pipeline.process_file(str(cleaned1), str(out2), "clean")
        cleaned2 = out2 / "cleaned" / "tle2099.cleaned.cleaned.txt"

        assert cleaned1.read_bytes() == cleaned2.read_bytes()
        # Idempotence (spec §8): re-cleaning applies zero fixes and zero quarantines.
        assert stats2.fix_counts == {}
        assert stats2.quarantined_count == 0

    def test_cleaned_output_revalidates_as_perfect(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean")

        stats = pipeline.process_file(
            str(out / "cleaned" / "tle2099.cleaned.txt"),
            str(tmp_path / "verify"),
            "validate",
        )
        assert stats.clean_count == 1
        assert stats.quarantined_count == 0

    def test_every_cleaned_line_passes_validate_line(self, tmp_path, line1, line2):
        src = tmp_path / "tle2099.txt"
        src.write_bytes((line1[:68] + "\n" + line2[:68] + "\n").encode("ascii"))
        out = tmp_path / "out"
        pipeline.process_file(str(src), str(out), "clean")

        lines = (out / "cleaned" / "tle2099.cleaned.txt").read_text().splitlines()
        assert tle.validate_line(lines[0], 1) == []
        assert tle.validate_line(lines[1], 2) == []
