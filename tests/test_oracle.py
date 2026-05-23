"""Asymmetric oracle check: a known-good TLE is accepted by both our
validator and the trusted `sgp4` parser. Disagreement on a *bad* TLE is
expected (sgp4 is permissive), so only acceptance is cross-checked.
"""

from sgp4.api import Satrec

from lintle import tle


class TestOracle:
    def test_canonical_tle_accepted_by_both(self, line1, line2):
        assert tle.validate_record(line1, line2) == []

        sat = Satrec.twoline2rv(line1, line2)
        assert sat.error == 0  # sgp4 reports no parse/initialisation error
