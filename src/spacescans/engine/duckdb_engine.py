# spacescans/engine/duckdb_engine.py
"""DuckDB aggregation engine — primary backend."""

from __future__ import annotations

import duckdb
import pandas as pd

from spacescans.models.specs import (
    DateRangeJoinSpec, DurationWeightedSpec, JoinSpec, MissingPolicy,
    SimpleAggSpec, TemporalAggSpec, WeightedAggSpec,
)


class DuckDBEngine:
    """In-memory DuckDB engine. Implements AggregationEngine protocol."""

    def __init__(self):
        self._conn = duckdb.connect()

    # --- join ---

    def join(self, left: pd.DataFrame, right: pd.DataFrame, spec: JoinSpec) -> pd.DataFrame:
        self._conn.register("_left", left)
        self._conn.register("_right", right)

        lk = _ensure_list(spec.left_key)
        rk = _ensure_list(spec.right_key)
        on_clause = " AND ".join(f"l.{l} = r.{r}" for l, r in zip(lk, rk))

        how_sql = {"left": "LEFT", "right": "RIGHT", "inner": "INNER", "outer": "FULL OUTER"}
        join_type = how_sql[spec.how]

        sql = f"SELECT l.*, r.* EXCLUDE ({', '.join(rk)}) FROM _left l {join_type} JOIN _right r ON {on_clause}"
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_left")
        self._conn.unregister("_right")
        return result

    # --- weighted_aggregate ---

    def weighted_aggregate(self, data: pd.DataFrame, spec: WeightedAggSpec) -> pd.DataFrame:
        self._conn.register("_data", data)
        group_cols = _ensure_list(spec.group_by)
        group_sql = ", ".join(group_cols)
        suffix = spec.output_suffix or ""

        selects = list(group_cols)
        for col in spec.value_cols:
            selects.append(_weighted_avg_expr(col, spec.weight_col, spec.missing_policy, f"{col}{suffix}"))

        sql = f"SELECT {', '.join(selects)} FROM _data GROUP BY {group_sql}"
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_data")
        return result

    # --- simple_aggregate ---

    def simple_aggregate(self, data: pd.DataFrame, spec: SimpleAggSpec) -> pd.DataFrame:
        self._conn.register("_data", data)
        group_cols = _ensure_list(spec.group_by)
        group_sql = ", ".join(group_cols)

        func_map = {
            "min": "MIN", "max": "MAX", "sum": "SUM",
            "count": "COUNT", "mean": "AVG", "first": "FIRST",
        }
        agg_fn = func_map[spec.agg_func]

        sql = f"SELECT {group_sql}, {agg_fn}({spec.agg_col}) AS {spec.agg_col} FROM _data GROUP BY {group_sql}"
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_data")
        return result

    # --- temporal_aggregate ---

    def temporal_aggregate(self, data: pd.DataFrame, spec: TemporalAggSpec) -> pd.DataFrame:
        self._conn.register("_data", data)
        group_cols = _ensure_list(spec.group_by)
        group_sql = ", ".join(group_cols)
        suffix = spec.output_suffix or ""

        selects = list(group_cols)
        for col in spec.value_cols:
            selects.append(_weighted_avg_expr(col, spec.weight_col, spec.missing_policy, f"{col}{suffix}"))

        sql = f"SELECT {', '.join(selects)} FROM _data GROUP BY {group_sql}"
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_data")
        return result

    # --- date_range_join ---

    def date_range_join(
        self, exposure: pd.DataFrame, episodes: pd.DataFrame, spec: DateRangeJoinSpec,
    ) -> pd.DataFrame:
        self._conn.register("_exp", exposure)
        self._conn.register("_ep", episodes)

        # Cast all date columns to DATE to handle VARCHAR/TIMESTAMP mismatches
        def _d(expr: str) -> str:
            return f"CAST({expr} AS DATE)"

        if spec.left_date_col:
            where = (
                f"{_d('e.' + spec.left_date_col)} >= {_d('p.' + spec.right_start_col)} AND "
                f"{_d('e.' + spec.left_date_col)} <= {_d('p.' + spec.right_end_col)}"
            )
            overlap_expr = "1 AS overlap_days"
        else:
            where = (
                f"{_d('e.' + spec.left_start_col)} <= {_d('p.' + spec.right_end_col)} AND "
                f"{_d('e.' + spec.left_end_col)} >= {_d('p.' + spec.right_start_col)}"
            )
            overlap_expr = (
                f"GREATEST(0, DATEDIFF('day', "
                f"GREATEST({_d('e.' + spec.left_start_col)}, {_d('p.' + spec.right_start_col)}), "
                f"LEAST({_d('e.' + spec.left_end_col)}, {_d('p.' + spec.right_end_col)})) + 1) AS overlap_days"
            )

        sql = (
            f"SELECT p.PATID, p.geoid, e.*, {overlap_expr} "
            f"FROM _ep p JOIN _exp e ON p.geoid = e.geoid WHERE {where}"
        )
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_exp")
        self._conn.unregister("_ep")
        return result

    # --- duration_weighted ---

    def duration_weighted(
        self, values: pd.DataFrame, episodes: pd.DataFrame, spec: DurationWeightedSpec,
    ) -> pd.DataFrame:
        self._conn.register("_vals", values)
        self._conn.register("_ep", episodes)

        value_selects = []
        for col in spec.value_cols:
            value_selects.append(
                _weighted_avg_expr(col, "days", spec.missing_policy, col)
            )

        if spec.group_by_episode:
            select_cols = f"s.{spec.patient_id_col}, s.{spec.geoid_col}"
            group_by_cols = f"s.{spec.patient_id_col}, s.{spec.geoid_col}"
        else:
            select_cols = f"s.{spec.patient_id_col}"
            group_by_cols = f"s.{spec.patient_id_col}"

        sql = f"""
            WITH stays AS (
                SELECT
                    p.{spec.patient_id_col},
                    p.{spec.geoid_col},
                    DATEDIFF('day', p.{spec.start_col}, p.{spec.end_col}) + 1 AS days
                FROM _ep p
                WHERE DATEDIFF('day', p.{spec.start_col}, p.{spec.end_col}) + 1 > 0
            )
            SELECT {select_cols}, {', '.join(value_selects)}
            FROM stays s
            LEFT JOIN _vals v ON s.{spec.geoid_col} = v.{spec.geoid_col}
            GROUP BY {group_by_cols}
        """
        result = self._conn.execute(sql).fetchdf()
        self._conn.unregister("_vals")
        self._conn.unregister("_ep")
        return result


# --- SQL helpers ---

def _ensure_list(val: str | list[str]) -> list[str]:
    return val if isinstance(val, list) else [val]


def _weighted_avg_expr(col: str, weight_col: str, policy: MissingPolicy, alias: str) -> str:
    if policy == MissingPolicy.SKIP:
        num = f"SUM(CASE WHEN {col} IS NOT NULL THEN {col} * {weight_col} ELSE 0 END)"
        den = f"NULLIF(SUM(CASE WHEN {col} IS NOT NULL THEN {weight_col} ELSE 0 END), 0)"
    elif policy == MissingPolicy.ZERO:
        num = f"SUM(COALESCE({col}, 0) * {weight_col})"
        den = f"NULLIF(SUM({weight_col}), 0)"
    elif policy == MissingPolicy.RAISE:
        num = f"SUM({col} * {weight_col})"
        den = f"NULLIF(SUM({weight_col}), 0)"
    else:
        raise ValueError(f"Unknown MissingPolicy: {policy}")
    return f"{num} / {den} AS {alias}"
