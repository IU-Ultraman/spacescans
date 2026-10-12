"""CSV uploads keyed by a Census geography: parsing, preview and validation.

Pure standard library, so it works with the base install. Upload is the only
place where a bad geography key can be caught cheaply; every rule here stands
for a defect that would otherwise surface as an empty join twenty minutes into
a run, or not at all.

Ported verbatim from spacescans-web ``app/custom_exposomes.py`` (validation
section): ``preview`` is :func:`preview_csv`, ``validate`` is
:func:`validate_csv`.
"""
from __future__ import annotations

import csv
import io
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Union

from spacescans.custom.errors import CustomExposomeError

# Boundary layer -> join column in the C3 weights table, expected key width.
# The widths are what makes a GEOID10 string vs a numeric fips detectable at
# upload time instead of as an empty join twenty minutes into a run.
BOUNDARY_SPEC: dict[str, dict[str, Any]] = {
    "Tract": {"join_col": "GEOID10", "key_len": 11, "label": "Census Tract (2010)"},
    "BG": {"join_col": "GEOID10", "key_len": 12, "label": "Block Group (2010)"},
    "ZCTA5": {"join_col": "ZCTA5CE10", "key_len": 5, "label": "ZIP Code Tabulation Area (2010)"},
    "County": {"join_col": "GEOID10", "key_len": 5, "label": "County (2010)"},
}

MAX_VALUE_COLS = 40
_NON_NUMERIC_TOLERANCE = 0.5      # reject a value column that is >50% unparseable
_PREVIEW_ROWS = 5
_MISSING_TOKENS = {"NA", "N/A", "NULL", "NAN", "."}

#: What the CSV functions accept: the file's bytes, or a path to the file.
Source = Union[bytes, bytearray, memoryview, str, "os.PathLike[str]"]


def read_source(source: Source) -> bytes:
    """The bytes of an upload given as bytes or as a path (str or PathLike)."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if isinstance(source, (str, os.PathLike)):
        path = Path(os.path.expanduser(os.fspath(source)))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CustomExposomeError(f"cannot read {path}: {exc.strerror or exc}") from exc
    raise TypeError(f"expected bytes or a path, got {type(source).__name__}")


def _normalise_header(name: str) -> str:
    """Trim and strip a UTF-8 BOM; otherwise leave the author's spelling."""
    return name.strip().lstrip("\ufeff")


