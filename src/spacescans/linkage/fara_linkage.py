"""FARA tract-level linkage — yearly areal + recode binary flags + column selection.

Matches v1 C4_Linkage_TRACT_FARA.py exactly: uses legacy SQL temporal weighting,
applies apply_fara_recode for 13 binary flag columns, selects final columns from
varnameCountRemoved.csv, and fills NaN with 0.
"""
from __future__ import annotations

from spacescans._extras import require
require("rda", "pyreadr")  # FARA reads .Rda

from pathlib import Path
import sqlite3
import numpy as np
import pandas as pd

from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import (
    load_patients,
    load_weights,
    resolve_output_grouping,
)
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.pipeline.registry import register_pattern


# ============================================================================
# Inlined v1 SQL helpers (originally in common/c4_yearly_areal_legacy.py).
# Copied verbatim so we can delete the python-scripts/common/ dependency.
# ============================================================================

def build_year_windows(years) -> pd.DataFrame:
    """Create annual start/end windows for a sorted list of years."""
    years = sorted(int(year) for year in years)
    windows = pd.DataFrame({"Year": years})
    windows["startdate"] = pd.to_datetime(windows["Year"].astype(str) + "-01-01")
    windows["enddate"] = pd.to_datetime(windows["Year"].astype(str) + "-12-31")
    return windows


def build_episode_year_table_legacy(
    vsehr_rh: pd.DataFrame,
    years,
    *,
    patid_col: str = "PATID",
    geoid_col: str = "geoid",
    start_col: str = "start",
    end_col: str = "end",
    cast_geoid_int: bool = False,
    overlap_as_int: bool = False,
) -> pd.DataFrame:
    """Build yearly overlap-day columns while preserving legacy episode layout."""
    dat3 = vsehr_rh[[patid_col, geoid_col, start_col, end_col]].copy().reset_index(drop=True)
    if cast_geoid_int:
        dat3[geoid_col] = dat3[geoid_col].astype(int)

    dat3[start_col] = pd.to_datetime(dat3[start_col])
    dat3[end_col] = pd.to_datetime(dat3[end_col])
    dat3["id"] = np.arange(1, len(dat3) + 1)

    year_windows = build_year_windows(years)
    for index, row in year_windows.iterrows():
        overlap_start = np.maximum(dat3[start_col].values, np.datetime64(row["startdate"]))
        overlap_end = np.minimum(dat3[end_col].values, np.datetime64(row["enddate"]))

        overlap_days = (
            overlap_end.astype("datetime64[D]") - overlap_start.astype("datetime64[D]")
        ).astype(float) + 1
        overlap_days = np.where(overlap_end >= overlap_start, overlap_days, 0)

        if overlap_as_int:
            overlap_days = overlap_days.astype(int)
        dat3[f"t1p{index + 1}"] = overlap_days

    return dat3


def compute_yearly_area_weighted_exposures_sql(
    dat3: pd.DataFrame,
    *,
    buffer_df: pd.DataFrame,
    exposure_df: pd.DataFrame,
    years,
    buffer_table_name: str,
    buffer_join_col: str,
    exposure_join_col: str,
    value_cols,
    exposure_year_col: str = "year",
    geoid_col: str = "geoid",
    weight_col: str = "value",
    merge_how: str = "inner",
) -> pd.DataFrame:
    """Replicate the legacy SQLite yearly area-weighted aggregation workflow."""
    value_cols = list(value_cols)
    con = sqlite3.connect(":memory:")

    try:
        buffer_df.to_sql(buffer_table_name, con, index=False, if_exists="replace")

        dat4 = None
        for year_index, year in enumerate(years, start=1):
            temp = exposure_df[exposure_df[exposure_year_col] == year][
                [exposure_join_col] + value_cols
            ].copy()
            temp.to_sql("temp", con, index=False, if_exists="replace")

            query = (
                f"SELECT {geoid_col}, {buffer_join_col}, {weight_col}, temp.* "
                f"FROM {buffer_table_name} LEFT OUTER JOIN temp "
                f"ON {buffer_table_name}.{buffer_join_col} = temp.{exposure_join_col};"
            )
            temp2 = pd.read_sql(query, con)

            dat2 = dat3.merge(temp2, on=geoid_col, how="left")
            dat2.to_sql("dat2", con, index=False, if_exists="replace")

            select_parts = ", ".join(
                f"SUM({var}*{weight_col})/SUM({weight_col}) AS {var}" for var in value_cols
            )
            query = f"SELECT id, {select_parts} FROM dat2 GROUP BY id;"
            result = pd.read_sql(query, con)
            result = result.rename(columns={var: f"{var}_{year_index}" for var in value_cols})

            if dat4 is None:
                dat4 = result
            else:
                dat4 = dat4.merge(result, on="id", how=merge_how)

        if dat4 is None:
            return pd.DataFrame(columns=["id"])
        return dat4
    finally:
        con.close()


