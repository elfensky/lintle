"""Shared pytest fixtures: a canonical, known-good TLE record."""

import pytest

CANONICAL_LINE1 = (
    "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"  # noqa: E501 — a TLE line is a fixed 69-column record
)
CANONICAL_LINE2 = (
    "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"  # noqa: E501 — a TLE line is a fixed 69-column record
)

# Fail loudly at collection time if either constant was mistranscribed.
assert len(CANONICAL_LINE1) == 69, len(CANONICAL_LINE1)
assert len(CANONICAL_LINE2) == 69, len(CANONICAL_LINE2)


@pytest.fixture
def line1():
    """A valid 69-character TLE line 1."""
    return CANONICAL_LINE1


@pytest.fixture
def line2():
    """A valid 69-character TLE line 2."""
    return CANONICAL_LINE2
