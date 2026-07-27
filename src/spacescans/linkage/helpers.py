# common_v2/linkage/helpers.py
"""Shared helpers for linkage patterns."""
from __future__ import annotations
import pandas as pd
from spacescans.models.config import FilterTransform, RecodeTransform, DeriveTransform
from spacescans.transforms.filter import filter_features
from spacescans.transforms.recode import recode_columns
from spacescans.transforms.derive import derive_variable


def build_episode_periods(
    patients: pd.DataFrame,
    years: list[int],
    *,
    patid_col: str = "PATID",
    geoid_col: str = "geoid",
    start_col: str = "start",
    end_col: str = "end",
) -> pd.DataFrame:
    patients = patients.copy()
    patients[start_col] = pd.to_datetime(patients[start_col])
    patients[end_col] = pd.to_datetime(patients[end_col])
    rows = []
    for _, episode in patients.iterrows():
        ep_start, ep_end = episode[start_col], episode[end_col]
        for year in years:
            yr_start = pd.Timestamp(f"{year}-01-01")
            yr_end = pd.Timestamp(f"{year}-12-31")
            overlap_start = max(ep_start, yr_start)
            overlap_end = min(ep_end, yr_end)
            days = max(0, (overlap_end - overlap_start).days + 1)
            if days > 0:
                rows.append({
                    patid_col: episode[patid_col],
                    geoid_col: episode[geoid_col],
                    "period_id": year,
                    "overlap_days": days,
                })
    if not rows:
        return pd.DataFrame(columns=[patid_col, geoid_col, "period_id", "overlap_days"])
    return pd.DataFrame(rows)


def prepare_episodes(
    patients: pd.DataFrame,
    *,
    patid_col: str = "PATID",
    geoid_col: str = "geoid",
    start_col: str = "start",
    end_col: str = "end",
) -> pd.DataFrame:
    df = patients[[patid_col, geoid_col, start_col, end_col]].copy()
    df = df.rename(columns={start_col: "start_date", end_col: "end_date"})
    df["start_date"] = pd.to_datetime(df["start_date"])
    df["end_date"] = pd.to_datetime(df["end_date"])
    return df


def _adapt_demo_conus(df: pd.DataFrame) -> pd.DataFrame:
    """Map demo_patients_conus_fast_*.rds columns to pipeline's expected format.

    Source columns: pid, startDate, endDate, longitude, latitude, bg_geoid (12-digit str)
    Optional column: episode_id (int) — when supplied (e.g. by spacescans-web's
    csv_to_parquet) the adapter uses it as the synthetic per-row geoid,
    enabling per-episode output grouping downstream. Fallback is
    range(len(df)) for legacy callers (R / CLI users).
    Target columns: PATID, start, end, long, lat, geoid (int)
    """
    df = df.rename(columns={
        "pid": "PATID",
        "startDate": "start",
        "endDate": "end",
        "longitude": "long",
        "latitude": "lat",
    })
    if "episode_id" in df.columns:
        df["geoid"] = df["episode_id"].astype(int)
    else:
        # geoid must be unique per patient — pipeline (esp. grid_weights
        # validation) assumes 1:1 patient↔geoid; using factorize(bg_geoid)
        # would collide multiple patients in the same block group.
        df["geoid"] = range(len(df))
    return df[["PATID", "start", "end", "long", "lat", "geoid"]].copy()


def load_patients(config) -> pd.DataFrame:
    """Load patient residential history, applying an adapter if configured."""
    from spacescans.io.readers import read_table

    df = read_table(config.buffer.patient_file)
    adapter = getattr(config.buffer, "patient_adapter", None)
    if adapter == "demo_conus":
        df = _adapt_demo_conus(df)
    return df


def load_weights(path: str, *, key: str | None = None, weight_col: str = "weight") -> pd.DataFrame:
    """Load a C3 weight table and normalize the weight column name.

    V1 pkl files use 'value' as the weight column; v2 standardizes to 'weight'.
    This function handles the rename transparently.
    """
    from spacescans.io.readers import read_table

    df = read_table(path, key=key)
    if weight_col not in df.columns and "value" in df.columns:
        df = df.rename(columns={"value": weight_col})
    return df


def resolve_output_grouping(config) -> str:
    """Return the validated output_grouping literal.

    Raises ValueError when config.time is None or when output_grouping
    holds an unsupported value. Used by linkage patterns that branch
    per-episode vs per-patient — centralizes the four-way dispatch
    predicate previously duplicated across precomputed_areal,
    yearly_areal, static_areal, and yearly_areal_bg_vintage.

    Note — "episode" mode currently depends on the ``demo_conus``
    adapter's ``geoid = episode_id`` convention introduced in Sprint 2
    (see ``_adapt_demo_conus`` above). Downstream linkage patterns
    group by ``["PATID", "geoid"]`` to materialize per-episode rows,
    which is only equivalent to per-episode output because the adapter
    assigns each row a synthetic unique geoid. For a future non-demo
    workload where ``geoid`` is a real block-group identifier shared
    across episodes, ``episode`` mode would collapse to
    per-(patient, geoid) and lose true episode resolution. Switching
    that contract will require introducing a dedicated ``episode_id``
    join key across patterns.
    """
    if config.time is None:
        raise ValueError(
            "linkage pattern requires a time block with output_grouping"
        )
    grouping = config.time.output_grouping
    if grouping not in ("patient", "episode"):
        raise ValueError(
            f"unsupported output_grouping: {grouping!r} "
            "(expected 'patient' or 'episode')"
        )
    return grouping


def apply_transforms(df: pd.DataFrame, transforms: list, target: str = "exposure") -> pd.DataFrame:
    for t in transforms:
        if t.target != target:
            continue
        if isinstance(t, FilterTransform):
            df = filter_features(
                df,
                column=t.column,
                values=t.values,
                prefixes=t.prefixes,
                exclude=t.exclude,
            )
        elif isinstance(t, RecodeTransform):
            df = recode_columns(df, t.rules)
        elif isinstance(t, DeriveTransform):
            df = derive_variable(
                df,
                formula=t.formula,
                registry_key=t.registry_key,
                output_col=t.output_col,
                params=t.params,
            )
    return df