def compute_patient_temporal_weighted_sql(
    dat5: pd.DataFrame,
    *,
    value_cols,
    years_count: int,
    patid_col: str = "PATID",
    overlap_prefix: str = "t1p",
    group_cols: list[str] | None = None,
) -> pd.DataFrame:
    """Replicate the legacy per-variable temporal weighting plus SQL PATID aggregation.

    ``group_cols`` (default ``[patid_col]``) controls the final GROUP BY. The
    Sprint 11 Phase A episode-grouping branch passes ``[patid_col, "geoid"]``
    so the SQL aggregation collapses per (PATID, geoid) rather than per PATID
    only — preserving one row per episode in the output.
    """
    value_cols = list(value_cols)
    if group_cols is None:
        group_cols = [patid_col]
    dat5 = dat5.copy()
    dat5f = None
    con = sqlite3.connect(":memory:")

    try:
        group_select = ", ".join(group_cols)
        for var in value_cols:
            var_1 = pd.to_numeric(dat5[f"{var}_1"], errors="coerce")
            t1_sum = dat5[f"{overlap_prefix}1"] * var_1
            t1_n = dat5[f"{overlap_prefix}1"].copy().astype(float)

            na_mask = var_1.isna()
            t1_sum[na_mask] = 0.0
            t1_n[na_mask] = 0.0

            for year_index in range(2, years_count + 1):
                var_i = pd.to_numeric(dat5[f"{var}_{year_index}"], errors="coerce")
                product = dat5[f"{overlap_prefix}{year_index}"] * var_i
                valid = product.notna()
                t1_sum[valid] = t1_sum[valid] + product[valid]
                t1_n[valid] = t1_n[valid] + dat5.loc[valid, f"{overlap_prefix}{year_index}"]

            t1_n[t1_n == 0] = np.nan

            dat5[f"{var}_sum"] = t1_sum
            dat5[f"{var}_n"] = t1_n
            dat5[var] = t1_sum / t1_n

            dat5.to_sql("dat5", con, index=False, if_exists="replace")
            query = (
                f"SELECT {group_select}, "
                f"SUM({var}_sum)/SUM({var}_n) AS {var} "
                f"FROM dat5 GROUP BY {group_select};"
            )
            result = pd.read_sql(query, con)

            if dat5f is None:
                dat5f = result
            else:
                dat5f = dat5f.merge(result, on=group_cols, how="left")

        if dat5f is None:
            return pd.DataFrame(columns=list(group_cols))
        return dat5f
    finally:
        con.close()


