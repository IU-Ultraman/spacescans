"""Registered datasets linked end to end: real pipeline steps, independent references.

``orchestrate.link(cohort, [], custom=[<names>])`` runs the C3 and C4 steps
in process on a synthetic cohort (multiple, overlapping and gapped episodes;
one participant outside every dated dataset's time span) for every kind the
registry adds: static (two-band), yearly, biweekly and daily rasters, static
and yearly point features, and a daily tract table. Each linked value is
compared with a reference computed here without the pipeline's C4 code: the
pixel values are read back with rasterio and weighted by the run's own C3
grid weights, the distances come from a brute-force search, and the
day-weighting is spelled out (PHASE_D §2.2):

    episode e, variable v:  sum_t d_et * z_etv / sum_t d_et   over the periods t whose
    spatial estimate z_etv is defined; d_et = days of e in t (both ends inclusive).

Episode level is checked here. The participant-level check (the same sums
over all of a participant's episodes) runs once ``link`` takes
``output_level`` (track A); until then it is skipped.

The tract table's C3 step needs national boundary files, so for that link
the C3 step is replaced by a synthetic weights table and only the C4 step
runs for real.
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from spacescans import custom

pytestmark = [pytest.mark.geo]
pytest.importorskip("exactextract")
rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

WEST, NORTH, RES, N = -86.5, 40.0, 0.01, 20
TOL = 1e-9

COHORT = pd.DataFrame({
    "pid": ["p1", "p1", "p2", "p2", "p3", "p4"],
    "startDate": ["2016-01-01", "2016-01-20", "2016-01-10", "2016-01-11", "2015-06-01",
                  "2019-01-01"],
    "endDate": ["2016-01-19", "2016-02-20", "2016-01-12", "2016-03-31", "2016-06-30",
                "2019-12-31"],
    "longitude": [-86.455, -86.425, -86.475, -86.395, -86.405, -86.445],
    "latitude": [39.955, 39.935, 39.975, 39.865, 39.915, 39.905],
})


def _d(text: str) -> date:
    return date.fromisoformat(text)


EPISODES = [(_d(s), _d(e)) for s, e in zip(COHORT["startDate"], COHORT["endDate"])]


def _overlap(episode: tuple[date, date], start: date, end: date) -> int:
    return max(0, (min(episode[1], end) - max(episode[0], start)).days + 1)


def _link(cohort, variables, *, level="episode", **kw):
    """``link`` at an output level; skips the participant level before track A's output_level."""
    from spacescans.orchestrate import link

    if "output_level" in inspect.signature(link).parameters:
        kw["output_level"] = level
    elif level != "episode":
        pytest.skip("participant-level output arrives with track A's output_level")
    return link(cohort, variables, **kw)


