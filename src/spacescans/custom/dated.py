"""Daily and biweekly datasets in a run: the tables the ``gridded`` pattern reads.

Daily polygon tables and daily or biweekly rasters link through the
pipeline's ``gridded`` pattern, the one TEMIS (daily) uses: C3 weights
``(geoid, grid_id, weight)`` joined to an exposure table keyed by
``grid_id`` with a ``date`` column (daily) or ``start_date``/``end_date``
(periods), area-weighted per location and period, then averaged over each
episode weighted by the days of the episode in each period
(:func:`spacescans.custom.steps` renders the step). Before the C4 step
renders, :func:`materialise_dated` writes ``output/values_<key>.parquet``,
restricted to what the cohort can use:

* a **raster**: the pixel values under the C3 grid weights' cells for every
  file whose period overlaps the cohort's dates (``grid_id`` is the row-major
  cell index, as for yearly rasters);
* a **polygon table**: the prepared table's rows for the geographies the C3
  boundary weights touch and the dates the cohort spans, keyed by the
  geography code as ``grid_id``; and ``output/weights_<key>.parquet``, the
  boundary weights with the code under ``grid_id`` and the overlap share
  under ``weight`` (the names ``gridded`` reads).

Nothing here reads more than the cohort needs: the national table is
filtered by DuckDB from Parquet, the rasters are read cell by cell
(:func:`spacescans.custom.raster.extract_bands`).
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from spacescans.custom.definition import raster_bands
from spacescans.custom.values import MaterialisedValues, grid_ids_of


def cohort_span(cohort_parquet: str | os.PathLike) -> tuple[date, date] | None:
    """The first start and last end date of the cohort's rows (None when none parses)."""
    import pandas as pd

    from spacescans.io.readers import read_table
    from spacescans.linkage.helpers import _adapt_demo_conus

    rows = _adapt_demo_conus(read_table(cohort_parquet))
    starts = pd.to_datetime(rows["start"], errors="coerce")
    ends = pd.to_datetime(rows["end"], errors="coerce")
    if starts.isna().all() or ends.isna().all():
        return None
    return starts.min().date(), ends.max().date()


def _overlaps(start: date, end: date, span: tuple[date, date] | None) -> bool:
    return span is None or not (end < span[0] or start > span[1])


def raster_values(definition: Mapping[str, Any], grid_ids: Any,
                  span: tuple[date, date] | None) -> Any:
    """``grid_id, <cols>, date`` (daily) or ``..., start_date, end_date`` (biweekly)."""
    import numpy as np
    import pandas as pd

    from spacescans.custom.raster import extract_bands

    cols, bands, nodata = raster_bands(definition)
    daily = definition.get("temporal") == "daily"
    frames = []
    for raster in definition["rasters"]:
        start = date.fromisoformat(str(raster["start"]))
        end = date.fromisoformat(str(raster["end"]))
        if not _overlaps(start, end, span):
            continue
        vals = extract_bands(raster["path"], grid_ids, bands=bands, nodata=nodata)
        # nodata cells stay (as NaN = NULL): the pattern skips them, the diagnosis counts them
        frame = pd.DataFrame({"grid_id": np.asarray(grid_ids),
                              **{c: vals[:, j] for j, c in enumerate(cols)}})
        if daily:
            frame["date"] = pd.Timestamp(start)
        else:
            frame["start_date"] = pd.Timestamp(start)
            frame["end_date"] = pd.Timestamp(end)
        frames.append(frame)
    if frames:
        return pd.concat(frames, ignore_index=True)
    empty: dict[str, Any] = {"grid_id": pd.Series(dtype="int64"),
                             **{c: pd.Series(dtype="float64") for c in cols}}
    for c in (("date",) if daily else ("start_date", "end_date")):
        empty[c] = pd.Series(dtype="datetime64[ns]")
    return pd.DataFrame(empty)


