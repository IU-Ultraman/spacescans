"""Registered features: the distance from each location to the nearest point, line or polygon.

A ``features`` dataset is a set of geometries of ONE family (points, lines or
polygons; their multi-part forms count as the same family) from a
GeoPackage, a GeoJSON file, a zipped Shapefile or a GeoParquet file. It is
``static`` (one file) or ``yearly``: one file per year (the year from a
``{path: year}`` mapping, a manifest CSV or the file name), or one file with
a year column (``key={"year_column": ...}``, default a column called
``year``). ``key={"layer": ...}`` picks the layer of a multi-layer file.

:func:`prepare_features` requires a coordinate reference system (GeoJSON is
WGS84 by definition), drops empty geometries, repairs invalid polygons,
refuses data that does not touch the continental US, and writes the
geometries alone (no attribute columns) as GeoParquet in EPSG:5070 (NAD83 /
Conus Albers, meters): ``features.parquet``, or ``features_<year>.parquet``
per year.

The linked value is one column (default ``<name>_dist_m``): the planar
distance in EPSG:5070 meters from the location (the cohort row's point, no
buffer) to the nearest feature, 0 inside a polygon. The search is exact
(an STR-tree nearest query over every feature of the period), so the result
does not depend on how far away the nearest feature is or on the other
locations in the cohort. At run time :func:`materialise_features` writes the
per-location distances (one row per cohort row and, for yearly data, per
year with a file) and :func:`spacescans.custom.steps` links them with the
``static_areal`` / ``yearly_areal`` patterns: a location's value over an
episode is the mean of its yearly distances weighted by the days the episode
spends in each year that has data. (The generic ``proximity`` linkage pattern
computes the same exact distance for hand-written YAML configs; registered
datasets do not go through it.)
"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.custom.periods import assign_periods, read_manifest, split_manifest
from spacescans.custom.prepare import (
    Prepared,
    common_definition,
    normalise_variables,
    public_variables,
    slug,
)
from spacescans.custom.registry import DatasetError, sha256_of

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

    from spacescans.custom.registry import DatasetRecord, Input
    from spacescans.custom.values import MaterialisedValues

FEATURES_FILE = "features.parquet"
CRS_METRIC = "EPSG:5070"
_SUFFIXES = (".gpkg", ".geojson", ".json", ".zip", ".parquet", ".geoparquet")
#: Geometry type -> family.
FAMILIES = {
    "Point": "point", "MultiPoint": "point",
    "LineString": "line", "MultiLineString": "line", "LinearRing": "line",
    "Polygon": "polygon", "MultiPolygon": "polygon",
}


def _require_geo() -> None:
    from spacescans._extras import require
    require("geo", "geopandas", "shapely", "pyproj")


def year_file(year: int) -> str:
    return f"features_{int(year)}.parquet"


# ---------------------------------------------------------------------------
# reading and checking
# ---------------------------------------------------------------------------

def _read(path: Path, layer: str | None) -> Any:
    """A GeoDataFrame from a GeoPackage, GeoJSON, zipped Shapefile or GeoParquet file."""
    import geopandas as gpd
    import pyogrio

    low = path.name.lower()
    if low.endswith((".parquet", ".geoparquet")):
        if layer is not None:
            raise DatasetError(f"{path.name}: a GeoParquet file has no layers")
        try:
            return gpd.read_parquet(path)
        except Exception:  # noqa: BLE001 - any reader failure is a user error here
            raise DatasetError(f"{path.name} could not be read as GeoParquet") from None
    # OGR opens the file by content with every driver: refuse an OGR VRT disguised as a
    # GeoPackage/GeoJSON/zip (format confusion -> local file read / SSRF). Review finding R2-1.
    from spacescans.custom.safety import reject_format_confusion
    reject_format_confusion(path, DatasetError)
    if not low.endswith(_SUFFIXES):
        raise DatasetError(f"{path.name}: features come as a GeoPackage (.gpkg), GeoJSON "
                           "(.geojson), zipped Shapefile (.zip) or GeoParquet file")
    try:
        layers = [str(row[0]) for row in pyogrio.list_layers(path)]
    except Exception:  # noqa: BLE001
        raise DatasetError(f"{path.name} could not be opened as a GeoPackage, GeoJSON or "
                           "zipped Shapefile") from None
    if layer is None:
        if len(layers) != 1:
            shown = ", ".join(layers[:10]) + (" ..." if len(layers) > 10 else "")
            raise DatasetError(f"{path.name} holds {len(layers)} layers ({shown}); name one "
                               "with key={'layer': ...}")
        layer = layers[0]
    elif layer not in layers:
        raise DatasetError(f"{path.name} has no layer {layer!r} (it has: {', '.join(layers[:10])})")
    try:
        return gpd.read_file(path, layer=layer)
    except Exception:  # noqa: BLE001
        raise DatasetError(f"{path.name}: layer {layer!r} could not be read") from None


def _polygonal(geom: Any) -> Any:
    """The polygonal part of a repaired geometry (``make_valid`` may add lines or points)."""
    import shapely
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in shapely.get_parts(geom) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return None
    return shapely.union_all(parts)


def _clean(gdf: Any, filename: str) -> tuple[Any, str, dict[str, int]]:
    """Drop empty geometries, check one family, repair polygons; (gdf, family, counts)."""
    import shapely

    if gdf.crs is None:
        raise DatasetError(f"{filename} has no coordinate reference system; assign one (for "
                           "example EPSG:4326) and export it again")
    geoms = gdf.geometry
    keep = ~(geoms.isna() | geoms.is_empty)
    dropped = int((~keep).sum())
    gdf = gdf[keep.to_numpy()].copy()
    if gdf.empty:
        raise DatasetError(f"{filename} holds no geometries")
    types = gdf.geom_type.value_counts()
    unknown = [t for t in types.index if t not in FAMILIES]
    if unknown:
        raise DatasetError(f"{filename}: geometry type(s) {', '.join(map(str, unknown))} are not "
                           "supported; use points, lines or polygons")
    families: dict[str, int] = {}
    for gtype, n in types.items():
        families[FAMILIES[gtype]] = families.get(FAMILIES[gtype], 0) + int(n)
    if len(families) > 1:
        shown = ", ".join(f"{n:,} {fam}s" for fam, n in sorted(families.items()))
        raise DatasetError(f"{filename} mixes geometry families ({shown}); a features dataset "
                           "holds points, lines or polygons, not several")
    family = next(iter(families))
    repaired = 0
    if family == "polygon":
        invalid = ~gdf.geometry.is_valid
        repaired = int(invalid.sum())
        if repaired:
            fixed = [_polygonal(g) for g in shapely.make_valid(gdf.geometry[invalid].to_numpy())]
            gdf.loc[invalid, gdf.geometry.name] = fixed
            empty = gdf.geometry.isna() | gdf.geometry.is_empty
            dropped += int(empty.sum())
            gdf = gdf[~empty.to_numpy()]
            if gdf.empty:
                raise DatasetError(f"{filename} holds no usable polygons")
    return gdf, family, {"dropped_empty": dropped, "repaired_invalid": repaired}


def _check_conus(gdf: Any, filename: str) -> list[float]:
    """WGS84 bounds of the features; refuses data that does not touch the continental US."""
    import shapely

    from spacescans.custom.raster import CONUS_BBOX

    wgs = gdf.to_crs("EPSG:4326")
    inside = int(wgs.intersects(shapely.box(*CONUS_BBOX)).sum())
    if not inside:
        raise DatasetError(f"{filename}: no feature lies over the continental US; check the "
                           "file's coordinate reference system (a wrong one puts the data "
                           "elsewhere)")
    return [float(v) for v in wgs.total_bounds]


def _year_column(gdf: Any, wanted: str | None, filename: str) -> str | None:
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    if wanted is not None:
        if wanted not in cols:
            raise DatasetError(f"{filename} has no column {wanted!r} (it has: "
                               f"{', '.join(map(str, cols[:20]))})")
        return wanted
    return next((c for c in cols if str(c).lower() == "year"), None)


def _split_years(gdf: Any, column: str, filename: str) -> dict[int, Any]:
    import pandas as pd

    raw = pd.to_numeric(gdf[column], errors="coerce")
    bad = raw.isna() | (raw != raw.round()) | (raw < 1900) | (raw > 2100)
    if bool(bad.any()):
        raise DatasetError(f"{filename}: {int(bad.sum()):,} of {len(gdf):,} features have no "
                           f"whole-number year in 1900-2100 in {column!r}")
    years = raw.astype("int64")
    return {int(y): gdf[(years == y).to_numpy()] for y in sorted(years.unique())}


def _variable(variables: Any, name: str, family: str) -> dict:
    vars_ = normalise_variables(variables)
    if vars_ is None:
        vars_ = [{"name": f"{slug(name)}_dist_m", "unit": None, "description": None}]
    if len(vars_) != 1:
        raise DatasetError("a features dataset links one variable: the distance to the nearest "
                           "feature")
    var = dict(vars_[0])
    var["unit"] = var.get("unit") or "m"
    var["description"] = var.get("description") or (
        f"Distance to the nearest {family} of {name} (meters, EPSG:5070)")
    return var


def prepare_features(inputs: Sequence[Input], work: Path, *, name: str, temporal: str,
                     key: Any, variables: Any) -> Prepared:
    """Check the feature file(s) and write GeoParquet (EPSG:5070) into ``work``."""
    _require_geo()
    if key is None:
        key = {}
    if not isinstance(key, dict):
        raise DatasetError("key for features is None or {'year_column': ..., 'layer': ...}")
    unknown = sorted(set(key) - {"year_column", "layer"})
    if unknown:
        raise DatasetError(f"unknown key field(s) for features: {', '.join(map(str, unknown))} "
                           "(known: layer, year_column)")
    layer, year_col_wanted = key.get("layer"), key.get("year_column")
    if temporal == "static" and year_col_wanted is not None:
        raise DatasetError("year_column is only used by yearly features")
    given = {i.path: i.period for i in inputs}
    data, manifest_path = split_manifest([i.path for i in inputs], _SUFFIXES)
    if not data:
        raise DatasetError("give a GeoPackage, GeoJSON, zipped Shapefile or GeoParquet file")
    if manifest_path is not None and temporal == "static":
        raise DatasetError("static features take no manifest")

    # (year or None, source path, frame)
    pieces: list[tuple[int | None, Path, Any]] = []
    if temporal == "static":
        if len(data) != 1 or given[data[0]] is not None:
            raise DatasetError("static features are one file, with no period")
        pieces.append((None, data[0], _read(data[0], layer)))
    else:
        first = _read(data[0], layer) if len(data) == 1 else None
        column = None
        if first is not None and given[data[0]] is None and manifest_path is None:
            column = _year_column(first, year_col_wanted, data[0].name)
        elif year_col_wanted is not None:
            raise DatasetError("year_column is used with one features file (no manifest and no "
                               "{path: year} mapping)")
        if column is not None:
            for year, part in _split_years(first, column, data[0].name).items():
                pieces.append((year, data[0], part))
        else:
            manifest = read_manifest(manifest_path, "yearly") if manifest_path else None
            periods = assign_periods([(p.name, given[p]) for p in data], "yearly", manifest)
            for path, period in sorted(zip(data, periods), key=lambda t: t[1].year):
                frame = first if (first is not None and path == data[0]) else _read(path, layer)
                pieces.append((period.year, path, frame))

    family = None
    counts = {"dropped_empty": 0, "repaired_invalid": 0}
    cleaned: list[tuple[int | None, Path, Any]] = []
    for year, path, frame in pieces:
        gdf, fam, c = _clean(frame, path.name)
        if family is not None and fam != family:
            raise DatasetError(f"{path.name} holds {fam}s but the other files hold {family}s; "
                               "a features dataset holds one geometry family")
        family = fam
        for k in counts:
            counts[k] += c[k]
        cleaned.append((year, path, gdf))
    import geopandas as gpd
    import pandas as pd
    everything = gpd.GeoDataFrame(geometry=pd.concat([g.geometry.to_crs("EPSG:4326")
                                                      for _, _, g in cleaned],
                                                     ignore_index=True), crs="EPSG:4326")
    bounds = _check_conus(everything, ", ".join(sorted({p.name for _, p, _ in cleaned})))

    files, prepared_sha, by_year = [], {}, {}
    for year, _path, gdf in cleaned:
        rel = FEATURES_FILE if year is None else year_file(year)
        out = gpd.GeoDataFrame(geometry=gdf.geometry.to_crs(CRS_METRIC).reset_index(drop=True),
                               crs=CRS_METRIC)
        out.to_parquet(work / rel, index=False)
        files.append(rel)
        prepared_sha[rel] = sha256_of(work / rel)[0]
        if year is not None:
            by_year[str(year)] = len(out)
    originals = []
    for path in [*dict.fromkeys(p for _, p, _ in cleaned), *([manifest_path] if manifest_path else [])]:
        sha, size = sha256_of(path)
        originals.append({"name": path.name, "sha256": sha, "bytes": size, "path": path})
    var = _variable(variables, name, family)
    details: dict[str, Any] = {
        "geometry_type": family,
        "feature_count": int(sum(len(g) for _, _, g in cleaned)),
        "bounds_wgs84": bounds,
        "crs": str(cleaned[0][2].crs),
        "crs_prepared": CRS_METRIC,
        **counts,
        "prepared_sha256": prepared_sha,
    }
    if layer is not None:
        details["layer"] = layer
    if by_year:
        details["features_by_year"] = by_year
        years = sorted(int(y) for y in by_year)
        details["coverage_years"] = [years[0], years[-1]]
    stored_key = {k: v for k, v in key.items() if v is not None} or None
    when = (f", {details['coverage_years'][0]} to {details['coverage_years'][1]}"
            if by_year else "")
    return Prepared(key=stored_key, variables=public_variables([var]), files=files,
                    details=details, originals=originals,
                    description=f"Registered {temporal} {family} features ({details['feature_count']:,}"
                                f" features{when}); value: distance to the nearest feature in "
                                "meters.")


def features_definition(record: DatasetRecord) -> dict[str, Any]:
    """The linkable definition of a registered features dataset."""
    from spacescans.custom.definition import STATIC_COVERAGE_YEARS

    details = record.details
    sha = details.get("prepared_sha256") or {}
    files = []
    for rel in record.files:
        m = re.fullmatch(r"features_(\d{4})\.parquet", rel)
        files.append({"path": os.fspath(record.path(rel)),
                      "year": int(m.group(1)) if m else None, "sha256": sha.get(rel)})
    years = (details.get("coverage_years") if record.temporal == "yearly"
             else list(STATIC_COVERAGE_YEARS))
    d = common_definition(record, geometry="features", boundary="Point",
                          spatial_method="proximity", coverage_years=years)
    d.update({
        "feature_type": details.get("geometry_type"),
        "join_col": "geoid",
        "key_col": "geoid",
        "year_col": "year" if record.temporal == "yearly" else None,
        "date_col": None,
        "values_path": None,
        "feature_files": files,
        "feature_count": details.get("feature_count"),
        "row_count": details.get("feature_count"),
        "distinct_keys": details.get("feature_count"),
        "sha256": record.checksum,
    })
    return d


# ---------------------------------------------------------------------------
# distances at run time
# ---------------------------------------------------------------------------

def nearest_distances(x: np.ndarray, y: np.ndarray, geometries: Sequence[Any]) -> np.ndarray:
    """Distance from each point ``(x, y)`` to the nearest of ``geometries`` (same planar CRS).

    NaN for a point with a missing coordinate, and for every point when there
    is no geometry.
    """
    import numpy as np
    import shapely

    x = np.asarray(x, dtype="float64")
    y = np.asarray(y, dtype="float64")
    out = np.full(x.shape[0], np.nan)
    geoms = [g for g in geometries if g is not None and not g.is_empty]
    ok = np.isfinite(x) & np.isfinite(y)
    if not geoms or not ok.any():
        return out
    tree = shapely.STRtree(geoms)
    where = np.flatnonzero(ok)
    idx, dist = tree.query_nearest(shapely.points(x[where], y[where]), return_distance=True,
                                   all_matches=False)
    out[where[idx[0]]] = dist
    return out


def cohort_points(cohort_parquet: str | os.PathLike) -> Any:
    """``geoid``, ``x``, ``y`` (EPSG:5070 meters) of each cohort row, as the steps see them.

    ``geoid`` is the row's episode id, exactly as the pipeline's
    ``demo_conus`` adapter derives it from ``input.parquet``.
    """
    import pandas as pd
    from pyproj import Transformer

    from spacescans.io.readers import read_table
    from spacescans.linkage.helpers import _adapt_demo_conus

    patients = _adapt_demo_conus(read_table(cohort_parquet))
    lon = pd.to_numeric(patients["long"], errors="coerce").to_numpy(dtype="float64")
    lat = pd.to_numeric(patients["lat"], errors="coerce").to_numpy(dtype="float64")
    x, y = Transformer.from_crs("EPSG:4326", CRS_METRIC, always_xy=True).transform(lon, lat)
    return pd.DataFrame({"geoid": patients["geoid"].astype("int64").to_numpy(),
                         "x": x, "y": y})


def materialise_features(definition: Any, *, cohort_parquet: str | os.PathLike,
                         values_out: str | os.PathLike, weights_out: str | os.PathLike
                         ) -> MaterialisedValues:
    """Write the per-location distance table and its one-to-one weights for a run.

    ``values_out``: ``geoid[, year], <variable>`` with a row per cohort row
    (and year with a file) whose distance is defined; ``weights_out``:
    ``geoid, weight`` (1.0), the identity "overlap" the areal patterns join
    through. Returns where the table went and the log line.
    """
    _require_geo()
    import geopandas as gpd
    import numpy as np
    import pandas as pd

    from spacescans.custom.values import MaterialisedValues

    points = cohort_points(cohort_parquet)
    col = list(definition["value_cols"])[0]
    yearly = bool(definition.get("year_col"))
    frames = []
    for item in definition.get("feature_files") or []:
        geoms = gpd.read_parquet(item["path"]).to_crs(CRS_METRIC).geometry.to_numpy()
        dist = nearest_distances(points["x"].to_numpy(), points["y"].to_numpy(), geoms)
        frame = pd.DataFrame({"geoid": points["geoid"].to_numpy(), col: dist})
        if yearly:
            frame.insert(1, "year", np.int64(item["year"]))
        frames.append(frame[np.isfinite(dist)])
    table = (pd.concat(frames, ignore_index=True) if frames else
             pd.DataFrame({"geoid": pd.Series(dtype="int64"), col: pd.Series(dtype="float64")}))
    out = Path(values_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    weights = pd.DataFrame({"geoid": points["geoid"].to_numpy(), "weight": 1.0})
    weights.to_parquet(weights_out, index=False)
    message = (f"materialised {len(table):,} (geoid{', year' if yearly else ''}) distance rows "
               f"for {len(points):,} locations from {len(definition.get('feature_files') or [])} "
               f"feature file(s); locations without a distance: "
               f"{int(len(points) * max(1, len(frames)) - len(table)):,}")
    return MaterialisedValues(path=out, rows=len(table), message=message)
