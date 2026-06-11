"""Render human-readable documentation for one quarantine rule or repair tag.

``lintle explain <TAG>`` resolves a ``RuleID`` or ``FixClass`` — by canonical
value (``TLE-CHK-001``, ``reconstructed-checksum``) or member-name alias — and
prints its definition, a verified good/bad (or before/after) example with the
relevant column marked, the repair-tier linkage, a safety note, and a
source-of-truth citation. Definitions are pulled from ``RULES`` / ``FIXES`` so
this module never writes a second, divergent description; examples come from
``explain_examples`` and are test-locked against the live validator. Logic and
formatting only — the coverage and tag-disjointness guards live in
``explain_examples`` (data) so they fire at the earliest import.
"""

from lintle.categories import FIXES, FixClass
from lintle.diagnostics import RULES, RuleID
from lintle.explain_examples import FIX_EXPLAIN, RULE_EXPLAIN, VerifyKind

# Label column shared by every example row, wide enough for "  before:" plus
# a trailing space so the example never butts against its label.
_LABEL_W = 10

_VERIFY_PHRASE = {
    VerifyKind.LINE: "line repair (a single line)",
    VerifyKind.PAIRING: "line pairing (before any repair)",
    VerifyKind.RECORD: "record validation (a paired line 1 + line 2)",
    VerifyKind.NONE: "the cleaner's internal crash-safety net",
}


class UnknownTag(ValueError):
    """Raised when a tag matches neither a RuleID nor a FixClass."""


def known_tags():
    """Return every explainable tag (rule IDs + fix tags), sorted."""
    return sorted([r.value for r in RuleID] + [f.value for f in FixClass])


def render(tag):
    """Return the explain text for ``tag``, or raise :class:`UnknownTag`."""
    rule = _resolve(tag, RuleID, RULE_EXPLAIN)
    if rule is not None:
        return _render_rule(rule)
    fix = _resolve(tag, FixClass, FIX_EXPLAIN)
    if fix is not None:
        return _render_fix(fix)
    raise UnknownTag(tag)


def _resolve(tag, enum_cls, registry):
    """Look up ``tag`` in ``registry`` by enum value or member-name alias."""
    try:
        return registry[enum_cls(tag)]
    except ValueError:
        pass
    if tag in enum_cls.__members__:
        return registry[enum_cls[tag]]
    return None


def _visible(text):
    """Escape control characters so a CR or tab in an example cannot mangle
    the terminal when the line is printed verbatim.
    """
    return text.replace("\r", "\\r").replace("\t", "\\t")


def _row(label, line):
    """One example row with the shared label column (``label`` empty = continuation)."""
    head = f"  {label}:" if label else ""
    return f"{head.ljust(_LABEL_W)}{_visible(line)}"


def _caret(line, column_range):
    """A caret row pointing at ``column_range`` under an example ``line``.

    Aligned to the line's *rendered* width: ``_visible`` escapes a CR or tab
    to two cells, so the offset is computed from the escaped prefix (and the
    caret width from the escaped marked span), not the raw source columns.
    For an all-ASCII line ``_visible`` is the identity, so this is unchanged.
    """
    low, high = column_range
    prefix = _visible(line[: low - 1])
    span = _visible(line[low - 1 : high])
    marker = " " * (_LABEL_W + len(prefix)) + "^" * len(span)
    label = f"column {low}" if low == high else f"columns {low}-{high}"
    return f"{marker}  {label}"


def _example_block(bad_lines, good_lines, column_range):
    """Render the bad block (caret under the first line) then the good block."""
    out = []
    for i, line in enumerate(bad_lines):
        out.append(_row("bad" if i == 0 else "", line))
        if i == 0 and column_range is not None:
            out.append(_caret(line, column_range))
    for i, line in enumerate(good_lines):
        out.append(_row("good" if i == 0 else "", line))
    return out


def _render_rule(entry):
    spec = RULES[entry.rule_id]
    out = [
        f"{entry.rule_id}  (quarantine rule · family {spec.family})",
        f"  {spec.short_title}",
        "",
        f"  Detected during: {_VERIFY_PHRASE[entry.verify]}",
    ]
    if entry.verify is VerifyKind.NONE:
        out.append("  (no reproducible example — see the note below.)")
    else:
        out.append("")
        out.extend(
            _example_block(entry.bad_lines, entry.good_lines, entry.column_range)
        )
    out += [
        "",
        f"  Repair tier: {entry.tier_note}",
        f"  Source of truth: {entry.citation}",
    ]
    return "\n".join(out)


def _render_fix(entry):
    spec = FIXES[entry.fix_class]
    out = [
        f"{entry.fix_class}  (repair tag · {entry.tier})",
        f"  {spec.short_title}",
        "",
        _row("before", entry.before),
        _row("after", entry.after),
        "",
        f"  Safety: {entry.safety_note}",
        f"  Source of truth: {entry.citation}",
    ]
    return "\n".join(out)
