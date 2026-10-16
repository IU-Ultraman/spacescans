"""Participant-level output equals the episode output weighted by contributing days.

Every C4 pattern (plus the FAQSD reader, the C4 steps of the line-distance sources NHD and
TIGER, the CBP fallback with its ZBP input and the generic proximity pattern) runs twice on
the same synthetic inputs: with ``time.output_grouping: episode`` and with ``patient``, which
is what ``output_level="participant"`` renders. The participant output must equal, to 1e-9,
a reference built here from the EPISODE output and each episode's per-variable contributing
days, which this file computes on its own from the synthetic inputs (PHASE_D 2.2):

    x_iv = sum_e D_ev * x_ev / sum_e D_ev,      D_ev = sum_t d_et * [z_etv defined]

with d_et the days of episode e in source period t (start and end inclusive) and z_etv
defined when some unit of the episode's buffer (or the location itself, for distances) has
a value for v in t. A participant none of whose days contributes is missing. On the way,
every episode's value must be present exactly when its contributing days are positive,
which checks the independent definedness against the pattern's own.

The cohort mixes single and multiple episodes, overlapping episodes, gaps, blank dates,
episodes partly or wholly outside the source's years and a one-day episode; the sources
have units and periods without a value and units absent altogether. FARA recodes ``Urban``
and derives 13 flags from the aggregated rates, so its participant output is held to a
reference computed from the raw inputs (spatial mean over the valued tracts, day-weighted
years, then the recode), and its plain rates to the two-stage reference as well. ACAG's
``_nbm`` columns are base minus biomass of the participant means. All data are synthetic.
"""
from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from spacescans import cohort as _cohort
from spacescans.engine.duckdb_engine import DuckDBEngine
from spacescans.models.config import (
    BufferConfig,
    DatasetConfig,
    EngineConfig,
    ExposureConfig,
    OutputConfig,
    SourceConfig,
    TimeConfig,
)

ATOL = 1e-9
YEARS = [2014, 2015, 2016]
SEED = 20261016


# ---------------------------------------------------------------------------------------------
# the cohort
# ---------------------------------------------------------------------------------------------

def _synthetic_cohort(seed: int = SEED) -> pd.DataFrame:
    """Random residential histories (moves, overlaps, gaps) plus hand-made edge cases."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []

    def add(pid: str, start: str, end: str) -> None:
        rows.append({"pid": pid, "startDate": start, "endDate": end,
                     "longitude": float(rng.uniform(-86.45, -85.95)),
                     "latitude": float(rng.uniform(39.55, 40.05))})

    def day(t: pd.Timestamp) -> str:
        return t.strftime("%Y-%m-%d")

    for i in range(36):
        start = pd.Timestamp("2013-03-01") + pd.Timedelta(days=int(rng.integers(0, 900)))
        for _ in range(int(rng.integers(1, 5))):
            end = start + pd.Timedelta(days=int(rng.integers(15, 800)))
            add(f"P{i:02d}", day(start), day(end))
            r = rng.random()
            if r < 0.3:                                     # the next episode overlaps this one
                start = end - pd.Timedelta(days=int(rng.integers(1, 120)))
            elif r < 0.55:                                  # a gap before the next one
                start = end + pd.Timedelta(days=int(rng.integers(20, 400)))
            else:                                           # the next day
                start = end + pd.Timedelta(days=1)
    add("BLANK1", "", "2015-06-30")                         # a blank start ...
    add("BLANK1", "2015-07-01", "2016-02-29")               # ... and a usable episode
    add("BLANK2", "2015-01-01", "")                         # no usable day at all
    add("BLANK2", "", "")
    add("OUTSIDE", "2021-01-01", "2021-12-31")              # after every source's years
    add("EDGE", "2012-01-01", "2014-01-31")                 # starts before the sources
    add("EDGE", "2016-12-15", "2019-06-30")                 # ends after them
    add("ONEDAY", "2015-05-05", "2015-05-05")
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def cohort(tmp_path_factory) -> tuple[Path, pd.DataFrame]:
    """The pipeline input (as link writes it) and the episodes read back from it."""
    path = tmp_path_factory.mktemp("cohort") / "input.parquet"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _cohort.to_pipeline_parquet(_synthetic_cohort(), path, pid_as_str=True,
                                    columns="required")
    t = pd.read_parquet(path)
    episodes = pd.DataFrame({
        "PATID": t["pid"].astype(str).to_numpy(), "geoid": t["episode_id"].astype(int).to_numpy(),
        "start": pd.to_datetime(t["startDate"]), "end": pd.to_datetime(t["endDate"]),
        "lon": t["longitude"].to_numpy(float), "lat": t["latitude"].to_numpy(float)})
    assert episodes["start"].isna().any() and episodes["end"].isna().any()
    return path, episodes


def _pids(episodes: pd.DataFrame) -> list[str]:
    return list(pd.unique(episodes["PATID"]))


# ---------------------------------------------------------------------------------------------
# the reference: days, definedness, the two-stage participant mean
# ---------------------------------------------------------------------------------------------

def _overlap_days(episodes: pd.DataFrame, p_start: pd.Timestamp, p_end: pd.Timestamp) -> np.ndarray:
    """Days of each episode within [p_start, p_end], both ends inclusive; 0 with a blank date."""
    s, e = episodes["start"], episodes["end"]
    lo = s.where(s > p_start, p_start)
    hi = e.where(e < p_end, p_end)
    d = (hi - lo).dt.days + 1
    ok = s.notna() & e.notna() & (d > 0)
    return np.where(ok, d.fillna(0), 0).astype(float)


def _duration(episodes: pd.DataFrame) -> np.ndarray:
    d = (episodes["end"] - episodes["start"]).dt.days + 1
    return np.where(d.notna() & (d > 0), d.fillna(0), 0).astype(float)


def _days_for(episodes: pd.DataFrame, periods: Iterable[tuple[object, pd.Timestamp, pd.Timestamp]],
              defined: set[tuple[int, object]]) -> pd.Series:
    """Contributing days per episode: the episode's days in each period whose estimate is defined."""
    total = np.zeros(len(episodes))
    geoids = episodes["geoid"].to_numpy()
    for period, p_start, p_end in periods:
        mask = np.fromiter(((g, period) in defined for g in geoids), bool, len(geoids))
        total += np.where(mask, _overlap_days(episodes, p_start, p_end), 0.0)
    return pd.Series(total, index=geoids)