def _apply_fara_recode(dat: pd.DataFrame) -> pd.DataFrame:
    """Replicate the original post-linkage FARA flag recodes (13 binary flags)."""
    dat = dat.copy()

    mask = dat["Urban"].notna()
    dat.loc[mask & (dat["Urban"] >= 0.5), "Urban"] = 1
    dat.loc[mask & (dat["Urban"] < 0.5), "Urban"] = 0

    mask = dat["LALOWI1_10"].notna() & dat["LALOWI1_10share"].notna()
    dat.loc[mask, "LILATracts_1And10"] = 0
    dat.loc[(dat["LALOWI1_10"] >= 500) | (dat["LALOWI1_10share"] >= 0.33), "LILATracts_1And10"] = 1

    mask = dat["LAPOP1_10"].notna() & dat["LAPOP1_10share"].notna()
    dat.loc[mask, "LA1and10"] = 0
    dat.loc[(dat["LAPOP1_10"] >= 500) | (dat["LAPOP1_10share"] >= 0.33), "LA1and10"] = 1

    mask = dat["lalowihalf"].notna() & dat["lalowihalfshare"].notna() & dat["lalowi10"].notna() & dat["lalowi10share"].notna() & dat["Urban"].notna()
    dat.loc[mask, "LILATracts_halfAnd10"] = 0
    dat.loc[((dat["lalowihalf"] >= 500) & (dat["Urban"] == 1)) | ((dat["lalowi10"] >= 500) & (dat["Urban"] == 0)) | ((dat["lalowihalfshare"] >= 0.33) & (dat["Urban"] == 1)) | ((dat["lalowi10share"] >= 0.33) & (dat["Urban"] == 0)), "LILATracts_halfAnd10"] = 1

    mask = dat["lalowi1"].notna() & dat["lalowi1share"].notna() & dat["lalowi20"].notna() & dat["lalowi20share"].notna() & dat["Urban"].notna()
    dat.loc[mask, "LILATracts_1And20"] = 0
    dat.loc[((dat["lalowi1"] >= 500) & (dat["Urban"] == 1)) | ((dat["lalowi20"] >= 500) & (dat["Urban"] == 0)) | ((dat["lalowi1share"] >= 0.33) & (dat["Urban"] == 1)) | ((dat["lalowi20share"] >= 0.33) & (dat["Urban"] == 0)), "LILATracts_1And20"] = 1

    mask = dat["lapophalf"].notna() & dat["lapop10"].notna() & dat["lapop10share"].notna() & dat["lapophalfshare"].notna() & dat["Urban"].notna()
    dat.loc[mask, "LAhalfand10"] = 0
    dat.loc[((dat["lapop10"] >= 500) & (dat["Urban"] == 0)) | ((dat["lapophalf"] >= 500) & (dat["Urban"] == 1)) | ((dat["lapop10share"] >= 0.33) & (dat["Urban"] == 0)) | ((dat["lapophalfshare"] >= 0.33) & (dat["Urban"] == 1)), "LAhalfand10"] = 1

    mask = dat["lapop1"].notna() & dat["lapop20"].notna() & dat["lapop20share"].notna() & dat["lapop1share"].notna() & dat["Urban"].notna()
    dat.loc[mask, "LA1and20"] = 0
    dat.loc[((dat["lapop20"] >= 500) & (dat["Urban"] == 0)) | ((dat["lapop1"] >= 500) & (dat["Urban"] == 1)) | ((dat["lapop20share"] >= 0.33) & (dat["Urban"] == 0)) | ((dat["lapop1share"] >= 0.33) & (dat["Urban"] == 1)), "LA1and20"] = 1

    mask = dat["lapophalf"].notna() & dat["lapophalfshare"].notna()
    dat.loc[mask, "LATracts_half"] = 0
    dat.loc[(dat["lapophalf"] >= 500) | (dat["lapophalfshare"] >= 0.33), "LATracts_half"] = 1

    mask = dat["lapop1"].notna() & dat["lapop1share"].notna()
    dat.loc[mask, "LATracts1"] = 0
    dat.loc[(dat["lapop1"] >= 500) | (dat["lapop1share"] >= 0.33), "LATracts1"] = 1

    mask = dat["lapop10"].notna() & dat["lapop10share"].notna()
    dat.loc[mask, "LATracts10"] = 0
    dat.loc[(dat["lapop10"] >= 500) | (dat["lapop10share"] >= 0.33), "LATracts10"] = 1

    mask = dat["lapop20"].notna() & dat["lapop20share"].notna()
    dat.loc[mask, "LATracts20"] = 0
    dat.loc[(dat["lapop20"] >= 500) | (dat["lapop20share"] >= 0.33), "LATracts20"] = 1

    mask = dat["lahunvhalf"].notna() & dat["lapop20"].notna() & dat["lapop20share"].notna()
    dat.loc[mask, "LATractsVehicle_20"] = 0
    dat.loc[(dat["lahunvhalf"] >= 100) | (dat["lapop20"] >= 500) | (dat["lapop20share"] >= 0.33), "LATractsVehicle_20"] = 1

    mask = dat["lahunvhalf"].notna()
    dat.loc[mask, "HUNVFlag"] = 0
    dat.loc[dat["lahunvhalf"] >= 100, "HUNVFlag"] = 1

    mask = dat["PCTGQTRS"].notna()
    dat.loc[mask, "GroupQuartersFlag"] = 0
    dat.loc[dat["PCTGQTRS"] >= 0.67, "GroupQuartersFlag"] = 1

    return dat


