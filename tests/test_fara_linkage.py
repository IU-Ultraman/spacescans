"""Sprint 11 Phase A: fara_linkage.py output_grouping dispatch.

Mirrors tests/test_precomputed_static_linkage.py — uses a 10-row mini fixture
with two multi-episode PATIDs (P1 -> {10, 11}; P2 -> {20, 21}) so the episode
branch must emit strictly more rows than the patient branch.

The FARA pipeline is more complex than precomputed_static (custom SQL recode,
multi-year area-weighted aggregation, label-CSV column selection), so the
mocks are slightly heavier than the precomputed_static tests, but the
behavioural contract is the same:

  * patient grouping → unique PATID, columns = [PATID, *varlist]
  * episode grouping → unique (PATID, geoid), columns = [PATID, geoid, *varlist]
  * unsupported grouping → ValueError
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# fara_linkage require()s the [rda] extra (reads .Rda), so gate these on it —
# base / geo / hdf4 jobs don't install pyreadr.
pytestmark = pytest.mark.rda

from spacescans.models.config import (
    BufferConfig,
    DatasetConfig,
    EngineConfig,
    ExposureConfig,
    OutputConfig,
    SourceConfig,
    TimeConfig,
)

# Same 10-row fixture used by precomputed_areal / precomputed_static tests —
# two multi-episode PATIDs (P1 -> {10, 11}; P2 -> {20, 21}) plus six
# single-episode PATIDs, so the episode branch yields strictly more rows
# than the patient branch.
FIXTURE = Path(__file__).parent / "fixtures" / "precomputed_static_mini.parquet"


# Minimal varnameCountRemoved.csv — just three columns the FARA recode pass
# emits at the end of run_fara_tract. These names match the real label CSV.
_LABEL_VARS = ["LILATracts_1And10", "LATracts1", "HUNVFlag"]


# Minimal FARA exposure frame — one row per (Fips, year). The fixture's
# geoids (10, 11, 20, 21, 30) double as Fips codes here. Columns are chosen
# so _apply_fara_recode produces the three label vars cleanly:
#  - Urban present (so the LILATracts_1And10 / LATracts1 recodes fire)
#  - LAPOP1_10 + LALOWI1_10 + lapop1 + lapop1share present
#  - lahunvhalf present (for HUNVFlag)
# Plus every rate_var referenced downstream gets a numeric column so the SQL
# layer doesn't choke; we keep them constant so the temporal-weighted average
# is deterministic.
# Real FARA column ordering from FARA/C4/fara_nationwide_2010_2019_interpolated.Rda
# (84 columns total). The run_fara_tract iloc selection is r_cols=3-16,18,32-36,37-85
# (python 0-based: 2-15, 17, 31-35, 36-84), so the slot mapping is rigid —
# the mock frame must replicate the exact column order or the iloc will
# pick the wrong column names.
_FARA_REAL_COLUMNS = [
    "State", "County", "Fips", "Urban", "Pop",                              # 0-4
    "LAPOP1_10share", "LAPOP1_10", "LALOWI1_10share", "LALOWI1_10",         # 5-8
    "LAHUNV1_10share", "LAHUNV1_10", "LAKIDS1_10share", "LAKIDS1_10",       # 9-12
    "LASENIORS1_10share", "LASENIORS1_10",                                  # 13-14
    "LILATracts_1And10", "LA1and10",                                        # 15-16
    "year",                                                                 # 17
    "LILATracts_halfAnd10", "LILATracts_1And20", "LILATracts_Vehicle",      # 18-20
    "Rural",                                                                # 21
    "LAhalfand10", "LA1and20",                                              # 22-23
    "LATracts_half", "LATracts1", "LATracts10", "LATracts20",               # 24-27
    "LATractsVehicle_20", "HUNVFlag",                                       # 28-29
    "GroupQuartersFlag", "Hu", "NUMGQTRS", "PCTGQTRS", "LowIncomeTracts",   # 30-34
    "UATYP10",                                                              # 35
    "lapophalf", "lapophalfshare", "lalowihalf", "lalowihalfshare",         # 36-39
    "lakidshalf", "lakidshalfshare", "laseniorshalf", "laseniorshalfshare", # 40-43
    "lahunvhalf", "lahunvhalfshare",                                        # 44-45
    "lapop1", "lapop1share", "lalowi1", "lalowi1share",                     # 46-49
    "lakids1", "lakids1share", "laseniors1", "laseniors1share",             # 50-53
    "lahunv1", "lahunv1share",                                              # 54-55
    "lapop10", "lapop10share", "lalowi10", "lalowi10share",                 # 56-59
    "lakids10", "lakids10share", "laseniors10", "laseniors10share",         # 60-63
    "lahunv10", "lahunv10share",                                            # 64-65
    "lapop20", "lapop20share", "lalowi20", "lalowi20share",                 # 66-69
    "lakids20", "lakids20share", "laseniors20", "laseniors20share",         # 70-73
    "lahunv20", "lahunv20share",                                            # 74-75
    "LAPOP05_10", "LAPOP05_10share", "LAPOP1_20", "LAPOP1_20share",         # 76-79
    "LALOWI05_10", "LALOWI05_10share", "LALOWI1_20", "LALOWI1_20share",     # 80-83
]
assert len(_FARA_REAL_COLUMNS) == 84, len(_FARA_REAL_COLUMNS)


def _make_demo_config(tmp_path: Path, output_grouping: str) -> DatasetConfig:
    label_csv = tmp_path / "labels.csv"
    pd.DataFrame({"var": _LABEL_VARS, "label": ["a", "b", "c"]}).to_csv(
        label_csv, index=False
    )
    return DatasetConfig(
        name="fara_mini",
        linkage_pattern="fara_tract",
        geometry_type="polygon",
        source=SourceConfig(file="/dev/null", join_col="GEOID10"),
        buffer=BufferConfig(patient_file=str(FIXTURE), buffer_m=270),
        exposure=ExposureConfig(
            file="/dev/null",
            key="fara1019",
            join_col="Fips",
            value_cols=[],
            year_col="year",
            label_file=str(label_csv),
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path=str(tmp_path / "out.parquet")),
    )


def _buffer_frame() -> pd.DataFrame:
    """Mini C3 weight table — one row per (GEOID10, geoid) with a unit
    weight. The fixture's geoids (10, 11, 20, 21, 30) act both as the
    patient-side ``geoid`` and the FARA-side ``Fips`` join key.
    """
    geoids = [10, 11, 20, 21, 30]
    return pd.DataFrame({
        "GEOID10": geoids,
        "geoid": geoids,
        "value": [1.0] * len(geoids),
    })


def _fara_frame() -> pd.DataFrame:
    """Mini FARA exposure — every Fips (= geoid) x year combo for the two
    fixture years (2013, 2014). All numeric columns set to the same small
    constants so the legacy SQL pipeline yields a deterministic per-Fips
    temporal-weighted mean of those same constants.

    The constants are chosen so the post-recode binary flags resolve to
    well-defined values:
      * Urban = 1 (so urban-branch recodes fire)
      * LAPOP1_10 = 1000 (>= 500 threshold → LA1and10 = 1)
      * LALOWI1_10 = 1000 (>= 500 → LILATracts_1And10 = 1)
      * lahunvhalf = 200 (>= 100 → HUNVFlag = 1)
    """
    # Defaults: numeric 0.5 (small + nonzero) for every rate column; the
    # recode-threshold columns get explicit values that drive the binary
    # flags to 1.
    defaults: dict[str, float] = {col: 0.5 for col in _FARA_REAL_COLUMNS}
    defaults["State"] = 1.0
    defaults["County"] = 1.0
    defaults["Urban"] = 1.0
    defaults["Rural"] = 0.0
    defaults["UATYP10"] = 1.0
    defaults["LAPOP1_10"] = 1000.0
    defaults["LAPOP1_10share"] = 0.5
    defaults["LALOWI1_10"] = 1000.0
    defaults["LALOWI1_10share"] = 0.5
    defaults["lapop1"] = 1000.0
    defaults["lapop1share"] = 0.5
    defaults["lahunvhalf"] = 200.0

    geoids = [10, 11, 20, 21, 30]
    # The fixture's patient episodes are all in 2017; including 2013 here
    # exercises the legacy ``year >= 2013`` filter inside run_fara_tract,
    # but we need at least one year that actually overlaps the cohort to
    # avoid an empty temporal-weighted aggregation.
    years = [2013, 2017]
    rows = []
    for year in years:
        for fips in geoids:
            row = dict(defaults)
            row["Fips"] = fips
            row["year"] = year
            rows.append(row)
    # Force the column order to match the real FARA .Rda file.
    return pd.DataFrame(rows)[_FARA_REAL_COLUMNS]


class _FakeReadTable:
    """Stand-in for spacescans.io.readers.read_table.

    Returns the buffer parquet when called with the source.file path
    (or with /dev/null + no key), and returns the FARA frame when
    called with the exposure.file path (or with /dev/null + key=fara1019).
    """

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, path, *, key=None):
        self.calls.append((str(path), key))
        # First call: source (no key). Subsequent call with key=fara1019:
        # exposure. Order matches run_fara_tract's three reads:
        # 1. load_patients → already mocked separately
        # 2. read_table(config.source.file)
        # 3. read_table(config.exposure.file, key=config.exposure.key)
        if key == "fara1019":
            return _fara_frame()
        return _buffer_frame()


def _patient_frame() -> pd.DataFrame:
    """The fixture parquet's adapted form — PATID/start/end/geoid columns.

    The fara_linkage uses load_patients(config) which would normally apply
    the demo_conus adapter; here we bypass that by mocking load_patients
    directly and returning a frame that matches what load_patients would.
    """
    fixture = pd.read_parquet(FIXTURE)
    # The fixture already has PATID, start, end, geoid columns (sprint 5
    # built it for precomputed_areal). FARA needs all four.
    return fixture[["PATID", "geoid", "start", "end"]].copy()


def _run(tmp_path: Path, output_grouping: str) -> pd.DataFrame:
    from spacescans.linkage import fara_linkage as mod

    cfg = _make_demo_config(tmp_path, output_grouping=output_grouping)
    fake_reader = _FakeReadTable()
    with patch.object(mod, "read_table", side_effect=fake_reader), \
         patch.object(mod, "load_patients", return_value=_patient_frame()):
        mod.run_fara_tract(cfg, engine=None)
    return pd.read_parquet(cfg.output.path)


def test_fara_groups_by_patid_when_output_grouping_patient(tmp_path):
    df = _run(tmp_path, output_grouping="patient")
    assert list(df.columns) == ["PATID"] + _LABEL_VARS
    assert df["PATID"].is_unique
    # 8 unique PATIDs in the fixture.
    assert len(df) == 8


def test_fara_groups_by_patid_geoid_when_episode(tmp_path):
    df_patient = _run(tmp_path, output_grouping="patient")
    df_episode = _run(tmp_path, output_grouping="episode")
    assert list(df_episode.columns) == ["PATID", "geoid"] + _LABEL_VARS
    # (PATID, geoid) is unique per row.
    assert df_episode.groupby(["PATID", "geoid"]).size().max() == 1
    # Strictly more rows than the patient branch — P1 and P2 each split into 2.
    assert len(df_episode) > len(df_patient)
    assert len(df_episode) == 10


def test_fara_rejects_unknown_output_grouping(tmp_path):
    with pytest.raises(ValueError, match="unsupported output_grouping"):
        _run(tmp_path, output_grouping="foo")