def _static_days(episodes: pd.DataFrame, defined: set[int]) -> pd.Series:
    geoids = episodes["geoid"].to_numpy()
    mask = np.fromiter((g in defined for g in geoids), bool, len(geoids))
    return pd.Series(np.where(mask, _duration(episodes), 0.0), index=geoids)


def _year_periods(years: Iterable[int]):
    return [(y, pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31")) for y in years]


def _defined_areal(weights: pd.DataFrame, values: pd.DataFrame, unit: str, var: str,
                   period: str | None = None, *, weight: str = "weight") -> set:
    """(episode, period) pairs (or episodes, for a static source) whose buffer has a valued unit."""
    m = weights.merge(values, on=unit, how="inner")
    m = m[m[var].notna() & (m[weight] > 0)]
    if period is None:
        return set(m["geoid"].astype(int))
    return set(zip(m["geoid"].astype(int), m[period]))


def _two_stage(episodes: pd.DataFrame, episode_out: pd.DataFrame,
               days: Mapping[str, pd.Series]) -> pd.DataFrame:
    """sum_e D_ev x_ev / sum_e D_ev per participant, from the EPISODE output (index: PATID)."""
    patid = episodes.set_index("geoid")["PATID"]
    out = episode_out.assign(geoid=episode_out["geoid"].astype(int)).set_index("geoid")
    assert out.index.is_unique
    ref = {}
    for var, d_v in days.items():
        x = pd.to_numeric(out[var], errors="coerce").reindex(patid.index)
        d = d_v.reindex(patid.index).fillna(0.0)
        bad = x.notna() != (d > 0)
        assert not bad.any(), (f"{var}: {int(bad.sum())} episode(s) whose value and contributing "
                               "days disagree")
        num = (x.fillna(0.0) * d).groupby(patid).sum()
        den = d.groupby(patid).sum()
        ref[var] = num / den.where(den > 0)
    return pd.DataFrame(ref)


def _assert_participant(participant_out: pd.DataFrame, ref: pd.DataFrame, pids: list[str],
                        columns: Iterable[str] | None = None) -> None:
    got = participant_out.assign(PATID=participant_out["PATID"].astype(str)).set_index("PATID")
    assert "geoid" not in got.columns
    assert got.index.is_unique and set(got.index) <= set(pids)
    for var in (list(columns) if columns is not None else list(ref.columns)):
        want = ref[var].reindex(pids).to_numpy(float)
        have = pd.to_numeric(got[var], errors="coerce").reindex(pids).to_numpy(float)
        assert (np.isnan(want) == np.isnan(have)).all(), f"{var}: missing participants differ"
        ok = np.isnan(want) | (np.abs(have - want) <= ATOL)
        assert ok.all(), f"{var}: max abs diff {np.nanmax(np.abs(have - want)):.3g}"
    # the case this is about: participants with more than one contributing episode
    assert ref.notna().any(axis=1).sum() >= 10


def _multi(episodes: pd.DataFrame, days: Mapping[str, pd.Series]) -> int:
    """How many participants have two or more episodes contributing to some variable."""
    patid = episodes.set_index("geoid")["PATID"]
    counts = [(d.reindex(patid.index).fillna(0) > 0).groupby(patid).sum() for d in days.values()]
    return int((pd.concat(counts, axis=1).max(axis=1) >= 2).sum())


def _run_both(run: Callable, make_config: Callable[[str, Path], DatasetConfig],
              tmp_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The pattern's output at episode grouping and at participant (patient) grouping."""
    outs = {}
    for grouping in ("episode", "patient"):
        cfg = make_config(grouping, tmp_path / f"out_{grouping}.parquet")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            run(cfg, DuckDBEngine())
        outs[grouping] = pd.read_parquet(cfg.output.path)
    assert "geoid" in outs["episode"].columns and "geoid" not in outs["patient"].columns
    return outs["episode"], outs["patient"]


def _buffer(cohort_path: Path) -> BufferConfig:
    return BufferConfig(patient_file=str(cohort_path), patient_adapter="demo_conus", buffer_m=270)


# ---------------------------------------------------------------------------------------------
# synthetic areal sources
# ---------------------------------------------------------------------------------------------

def _weights(episodes: pd.DataFrame, rng, unit: str, units: list, *, weight: str = "weight"
             ) -> pd.DataFrame:
    rows = []
    for g in episodes["geoid"]:
        for u in rng.choice(units, size=int(rng.integers(1, 4)), replace=False):
            rows.append({"geoid": int(g), unit: u, weight: float(rng.uniform(0.05, 1.0))})
    return pd.DataFrame(rows)


def _values(rng, unit: str, units: list, value_cols: Iterable[str], *,
            years: Iterable[int] | None = YEARS, missing: float = 0.2,
            absent: int = 3, year_col: str = "year") -> pd.DataFrame:
    """Unit x year values; ``missing`` of them NaN, the last ``absent`` units have no row."""
    rows = []
    for u in units[:len(units) - absent]:
        for y in (years if years is not None else [None]):
            row = {unit: u} if y is None else {unit: u, year_col: y}
            for c in value_cols:
                row[c] = np.nan if rng.random() < missing else float(rng.normal(10, 3))
            rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# yearly_areal, static_areal, yearly_areal_bg_vintage
# ---------------------------------------------------------------------------------------------

def test_yearly_areal(cohort, tmp_path):
    from spacescans.linkage.yearly_areal_linkage import run_yearly_areal

    path, episodes = cohort
    rng = np.random.default_rng(1)
    units = [f"T{u:03d}" for u in range(30)]
    weights = _weights(episodes, rng, "GEOID10", units)
    values = _values(rng, "GEOID10", units, ["a", "b"])
    weights.to_parquet(tmp_path / "w.parquet", index=False)
    values.to_parquet(tmp_path / "v.parquet", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="yearly", linkage_pattern="yearly_areal", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "w.parquet"), join_col="GEOID10"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "v.parquet"), join_col="GEOID10",
                                    value_cols=["a", "b"], year_col="year"),
            time=TimeConfig(years=YEARS, output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_yearly_areal, config, tmp_path)
    days = {v: _days_for(episodes, _year_periods(YEARS),
                         _defined_areal(weights, values, "GEOID10", v, "year"))
            for v in ("a", "b")}
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_static_areal(cohort, tmp_path):
    from spacescans.linkage.static_areal_linkage import run_static_areal

    path, episodes = cohort
    rng = np.random.default_rng(2)
    units = [f"B{u:03d}" for u in range(30)]
    weights = _weights(episodes, rng, "GEOID10", units)
    values = _values(rng, "GEOID10", units, ["walk", "noise"], years=None, missing=0.3)
    weights.to_parquet(tmp_path / "w.parquet", index=False)
    values.to_parquet(tmp_path / "v.parquet", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="static", linkage_pattern="static_areal", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "w.parquet"), join_col="GEOID10"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "v.parquet"), join_col="GEOID10",
                                    value_cols=["walk", "noise"]),
            time=TimeConfig(temporal_resolution="static", temporal_mode="static",
                            output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_static_areal, config, tmp_path)
    days = {v: _static_days(episodes, _defined_areal(weights, values, "GEOID10", v))
            for v in ("walk", "noise")}
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_yearly_areal_bg_vintage(cohort, tmp_path):
    from spacescans.linkage.yearly_areal_bg_vintage_linkage import run_yearly_areal_bg_vintage

    path, episodes = cohort
    rng = np.random.default_rng(3)
    old, new = [f"BG10_{u:03d}" for u in range(25)], [f"BG20_{u:03d}" for u in range(25)]
    w10 = _weights(episodes, rng, "GEOID10", old)
    w20 = _weights(episodes, rng, "GEOID", new)
    v10 = _values(rng, "bg_fips_2010", old, ["ndi"], years=[2014, 2015], year_col="index_year")
    v20 = _values(rng, "bg_fips_2020", new, ["ndi"], years=[2016], year_col="index_year")
    exposure = pd.concat([v10.assign(bg_vintage=2010), v20.assign(bg_vintage=2020)],
                         ignore_index=True)
    w10.to_parquet(tmp_path / "w10.parquet", index=False)
    w20.to_parquet(tmp_path / "w20.parquet", index=False)
    exposure.to_parquet(tmp_path / "ndi.parquet", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="ndi", linkage_pattern="yearly_areal_bg_vintage", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "w10.parquet"), join_col="GEOID10"),
            source_2020=SourceConfig(file=str(tmp_path / "w20.parquet"), join_col="GEOID"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "ndi.parquet"), value_cols=["ndi"],
                                    year_col="index_year", vintage_col="bg_vintage",
                                    join_col_2010="bg_fips_2010", join_col_2020="bg_fips_2020"),
            time=TimeConfig(years=YEARS, output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_yearly_areal_bg_vintage, config, tmp_path)
    defined = (_defined_areal(w10.rename(columns={"GEOID10": "bg_fips_2010"}), v10,
                              "bg_fips_2010", "ndi", "index_year")
               | _defined_areal(w20.rename(columns={"GEOID": "bg_fips_2020"}), v20,
                                "bg_fips_2020", "ndi", "index_year"))
    days = {"ndi": _days_for(episodes, _year_periods(YEARS), defined)}
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


