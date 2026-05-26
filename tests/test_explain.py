"""Tests for ``lintle explain`` — the self-documenting validator.

The keystone of this feature is that every example shown to an operator is
the *same object* a test feeds to the live ``tle.py`` / ``repair.py``. Good
examples must actually pass; bad examples must actually fail with exactly the
cited rule; before/after fix pairs must actually round-trip through the
repairer producing the cited tag. That coupling is what stops the docs from
silently drifting away from validator behaviour.
"""

import importlib

import pytest

from lintle import explain, pipeline, repair, tle
from lintle.categories import FixClass
from lintle.diagnostics import RULES, RepairTier, RuleID
from lintle.explain_examples import (
    FIX_EXPLAIN,
    RULE_EXPLAIN,
    VerifyKind,
)


def _classify_line(line, lineno):
    """Route a single line through the repairer and return the rule it fired,
    or ``None`` if it was repaired/accepted. Uses ``latin-1`` so a non-ASCII
    example survives as a non-ASCII byte rather than raising on encode.
    """
    _, _, diag = repair.repair_line(line.encode("latin-1"), lineno, 1)
    return diag.rule_id if diag else None


class TestRuleExplainCoverage:
    """Every rejection rule is documented; entries are self-consistent."""

    def test_every_ruleid_has_an_entry(self):
        assert set(RULE_EXPLAIN) == set(RuleID)

    def test_entry_keys_match_their_rule_id(self):
        for rule_id, entry in RULE_EXPLAIN.items():
            assert entry.rule_id is rule_id


class TestRuleExamplesMatchValidator:
    """The keystone for the rejection vocabulary."""

    def test_good_line_examples_pass_validation(self):
        for entry in RULE_EXPLAIN.values():
            if entry.verify is VerifyKind.LINE and entry.good_lines:
                (good,) = entry.good_lines
                assert tle.validate_line(good, entry.lineno) == [], entry.rule_id

    def test_bad_line_examples_fail_with_exactly_this_rule(self):
        for entry in RULE_EXPLAIN.values():
            if entry.verify is VerifyKind.LINE:
                (bad,) = entry.bad_lines
                assert _classify_line(bad, entry.lineno) is entry.rule_id

    def test_record_examples_reject_with_this_rule(self):
        for entry in RULE_EXPLAIN.values():
            if entry.verify is VerifyKind.RECORD:
                good1, good2 = entry.good_lines
                assert isinstance(
                    repair.process_record(good1.encode(), 1, good2.encode(), 2),
                    repair.Accepted,
                )
                bad1, bad2 = entry.bad_lines
                result = repair.process_record(bad1.encode(), 1, bad2.encode(), 2)
                assert isinstance(result, repair.Rejected)
                assert result.primary.rule_id is entry.rule_id

    def test_pairing_examples_orphan_with_this_rule(self, tmp_path):
        for entry in RULE_EXPLAIN.values():
            if entry.verify is VerifyKind.PAIRING:
                src = tmp_path / f"{entry.rule_id}.txt"
                src.write_bytes(("\n".join(entry.bad_lines) + "\n").encode("latin-1"))
                fired = {
                    item.diagnostic.rule_id
                    for item in pipeline.iter_records(str(src))
                    if isinstance(item, pipeline.Orphan)
                }
                assert entry.rule_id in fired

    def test_column_ranges_are_in_bounds(self):
        for entry in RULE_EXPLAIN.values():
            if entry.column_range is not None:
                low, high = entry.column_range
                assert 1 <= low <= high <= tle.LINE_LENGTH


class TestFixExplainCoverage:
    """Every repair tag is documented; entries are self-consistent."""

    def test_every_fixclass_has_an_entry(self):
        assert set(FIX_EXPLAIN) == set(FixClass)

    def test_entry_keys_match_their_fix_class(self):
        for fix_class, entry in FIX_EXPLAIN.items():
            assert entry.fix_class is fix_class


class TestFixExamplesMatchRepairer:
    """The keystone for the repair vocabulary."""

    def test_before_repairs_to_after_producing_this_fix(self):
        for entry in FIX_EXPLAIN.values():
            clean, fixes, diag = repair.repair_line(
                entry.before.encode("latin-1"), entry.lineno, 1
            )
            assert diag is None, entry.fix_class
            assert clean == entry.after
            assert entry.fix_class in fixes

    def test_after_example_is_valid(self):
        for entry in FIX_EXPLAIN.values():
            assert tle.validate_line(entry.after, entry.lineno) == []

    def test_tier_matches_the_known_mapping(self):
        for entry in FIX_EXPLAIN.values():
            expected = (
                RepairTier.CHECKSUM_RECONSTRUCT
                if entry.fix_class is FixClass.RECONSTRUCTED_CHECKSUM
                else RepairTier.NORMALIZATION
            )
            assert entry.tier is expected


class TestCitationsResolve:
    """Every citation is a live ``module.symbol`` — rots loudly, never silently."""

    def _resolve(self, citation):
        mod_name, _, attr = citation.partition(".")
        module = importlib.import_module(f"lintle.{mod_name}")
        return hasattr(module, attr)

    def test_rule_citations_resolve(self):
        for entry in RULE_EXPLAIN.values():
            assert self._resolve(entry.citation), entry.citation

    def test_fix_citations_resolve(self):
        for entry in FIX_EXPLAIN.values():
            assert self._resolve(entry.citation), entry.citation


class TestRenderRule:
    """``render`` of a rejection rule surfaces every required element."""

    def test_includes_id_definition_examples_and_citation(self):
        entry = RULE_EXPLAIN[RuleID.CHECKSUM_MISMATCH]
        out = explain.render("TLE-CHK-001")
        assert "TLE-CHK-001" in out
        assert RULES[RuleID.CHECKSUM_MISMATCH].short_title in out
        assert entry.bad_lines[0] in out
        assert entry.good_lines[0] in out
        assert entry.citation in out

    def test_accepts_member_name_alias(self):
        assert explain.render("CHECKSUM_MISMATCH") == explain.render("TLE-CHK-001")

    def test_marks_the_failing_column(self):
        assert "^" in explain.render("TLE-CHK-001")

    def test_renders_internal_error_without_examples(self):
        out = explain.render("TLE-INT-001")
        assert "TLE-INT-001" in out
        assert RULES[RuleID.INTERNAL_ERROR].short_title in out


class TestRenderFix:
    """``render`` of a repair tag surfaces examples, tier, and safety note."""

    def test_includes_tag_examples_tier_safety_and_citation(self):
        entry = FIX_EXPLAIN[FixClass.RECONSTRUCTED_CHECKSUM]
        out = explain.render("reconstructed-checksum")
        assert "reconstructed-checksum" in out
        assert entry.before in out
        assert entry.after in out
        assert "tier-2" in out
        assert entry.citation in out
        assert "deterministic" in out.lower()

    def test_accepts_member_name_alias(self):
        assert explain.render("RECONSTRUCTED_CHECKSUM") == explain.render(
            "reconstructed-checksum"
        )


class TestRenderUnknownTag:
    def test_unknown_tag_raises(self):
        with pytest.raises(explain.UnknownTag):
            explain.render("NOT-A-REAL-TAG")

    def test_known_tags_lists_every_vocabulary_member(self):
        tags = explain.known_tags()
        assert "TLE-CHK-001" in tags
        assert "reconstructed-checksum" in tags
        assert len(tags) == len(RuleID) + len(FixClass)
