"""Pre-run coverage: how many cohort episodes each variable can cover.

Port of spacescans-web ``task_manager.compute_coverage`` (lines 61-195,
commit 3991c3b) without the task directory: the caller passes the cohort
(a table or a CSV path) and the variables' catalog entries. The returned
dict has the web's shape and values, so ``/api/tasks/{id}/coverage`` can
serve it unchanged.

An episode is in the time window when it overlaps the variable's
``coverage_years`` (static variables skip this check), and in the region
when the variable's ``coverage_region`` is ``"CONUS"`` and the point lies
in the box lon -125..-66, lat 24..50 (any other region counts every point
as inside). The keys say ``patients_*`` as in the web, but they count
episodes (cohort rows), not distinct patients.

This is an estimate from the catalog's years and a bounding box. The
years a template computes can be narrower than the catalog's (NDI: catalog
2012-2022, computed 2013-2019), and the box includes some non-US land.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Union

import pandas as pd

from spacescans import cohort as _cohort

__all__ = [
    "CONUS_BBOX",
    "OUTSIDE_WARN_PCT",
    "compute_coverage",
    "read_coverage_csv",
]

#: (lon_min, lat_min, lon_max, lat_max) used for ``coverage_region == "CONUS"``.
CONUS_BBOX: tuple[float, float, float, float] = (-125, 24, -66, 50)
#: A warning is added when more than this percentage of rows fall outside.
OUTSIDE_WARN_PCT = 5

# compute_coverage reads input.csv with these four columns as text (no
# zcta5, unlike csv_to_parquet's zcta5 variant); they are not used.
_FIPS_DTYPES = {"state_fips": "string", "county_fips": "string",
                "tract_geoid": "string", "bg_geoid": "string"}

PathLike = Union[str, "os.PathLike[str]"]


def read_coverage_csv(path: PathLike) -> pd.DataFrame:
    """Read a cohort CSV as the web's coverage endpoint does, dates parsed.

    Raises FileNotFoundError("No input uploaded") when the file is absent,
    and CohortError (a ValueError) when the dates match no accepted format.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("No input uploaded")
    df = pd.read_csv(path, dtype=_FIPS_DTYPES)
    _cohort.parse_date_columns(df)
    return df


def _meta(entry: Any) -> Mapping[str, Any]:
    """A catalog entry as a mapping; accepts objects carrying ``.raw``."""
    raw = getattr(entry, "raw", None)
    return raw if raw is not None else entry


def _dated(df: pd.DataFrame) -> pd.DataFrame:
    """`df` with startDate/endDate as naive datetime64, parsing a copy if needed.

    Timezone-aware columns are refused with CohortError.
    """
    if all(pd.api.types.is_datetime64_dtype(df[c].dtype)
           for c in _cohort.DATE_COLUMNS if c in df.columns):
        return df
    df = df.copy()
    _cohort._coerce_dates(df)
    return df


def compute_coverage(
    cohort: pd.DataFrame | PathLike,
    variable_keys: Sequence[str],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-variable coverage of a cohort, in the web's response shape.

    `catalog` maps variable keys to catalog entries (the dicts of
    variables.json plus any custom overlay, or objects with a ``.raw``
    dict). A key missing from it raises KeyError whose first argument is the
    comma-separated list of unknown keys. A CSV path is read with
    :func:`read_coverage_csv`; a table's date columns are parsed (on a copy)
    unless they are already datetime64.

    Returns::

        {"row_count": int,
         "variables": {key: {"coverage_years": [y0, y1],
                             "patients_in_time_window": int,
                             "patients_in_region": int,
                             "patients_covered": int,
                             "coverage_pct": float (2 dp),
                             "warnings": [str, ...],
                             "boundary": str, "display_unit": str,
                             "temporal": str}}}
    """
    is_path = not isinstance(cohort, pd.DataFrame)
    if is_path and not Path(cohort).exists():
        raise FileNotFoundError("No input uploaded")

    resolved: dict[str, Mapping[str, Any]] = {}
    unknown: list[str] = []
    for var in variable_keys:
        try:
            resolved[var] = _meta(catalog[var])
        except KeyError:
            unknown.append(var)
    if unknown:
        raise KeyError(", ".join(unknown))

    df = read_coverage_csv(cohort) if is_path else _dated(cohort)
    n_total = len(df)

    if n_total == 0:
        return {
            "row_count": 0,
            "variables": {
                var: {
                    "coverage_years": list(resolved[var]["coverage_years"]),
                    "patients_in_time_window": 0,
                    "patients_in_region": 0,
                    "patients_covered": 0,
                    "coverage_pct": 0.0,
                    "warnings": ["Cohort is empty — no patients to evaluate"],
                    "boundary": resolved[var]["boundary"],
                    "display_unit": resolved[var]["display_unit"],
                    "temporal": resolved[var].get("temporal", "yearly"),
                }
                for var in variable_keys
            },
        }

    lon_min, lat_min, lon_max, lat_max = CONUS_BBOX
    out_vars: dict[str, dict[str, Any]] = {}
    for var in variable_keys:
        m = resolved[var]
        y0, y1 = m["coverage_years"]
        # Static products have no temporal dimension; skip the time gate.
        temporal = m.get("temporal", "yearly")
        is_static = temporal == "static"
        if is_static:
            in_time = pd.Series(True, index=df.index)
        else:
            cov_start = pd.Timestamp(f"{y0}-01-01")
            cov_end = pd.Timestamp(f"{y1}-12-31")
            in_time = (df["startDate"] <= cov_end) & (df["endDate"] >= cov_start)
        if m.get("coverage_region") == "CONUS":
            in_region = (
                df["longitude"].between(lon_min, lon_max)
                & df["latitude"].between(lat_min, lat_max)
            )
        else:
            in_region = pd.Series(True, index=df.index)
        covered = in_time & in_region

        warnings: list[str] = []
        if not is_static:
            time_out_pct = (~in_time).sum() / n_total * 100
            if time_out_pct > OUTSIDE_WARN_PCT:
                warnings.append(
                    f"{time_out_pct:.0f}% of patients have episodes entirely outside "
                    f"{y0}-{y1}"
                )
        region_out_pct = (~in_region).sum() / n_total * 100
        if region_out_pct > OUTSIDE_WARN_PCT:
            warnings.append(
                f"{region_out_pct:.0f}% of patients fall outside the "
                f"{m['coverage_region']} coverage region"
            )

        out_vars[var] = {
            "coverage_years": [y0, y1],
            "patients_in_time_window": int(in_time.sum()),
            "patients_in_region": int(in_region.sum()),
            "patients_covered": int(covered.sum()),
            "coverage_pct": round(100 * covered.sum() / n_total, 2),
            "warnings": warnings,
            "boundary": m["boundary"],
            "display_unit": m["display_unit"],
            "temporal": temporal,
        }

    return {"row_count": n_total, "variables": out_vars}