def _grid(seed: int, *, nodata_block: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.uniform(1, 100, (N, N)).astype("float32")
    if nodata_block:
        arr[2:6, 4:8] = -9999          # under episode 0's buffer: the SKIP rule
    return arr


def _tif(path: Path, arr) -> Path:
    from rasterio.transform import from_origin

    arr = np.asarray(arr, dtype="float32")
    arr = arr[None, ...] if arr.ndim == 2 else arr
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[2], height=arr.shape[1],
                       count=arr.shape[0], dtype="float32", crs="EPSG:4326",
                       transform=from_origin(WEST, NORTH, RES, RES), nodata=-9999) as dst:
        dst.write(arr)
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Registers every dataset once and links them in one run; returns what the tests need."""
    base = tmp_path_factory.mktemp("registered_link")
    root, src = base / "datasets", base / "src"
    src.mkdir()
    grids: dict[str, dict] = {}

    two_band = np.stack([_grid(1), _grid(2, nodata_block=True)])
    _tif(src / "static.tif", two_band)
    custom.register(src / "static.tif", name="Static two-band", kind="raster",
                    temporal="static", root=root,
                    variables=[{"name": "s_b2", "band": 2}, {"name": "s_b1", "band": 1}])
    grids["static"] = {None: two_band}

    yearly = {2015: _grid(3), 2016: _grid(4, nodata_block=True)}
    for y, arr in yearly.items():
        _tif(src / f"ndvi_{y}.tif", arr)
    custom.register([src / f"ndvi_{y}.tif" for y in yearly], name="NDVI yearly", kind="raster",
                    temporal="yearly", root=root, variables=["ndvi"])
    grids["yearly"] = yearly

    starts = ["20160101", "20160116", "20160201", "20160216"]
    biweekly = {s: _grid(10 + i, nodata_block=(i == 1)) for i, s in enumerate(starts)}
    for s, arr in biweekly.items():
        _tif(src / f"pm_{s}.tif", arr)
    custom.register([src / f"pm_{s}.tif" for s in starts], name="PM biweekly", kind="raster",
                    temporal="biweekly", root=root, variables=["pm_bw"])
    grids["biweekly"] = biweekly

    days = ["2016-01-01", "2016-01-02", "2016-01-03", "2016-01-11", "2016-02-14"]
    daily = {d: _grid(20 + i, nodata_block=(i == 0)) for i, d in enumerate(days)}
    for d, arr in daily.items():
        _tif(src / f"t_{d}.tif", arr)
    custom.register([src / f"t_{d}.tif" for d in days], name="T daily", kind="raster",
                    temporal="daily", root=root, variables=["t_daily"])
    grids["daily"] = daily

    sites = gpd.GeoDataFrame(
        {"year": [2015, 2015, 2016, 2016]},
        geometry=gpd.points_from_xy([-86.41, -86.30, -86.47, -86.10],
                                    [39.90, 39.95, 39.97, 39.50]), crs="EPSG:4326")
    sites.to_file(src / "sites.gpkg")
    custom.register(src / "sites.gpkg", name="Sites yearly", kind="features", temporal="yearly",
                    root=root, variables=["site_m"])
    sites[["geometry"]].to_file(src / "all_sites.geojson")
    custom.register(src / "all_sites.geojson", name="Sites static", kind="features",
                    temporal="static", root=root)

    names = ["Static two-band", "NDVI yearly", "PM biweekly", "T daily", "Sites yearly",
             "Sites static"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(custom.DATASETS_DIR_ENV, str(root))
        res = _link(COHORT, [], custom=names, data_dir=base / "data", run_dir=base / "run",
                    executor="inprocess")
    return {"res": res, "root": root, "grids": grids, "sites": sites, "base": base,
            "names": names}


def _weights(run_dir: Path) -> pd.DataFrame:
    (path,) = sorted((run_dir / "output").glob("c3_customgrid_*.parquet"))
    return pd.read_parquet(path)


def _z(weights: pd.DataFrame, arr: np.ndarray, geoid: int) -> float:
    """Buffer-weighted mean of the defined pixels under episode ``geoid``; NaN if none."""
    w = weights[weights["geoid"] == geoid]
    ids = w["grid_id"].to_numpy(dtype="int64")
    vals = arr[ids // N, ids % N].astype("float64")
    ok = vals != -9999
    if not ok.any():
        return float("nan")
    return float((w["weight"].to_numpy()[ok] * vals[ok]).sum() / w["weight"].to_numpy()[ok].sum())


def _reference(contrib: list[list[tuple[int, float]]]) -> tuple[list[float], list[tuple[float, int]]]:
    """Episode values and (sum d*z, sum d) per episode from (days, estimate) pairs."""
    values, sums = [], []
    for pairs in contrib:
        ok = [(d, z) for d, z in pairs if d > 0 and np.isfinite(z)]
        num = sum(d * z for d, z in ok)
        den = sum(d for d, _ in ok)
        values.append(num / den if den else float("nan"))
        sums.append((num, den))
    return values, sums


def _assert_column(res, col: str, expected: list[float]):
    got = res.exposures.sort_values("episode_id")[col].to_numpy(dtype="float64")
    assert np.isnan(got).tolist() == np.isnan(expected).tolist(), (col, got, expected)
    np.testing.assert_allclose(got[~np.isnan(got)], np.asarray(expected)[~np.isnan(got)],
                               rtol=0, atol=TOL, err_msg=col)


def _raster_contrib(world, kind: str, band: int | None = None):
    weights = _weights(world["res"].run_dir)
    out = []
    for geoid, episode in enumerate(EPISODES):
        pairs = []
        for key, arr in world["grids"][kind].items():
            arr = arr[band] if band is not None else arr
            if kind == "static":
                start, end = episode
            elif kind == "yearly":
                start, end = date(key, 1, 1), date(key, 12, 31)
            elif kind == "daily":
                start = end = _d(key)
            else:
                start = date(int(key[:4]), int(key[4:6]), int(key[6:]))
                end = (date(start.year, start.month, 15) if start.day == 1 else
                       date(start.year, start.month + 1, 1) - timedelta(days=1))
            pairs.append((_overlap(episode, start, end), _z(weights, arr, geoid)))
        out.append(pairs)
    return out


@pytest.mark.parametrize("col,kind,band", [
    ("s_b2", "static", 1), ("s_b1", "static", 0), ("ndvi", "yearly", None),
    ("pm_bw", "biweekly", None), ("t_daily", "daily", None),
])
def test_raster_episode_values_equal_the_reference(world, col, kind, band):
    expected, _ = _reference(_raster_contrib(world, kind, band))
    _assert_column(world["res"], col, expected)


def test_the_dated_rasters_leave_episodes_without_data_missing(world):
    ex = world["res"].exposures.sort_values("episode_id")
    assert ex["pm_bw"].isna().tolist() == [False, False, False, False, False, True]
    assert ex["t_daily"].isna().tolist() == [False, False, False, False, False, True]
    # episode 0 lies in 2016 only, over the 2016 grid's nodata block: no defined estimate
    assert ex["ndvi"].isna().tolist() == [True, False, False, False, False, True]


def _distances(sites: gpd.GeoDataFrame) -> np.ndarray:
    from pyproj import Transformer

    to_m = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    xs, ys = to_m.transform(COHORT["longitude"].to_numpy(), COHORT["latitude"].to_numpy())
    geoms = list(sites.to_crs("EPSG:5070").geometry)
    return np.array([min(shapely.Point(x, y).distance(g) for g in geoms) for x, y in zip(xs, ys)])


def _feature_contrib(world, yearly: bool):
    sites = world["sites"]
    out = []
    for geoid, episode in enumerate(EPISODES):
        if not yearly:
            out.append([((episode[1] - episode[0]).days + 1, _distances(sites)[geoid])])
            continue
        out.append([(_overlap(episode, date(y, 1, 1), date(y, 12, 31)),
                     _distances(sites[sites["year"] == y])[geoid]) for y in (2015, 2016)])
    return out


def test_feature_distances_equal_a_brute_force_reference(world):
    static, _ = _reference(_feature_contrib(world, yearly=False))
    yearly, _ = _reference(_feature_contrib(world, yearly=True))
    _assert_column(world["res"], "sites_static_dist_m", static)
    _assert_column(world["res"], "site_m", yearly)
    # a year without a features file contributes nothing: p4 lives in 2019 only
    assert np.isnan(yearly[5]) and not np.isnan(static[5])


def test_the_run_is_reported_like_any_custom_link(world):
    res = world["res"]
    assert res.ok and res.failed == [] and list(res.partials) == ["custom"]
    assert list(res.exposures.columns) == ["episode_id", "s_b2", "s_b1", "ndvi", "pm_bw",
                                           "t_daily", "site_m", "sites_static_dist_m"]
    feats = res.features.set_index("feature_name")
    assert feats.loc["site_m", "short_description"] == "Distance to the nearest point of " \
                                                       "Sites yearly (meters, EPSG:5070)"
    keys = [custom.get_dataset(n, world["root"]).variable_key for n in world["names"]]
    assert res.coverage["variable"].tolist() == keys
    prov = res.provenance["custom"]
    assert set(prov) == set(keys)
    run = res.run_dir
    # features run no C3; the raster grid is computed once for the four rasters (one grid)
    c3 = sorted(p.name for p in (run / "output").glob("c3_*.parquet"))
    assert len(c3) == 1 and c3[0].startswith("c3_customgrid_")
    materialised = sorted(p.name for p in (run / "output").glob("values_*.parquet"))
    assert len(materialised) == 6
    assert sorted(p.name for p in (run / "output").glob("weights_*.parquet")) == sorted(
        f"weights_{k}.parquet" for k in keys[4:])


def test_participant_values_pool_the_episode_days(world, tmp_path):
    """§2.2: x_i = sum over the participant's episodes and periods of d*z / sum of d."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(custom.DATASETS_DIR_ENV, str(world["root"]))
        res = _link(COHORT, [], level="participant", custom=world["names"],
                    data_dir=tmp_path / "data", run_dir=tmp_path / "run", executor="inprocess")
    table = res.exposures.set_index("pid")
    assert list(table.index) == ["p1", "p2", "p3", "p4"]
    checks = {"s_b2": _raster_contrib(world, "static", 1), "ndvi": _raster_contrib(world, "yearly"),
              "pm_bw": _raster_contrib(world, "biweekly"),
              "t_daily": _raster_contrib(world, "daily"),
              "site_m": _feature_contrib(world, yearly=True),
              "sites_static_dist_m": _feature_contrib(world, yearly=False)}
    for col, contrib in checks.items():
        _, sums = _reference(contrib)
        for pid in table.index:
            rows = [i for i, p in enumerate(COHORT["pid"]) if p == pid]
            num = sum(sums[i][0] for i in rows)
            den = sum(sums[i][1] for i in rows)
            want = num / den if den else float("nan")
            got = float(table.loc[pid, col])
            if np.isnan(want):
                assert np.isnan(got), (col, pid)
            else:
                assert got == pytest.approx(want, abs=TOL), (col, pid)


