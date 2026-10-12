"""Units of uploaded value columns.

Units are per value column: one dataset can carry a greenness index next to a
temperature, so a single dataset-level unit cannot be right. The catalog still
has exactly one ``display_unit`` per variable (printable ASCII, at most 50
characters), so that field is DERIVED: the distinct units in column order,
joined with " / " (which is how the shipped FAQSD entry already reads:
"ppb / ug/m3").

Ported verbatim from spacescans-web ``app/custom_exposomes.py``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

MAX_UNIT = 50
DEFAULT_UNIT = "unitless"

# Symbols that appear in units and have a lossless ASCII spelling. Dropping
# them instead would turn "µg/m³" into "g/m3", a different unit.
_UNIT_ASCII = str.maketrans({
    "\u00b5": "u", "\u03bc": "u",          # micro sign, Greek mu
    "\u00b0": "deg",                        # degree
    "\u00b2": "2", "\u00b3": "3",          # superscripts
    "\u2212": "-", "\u2013": "-", "\u2014": "-",
    "\u00d7": "x", "\u00b7": "*",
})


def clean_unit(raw: str) -> str:
    """Printable ASCII, non-empty: what the catalog schema allows for the one
    dataset-level ``display_unit``. Per-column units are stored as typed.

    Known symbols are transliterated, anything else non-ASCII is dropped, and a
    blank result becomes "unitless" rather than the empty string the schema
    forbids.
    """
    text = (raw or "").strip().translate(_UNIT_ASCII)
    kept = "".join(ch for ch in text if "\x20" <= ch <= "\x7e")
    return kept[:MAX_UNIT].strip() or DEFAULT_UNIT


def unit_as_typed(raw: str) -> str:
    """A per-column unit: trimmed and capped, otherwise the author's own."""
    return (raw or "").strip()[:MAX_UNIT]


def summarise_units(value_cols: Sequence[str], units: Mapping[str, str]) -> str:
    """The one catalog-level unit string, derived from the per-column ones.

    Distinct units in column order, " / "-joined, capped at the schema's 50
    characters; no units at all -> "unitless".

    Note (kept for parity with the web): a summary longer than 50 characters is
    cut to 49 and ends in U+2026, which is not printable ASCII, so such an
    entry does not satisfy the catalog's ``display_unit`` pattern.
    """
    seen: list[str] = []
    for col in value_cols:
        raw = (units.get(col) or "").strip()
        if not raw:
            continue
        cleaned = clean_unit(raw)
        if cleaned not in seen:
            seen.append(cleaned)
    if not seen:
        return DEFAULT_UNIT
    joined = " / ".join(seen)
    return joined if len(joined) <= MAX_UNIT else (joined[: MAX_UNIT - 1].rstrip() + "…")


def selected_labels(labels: Mapping[str, str] | None, value_cols: Sequence[str]) -> dict[str, str]:
    """Labels for the selected columns only; blank labels are dropped."""
    return {c: v.strip() for c, v in (labels or {}).items()
            if c in value_cols and v and v.strip()}


def selected_units(units: Mapping[str, str] | None, value_cols: Sequence[str]) -> dict[str, str]:
    """Per-column units for the selected columns only, kept as typed."""
    return {c: unit_as_typed(v) for c, v in (units or {}).items()
            if c in value_cols and v and v.strip()}
