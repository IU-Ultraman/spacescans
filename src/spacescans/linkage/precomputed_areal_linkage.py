# common_v2/linkage/precomputed_areal_linkage.py
"""Precomputed yearly-areal linkage pattern.

Used when the exposure table is already at geoid × year resolution (no spatial
aggregation needed).  The reader plugin loads the precomputed pkl and returns a
DataFrame with [geoid, year, <value_cols>].

Aggregation logic (mirrors v1 TIGER script, lines 155-242):
  1. Load patients + precomputed geoid × year exposure.
  2. For each patient episode, compute the overlap days with each calendar year.
  3. Duration-weighted average of each value column across years.

This pattern replaces the ``proximity`` pattern for TIGER roads, where the
intermediate ``annual_proximity.pkl`` already contains geoid-level distances.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients, resolve_output_grouping
from spacescans.pipeline.registry import get_reader, register_pattern


@register_pattern("precomputed_areal")
def run_precomputed_areal(config, engine) -> Path:
    """Aggregate geoid × year exposure to patient level via overlap-day TWA.

    The plugin (``config.plugin``) must implement ``load_exposure(years=...)``
    returning a DataFrame with at least [geoid, year, <value_cols>].
    """
    patients = load_patients(config)

    # Load exposure via plugin
    reader_cls = get_reader(config.plugin)
    reader = reader_cls(config)
    years = config.time.years if config.time else None
    exposure = reader.load_exposure(years=years)

    # Resolve value columns from exposure (excluding geoid / year)
    value_cols = list(config.exposure.value_cols) if config.exposure else [
        c for c in exposure.columns if c not in ("geoid", "year")
    ]

    # Build annual windows: start_date/end_date per year in the exposure table
    ap = exposure.copy()
    ap["geoid"] = ap["geoid"].astype("int64")
    ap["year"] = ap["year"].astype("int64")
    ap["start_date"] = ap["year"].apply(lambda y: f"{int(y):04d}-01-01")
    ap["end_date"] = ap["year"].apply(lambda y: f"{int(y):04d}-12-31")

    # Prepare patient episodes
    vsehr = patients[["PATID", "geoid", "start", "end"]].copy()
    vsehr["PATID"] = vsehr["PATID"].astype(str)
    vsehr["geoid"] = pd.to_numeric(vsehr["geoid"], errors="coerce").astype("int64")
    vsehr["start_date"] = pd.to_datetime(vsehr["start"]).dt.strftime("%Y-%m-%d")
    vsehr["end_date"] = pd.to_datetime(vsehr["end"]).dt.strftime("%Y-%m-%d")
    vsehr = vsehr[["PATID", "geoid", "start_date", "end_date"]].dropna(subset=["geoid"])

    ap_cols = ["geoid", "start_date", "end_date"] + value_cols
    ap_sql = ap[[c for c in ap_cols if c in ap.columns]].copy()

    con = sqlite3.connect(":memory:")
    try:
        vsehr.to_sql("vsehr_sql", con, index=False, if_exists="replace")
        ap_sql.to_sql("ap_sql", con, index=False, if_exists="replace")

        # Compute overlap days between patient episode and each annual window
        patient_year = pd.read_sql(
            """
            SELECT *
            FROM (
                SELECT
                    v.PATID,
                    v.geoid,
                    a.start_date,
                    a.end_date,
                    {value_selects},
                    CAST(
                        MAX(
                            0,
                            (julianday(CASE WHEN date(a.end_date) < date(v.end_date)
                                            THEN date(a.end_date) ELSE date(v.end_date) END)
                             - julianday(CASE WHEN date(a.start_date) > date(v.start_date)
                                            THEN date(a.start_date) ELSE date(v.start_date) END)
                             + 1)
                        ) AS INTEGER
                    ) AS overlap_days
                FROM vsehr_sql AS v
                JOIN ap_sql AS a
                  ON v.geoid = a.geoid
                 AND date(a.start_date) <= date(v.end_date)
                 AND date(a.end_date)   >= date(v.start_date)
            ) q
            WHERE overlap_days > 0
            """.format(value_selects=", ".join(f"a.{c}" for c in value_cols)),
            con,
        )
        patient_year.to_sql("patient_year", con, index=False, if_exists="replace")

        # Duration-weighted average per patient
        twa_selects = []
        for col in value_cols:
            twa_selects.append(
                f"SUM({col} * overlap_days) / NULLIF(SUM(CASE WHEN {col} IS NOT NULL "
                f"THEN overlap_days ELSE 0 END), 0) AS {col}"
            )

        # Duration-weighted average per patient (or per patient-episode).
        # Dispatch on TimeConfig.output_grouping — mirrors the conditional in
        # yearly_areal_linkage.py:47-58 but expressed as an SQL clause edit
        # because this pattern bypasses the engine.temporal_aggregate path.
        grouping = resolve_output_grouping(config)
        if grouping == "patient":
            select_keys = "PATID"
            group_keys = "PATID"
        else:  # "episode" — helper already rejected other values
            select_keys = "PATID, geoid"
            group_keys = "PATID, geoid"

        result = pd.read_sql(
            f"""
            SELECT {select_keys}, {', '.join(twa_selects)}
            FROM patient_year
            GROUP BY {group_keys}
            """,
            con,
        )
    finally:
        con.close()

    return write_table(result, config.output.path)