# ---------------------------------------------------------------------------------------------
# gridded (daily and windowed), acag_multi
# ---------------------------------------------------------------------------------------------

def _grid_weights(episodes, rng, n_cells=40) -> pd.DataFrame:
    return _weights(episodes, rng, "grid_id", list(range(1, n_cells + 1)))


def _gridded_config(path, tmp_path, grouping, out, **exposure) -> DatasetConfig:
    return DatasetConfig(
        name="grid", linkage_pattern="gridded", geometry_type="raster",
        source=SourceConfig(file=str(tmp_path / "w.parquet")),
        buffer=_buffer(path),
        exposure=ExposureConfig(file=str(tmp_path / "x.parquet"), join_col="grid_id", **exposure),
        time=TimeConfig(years=[2015], output_grouping=grouping),
        engine=EngineConfig(), output=OutputConfig(path=str(out)))


def test_gridded_daily(cohort, tmp_path):
    pytest.importorskip("exactextract")
    from spacescans.linkage.gridded_linkage import run_gridded

    path, episodes = cohort
    rng = np.random.default_rng(4)
    weights = _grid_weights(episodes, rng)
    dates = pd.date_range("2015-01-01", "2015-12-31")
    rows = [{"grid_id": c, "date": d, "uv": np.nan if rng.random() < 0.25 else float(rng.normal(5, 1))}
            for c in range(1, 38) for d in dates]             # cells 38-40 have no data at all
    exposure = pd.DataFrame(rows)
    weights.to_parquet(tmp_path / "w.parquet", index=False)
    exposure.to_parquet(tmp_path / "x.parquet", index=False)

    ep, part = _run_both(run_gridded, lambda g, out: _gridded_config(
        path, tmp_path, g, out, value_cols=["uv"], date_col="date"), tmp_path)
    defined = _defined_areal(weights, exposure, "grid_id", "uv", "date")
    days = {"uv": _days_for(episodes, [(d, d, d) for d in dates], defined)}
    assert _multi(episodes, days) >= 3
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def _windows(start: str, end: str, step_days: int = 14):
    starts = pd.date_range(start, end, freq=f"{step_days}D")
    return [(s, s, s + pd.Timedelta(days=step_days - 1)) for s in starts]


