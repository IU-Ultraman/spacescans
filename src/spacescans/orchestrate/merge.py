"""Joining linked exposures back to the cohort, keyed on (pid, episode_id).

Port of spacescans-web ``backend/app/experiments/_merge.py`` (commit
3991c3b) as plain DataFrame functions. Nothing here reads the catalog or
writes logs: callers pass each variable's value columns, and turn the
returned :class:`MatchStats` into a warning if they want one.

Two stages, with different semantics on purpose:

* :func:`merge_partial` (``write_partial`` 20-83) combines one source's
  C4 outputs. ``PATID`` becomes ``pid`` and ``geoid`` becomes
  ``episode_id``; only the variable's value columns that are present are
  kept; variables are outer-merged in request order and, on a repeated
  column, the first variable wins. The result is **not** joined to the
  cohort: episodes with no output are absent, not NaN. With one variable
  the C4 row order is kept; with more, pandas' outer merge sorts the keys.
* :func:`fan_in` (``fan_in`` 86-120) left-joins every source's partial
  onto the cohort in the given order; a column whose name is already
  taken gets the suffix ``_<source>_dup``. ``episode_id`` is dropped by
  default, as in the web's result.csv.

``episode_id`` is the input row number (see :mod:`spacescans.cohort`), so
the cohort passed here must be the same table, in the same row order, that
was written to input.parquet.

The ``*_from_dir`` helpers read a run directory with the web's conventions
(input.csv and partial CSVs as text, C4 outputs as parquet, FAQSD's
in-place rename), so the web adapter and new callers share one reading
of those files. Writing results and logging stay with the caller.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import pandas as pd

__all__ = [
    "KEYS",
    "LOW_MATCH_EVENT",
    "LOW_MATCH_PCT",
    "MatchStats",
    "episode_keys",
    "fan_in",
    "fan_in_from_dir",
    "match_stats",
    "merge_partial",
    "partial_csv_name",
    "partial_from_dir",
    "select_output",
]

#: Join keys between the cohort and every linked output.
KEYS: tuple[str, str] = ("pid", "episode_id")
#: Below this share of matched episodes the web logs a warning (_merge.py:72).
LOW_MATCH_PCT = 90.0
#: The ``event`` field of that warning in logs.jsonl.
LOW_MATCH_EVENT = "merge_partial_low_match_pct"

PathLike = Union[str, "os.PathLike[str]"]


@dataclass(frozen=True)
class MatchStats:
    """How many cohort episodes got at least one non-missing value."""

    experiment: str
    cohort_n: int
    matched_n: int
    match_pct: float

    @property
    def low(self) -> bool:
        """True when the web would log ``merge_partial_low_match_pct``."""
        return self.match_pct < LOW_MATCH_PCT

    def as_log_fields(self) -> dict[str, Any]:
        """The warning's fields, in the web's order (it adds ts and level)."""
        return {
            "experiment_key": self.experiment,
            "event": LOW_MATCH_EVENT,
            "match_pct": self.match_pct,
            "cohort_n": self.cohort_n,
            "matched_n": self.matched_n,
        }


def partial_csv_name(experiment: str) -> str:
    """File name of one source's partial result: ``result_<experiment>.csv``."""
    return f"result_{experiment}.csv"


def _with_episode_id(cohort: pd.DataFrame) -> pd.DataFrame:
    """Cohort plus episode_id = row number; PATID read as pid when no pid.

    Any existing episode_id column is overwritten in place, as the web does.
    """
    df = cohort.copy()
    if "pid" not in df.columns:
        df = df.rename(columns={"PATID": "pid"})
    df["episode_id"] = list(range(len(df)))
    df["episode_id"] = df["episode_id"].astype(int)
    return df


def episode_keys(cohort: pd.DataFrame) -> pd.DataFrame:
    """The cohort's join keys: ``pid`` and ``episode_id`` (the row number).

    ``_merge.write_partial`` lines 31-36. Only an exact ``PATID`` column is
    read as ``pid``, as in the web's merge step.
    """
    return _with_episode_id(cohort)[list(KEYS)]


def select_output(df: pd.DataFrame, value_cols: Sequence[str], *,
                  renames: Mapping[str, str] | None = None) -> pd.DataFrame:
    """One C4 output reduced to ``pid``, ``episode_id`` and its value columns.

    `renames` (FAQSD's ``o3 -> faqsd_o3``) is applied first, then
    ``PATID -> pid`` and ``geoid -> episode_id`` (cast to int), then only
    the `value_cols` present are kept, in `value_cols` order.
    """
    if renames:
        df = df.rename(columns=dict(renames))
    df = df.rename(columns={"PATID": "pid", "geoid": "episode_id"})
    df["episode_id"] = df["episode_id"].astype(int)
    kept = [c for c in value_cols if c in df.columns]
    return df[list(KEYS) + kept]


def match_stats(keys: pd.DataFrame, merged: pd.DataFrame, *,
                experiment: str) -> MatchStats:
    """Share of cohort episodes with at least one value in `merged`.

    ``_merge.write_partial`` lines 63-70: a partial with no value columns
    counts as 100% matched.
    """
    joined = keys.merge(merged, on=list(KEYS), how="left")
    value_only = joined.drop(columns=list(KEYS))
    if value_only.shape[1] == 0:
        match_pct = 100.0
        matched = len(keys)
    else:
        matched = int(value_only.notna().any(axis=1).sum())
        match_pct = round(100.0 * matched / max(len(keys), 1), 2)
    return MatchStats(experiment=experiment, cohort_n=len(keys),
                      matched_n=int(matched), match_pct=match_pct)