@register_pattern("fara_tract")
def run_fara_tract(config: DatasetConfig, engine: AggregationEngine) -> Path:
    """FARA tract linkage with recode flags — matches v1 exactly.

    Sprint 11 Phase A: dispatches on ``time.output_grouping``. The default
    ``patient`` branch is byte-identical to the pre-Sprint-11 v1 behavior
    (single PATID groupby). The ``episode`` branch groups by ``(PATID,
    geoid)`` so the FARA C4 output carries one row per episode, mirroring
    the precomputed_areal / precomputed_static / yearly_areal Phase A
    contracts and enabling spacescans-web's episode-level result join.
    """
    grouping = resolve_output_grouping(config)
    vsehr_rh = load_patients(config)
    buffer_df = read_table(config.source.file)
    fara_raw = read_table(config.exposure.file, key=config.exposure.key)
    label_csv = pd.read_csv(config.exposure.label_file)

    # Filter year >= 2013
    fara = fara_raw[fara_raw["year"] >= 2013].copy()

    # Column selection by R index (v1 lines 179-186)
    r_cols = list(range(3, 16)) + [18] + list(range(32, 36)) + list(range(37, 85))
    py_cols = [c - 1 for c in r_cols]
    fara = fara.iloc[:, py_cols].copy()

    r_temp_cols = list(range(1, 14)) + list(range(15, 67))
    py_temp_cols = [c - 1 for c in r_temp_cols]
    fara_var_cols = [fara.columns[i] for i in py_temp_cols]
    rate_vars = fara_var_cols[1:]

    # Legacy SQL pipeline
    years = sorted(fara["year"].unique().astype(int))
    dat3 = build_episode_year_table_legacy(vsehr_rh, years)
    dat4 = compute_yearly_area_weighted_exposures_sql(
        dat3,
        buffer_df=buffer_df,
        exposure_df=fara,
        years=years,
        buffer_table_name="buffer270mTRACT25m",
        buffer_join_col="GEOID10",
        exposure_join_col="Fips",
        value_cols=rate_vars,
        exposure_year_col="year",
    )
    dat5 = dat3.merge(dat4, on="id", how="outer")
    group_cols = ["PATID", "geoid"] if grouping == "episode" else ["PATID"]
    dat5f = compute_patient_temporal_weighted_sql(
        dat5,
        value_cols=rate_vars,
        years_count=len(years),
        group_cols=group_cols,
    )

    # Recode binary flags
    dat5f = _apply_fara_recode(dat5f)

    # Select final columns from label CSV + fillna(0)
    varlist = label_csv["var"].tolist()
    result = dat5f[group_cols + varlist].copy().fillna(0)

    return write_table(result, config.output.path)