def test_gridded_windowed(cohort, tmp_path):
    pytest.importorskip("exactextract")
    from spacescans.linkage.gridded_linkage import run_gridded

    path, episodes = cohort
    rng = np.random.default_rng(5)
    weights = _grid_weights(episodes, rng)
    windows = _windows("2014-06-01", "2016-06-30", 30)
    rows = [{"grid_id": c, "start_date": s, "end_date": e,
             "value": np.nan if rng.random() < 0.25 else float(rng.normal(20, 4))}
            for c in range(1, 38) for _p, s, e in windows]
    exposure = pd.DataFrame(rows)
    weights.to_parquet(tmp_path / "w.parquet", index=False)
    exposure.to_parquet(tmp_path / "x.parquet", index=False)

    ep, part = _run_both(run_gridded, lambda g, out: _gridded_config(
        path, tmp_path, g, out, value_cols=["value"], start_col="start_date",
        end_col="end_date"), tmp_path)
    defined = _defined_areal(weights, exposure, "grid_id", "value", "start_date")
    days = {"value": _days_for(episodes, windows, defined)}
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_acag_multi(cohort, tmp_path):
    pytest.importorskip("rasterio")
    pytest.importorskip("xarray")
    from spacescans.linkage import acag_linkage

    path, episodes = cohort
    rng = np.random.default_rng(6)
    weights = _grid_weights(episodes, rng)
    weights.to_parquet(tmp_path / "w.parquet", index=False)
    windows = _windows("2014-12-20", "2016-01-10", 14)
    root = tmp_path / "xNorthAmerica"
    species = {"PM25": "pm25", "PM25_bm": "pm25_bm"}
    tables = {}
    for folder in species:
        (root / folder / "BiWeekly").mkdir(parents=True)
        tables[folder] = pd.DataFrame([
            {"grid_id": c, "start_date": s, "end_date": e,
             "value": np.nan if rng.random() < 0.25 else float(rng.normal(8, 2))}
            for c in range(1, 38) for _p, s, e in windows])

    class Reader:
        def __init__(self, config):
            self.folder = Path(config.exposure.file).parent.name

        def load_exposure(self, years=None):
            return tables[self.folder].copy()

    def config(grouping, out):
        return DatasetConfig(
            name="acag", linkage_pattern="acag_multi", geometry_type="raster",
            source=SourceConfig(file=str(tmp_path / "w.parquet")), buffer=_buffer(path),
            exposure=ExposureConfig(file=str(root), value_cols=["value"],
                                    start_col="start_date", end_col="end_date"),
            time=TimeConfig(years=[2015], output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    with patch.object(acag_linkage, "ACAGExposureSource", Reader):
        ep, part = _run_both(acag_linkage.run_acag_multi, config, tmp_path)
    days = {col: _days_for(episodes, windows,
                           _defined_areal(weights, tables[folder], "grid_id", "value", "start_date"))
            for folder, col in species.items()}
    ref = _two_stage(episodes, ep, days)
    ref["pm25_nbm"] = ref["pm25"] - ref["pm25_bm"]       # derived from the participant means
    _assert_participant(part, ref, _pids(episodes))


# ---------------------------------------------------------------------------------------------
# precomputed patterns: the C4 of the line-distance sources TIGER (yearly) and NHD (static)
# ---------------------------------------------------------------------------------------------

def test_tiger_roads_distances_precomputed_areal(cohort, tmp_path):
    pytest.importorskip("geopandas")
    import spacescans.plugins.readers.tiger_roads  # noqa: F401  (registers the reader)
    from spacescans.linkage.precomputed_areal_linkage import run_precomputed_areal

    path, episodes = cohort
    rng = np.random.default_rng(7)
    cols = ["dist_pri", "dist_sec", "dist_prisec"]
    rows = []
    for g in episodes["geoid"]:
        for y in YEARS:
            if rng.random() < 0.15:            # tiger_proximity skips a year without roads
                continue
            row = {"geoid": int(g), "year": y}
            for c in cols:
                row[c] = np.nan if rng.random() < 0.15 else float(rng.uniform(10, 5000))
            rows.append(row)
    annual = pd.DataFrame(rows)
    annual.to_parquet(tmp_path / "annual_proximity.parquet", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="tiger", linkage_pattern="precomputed_areal", geometry_type="line",
            source=SourceConfig(file=str(tmp_path)), buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "annual_proximity.parquet"),
                                    join_col="geoid", value_cols=cols),
            time=TimeConfig(years=YEARS, output_grouping=grouping),
            engine=EngineConfig(), plugin="tiger_roads", output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_precomputed_areal, config, tmp_path)
    days = {}
    for c in cols:
        ok = annual[annual[c].notna()]
        days[c] = _days_for(episodes, _year_periods(YEARS), set(zip(ok["geoid"], ok["year"])))
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_nhd_distances_precomputed_static(cohort, tmp_path):
    pytest.importorskip("geopandas")
    import spacescans.plugins.readers.nhd  # noqa: F401  (registers the reader)
    from spacescans.linkage.precomputed_static_linkage import run_precomputed_static

    path, episodes = cohort
    rng = np.random.default_rng(8)
    cols = ["dist_flow_m", "dist_water_m", "dist_area_m", "dist_coast_m", "dist_blue_m"]
    keep = episodes["geoid"][rng.random(len(episodes)) > 0.1]     # a few locations not computed
    prox = pd.DataFrame({"geoid": keep.astype(int).to_numpy()})
    for c in cols:
        prox[c] = np.where(rng.random(len(prox)) < (0.6 if c == "dist_coast_m" else 0.1),
                           np.nan, rng.uniform(5, 20000, len(prox)))
    prox.to_parquet(tmp_path / "proximity_blue.parquet", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="nhd", linkage_pattern="precomputed_static", geometry_type="line",
            source=SourceConfig(file=str(tmp_path)), buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "proximity_blue.parquet"),
                                    join_col="geoid", value_cols=cols),
            time=TimeConfig(temporal_resolution="static", temporal_mode="static",
                            output_grouping=grouping),
            engine=EngineConfig(), plugin="nhd", output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_precomputed_static, config, tmp_path)
    days = {c: _static_days(episodes, set(prox.loc[prox[c].notna(), "geoid"])) for c in cols}
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


