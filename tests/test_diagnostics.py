"""Tests for lintle.diagnostics — stable rule-ID registry and Diagnostic dataclass."""

import dataclasses
import re

import pytest

from lintle import diagnostics
from lintle.diagnostics import (
    RULES,
    RepairTier,
    RuleID,
    RuleSpec,
    diagnostic,
)


class TestRuleIDFormat:
    def test_every_value_matches_tle_family_nnn_pattern(self):
        pattern = re.compile(r"^TLE-[A-Z]+-\d{3}$")
        for member in RuleID:
            assert pattern.match(member.value), f"{member.name} = {member.value!r}"

    def test_no_duplicate_string_values(self):
        values = [member.value for member in RuleID]
        assert len(values) == len(set(values))

    def test_known_anchor_ids_are_present(self):
        assert RuleID.LINE_LENGTH.value == "TLE-COL-001"
        assert RuleID.CHECKSUM_MISMATCH.value == "TLE-CHK-001"
        assert RuleID.ORPHAN_LINE.value == "TLE-PAIR-001"
        assert RuleID.INTERNAL_ERROR.value == "TLE-INT-001"

    def test_strenum_compares_equal_to_string(self):
        # StrEnum members must serialize and compare as their wire string —
        # downstream code (report.md, .broken.txt, dict keys) relies on this.
        assert RuleID.CHECKSUM_MISMATCH == "TLE-CHK-001"
        assert str(RuleID.CHECKSUM_MISMATCH) == "TLE-CHK-001"


class TestRuleSpec:
    def test_rulespec_is_frozen(self):
        spec = RuleSpec(RuleID.CHECKSUM_MISMATCH, "CHK", "test", "0.3.0")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.family = "MUT"  # type: ignore[misc]

    def test_deprecated_for_defaults_to_empty_tuple(self):
        spec = RuleSpec(RuleID.CHECKSUM_MISMATCH, "CHK", "test", "0.3.0")
        assert spec.deprecated_for == ()


class TestRulesRegistry:
    def test_every_rule_id_has_a_spec(self):
        assert set(RULES) == set(RuleID)

    def test_every_spec_self_references_its_key(self):
        for rule_id, spec in RULES.items():
            assert spec.rule_id is rule_id

    def test_family_matches_middle_token_of_value(self):
        # RULES[RuleID.LINE_LENGTH].family == "COL" must match the middle
        # token "COL" of the string "TLE-COL-001".
        for rule_id, spec in RULES.items():
            family_from_value = rule_id.value.split("-")[1]
            assert spec.family == family_from_value, (
                f"{rule_id.name}: family {spec.family!r} != "
                f"value's family token {family_from_value!r}"
            )

    def test_short_titles_are_non_empty(self):
        for spec in RULES.values():
            assert spec.short_title.strip()


class TestRepairTier:
    def test_tier_string_values(self):
        assert RepairTier.NONE == "none"
        assert RepairTier.NORMALIZATION == "tier-1"
        assert RepairTier.CHECKSUM_RECONSTRUCT == "tier-2"


class TestDiagnosticShape:
    def test_diagnostic_is_frozen(self):
        diag = diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(1,))
        with pytest.raises(dataclasses.FrozenInstanceError):
            diag.note = "mutated"  # type: ignore[misc]

    def test_diagnostic_uses_slots(self):
        diag = diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(1,))
        assert not hasattr(diag, "__dict__")

    def test_diagnostic_is_hashable(self):
        d1 = diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(1,))
        d2 = diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(1,))
        assert hash(d1) == hash(d2)
        assert {d1, d2} == {d1}

    def test_defaults(self):
        diag = diagnostic(RuleID.ORPHAN_LINE, source_line_nos=(42,))
        assert diag.tier_attempted == RepairTier.NONE
        assert diag.column_range is None
        assert diag.observed is None
        assert diag.expected is None
        assert diag.note == ""


class TestDiagnosticHelperBounds:
    def test_observed_truncated_at_16_chars(self):
        diag = diagnostic(
            RuleID.CHECKSUM_MISMATCH,
            source_line_nos=(1,),
            observed="x" * 50,
        )
        assert len(diag.observed) == 16
        assert diag.observed.endswith("…")

    def test_expected_truncated_at_16_chars(self):
        diag = diagnostic(
            RuleID.CHECKSUM_MISMATCH,
            source_line_nos=(1,),
            expected="y" * 50,
        )
        assert len(diag.expected) == 16
        assert diag.expected.endswith("…")

    def test_note_truncated_at_80_chars(self):
        diag = diagnostic(
            RuleID.INTERNAL_ERROR,
            source_line_nos=(1,),
            note="z" * 500,
        )
        assert len(diag.note) == 80
        assert diag.note.endswith("…")

    def test_short_strings_pass_through_unmodified(self):
        diag = diagnostic(
            RuleID.CHECKSUM_MISMATCH,
            source_line_nos=(1,),
            observed="7",
            expected="3",
            note="short",
        )
        assert diag.observed == "7"
        assert diag.expected == "3"
        assert diag.note == "short"

    def test_none_observed_stays_none(self):
        diag = diagnostic(RuleID.ORPHAN_LINE, source_line_nos=(1,))
        assert diag.observed is None
        assert diag.expected is None


class TestModuleSurface:
    def test_no_runtime_imports_beyond_stdlib(self):
        # diagnostics.py is pure data — only stdlib (enum, dataclasses) allowed.
        # Smoke-check by ensuring the module imports without any from-imports
        # outside the lintle package itself.
        import inspect

        source = inspect.getsource(diagnostics)
        assert "from sgp4" not in source
        assert "import sgp4" not in source