def parse_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    """Header and non-blank rows of a CSV upload (UTF-8, else Latin-1)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except Exception as exc:  # pragma: no cover - latin-1 decodes anything
            raise CustomExposomeError(f"file is not readable text: {exc}") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CustomExposomeError("file is empty") from None
    header = [_normalise_header(h) for h in header]
    if not any(header):
        raise CustomExposomeError("first line has no column names")
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise CustomExposomeError("file has a header but no data rows")
    return header, rows


def _column(header: list[str], rows: list[list[str]], name: str) -> list[str]:
    idx = header.index(name)
    return [(r[idx].strip() if idx < len(r) else "") for r in rows]


def _numeric_failures(values: list[str]) -> int:
    bad = 0
    for v in values:
        if v == "" or v.upper() in _MISSING_TOKENS:
            continue           # blanks are missing data, not a parse failure
        try:
            float(v)
        except ValueError:
            bad += 1
    return bad


def preview_csv(source: Source) -> dict[str, Any]:
    """Columns, sample rows and a numeric-looking flag, for the mapping UI.

    ``source`` is the upload's bytes or a path to the CSV.
    """
    header, rows = parse_csv(read_source(source))
    columns = []
    for name in header:
        if not name:
            continue
        values = _column(header, rows, name)
        non_empty = [v for v in values if v != ""]
        numeric = bool(non_empty) and _numeric_failures(values) == 0
        col: dict[str, Any] = {
            "name": name,
            "numeric": numeric,
            # A few distinct values so a text column is recognisable at a glance.
            "distinct_sample": sorted({v for v in non_empty[:200]})[:5],
        }
        if numeric:
            # The observed range is what tells a user which numeric column is
            # which (0.30-0.40 is an index; 80-85 is a temperature). Parsed as
            # numbers: a string sort would put "10" before "9".
            parsed = [float(v) for v in non_empty if v.upper() not in _MISSING_TOKENS]
            if parsed:
                col["range"] = [min(parsed), max(parsed)]
        columns.append(col)
    return {
        "columns": columns,
        "row_count": len(rows),
        "sample_rows": [dict(zip(header, r)) for r in rows[:_PREVIEW_ROWS]],
    }


def validate_csv(
    source: Source,
    *,
    boundary: str,
    key_col: str,
    value_cols: Sequence[str],
    year_col: str | None = None,
) -> dict[str, Any]:
    """Check an upload against the boundary's contract.

    Returns the derived facts a definition needs: ``row_count``,
    ``coverage_years`` (``[0, 0]`` without a year column) and
    ``distinct_keys``. Raises :class:`CustomExposomeError` with a message meant
    for the user.

    What this cannot check is set membership against the real national key
    universe; that would mean loading fifty tract shapefiles per preview. The
    key-width check catches the class of defect that matters (a numeric fips
    against a zero-padded GEOID10 string); the true linkage rate is reported
    after the first run (:func:`spacescans.custom.diagnostics`).
    """
    if boundary not in BOUNDARY_SPEC:
        raise CustomExposomeError(
            f"unknown boundary {boundary!r}; expected one of {sorted(BOUNDARY_SPEC)}"
        )
    if not value_cols:
        raise CustomExposomeError("select at least one value column")
    if len(value_cols) > MAX_VALUE_COLS:
        raise CustomExposomeError(
            f"{len(value_cols)} value columns exceeds the limit of {MAX_VALUE_COLS}"
        )

    header, rows = parse_csv(read_source(source))
    missing = [c for c in [key_col, *value_cols, *([year_col] if year_col else [])]
               if c not in header]
    if missing:
        raise CustomExposomeError(
            f"column(s) not in the file: {', '.join(missing)}. "
            f"The file has: {', '.join(h for h in header if h)}"
        )
    overlap = set(value_cols) & ({key_col} | ({year_col} if year_col else set()))
    if overlap:
        raise CustomExposomeError(
            f"column(s) used twice: {', '.join(sorted(overlap))}"
        )

    # --- geography key ---
    keys = _column(header, rows, key_col)
    blanks = sum(1 for k in keys if k == "")
    if blanks:
        raise CustomExposomeError(
            f"{blanks:,} of {len(keys):,} rows have no value in {key_col!r}"
        )
    expected_len = BOUNDARY_SPEC[boundary]["key_len"]
    wrong_width = [k for k in keys if not (k.isdigit() and len(k) == expected_len)]
    if wrong_width:
        sample = ", ".join(repr(k) for k in wrong_width[:3])
        hint = ""
        if any(k.isdigit() and len(k) == expected_len - 1 for k in wrong_width):
            hint = (
                f" Values one digit short usually mean a leading zero was lost by a "
                f"spreadsheet; {boundary} keys are zero-padded to {expected_len} digits."
            )
        raise CustomExposomeError(
            f"{len(wrong_width):,} of {len(keys):,} values in {key_col!r} are not "
            f"{expected_len}-digit {boundary} codes (e.g. {sample}).{hint}"
        )

    # --- year ---
    years: list[int] = []
    if year_col:
        raw_years = _column(header, rows, year_col)
        bad_years = []
        for y in raw_years:
            try:
                year = int(float(y))
            except ValueError:
                bad_years.append(y)
                continue
            if not 1900 <= year <= 2100:
                bad_years.append(y)
            else:
                years.append(year)
        if bad_years:
            raise CustomExposomeError(
                f"{len(bad_years):,} rows have an unusable year in {year_col!r} "
                f"(e.g. {', '.join(repr(y) for y in bad_years[:3])})"
            )

    # --- duplicate keys: they would multiply rows in the C4 join ---
    if year_col:
        seen_pairs: list[Any] = list(zip(keys, years))
        dup_label = f"({key_col}, {year_col})"
    else:
        seen_pairs = keys
        dup_label = key_col
    if len(set(seen_pairs)) != len(seen_pairs):
        dupes = len(seen_pairs) - len(set(seen_pairs))
        raise CustomExposomeError(
            f"{dupes:,} duplicate {dup_label} rows. Each geography "
            f"{'and year ' if year_col else ''}must appear once."
        )

    # --- value columns ---
    non_numeric: dict[str, int] = {}
    all_blank = []
    for col in value_cols:
        values = _column(header, rows, col)
        if all(v == "" for v in values):
            all_blank.append(col)
            continue
        bad = _numeric_failures(values)
        if bad / max(1, len(values)) > _NON_NUMERIC_TOLERANCE:
            non_numeric[col] = bad
    if all_blank:
        raise CustomExposomeError(
            f"value column(s) with no data at all: {', '.join(all_blank)}"
        )
    if non_numeric:
        detail = ", ".join(f"{c} ({n:,} rows)" for c, n in non_numeric.items())
        raise CustomExposomeError(
            f"value column(s) mostly non-numeric: {detail}. "
            "Exposure values must be numbers."
        )

    coverage = [min(years), max(years)] if years else [0, 0]
    return {
        "row_count": len(rows),
        "coverage_years": coverage,
        "distinct_keys": len(set(keys)),
    }