# ---------------------------------------------------------------------------------------------
# the FAQSD reader (daily tract surfaces)
# ---------------------------------------------------------------------------------------------

def test_faqsd_daily_reader(cohort, tmp_path):
    import spacescans.plugins.readers.faqsd  # noqa: F401  (registers the reader)
    from spacescans.linkage.faqsd_linkage import run_faqsd_daily_areal

    path, episodes = cohort
    rng = np.random.default_rng(9)
    tracts = [18097000100 + 100 * k for k in range(30)]
    weights = _weights(episodes, rng, "GEOID10", tracts, weight="value")
    weights.assign(GEOID10=weights["GEOID10"].astype(str)).to_parquet(tmp_path / "w.parquet",
                                                                       index=False)
    data = tmp_path / "FAQSD"
    data.mkdir()
    dates = pd.date_range("2015-01-01", "2015-12-31")
    grid = pd.DataFrame([(d, f) for d in dates for f in tracts[:27]], columns=["date", "fips"])
    o3 = rng.normal(40, 5, len(grid))
    pm = rng.normal(9, 2, len(grid))
    o3[rng.random(len(grid)) < 0.2] = np.nan
    pm[rng.random(len(grid)) < 0.2] = np.nan
    for name, vals in (("2015_ozone_daily_8hour_maximum.txt", o3),
                       ("2015_pm25_daily_average.txt", pm)):
        pd.DataFrame({"Date": grid["date"].dt.strftime("%Y/%m/%d"), "FIPS": grid["fips"],
                      "Longitude": -86.0, "Latitude": 39.8, "value": vals, "se": 1.0}
                     ).to_csv(data / name, index=False)
    daily = grid.assign(o3=o3, pm25=pm)

    def config(grouping, out):
        return DatasetConfig(
            name="faqsd", linkage_pattern="faqsd_daily_areal", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "w.parquet"), join_col="GEOID10"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(data), value_cols=["o3", "pm25"]),
            time=TimeConfig(years=[2015], temporal_resolution="daily", output_grouping=grouping),
            engine=EngineConfig(), plugin="faqsd", output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_faqsd_daily_areal, config, tmp_path)
    w = weights.rename(columns={"GEOID10": "fips"})
    days = {v: _days_for(episodes, [(d, d, d) for d in dates],
                         _defined_areal(w, daily, "fips", v, "date", weight="value"))
            for v in ("o3", "pm25")}
    assert _multi(episodes, days) >= 3
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


# ---------------------------------------------------------------------------------------------
# FARA: spatial mean over the valued tracts (C-19), recodes from the aggregated rates
# ---------------------------------------------------------------------------------------------

_FARA_COLUMNS = [
    "State", "County", "Fips", "Urban", "Pop",
    "LAPOP1_10share", "LAPOP1_10", "LALOWI1_10share", "LALOWI1_10",
    "LAHUNV1_10share", "LAHUNV1_10", "LAKIDS1_10share", "LAKIDS1_10",
    "LASENIORS1_10share", "LASENIORS1_10",
    "LILATracts_1And10", "LA1and10",
    "year",
    "LILATracts_halfAnd10", "LILATracts_1And20", "LILATracts_Vehicle",
    "Rural",
    "LAhalfand10", "LA1and20",
    "LATracts_half", "LATracts1", "LATracts10", "LATracts20",
    "LATractsVehicle_20", "HUNVFlag",
    "GroupQuartersFlag", "Hu", "NUMGQTRS", "PCTGQTRS", "LowIncomeTracts",
    "UATYP10",
    "lapophalf", "lapophalfshare", "lalowihalf", "lalowihalfshare",
    "lakidshalf", "lakidshalfshare", "laseniorshalf", "laseniorshalfshare",
    "lahunvhalf", "lahunvhalfshare",
    "lapop1", "lapop1share", "lalowi1", "lalowi1share",
    "lakids1", "lakids1share", "laseniors1", "laseniors1share",
    "lahunv1", "lahunv1share",
    "lapop10", "lapop10share", "lalowi10", "lalowi10share",
    "lakids10", "lakids10share", "laseniors10", "laseniors10share",
    "lahunv10", "lahunv10share",
    "lapop20", "lapop20share", "lalowi20", "lalowi20share",
    "lakids20", "lakids20share", "laseniors20", "laseniors20share",
    "lahunv20", "lahunv20share",
    "LAPOP05_10", "LAPOP05_10share", "LAPOP1_20", "LAPOP1_20share",
    "LALOWI05_10", "LALOWI05_10share", "LALOWI1_20", "LALOWI1_20share",
]
#: The rates the 13 flag recodes read (and Urban, recoded in place).
_RECODE_INPUTS = ["Urban", "LALOWI1_10", "LALOWI1_10share", "LAPOP1_10", "LAPOP1_10share",
                  "lalowihalf", "lalowihalfshare", "lalowi10", "lalowi10share", "lalowi1",
                  "lalowi1share", "lalowi20", "lalowi20share", "lapophalf", "lapop10",
                  "lapop10share", "lapophalfshare", "lapop1", "lapop20", "lapop20share",
                  "lapop1share", "lahunvhalf", "PCTGQTRS"]