# ---------------------------------------------------------------------------
# daily tract table (synthetic C3 weights, real C4)
# ---------------------------------------------------------------------------

CODES = {"A": "18097000100", "B": "18097000200", "C": "18097000300", "D": "18097000400"}
# episode -> [(code, overlap share)]; C has no row in the table at all
TRACT_WEIGHTS = {0: [("A", 0.6), ("B", 0.4)], 1: [("B", 1.0)], 2: [("C", 1.0)],
                 3: [("A", 0.5), ("D", 0.5)], 4: [("A", 1.0)], 5: [("A", 1.0)]}


def _table_value(code: str, day: date) -> float | None:
    if code == "D" and day.day % 2 == 0:
        return None
    if code == "B" and day == date(2016, 1, 5):
        return None
    return 10.0 * "ABCD".index(code) + day.day + day.month / 10


class _FakeTractC3:
    """The in-process executor, except that ``c3_tract_us`` writes the synthetic weights."""

    def __init__(self):
        from spacescans.orchestrate.execute import InProcessExecutor
        self.real = InProcessExecutor(threads=1)

    def command(self, config, data_dir):
        return self.real.command(config, data_dir)

    def run(self, config, *, data_dir, cwd, on_line, cancel=None):
        import yaml

        from spacescans.orchestrate.execute import StepRun

        if Path(config).stem != "c3_tract_us":
            return self.real.run(config, data_dir=data_dir, cwd=cwd, on_line=on_line,
                                 cancel=cancel)
        cfg = yaml.safe_load(Path(config).read_text())
        rows = [{"geoid": g, "GEOID10": CODES[c], "value": w}
                for g, pairs in TRACT_WEIGHTS.items() for c, w in pairs]
        out = Path(cfg["output"]["path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(out, index=False)
        return StepRun(rc=0, seconds=0.0, maxrss_bytes=None, tail=[])


@pytest.fixture
def daily_table(tmp_path, monkeypatch):
    import importlib

    link_mod = importlib.import_module("spacescans.orchestrate.link")
    monkeypatch.setattr(link_mod, "_check_data",
                        lambda groups, overlay, data_dir: ({"data_dir": str(data_dir),
                                                            "data_release": "test",
                                                            "variables": {}, "datasets": {}}, []))
    lines = ["GEOID10,date,pm25"]
    day = date(2016, 1, 1)
    while day <= date(2016, 2, 29):
        for code in ("A", "B", "D"):
            v = _table_value(code, day)
            lines.append(f"{CODES[code]},{day.isoformat()},{'NA' if v is None else v}")
        day += timedelta(days=1)
    src = tmp_path / "pm_daily.csv"
    src.write_text("\n".join(lines) + "\n")
    root = tmp_path / "datasets"
    rec = custom.register(src, name="PM daily tracts", kind="polygon_table", temporal="daily",
                          key="tract", root=root)
    return rec


def _table_contrib():
    out = []
    for geoid, episode in enumerate(EPISODES):
        pairs = []
        day = episode[0]
        while day <= episode[1]:
            vals = [(w, _table_value(c, day)) for c, w in TRACT_WEIGHTS[geoid]
                    if c != "C" and day <= date(2016, 2, 29) and day >= date(2016, 1, 1)]
            vals = [(w, v) for w, v in vals if v is not None]
            z = (sum(w * v for w, v in vals) / sum(w for w, _ in vals)) if vals else float("nan")
            pairs.append((1, z))
            day += timedelta(days=1)
        out.append(pairs)
    return out


def test_a_daily_tract_table_links_through_the_gridded_path(daily_table, tmp_path):
    rec = daily_table
    res = _link(COHORT, [], custom=[rec], data_dir=tmp_path / "data", run_dir=tmp_path / "run",
                executor=_FakeTractC3())
    assert res.ok, res.errors
    expected, _ = _reference(_table_contrib())
    _assert_column(res, "pm25", expected)
    assert np.isnan(expected[2]) and np.isnan(expected[5])      # code C; 2019
    run = res.run_dir / "output"
    weights = pd.read_parquet(run / f"weights_{rec.variable_key}.parquet")
    assert list(weights.columns) == ["geoid", "grid_id", "weight"]
    values = pd.read_parquet(run / f"values_{rec.variable_key}.parquet")
    assert list(values.columns) == ["grid_id", "date", "pm25"]
    assert set(values["grid_id"]) == {CODES["A"], CODES["B"], CODES["D"]}
    # only the codes under the cohort and its dates were read from the national table
    assert len(values) == 3 * 60
    cfg = (res.run_dir / "pipeline_configs" / f"c4_{rec.variable_key}.yaml").read_text()
    assert "linkage_pattern: gridded" in cfg and "date_col: date" in cfg
    diag = [w["message"] for w in res.warnings if w["code"] == "custom_linkage"]
    assert diag == []      # 4 of 6 episodes linked: above the 50 % warning line


def test_the_mapping_form_and_mixed_lists_still_work(daily_table, tmp_path):
    rec = daily_table
    d = rec.definition()
    res = _link(COHORT, [rec.variable_key], custom={rec.variable_key: d},
                data_dir=tmp_path / "data", run_dir=tmp_path / "run1", executor=_FakeTractC3())
    assert res.ok and "pm25" in res.exposures.columns
    res2 = _link(COHORT, [], custom=[d], data_dir=tmp_path / "data", run_dir=tmp_path / "run2",
                 executor=_FakeTractC3())
    pd.testing.assert_frame_equal(res.exposures, res2.exposures)
