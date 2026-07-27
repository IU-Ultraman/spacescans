# common_v2/models/specs.py
"""Operation spec objects — backend-independent aggregation semantics."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class MissingPolicy(str, Enum):
    SKIP = "skip"
    ZERO = "zero"
    RAISE = "raise"


class JoinSpec(BaseModel):
    left_key: str | list[str]
    right_key: str | list[str]
    how: Literal["inner", "left", "right", "outer"] = "left"


class WeightedAggSpec(BaseModel):
    group_by: str | list[str]
    value_cols: list[str]
    weight_col: str = "weight"
    missing_policy: MissingPolicy = MissingPolicy.SKIP
    output_suffix: str | None = None


class TemporalAggSpec(BaseModel):
    """Long-table oriented temporal aggregation."""
    group_by: str | list[str] = "PATID"
    period_col: str = "period_id"
    value_cols: list[str]
    weight_col: str = "overlap_days"
    missing_policy: MissingPolicy = MissingPolicy.SKIP
    output_suffix: str | None = None


class DateRangeJoinSpec(BaseModel):
    left_date_col: str | None = None
    left_start_col: str | None = None
    left_end_col: str | None = None
    right_start_col: str = "start_date"
    right_end_col: str = "end_date"
    overlap_unit: Literal["days"] = "days"


class DurationWeightedSpec(BaseModel):
    patient_id_col: str = "PATID"
    geoid_col: str = "geoid"
    value_cols: list[str]
    start_col: str = "start_date"
    end_col: str = "end_date"
    missing_policy: MissingPolicy = MissingPolicy.SKIP
    group_by_episode: bool = False  # if True, GROUP BY patient_id_col, geoid_col


class SimpleAggSpec(BaseModel):
    """Non-weighted aggregation."""
    group_by: str | list[str]
    agg_col: str
    agg_func: Literal["min", "max", "sum", "count", "mean", "first"]