_FLAGS = ["LILATracts_1And10", "LA1and10", "LILATracts_halfAnd10", "LILATracts_1And20",
          "LAhalfand10", "LA1and20", "LATracts_half", "LATracts1", "LATracts10", "LATracts20",
          "LATractsVehicle_20", "HUNVFlag", "GroupQuartersFlag"]
_PLAIN_RATES = ["Pop", "LAHUNV1_10share", "lakids1share", "LALOWI1_20share", "NUMGQTRS"]


def _fara_inputs(episodes, rng):
    tracts = list(range(1001, 1031))
    weights = _weights(episodes, rng, "GEOID10", tracts, weight="value")
    rows = []
    for t in tracts[:27]:                                   # three tracts have no FARA row
        for y in (2012, *YEARS):                            # 2012 is dropped (year >= 2013)
            row = {c: float(rng.uniform(0.0, 1.0)) for c in _FARA_COLUMNS}
            for c in ("Pop", "LAPOP1_10", "LALOWI1_10", "lalowihalf", "lalowi10", "lalowi1",
                      "lalowi20", "lapophalf", "lapop10", "lapop1", "lapop20", "NUMGQTRS"):
                row[c] = float(rng.uniform(0, 1500))
            row["lahunvhalf"] = float(rng.uniform(0, 250))
            row.update({"State": 18.0, "County": 97.0, "Fips": t, "year": y})
            for c in _FARA_COLUMNS[3:]:
                if c != "year" and rng.random() < 0.15:
                    row[c] = np.nan
            rows.append(row)
    return weights, pd.DataFrame(rows)[_FARA_COLUMNS]


def _fara_raw_reference(episodes, weights, fara, rates) -> pd.DataFrame:
    """Participant rates from the raw inputs: per (episode, year) the weighted mean over the
    tracts WITH a value, then sum(d * z) / sum(d) over all of a participant's episode-years."""
    patid = episodes.set_index("geoid")["PATID"]
    joined = weights.merge(fara[fara["year"] >= 2013], left_on="GEOID10", right_on="Fips")
    num = {v: pd.Series(0.0, index=patid.index) for v in rates}
    den = {v: pd.Series(0.0, index=patid.index) for v in rates}
    for y, p_start, p_end in _year_periods(YEARS):
        d = pd.Series(_overlap_days(episodes, p_start, p_end), index=episodes["geoid"].to_numpy())
        yr = joined[joined["year"] == y]
        for v in rates:
            ok = yr[yr[v].notna()]
            z = ((ok[v] * ok["value"]).groupby(ok["geoid"]).sum()
                 / ok["value"].groupby(ok["geoid"]).sum()).reindex(patid.index)
            has = z.notna() & (d.reindex(patid.index) > 0)
            num[v] += np.where(has, z.fillna(0.0) * d.reindex(patid.index), 0.0)
            den[v] += np.where(has, d.reindex(patid.index), 0.0)
    out = {}
    for v in rates:
        n, m = num[v].groupby(patid).sum(), den[v].groupby(patid).sum()
        out[v] = n / m.where(m > 0)
    return pd.DataFrame(out)


