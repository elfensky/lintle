"""Tests for the FixClass repair-tag metadata registry."""

from lintle.categories import FIXES, FixClass, FixSpec


class TestFixRegistry:
    """``FIXES`` gives every repair tag a canonical one-line definition,
    mirroring ``RULES``/``RuleSpec`` on the quarantine side.
    """

    def test_every_fixclass_has_a_spec(self):
        assert set(FIXES) == set(FixClass)

    def test_spec_keys_match_their_fix_class(self):
        for fix_class, spec in FIXES.items():
            assert isinstance(spec, FixSpec)
            assert spec.fix_class is fix_class

    def test_specs_have_a_nonempty_short_title(self):
        for spec in FIXES.values():
            assert spec.short_title.strip()

    def test_specs_record_an_introduced_version(self):
        for spec in FIXES.values():
            assert spec.introduced