def merge_partial(
    keys: pd.DataFrame,
    outputs: Sequence[tuple[str, pd.DataFrame]],
    value_cols: Mapping[str, Sequence[str]],
    *,
    experiment: str,
    renames: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, MatchStats]:
    """Combine one source's C4 outputs into its partial result.

    `keys` is :func:`episode_keys` of the cohort. `outputs` is
    ``[(variable, c4_frame), ...]`` in request order; `value_cols` maps each
    variable to its catalog value columns (a variable missing from it raises
    KeyError). Returns the partial table, as written to
    ``result_<experiment>.csv``, and the match statistics.
    """
    merged: pd.DataFrame | None = None
    for var_key, raw in outputs:
        df = select_output(raw, value_cols[var_key], renames=renames)
        if merged is None:
            merged = df
        else:
            new_cols = [c for c in df.columns
                        if c in KEYS or c not in merged.columns]
            df = df[new_cols]
            merged = merged.merge(df, on=list(KEYS), how="outer")

    if merged is None:
        merged = keys.copy()

    return merged, match_stats(keys, merged, experiment=experiment)


def fan_in(
    base: pd.DataFrame,
    partials: Sequence[tuple[str, pd.DataFrame]],
    *,
    reserved: Sequence[str] = (),
    drop_episode_id: bool = True,
) -> pd.DataFrame:
    """Left-join each source's partial onto the cohort, in the given order.

    `base` is the cohort table (the web passes input.csv read as text);
    ``episode_id`` is set to its row number. Each partial needs ``pid`` and
    ``episode_id``. A partial column whose name is already in the table gets
    ``_<source>_dup``; names in `reserved` (columns the caller holds but did
    not pass, such as an R user's other columns) are treated as taken too.
    Row count and order are the cohort's.
    """
    df = _with_episode_id(base)
    taken = set(reserved)
    for exp_key, partial in partials:
        partial = partial.copy()
        partial["episode_id"] = partial["episode_id"].astype(int)
        if taken:
            clash = {c: f"{c}_{exp_key}_dup" for c in partial.columns
                     if c not in KEYS and c in taken and c not in df.columns}
            dup = [new for new in clash.values() if new in df.columns]
            if dup:
                raise ValueError(
                    f"fan_in: renaming reserved columns from {exp_key!r} would "
                    f"duplicate {dup}"
                )
            partial = partial.rename(columns=clash)
        df = df.merge(
            partial,
            on=list(KEYS),
            how="left",
            suffixes=("", f"_{exp_key}_dup"),
        )
    if drop_episode_id:
        df = df.drop(columns=["episode_id"])
    return df


# ---------------------------------------------------------------------------
# Run-directory helpers (the web's file conventions)
# ---------------------------------------------------------------------------


def _read_text_csv(path: PathLike) -> pd.DataFrame:
    """A CSV with every column read as text, as the web's merge step does."""
    return pd.read_csv(path, dtype=str)


def partial_from_dir(
    run_dir: PathLike,
    experiment: str,
    variables: Sequence[str],
    parquet_map: Mapping[str, str],
    value_cols: Mapping[str, Sequence[str]],
    *,
    renames: Mapping[str, str] | None = None,
    rewrite_in_place: bool = False,
) -> tuple[pd.DataFrame, MatchStats]:
    """:func:`merge_partial` over a run directory laid out like a web task.

    Reads ``<run_dir>/input.csv`` as text and
    ``<run_dir>/output/<parquet_map[v]>`` for each variable. With
    ``rewrite_in_place`` the `renames` are first written back into each
    C4 parquet, as the web's FAQSD runner does (faqsd.py:213-216).
    The caller writes the table with ``to_csv(path, index=False)`` to
    ``output/`` + :func:`partial_csv_name` and logs the warning when
    ``stats.low``.
    """
    run_dir = Path(run_dir)
    out_dir = run_dir / "output"
    if rewrite_in_place and renames:
        for var_key in variables:
            path = out_dir / parquet_map[var_key]
            df = pd.read_parquet(path)
            df = df.rename(columns=dict(renames))
            df.to_parquet(path, index=False)
    keys = episode_keys(_read_text_csv(run_dir / "input.csv"))
    outputs = [(v, pd.read_parquet(out_dir / parquet_map[v])) for v in variables]
    return merge_partial(keys, outputs, value_cols, experiment=experiment,
                         renames=renames)


def fan_in_from_dir(
    run_dir: PathLike,
    experiment_keys: Sequence[str],
    *,
    reserved: Sequence[str] = (),
    drop_episode_id: bool = True,
) -> pd.DataFrame:
    """:func:`fan_in` over a run directory laid out like a web task.

    Reads ``input.csv`` and each ``output/result_<exp>.csv`` as text, so
    values reach result.csv exactly as the partials wrote them. The caller
    writes ``output/result.csv`` with ``to_csv(path, index=False)`` and the
    feature dictionary for its columns.
    """
    run_dir = Path(run_dir)
    base = _read_text_csv(run_dir / "input.csv")
    partials = [
        (exp, _read_text_csv(run_dir / "output" / partial_csv_name(exp)))
        for exp in experiment_keys
    ]
    return fan_in(base, partials, reserved=reserved,
                  drop_episode_id=drop_episode_id)