def test_fara_tract(cohort, tmp_path):
    pytest.importorskip("pyreadr")
    from spacescans.linkage import fara_linkage

    path, episodes = cohort
    rng = np.random.default_rng(10)
    weights, fara = _fara_inputs(episodes, rng)
    weights.to_parquet(tmp_path / "buffer.parquet", index=False)
    fara.to_parquet(tmp_path / "fara.parquet", index=False)
    labels = _RECODE_INPUTS + _FLAGS + _PLAIN_RATES
    pd.DataFrame({"var": labels, "label": labels}).to_csv(tmp_path / "labels.csv", index=False)

    def config(grouping, out):
        return DatasetConfig(
            name="fara", linkage_pattern="fara_tract", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "buffer.parquet"), join_col="GEOID10"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "fara.parquet"), key="fara1019",
                                    join_col="Fips", value_cols=[], year_col="year",
                                    label_file=str(tmp_path / "labels.csv")),
            time=TimeConfig(years=YEARS, output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    ep, part = _run_both(fara_linkage.run_fara_tract, config, tmp_path)
    pids = _pids(episodes)
    # (1) every delivered column from the raw inputs: rates, then the recodes on them
    raw = _fara_raw_reference(episodes, weights, fara, [c for c in labels if c not in _FLAGS])
    recoded = fara_linkage._apply_fara_recode(raw.reindex(columns=raw.columns.tolist() + _FLAGS))
    _assert_participant(part, recoded, pids, columns=labels)
    # (2) the rates no recode touches also through the episode output and contributing days
    jw = weights.merge(fara[fara["year"] >= 2013], left_on="GEOID10", right_on="Fips")
    days = {v: _days_for(episodes, _year_periods(YEARS),
                         set(zip(jw.loc[jw[v].notna(), "geoid"], jw.loc[jw[v].notna(), "year"])))
            for v in _PLAIN_RATES + [c for c in _RECODE_INPUTS if c != "Urban"]}
    _assert_participant(part, _two_stage(episodes, ep, days), pids)


# ---------------------------------------------------------------------------------------------
# cbp_fallback: per-episode ZBP / county split, participant means
# ---------------------------------------------------------------------------------------------

def test_cbp_fallback_with_its_zbp_input(cohort, tmp_path):
    from spacescans.linkage.cbp_fallback_linkage import run_cbp_fallback
    from spacescans.linkage.yearly_areal_linkage import run_yearly_areal

    path, episodes = cohort
    rng = np.random.default_rng(11)
    cols = ["r_civic", "r_bowling"]
    zips = [f"{z:05d}" for z in range(46200, 46230)]
    counties = [f"18{c:03d}" for c in range(1, 21)]
    wz = _weights(episodes, rng, "ZCTA5CE10", zips)
    vz = _values(rng, "zip", zips, cols, missing=0.15, absent=12)   # many ZIPs without ZBP
    wc = _weights(episodes, rng, "GEOID10", counties)
    vc = _values(rng, "fips", counties, cols, missing=0.1, absent=2)
    for name, df in (("wz", wz), ("vz", vz), ("wc", wc), ("vc", vc)):
        df.to_parquet(tmp_path / f"{name}.parquet", index=False)

    # the ZBP step as the catalog renders it at the participant level: per episode, with days
    zbp_out = tmp_path / "zbp.parquet"
    run_yearly_areal(DatasetConfig(
        name="zbp", linkage_pattern="yearly_areal", geometry_type="polygon",
        source=SourceConfig(file=str(tmp_path / "wz.parquet"), join_col="ZCTA5CE10"),
        buffer=_buffer(path),
        exposure=ExposureConfig(file=str(tmp_path / "vz.parquet"), join_col="zip",
                                value_cols=cols, year_col="year"),
        time=TimeConfig(years=YEARS, output_grouping="episode", contributing_days=True),
        engine=EngineConfig(), output=OutputConfig(path=str(zbp_out))), DuckDBEngine())
    zbp = pd.read_parquet(zbp_out)
    assert {"r_civic__days", "r_bowling__days"} <= set(zbp.columns)

    def config(grouping, out):
        return DatasetConfig(
            name="cbp", linkage_pattern="cbp_fallback", geometry_type="polygon",
            source=SourceConfig(file=str(tmp_path / "wc.parquet"), join_col="GEOID10"),
            buffer=_buffer(path),
            exposure=ExposureConfig(file=str(tmp_path / "vc.parquet"), join_col="fips",
                                    value_cols=cols, year_col="year", zbp_file=str(zbp_out)),
            time=TimeConfig(years=YEARS, output_grouping=grouping),
            engine=EngineConfig(), output=OutputConfig(path=str(out)))

    ep, part = _run_both(run_cbp_fallback, config, tmp_path)
    periods = _year_periods(YEARS)
    zdays = {v: _days_for(episodes, periods, _defined_areal(
        wz.rename(columns={"ZCTA5CE10": "zip"}), vz, "zip", v, "year")) for v in cols}
    cdays = {v: _days_for(episodes, periods, _defined_areal(
        wc.rename(columns={"GEOID10": "fips"}), vc, "fips", v, "year")) for v in cols}
    in_years = pd.Series(sum(_overlap_days(episodes, s, e) for _y, s, e in periods),
                         index=episodes["geoid"].to_numpy())
    # an episode falls back when ZBP has rows for it (it overlaps the years) but no r_civic
    falls_back = (in_years > 0) & (zdays["r_civic"] == 0)
    assert falls_back.sum() >= 10 and (~falls_back & (in_years > 0)).sum() >= 10
    days = {v: cdays[v].where(falls_back, zdays[v]) for v in cols}
    # participants mixing a ZBP episode and a county one: the case the v1 rule got wrong
    patid = episodes.set_index("geoid")["PATID"]
    mixed = (falls_back.groupby(patid).any() & (~falls_back & (in_years > 0)).groupby(patid).any())
    assert mixed.sum() >= 3
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


# ---------------------------------------------------------------------------------------------
# proximity: user-supplied points, lines or polygons
# ---------------------------------------------------------------------------------------------

def _features(kind: str, rng):
    import geopandas as gpd
    from shapely.geometry import LineString, Point, box

    if kind == "point":
        geoms = [Point(rng.uniform(-86.5, -85.9), rng.uniform(39.5, 40.1)) for _ in range(6)]
    elif kind == "line":
        geoms = [LineString([(rng.uniform(-86.5, -85.9), rng.uniform(39.5, 40.1)),
                             (rng.uniform(-86.5, -85.9), rng.uniform(39.5, 40.1))])
                 for _ in range(5)]
    else:
        geoms = [box(x, y, x + 0.05, y + 0.04)
                 for x, y in zip(rng.uniform(-86.5, -86.0, 4), rng.uniform(39.5, 40.0, 4))]
    return gpd.GeoDataFrame({"kind": [kind] * len(geoms)}, geometry=geoms, crs="EPSG:4326")


def _proximity_config(path, source: SourceConfig, time: TimeConfig, out, geometry: str
                      ) -> DatasetConfig:
    return DatasetConfig(
        name="prox", linkage_pattern="proximity", geometry_type=geometry,
        source=source, buffer=_buffer(path),
        exposure=ExposureConfig(file=str(source.file), value_cols=["feat_dist_m"]),
        time=time, engine=EngineConfig(), output=OutputConfig(path=str(out)))


@pytest.mark.parametrize("kind", ["point", "line", "polygon"])
def test_proximity_static(cohort, tmp_path, kind):
    pytest.importorskip("geopandas")
    from spacescans.linkage.proximity_linkage import run_proximity

    path, episodes = cohort
    rng = np.random.default_rng(12)
    feats = _features(kind, rng)
    target = tmp_path / ("features.parquet" if kind != "line" else "features.geojson")
    feats.to_parquet(target) if target.suffix == ".parquet" else feats.to_file(target)

    ep, part = _run_both(run_proximity, lambda g, out: _proximity_config(
        path, SourceConfig(file=str(target)),
        TimeConfig(temporal_resolution="static", temporal_mode="static", output_grouping=g),
        out, kind), tmp_path)
    assert list(ep.columns) == ["PATID", "geoid", "feat_dist_m"]
    # the distances themselves: to the nearest feature, in the pattern's metric CRS
    import geopandas as gpd

    from spacescans.models.config import AEA_CRS_DEFAULT
    pts = gpd.GeoSeries(gpd.points_from_xy(episodes["lon"], episodes["lat"]), crs="EPSG:4326"
                        ).to_crs(AEA_CRS_DEFAULT)
    union = feats.to_crs(AEA_CRS_DEFAULT).geometry.union_all()
    expect = pd.Series(pts.distance(union).to_numpy(), index=episodes["geoid"].to_numpy())
    got = ep.set_index("geoid")["feat_dist_m"]
    np.testing.assert_allclose(got.to_numpy(), expect.reindex(got.index).to_numpy(),
                               rtol=0, atol=1e-6)
    days = {"feat_dist_m": _static_days(episodes, set(episodes["geoid"]))}
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_proximity_yearly_from_a_year_column(cohort, tmp_path):
    pytest.importorskip("geopandas")
    from spacescans.linkage.proximity_linkage import run_proximity

    path, episodes = cohort
    rng = np.random.default_rng(13)
    lines = _features("line", rng)
    feats = pd.concat([lines.iloc[:3].assign(yr=2014), lines.iloc[2:].assign(yr=2016)],
                      ignore_index=True)                        # no feature in 2015
    feats = feats.set_crs("EPSG:4326", allow_override=True)
    target = tmp_path / "roads.parquet"
    feats.to_parquet(target)

    ep, part = _run_both(run_proximity, lambda g, out: _proximity_config(
        path, SourceConfig(file=str(target), year_col="yr"),
        TimeConfig(years=YEARS, temporal_mode="yearly", output_grouping=g), out, "line"),
        tmp_path)
    defined = {(int(g), y) for g in episodes["geoid"] for y in (2014, 2016)}
    days = {"feat_dist_m": _days_for(episodes, _year_periods(YEARS), defined)}
    assert _multi(episodes, days) >= 5
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_proximity_yearly_from_one_file_per_year(cohort, tmp_path):
    pytest.importorskip("geopandas")
    from spacescans.linkage.proximity_linkage import run_proximity

    path, episodes = cohort
    rng = np.random.default_rng(14)
    files = []
    for y, kind in zip(YEARS, ("point", "point", "polygon")):
        f = tmp_path / f"sites_{y}.parquet"
        _features(kind, rng).to_parquet(f)
        files.append(str(f))

    ep, part = _run_both(run_proximity, lambda g, out: _proximity_config(
        path, SourceConfig(file=files),
        TimeConfig(years=YEARS, temporal_mode="yearly", output_grouping=g), out, "point"),
        tmp_path)
    defined = {(int(g), y) for g in episodes["geoid"] for y in YEARS}
    days = {"feat_dist_m": _days_for(episodes, _year_periods(YEARS), defined)}
    _assert_participant(part, _two_stage(episodes, ep, days), _pids(episodes))


def test_proximity_refuses_features_without_a_crs(cohort, tmp_path):
    pytest.importorskip("geopandas")
    from spacescans.linkage.proximity_linkage import run_proximity

    path, _episodes = cohort
    feats = _features("point", np.random.default_rng(15)).set_crs(None, allow_override=True)
    target = tmp_path / "nocrs.parquet"
    feats.to_parquet(target)
    cfg = _proximity_config(path, SourceConfig(file=str(target)),
                            TimeConfig(temporal_mode="static"), tmp_path / "o.parquet", "point")
    with pytest.raises(ValueError, match="no CRS"):
        run_proximity(cfg, DuckDBEngine())


def test_proximity_names_one_column_per_category(cohort, tmp_path):
    pytest.importorskip("geopandas")
    from spacescans.linkage.proximity_linkage import run_proximity

    path, episodes = cohort
    rng = np.random.default_rng(16)
    feats = pd.concat([_features("point", rng).assign(cat="park"),
                       _features("polygon", rng).assign(cat="lake")], ignore_index=True)
    feats = feats.set_crs("EPSG:4326", allow_override=True)
    target = tmp_path / "cats.parquet"
    feats.to_parquet(target)
    cfg = _proximity_config(path, SourceConfig(file=str(target), category_col="cat"),
                            TimeConfig(temporal_mode="static", output_grouping="episode"),
                            tmp_path / "o.parquet", "polygon")
    run_proximity(cfg, DuckDBEngine())
    out = pd.read_parquet(cfg.output.path)
    assert list(out.columns) == ["PATID", "geoid", "feat_dist_m_lake", "feat_dist_m_park"]
    assert len(out) == int((_duration(episodes) > 0).sum())


# ---------------------------------------------------------------------------------------------
# the helper the CBP fallback uses
# ---------------------------------------------------------------------------------------------

def test_participant_means_weights_episode_means_by_their_days():
    from spacescans.linkage.helpers import participant_means

    eps = pd.DataFrame({"PATID": ["a", "a", "b", "c", "c"],
                        "v": [1.0, 3.0, np.nan, 2.0, np.nan],
                        "v__days": [10.0, 30.0, 0.0, 5.0, 7.0]})
    out = participant_means(eps, ["v"]).set_index("PATID")["v"]
    assert out["a"] == pytest.approx((10 + 90) / 40)
    assert np.isnan(out["b"])                           # no contributing day
    assert out["c"] == pytest.approx(2.0)               # NaN mean with days > 0 is skipped
    assert list(out.index) == ["a", "b", "c"]
