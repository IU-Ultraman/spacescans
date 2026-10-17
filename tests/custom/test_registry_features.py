"""Registered features: points, lines or polygons; distance to the nearest one."""
from __future__ import annotations

import os
import zipfile

import numpy as np
import pandas as pd
import pytest

from spacescans import custom

pytestmark = [pytest.mark.geo]
gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "datasets"


def _points(lons, lats, **cols):
    return gpd.GeoDataFrame(cols, geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")


def _zip_shapefile(gdf, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    gdf.to_file(folder / f"{name}.shp")
    out = folder.parent / f"{name}.zip"
    with zipfile.ZipFile(out, "w") as z:
        for f in sorted(os.listdir(folder)):
            z.write(folder / f, f)
    return out


def test_static_points_become_geoparquet_in_conus_albers(tmp_path, root):
    src = tmp_path / "sites.gpkg"
    _points([-86.1, -86.3, -87.0], [39.8, 39.7, 41.0], site=["a", "b", "c"]).to_file(src)
    rec = custom.register(src, name="Superfund sites", kind="features", temporal="static",
                          root=root)
    assert rec.files == ["features.parquet"] and rec.key is None
    assert rec.variables == [{"name": "superfund_sites_dist_m", "unit": "m",
                              "description": "Distance to the nearest point of Superfund sites "
                                             "(meters, EPSG:5070)"}]
    stored = gpd.read_parquet(rec.path("features.parquet"))
    assert stored.crs.to_epsg() == 5070
    assert list(stored.columns) == ["geometry"]           # no attribute columns kept
    assert len(stored) == 3
    d = rec.details
    assert (d["geometry_type"], d["feature_count"], d["crs_prepared"]) == ("point", 3, "EPSG:5070")
    assert d["dropped_empty"] == 0 and len(d["bounds_wgs84"]) == 4
    definition = rec.definition()
    assert (definition["geometry"], definition["spatial_method"], definition["feature_type"]) == (
        "features", "proximity", "point")
    assert definition["value_cols"] == ["superfund_sites_dist_m"]
    assert definition["value_units"] == {"superfund_sites_dist_m": "m"}
    assert definition["display_unit"] == "m"
    assert definition["feature_files"][0]["year"] is None
    checked = custom.check_definition(definition, key=rec.variable_key, deep=True)
    assert checked["join_col"] == "geoid"
    assert custom.needs_c3(definition) is False and custom.is_features(definition)


def test_lines_from_a_zipped_shapefile_and_polygons_from_geojson(tmp_path, root):
    lines = gpd.GeoDataFrame(geometry=[shapely.LineString([(-86.5, 39.5), (-86.0, 40.0)]),
                                       shapely.MultiLineString([[(-87, 39), (-86.9, 39.1)]])],
                             crs="EPSG:4326")
    rec = custom.register(_zip_shapefile(lines, tmp_path / "shp", "roads"), name="roads",
                          kind="features", temporal="static", root=root,
                          variables=[{"name": "road_m", "description": "to a road"}])
    assert rec.details["geometry_type"] == "line"
    assert rec.variables == [{"name": "road_m", "unit": "m", "description": "to a road"}]

    bowtie = shapely.Polygon([(-86.4, 39.6), (-86.2, 39.8), (-86.2, 39.6), (-86.4, 39.8)])
    assert not bowtie.is_valid
    polys = gpd.GeoDataFrame(geometry=[shapely.box(-86.1, 39.9, -86.0, 40.0), bowtie, None],
                             crs="EPSG:4326")
    src = tmp_path / "parks.geojson"
    polys.to_file(src)
    rec = custom.register(src, name="parks", kind="features", temporal="static", root=root)
    assert rec.details["geometry_type"] == "polygon"
    assert rec.details["repaired_invalid"] == 1 and rec.details["dropped_empty"] == 1
    stored = gpd.read_parquet(rec.path("features.parquet"))
    assert stored.is_valid.all() and len(stored) == 2


@pytest.mark.filterwarnings("ignore:'crs' was not provided")
def test_features_are_checked(tmp_path, root):
    mixed = gpd.GeoDataFrame(geometry=[shapely.Point(-86, 39),
                                       shapely.LineString([(-86, 39), (-85, 40)])],
                             crs="EPSG:4326")
    mixed.to_file(tmp_path / "mixed.geojson")
    with pytest.raises(custom.DatasetError, match=r"mixes geometry families \(1 lines, 1 points\)"):
        custom.register(tmp_path / "mixed.geojson", name="x", kind="features",
                        temporal="static", root=root)
    far = _points([10.123456], [50.654321])
    far.to_file(tmp_path / "far.geojson")
    with pytest.raises(custom.DatasetError, match="no feature lies over the continental US") as e:
        custom.register(tmp_path / "far.geojson", name="x", kind="features", temporal="static",
                        root=root)
    assert "10.1" not in str(e.value) and "50.6" not in str(e.value)
    nocrs = _points([-86.0], [39.0]).set_crs(None, allow_override=True)
    with pytest.raises(custom.DatasetError, match="no coordinate reference system"):
        custom.register(_zip_shapefile(nocrs, tmp_path / "nocrs", "nocrs"), name="x",
                        kind="features", temporal="static", root=root)
    gc = gpd.GeoDataFrame(geometry=[shapely.GeometryCollection([shapely.Point(-86, 39)])],
                          crs="EPSG:4326")
    gc.to_file(tmp_path / "gc.gpkg")
    with pytest.raises(custom.DatasetError, match="GeometryCollection are not supported"):
        custom.register(tmp_path / "gc.gpkg", name="x", kind="features", temporal="static",
                        root=root)
    two = tmp_path / "two.gpkg"
    _points([-86.0], [39.0]).to_file(two, layer="sites")
    _points([-86.1], [39.1]).to_file(two, layer="wells")
    with pytest.raises(custom.DatasetError, match=r"holds 2 layers \(sites, wells\)"):
        custom.register(two, name="x", kind="features", temporal="static", root=root)
    rec = custom.register(two, name="wells", kind="features", temporal="static", root=root,
                          key={"layer": "wells"})
    assert rec.key == {"layer": "wells"} and rec.details["layer"] == "wells"
    with pytest.raises(custom.DatasetError, match="has no layer 'pipes'"):
        custom.register(two, name="x", kind="features", temporal="static", root=root,
                        key={"layer": "pipes"})
    (tmp_path / "x.kml").write_text("<kml/>")
    with pytest.raises(custom.DatasetError, match="expected .gpkg"):
        custom.register(tmp_path / "x.kml", name="x", kind="features", temporal="static",
                        root=root)
    with pytest.raises(custom.DatasetError, match="links one variable"):
        custom.register(two, name="x", kind="features", temporal="static", root=root,
                        key={"layer": "wells"}, variables=["a", "b"])
    with pytest.raises(custom.DatasetError, match="temporal must be one of 'static', 'yearly'"):
        custom.register(two, name="x", kind="features", temporal="daily", root=root)
    assert [r.name for r in custom.list_datasets(root)] == ["wells"]


def test_yearly_features_from_a_year_column(tmp_path, root):
    src = tmp_path / "sites.gpkg"
    _points([-86.1, -86.3, -86.2], [39.8, 39.7, 39.9], year=[2016, 2015, 2016]).to_file(src)
    rec = custom.register(src, name="sites", kind="features", temporal="yearly", root=root)
    assert rec.files == ["features_2015.parquet", "features_2016.parquet"]
    assert rec.details["features_by_year"] == {"2015": 1, "2016": 2}
    assert rec.details["coverage_years"] == [2015, 2016]
    d = rec.definition()
    assert d["year_col"] == "year" and [f["year"] for f in d["feature_files"]] == [2015, 2016]
    custom.check_definition(d, key=rec.variable_key, deep=True)
    bad = tmp_path / "bad.gpkg"
    _points([-86.1, -86.3], [39.8, 39.7], opened=[2016.5, None]).to_file(bad)
    with pytest.raises(custom.DatasetError, match="2 of 2 features have no whole-number year in "
                                                  "1900-2100 in 'opened'"):
        custom.register(bad, name="x", kind="features", temporal="yearly", root=root,
                        key={"year_column": "opened"})
    with pytest.raises(custom.DatasetError, match="only used by yearly"):
        custom.register(src, name="x", kind="features", temporal="static", root=root,
                        key={"year_column": "year"})


def test_yearly_features_one_file_per_year(tmp_path, root):
    a, b = tmp_path / "wells_2014.geojson", tmp_path / "wells_2015.geojson"
    _points([-86.1], [39.8]).to_file(a)
    _points([-86.2, -86.4], [39.9, 39.6]).to_file(b)
    rec = custom.register([b, a], name="wells", kind="features", temporal="yearly", root=root)
    assert rec.files == ["features_2014.parquet", "features_2015.parquet"]
    assert rec.details["features_by_year"] == {"2014": 1, "2015": 2}
    rec = custom.register({a: 2010, b: 2011}, name="wells2", kind="features", temporal="yearly",
                          root=root)
    assert rec.details["coverage_years"] == [2010, 2011]
    lines = gpd.GeoDataFrame(geometry=[shapely.LineString([(-86, 39), (-85, 40)])],
                             crs="EPSG:4326")
    c = tmp_path / "roads_2016.geojson"
    lines.to_file(c)
    with pytest.raises(custom.DatasetError, match="holds lines but the other files hold points"):
        custom.register([a, c], name="x", kind="features", temporal="yearly", root=root)


def test_nearest_distances_match_a_brute_force_search():
    rng = np.random.default_rng(7)
    x = rng.uniform(0, 100_000, 300)
    y = rng.uniform(0, 100_000, 300)
    x[5] = np.nan
    lines = [shapely.LineString(rng.uniform(0, 100_000, (3, 2))) for _ in range(40)]
    polys = [shapely.Point(p).buffer(2_000) for p in rng.uniform(0, 100_000, (25, 2))]
    for geoms in (lines, polys, list(shapely.points(rng.uniform(0, 100_000, (60, 2))))):
        got = custom.nearest_distances(x, y, geoms)
        assert np.isnan(got[5])
        for i in (0, 1, 50, 299):
            expect = min(shapely.Point(x[i], y[i]).distance(g) for g in geoms)
            assert got[i] == pytest.approx(expect, abs=1e-6)
        brute = np.array([min(shapely.Point(a, b).distance(g) for g in geoms)
                          if np.isfinite(a) else np.nan for a, b in zip(x, y)])
        np.testing.assert_allclose(got, brute, rtol=0, atol=1e-6)
    inside = custom.nearest_distances([0.0], [0.0], [shapely.box(-1, -1, 1, 1)])
    assert inside.tolist() == [0.0]
    assert np.isnan(custom.nearest_distances([1.0], [2.0], [])).all()


def test_materialised_distances_follow_the_cohort_rows(tmp_path, root):
    from pyproj import Transformer

    from spacescans import cohort as _cohort
    from spacescans.custom.features import materialise_features

    src = tmp_path / "sites.gpkg"
    sites = _points([-86.10, -86.30, -86.20], [39.80, 39.70, 39.90], year=[2015, 2015, 2016])
    sites.to_file(src)
    rec = custom.register(src, name="sites", kind="features", temporal="yearly", root=root)
    people = pd.DataFrame({"pid": ["a", "a", "b"],
                           "startDate": ["2015-01-01", "2016-01-01", "2015-06-01"],
                           "endDate": ["2015-12-31", "2016-12-31", "2016-06-01"],
                           "longitude": [-86.15, -86.25, -86.31], "latitude": [39.85, 39.75, 39.71]})
    cohort_parquet = tmp_path / "input.parquet"
    _cohort.to_pipeline_parquet(people, cohort_parquet, pid_as_str=True, columns="required")
    res = materialise_features(rec.definition(), cohort_parquet=cohort_parquet,
                               values_out=tmp_path / "v.parquet", weights_out=tmp_path / "w.parquet")
    values = pd.read_parquet(res.path)
    assert list(values.columns) == ["geoid", "year", "sites_dist_m"]
    assert values[["geoid", "year"]].values.tolist() == [[0, 2015], [1, 2015], [2, 2015],
                                                         [0, 2016], [1, 2016], [2, 2016]]
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    for _, row in values.iterrows():
        px, py = to_m.transform(people["longitude"][row.geoid], people["latitude"][row.geoid])
        year_sites = sites[sites["year"] == row.year].to_crs("EPSG:5070")
        expect = min(shapely.Point(px, py).distance(g) for g in year_sites.geometry)
        assert row.sites_dist_m == pytest.approx(expect, abs=1e-6)
    weights = pd.read_parquet(tmp_path / "w.parquet")
    assert weights.values.tolist() == [[0, 1.0], [1, 1.0], [2, 1.0]]
    assert "6 (geoid, year) distance rows for 3 locations from 2 feature file(s)" in res.message


def test_a_features_definition_survives_rs_json_round_trip(tmp_path, root):
    def jsonlite(value):
        if isinstance(value, dict):
            return {k: jsonlite(v) for k, v in value.items()}
        if isinstance(value, list):
            items = [jsonlite(v) for v in value]
            return items[0] if len(items) == 1 else items
        return {} if value is None else value

    src = tmp_path / "sites.geojson"
    _points([-86.1], [39.8]).to_file(src)
    rec = custom.register(src, name="sites", kind="features", temporal="static", root=root)
    d = rec.definition()
    back = custom.check_definition(jsonlite(d), key=rec.variable_key, deep=True)
    assert back["feature_files"] == d["feature_files"]
    assert back["value_cols"] == d["value_cols"] and back["year_col"] is None


def _crs_arg(args, kwargs):
    return args[0] if args else kwargs.get("crs")


def test_features_that_proj_cannot_transform_are_refused(tmp_path, root, monkeypatch):
    """PROJ returns infinity when a transformation fails (e.g. a grid download); refuse it."""
    src = tmp_path / "sites.gpkg"
    _points([-86.1, -86.3], [39.8, 39.7]).to_file(src)
    real = gpd.GeoSeries.to_crs

    def broken(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        if str(_crs_arg(args, kwargs)) == "EPSG:5070":
            out = out.copy()
            out.iloc[0] = shapely.Point(float("inf"), float("inf"))
        return out

    monkeypatch.setattr(gpd.GeoSeries, "to_crs", broken)
    with pytest.raises(custom.DatasetError,
                       match=r"sites\.gpkg: 1 feature\(s\) have non-finite coordinates in "
                             r"EPSG:5070.*PROJ_NETWORK=OFF"):
        custom.register(src, name="sites", kind="features", temporal="static", root=root)
    monkeypatch.undo()
    assert [r.name for r in custom.list_datasets(root)] == []
    rec = custom.register(src, name="sites", kind="features", temporal="static", root=root)
    assert rec.details["feature_count"] == 2


def test_features_with_infinite_coordinates_in_the_file_are_refused(tmp_path, root):
    src = tmp_path / "bad.gpkg"
    gpd.GeoDataFrame(geometry=[shapely.Point(-86.1, 39.8), shapely.Point(float("inf"), 39.7)],
                     crs="EPSG:4326").to_file(src)
    with pytest.raises(custom.DatasetError,
                       match=r"bad\.gpkg: 1 feature\(s\) have non-finite coordinates in EPSG:4326"):
        custom.register(src, name="bad", kind="features", temporal="static", root=root)


def test_locations_that_proj_cannot_transform_stop_the_link(tmp_path, monkeypatch):
    import pyproj

    from spacescans import cohort as _cohort
    from spacescans.custom.features import cohort_points

    people = pd.DataFrame({"pid": ["a", "b"], "startDate": ["2015-01-01"] * 2,
                           "endDate": ["2015-12-31"] * 2,
                           "longitude": [-86.15, -86.25], "latitude": [39.85, 39.75]})
    cohort_parquet = tmp_path / "input.parquet"
    _cohort.to_pipeline_parquet(people, cohort_parquet, pid_as_str=True, columns="required")
    assert np.isfinite(cohort_points(cohort_parquet)[["x", "y"]].to_numpy()).all()
    real = pyproj.Transformer.from_crs

    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def transform(self, xx, yy):
            x, y = self.inner.transform(xx, yy)
            x = np.array(x, dtype="float64")
            x[1] = np.inf
            return x, y

    monkeypatch.setattr(pyproj.Transformer, "from_crs", lambda *a, **k: Broken(real(*a, **k)))
    with pytest.raises(custom.DatasetError,
                       match=r"^1 location\(s\) could not be transformed to EPSG:5070") as e:
        cohort_points(cohort_parquet)
    assert "-86" not in str(e.value) and "39." not in str(e.value)   # no coordinates in the text