def boundary_weights(definition: Mapping[str, Any], c3_parquet: str | os.PathLike) -> Any:
    """The boundary C3 weights as ``geoid, grid_id (the zero-padded code), weight``."""
    import pandas as pd

    from spacescans.custom.table import BOUNDARY_SPEC

    weights = pd.read_parquet(c3_parquet)
    join_col = definition["join_col"]
    width = int(BOUNDARY_SPEC[definition["boundary"]]["key_len"])
    codes = weights[join_col]
    if pd.api.types.is_numeric_dtype(codes):
        codes = pd.to_numeric(codes).astype("int64").astype(str)
    codes = codes.astype(str).str.strip().str.zfill(width)
    weight_col = "weight" if "weight" in weights.columns else "value"
    return pd.DataFrame({"geoid": weights["geoid"].to_numpy(), "grid_id": codes.to_numpy(),
                         "weight": weights[weight_col].astype("float64").to_numpy()})


def table_values(definition: Mapping[str, Any], weights: Any,
                 span: tuple[date, date] | None, out: Path) -> int:
    """Write the daily table's rows for the touched codes (and cohort dates) to ``out``."""
    import duckdb

    cols = list(definition["value_cols"])
    key_col = definition.get("key_col") or "geo_key"
    date_col = definition.get("date_col") or "date"
    quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
    path = str(definition["values_path"]).replace("'", "''")
    where = ""
    params: list[Any] = []
    if span is not None:
        where = f' AND CAST(v."{date_col}" AS DATE) BETWEEN ? AND ?'
        params = [span[0], span[1]]
    target = str(out).replace("'", "''")
    # spills go where a link's DuckDB steps spill: <run_dir>/.tmp
    con = duckdb.connect(config={"temp_directory": str(out.parent.parent / ".tmp")})
    try:
        con.register("w", weights[["grid_id"]].drop_duplicates())
        con.execute(
            f'CREATE TABLE vals AS SELECT CAST(v."{key_col}" AS VARCHAR) AS grid_id, '
            f'CAST(v."{date_col}" AS DATE) AS date, {quoted} '
            f"FROM read_parquet('{path}') v "
            f'WHERE CAST(v."{key_col}" AS VARCHAR) IN (SELECT grid_id FROM w){where}', params)
        con.execute(f"COPY vals TO '{target}' (FORMAT PARQUET)")
        return int(con.execute("SELECT count(*) FROM vals").fetchone()[0])
    finally:
        con.close()


def materialise_dated(definition: Mapping[str, Any], *, c3_parquet: str | os.PathLike,
                      cohort_parquet: str | os.PathLike, values_out: str | os.PathLike,
                      weights_out: str | os.PathLike | None = None) -> MaterialisedValues:
    """Write the ``gridded`` inputs of a daily/biweekly dataset; see the module docstring.

    ``weights_out`` is needed for a polygon table (the renamed boundary weights).
    """
    out = Path(values_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    span = cohort_span(cohort_parquet)
    temporal = definition.get("temporal")
    if definition.get("geometry") == "raster":
        grid_ids = grid_ids_of(c3_parquet)
        table = raster_values(definition, grid_ids, span)
        table.to_parquet(out, index=False)
        periods = (table["date"].nunique() if temporal == "daily"
                   else table["start_date"].nunique()) if len(table) else 0
        cols = list(definition["value_cols"])
        message = (f"materialised {len(table):,} (grid_id, {'date' if temporal == 'daily' else 'period'})"
                   f" rows from {periods:,} of {len(definition['rasters']):,} {temporal} raster(s) "
                   f"over {len(grid_ids):,} cells; nodata per column: "
                   + ", ".join(f"{c} {int(table[c].isna().sum()):,}" for c in cols))
        return MaterialisedValues(path=out, rows=len(table), message=message)
    if weights_out is None:
        raise ValueError("a daily polygon table needs weights_out")
    weights = boundary_weights(definition, c3_parquet)
    weights.to_parquet(weights_out, index=False)
    rows = table_values(definition, weights, span, out)
    message = (f"materialised {rows:,} (code, date) rows of the daily table for "
               f"{weights['grid_id'].nunique():,} {definition['boundary']} codes under the "
               "cohort's buffers")
    return MaterialisedValues(path=out, rows=rows, message=message)
