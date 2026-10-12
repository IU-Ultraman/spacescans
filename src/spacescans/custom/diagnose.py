"""After a run: how well each uploaded column linked, and why when it did not.

Upload validation can check the shape of a geography key but not its
membership in the national key universe; this is where the user finds out
whether their keys actually matched. "0 % linked" on its own reads as a bug;
almost always it is a file for one region run against a cohort from another,
so the diagnosis names both sets.

Diagnostics never fail a run: every reader error becomes a message.

Ported from spacescans-web ``app/experiments/custom.py`` (lines 418-522). The
web wrote these straight to ``logs.jsonl``; here they are returned as records
``{"level", "source", "msg"}`` with the web's exact text.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spacescans.custom.definition import is_raster
from spacescans.custom.steps import c3_step_for, definition_for, parquet_map

#: A column linking fewer episodes than this share is reported as a warning.
LOW_LINKAGE = 0.5


def _record(level: str, msg: str) -> dict[str, str]:
    return {"level": level, "source": "merge", "msg": msg}


def raster_diagnosis(run_dir: str | Path, var_key: str, definition: Mapping[str, Any]) -> str:
    """For a raster the cells always exist; a poor rate means nodata under the
    cohort, or a cohort outside the raster's extent."""
    import pandas as pd
    try:
        table = pd.read_parquet(Path(run_dir) / "output" / f"values_{var_key}.parquet")
    except Exception as exc:  # pragma: no cover
        return f"{var_key}: could not read the materialised values ({exc!r})"
    cols = [c for c in definition["value_cols"] if c in table.columns]
    shares = {c: (float(table[c].isna().mean()) if len(table) else 1.0) for c in cols}
    worst = max(shares.values(), default=1.0)
    b = (definition.get("grid") or {}).get("bounds_wgs84") or [None] * 4
    extent = (f"lon {b[0]:.1f}…{b[2]:.1f}, lat {b[1]:.1f}…{b[3]:.1f}"
              if None not in b else "unknown extent")
    if len(cols) == 1:
        what = f"{worst:.0%} of the"
    else:
        what = "nodata share by column (" + ", ".join(
            f"{c} {v:.0%}" for c, v in shares.items()) + ") of the"
    return (
        f"{var_key}: {what} {table['grid_id'].nunique():,} raster "
        f"cells under the cohort's buffers {'are nodata ' if len(cols) == 1 else ''}"
        f"(raster extent {extent}). "
        + ("The raster has no data where this cohort lives." if worst > 0.5
           else "Episodes over nodata cells get no value.")
    )


def coverage_diagnosis(run_dir: str | Path, var_key: str, definition: Mapping[str, Any]) -> str:
    """Say WHY a custom column linked poorly: which geographies the cohort's
    buffers touched versus which the uploaded file covers. Never raises."""
    import pandas as pd
    if is_raster(definition):
        return raster_diagnosis(run_dir, var_key, definition)
    try:
        c3_name = c3_step_for(definition).name
        weights = pd.read_parquet(Path(run_dir) / "output" / f"{c3_name}.parquet")
        touched = set(weights[definition["join_col"]].astype(str))
        uploaded = pd.read_csv(definition["values_path"], dtype=str,
                               usecols=[definition["key_col"]])
        covered = set(uploaded[definition["key_col"]].str.strip())
    except Exception as exc:  # diagnostics must never fail a run
        return f"{var_key}: could not compare geographies ({exc!r})"

    def _states(codes: set[str]) -> str:
        counts = pd.Series([c[:2] for c in codes]).value_counts()
        shown = ", ".join(f"{st} ({n})" for st, n in counts.head(6).items())
        more = f", +{len(counts) - 6} more" if len(counts) > 6 else ""
        return f"{len(counts)} state{'s' if len(counts) != 1 else ''}: {shown}{more}"

    common = touched & covered
    return (
        f"{var_key}: the cohort's buffers touch {len(touched):,} distinct "
        f"{definition['boundary']} codes across {_states(touched)}; the uploaded "
        f"file covers {len(covered):,} codes across {_states(covered)}; "
        f"{len(common):,} in common. "
        + ("The file does not cover where this cohort lives — upload values for "
           "those geographies, or run it on a cohort from that region."
           if not common else
           "Episodes outside the file's geographies get no value.")
    )


def diagnostics(run_dir: str | Path, variables: Sequence[str],
                definitions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    """The share of non-null values per column (the real linkage rate), plus a
    diagnosis for each variable with a column under 50 %.

    Reads each variable's C4 output ``run_dir/output/c4_<key>.parquet``
    (skipped when absent). Returns the web's ``logs.jsonl`` records in order;
    the web's ``_report_linkage_rate`` appends exactly these.
    """
    import pandas as pd
    out: list[dict[str, str]] = []
    for var_key in variables:
        parquet = Path(run_dir) / "output" / parquet_map([var_key])[var_key]
        if not parquet.exists():
            continue
        try:
            df = pd.read_parquet(parquet)
        except Exception as exc:  # pragma: no cover - diagnostics only
            out.append(_record("warning", f"{var_key}: could not read {parquet.name}: {exc!r}"))
            continue
        definition = definition_for(definitions, var_key)
        low = False
        for col in definition["value_cols"]:
            if col not in df.columns:
                out.append(_record("warning", f"{var_key}: value column {col!r} missing from output"))
                continue
            matched = float(df[col].notna().mean()) if len(df) else 0.0
            level = "info" if matched >= LOW_LINKAGE else "warning"
            low = low or matched < LOW_LINKAGE
            out.append(_record(
                level,
                f"{var_key}.{col}: {matched:.1%} of episodes linked "
                f"({int(df[col].notna().sum()):,}/{len(df):,})",
            ))
        if low:
            out.append(_record("warning", coverage_diagnosis(run_dir, var_key, definition)))
    return out
