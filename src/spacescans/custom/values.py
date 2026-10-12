"""The C4 value table of a raster dataset, built from the pixels under a cohort.

One row per grid cell the cohort's buffers touch (per year, for a yearly
dataset), with one column per selected band holding the pixel value; NaN where
the pixel is nodata. Only the cells in the C3 weights are read, so the table is
a few thousand rows however large the national raster is.

Ported from spacescans-web ``app/experiments/custom.py``
(``_materialise_raster_values``, lines 289-325). Needs the ``[geo]`` extra.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from spacescans.custom.definition import raster_bands

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


@dataclass(frozen=True)
class MaterialisedValues:
    """Where the table went, how many rows it has, and the web's log line for it."""

    path: Path
    rows: int
    message: str


def grid_ids_of(c3_parquet: str | Path) -> np.ndarray:
    """The distinct grid ids in a grid_weights output, sorted, as int64."""
    import pandas as pd
    weights = pd.read_parquet(c3_parquet, columns=["grid_id"])
    return np.sort(weights["grid_id"].dropna().astype("int64").unique())


def value_table(definition: Mapping[str, Any], grid_ids: np.ndarray) -> "pd.DataFrame":
    """``(grid_id, <col>...[, year])`` for the given grid ids, one block per raster."""
    import pandas as pd

    from spacescans.custom.raster import extract_bands

    cols, bands, nodata = raster_bands(definition)
    frames = []
    for raster in definition["rasters"]:
        vals = extract_bands(raster["path"], grid_ids, bands=bands, nodata=nodata)
        frame = pd.DataFrame({"grid_id": grid_ids,
                              **{col: vals[:, j] for j, col in enumerate(cols)}})
        if raster.get("year") is not None:
            frame["year"] = int(raster["year"])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def materialise_values(definition: Mapping[str, Any], *, c3_parquet: str | Path,
                       out_path: str | Path) -> MaterialisedValues:
    """Write the C4 value table of a raster dataset from its C3 weights.

    ``c3_parquet`` is the dataset's ``c3_customgrid_<hash>`` output;
    ``out_path`` is where the table is written (a run puts it at
    ``output/values_<key>.parquet``). The returned ``message`` is the text the
    web logs for it, under the C4 step's name.
    """
    grid_ids = grid_ids_of(c3_parquet)
    cols, bands, _nodata = raster_bands(definition)
    table = value_table(definition, grid_ids)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    message = (
        f"materialised {len(table):,} (grid_id{', year' if 'year' in table else ''}) "
        f"rows from {len(definition['rasters'])} raster(s), band(s) "
        f"{', '.join(map(str, bands))}; nodata per column: "
        + ", ".join(f"{c} {int(table[c].isna().sum()):,}" for c in cols)
    )
    return MaterialisedValues(path=out, rows=len(table), message=message)
