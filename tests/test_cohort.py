"""spacescans.cohort behaviour beyond the web port: strict validation and the
text-id path (pid_as_str=True) used by R and new callers."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from spacescans import cohort
from spacescans.linkage.helpers import _adapt_demo_conus

COHORTS = Path(__file__).parent / "fixtures" / "web_parity" / "cohorts"


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(COHORTS / f"{name}.csv", dtype=str, keep_default_na=False)


def _frame(**overrides) -> pd.DataFrame:
    df = pd.DataFrame({
        "pid": ["A", "A", "B", "C"],
        "startDate": ["2015-01-01", "2016-01-01", "2015-06-01", "2017-02-01"],
        "endDate": ["2015-12-31", "2016-12-31", "2015-12-31", "2017-12-31"],
        "longitude": [-86.15, -86.2, -93.0, -122.4],
        "latitude": [39.77, 39.8, 45.0, 37.7],
    })
    for k, v in overrides.items():
        df[k] = v
    return df


def _codes(issues):
    return [i.code for i in issues]


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def test_a_clean_cohort_validates():
    rep = cohort.validate(_read("iso"))
    assert rep.ok and not rep.errors and not rep.warnings
    assert (rep.n_rows, rep.n_patients, rep.id_column) == (59, 25, "pid")
    assert rep.date_format == "%Y-%m-%d"
    upload = json.loads((COHORTS.parent / "expected" / "upload" / "iso.json").read_text())
    assert rep.date_range == upload["summary"]["date_range"]
    rep.raise_for_errors()                     # no-op
    json.dumps(rep.as_dict())                  # serialisable for the bridge


def test_us_dates_and_patid_alias_are_accepted_without_mutation():
    df = _read("patid_mixed")
    cols = df.columns.tolist()
    rep = cohort.validate(df)
    assert rep.ok and rep.id_column == "PatID"
    assert df.columns.tolist() == cols
    assert cohort.validate(_read("us_slash")).date_format == "%m/%d/%Y"


def test_missing_columns_use_the_web_message():
    rep = cohort.validate(_read("missing_pid"))
    assert _codes(rep.errors) == ["missing_columns"]
    assert rep.errors[0].message == (
        "Missing required columns: pid (the patient id may be named pid or PATID)")
    with pytest.raises(cohort.CohortError) as e:
        rep.raise_for_errors()
    assert e.value.code == "invalid" and e.value.issues == rep.errors


def test_row_level_errors_carry_one_based_row_numbers():
    df = _frame(pid=["A", None, "B", " "],
                longitude=["-86.1", "abc", "-93.0", "-122.4"],
                latitude=[39.7, 45.0, 95.0, 37.7],
                endDate=["2015-12-31", "2016-12-31", "2015-12-31", "2016-01-31"])
    rep = cohort.validate(df)
    by = {i.code: i for i in rep.errors}
    assert by["pid_missing"].rows == (2, 4) and by["pid_missing"].count == 2
    assert by["coord_not_numeric"].rows == (2,)
    assert by["coord_range"].rows == (3,) and by["coord_range"].column == "latitude"
    assert by["date_order"].rows == (4,)
    assert "rows 2, 4" in by["pid_missing"].message


def test_missing_coordinates_and_blank_dates():
    rep = cohort.validate(_read("blank_dates"))
    errors = {i.code: i for i in rep.errors}
    warnings = [(i.code, i.column, i.rows) for i in rep.warnings]
    # Row 13 is a short line: no endDate and no coordinates.
    assert errors["coord_missing"].rows == (13,)
    assert ("date_missing", "startDate", (8,)) in warnings
    assert ("date_missing", "endDate", (3, 6, 13)) in warnings


def test_bad_dates_and_timezones_are_errors():
    rep = cohort.validate(_read("bad_format"))
    assert _codes(rep.errors) == ["date_format"]
    assert rep.errors[0].message.startswith("Unrecognized date format: '19/8/2017'")
    tz = _frame(startDate=pd.to_datetime(["2015-01-01"] * 4).tz_localize("UTC"))
    assert "date_timezone" in _codes(cohort.validate(tz).errors)


def test_empty_cohort_and_episode_id_column():
    assert _codes(cohort.validate(_read("empty")).errors) == ["empty"]
    rep = cohort.validate(_read("episode_id_col"))
    assert rep.ok and _codes(rep.warnings) == ["episode_id_overwritten"]


def test_row_lists_are_capped_but_counted():
    df = _frame()
    df = pd.concat([df] * 10, ignore_index=True)
    df["latitude"] = 100.0
    issue = cohort.validate(df).errors[0]
    assert issue.count == 40 and len(issue.rows) == 20
    assert "and 20 more" in issue.message


def test_outside_conus_is_not_a_validation_error():
    assert cohort.validate(_read("outside")).ok


# ---------------------------------------------------------------------------
# to_pipeline_parquet(pid_as_str=True) and table inputs
# ---------------------------------------------------------------------------


def test_text_ids_keep_leading_zeros(tmp_path):
    src = COHORTS / "numeric_pid.csv"
    legacy = pd.read_parquet(cohort.csv_to_parquet(src, tmp_path / "a.parquet").path)
    info = cohort.to_pipeline_parquet(src, tmp_path / "b.parquet", pid_as_str=True)
    text = pd.read_parquet(info.path)
    raw = _read("numeric_pid")["pid"].tolist()
    assert pd.api.types.is_integer_dtype(legacy["pid"])
    assert text["pid"].tolist() == raw
    assert any(p.startswith("0") for p in raw)
    assert info.patients == len(set(raw))


def test_text_path_applies_the_alias_and_requires_columns(tmp_path):
    info = cohort.to_pipeline_parquet(COHORTS / "patid_upper.csv",
                                      tmp_path / "x.parquet", pid_as_str=True)
    assert info.columns[0] == "pid"
    with pytest.raises(cohort.CohortError) as e:
        cohort.to_pipeline_parquet(COHORTS / "missing_lat.csv", tmp_path / "y.parquet",
                                   pid_as_str=True)
    assert e.value.code == "missing_columns"
    assert str(e.value) == "Missing required columns: latitude"


def test_required_columns_only(tmp_path):
    info = cohort.to_pipeline_parquet(COHORTS / "extra_cols.csv", tmp_path / "x.parquet",
                                      pid_as_str=True, columns="required")
    assert info.columns == cohort.REQUIRED + ("episode_id",)
    assert pd.read_parquet(info.path).columns.tolist() == list(info.columns)
    with pytest.raises(ValueError):
        cohort.to_pipeline_parquet(COHORTS / "iso.csv", tmp_path / "z.parquet",
                                   pid_as_str=True, columns="some")


def test_table_input_is_not_modified_and_episode_id_is_positional(tmp_path):
    df = _frame(episode_id=[9, 9, 9, 9]).set_axis([10, 11, 12, 13])
    before = df.copy()
    info = cohort.to_pipeline_parquet(df, tmp_path / "x.parquet", pid_as_str=True)
    pd.testing.assert_frame_equal(df, before)
    out = pd.read_parquet(info.path)
    assert out["episode_id"].tolist() == [0, 1, 2, 3]
    assert info.episode_id_overwritten
    assert info.sha256 == hashlib.sha256(info.path.read_bytes()).hexdigest()
    assert (info.rows, info.patients) == (4, 3)


@pytest.mark.parametrize("kind", ["text", "datetime64", "date_objects"])
def test_date_inputs_all_become_datetime64(kind, tmp_path):
    df = _frame()
    if kind != "text":
        for c in cohort.DATE_COLUMNS:
            df[c] = pd.to_datetime(df[c])
            if kind == "date_objects":
                df[c] = df[c].dt.date
    info = cohort.to_pipeline_parquet(df, tmp_path / "x.parquet", pid_as_str=True)
    out = pd.read_parquet(info.path)
    assert pd.api.types.is_datetime64_dtype(out["startDate"])
    assert out["endDate"].iloc[3] == pd.Timestamp("2017-12-31")
    assert info.date_format == (None if kind == "datetime64" else "%Y-%m-%d")


def test_timezone_aware_dates_are_refused(tmp_path):
    df = _frame(startDate=pd.to_datetime(["2015-01-01"] * 4).tz_localize("UTC"))
    with pytest.raises(cohort.CohortError) as e:
        cohort.to_pipeline_parquet(df, tmp_path / "x.parquet", pid_as_str=True)
    assert e.value.code == "date_timezone"


def test_numeric_ids_in_a_table_become_text(tmp_path):
    ints = cohort.to_pipeline_parquet(_frame(pid=[1001, 1001, 7, 8]),
                                      tmp_path / "a.parquet", pid_as_str=True)
    assert pd.read_parquet(ints.path)["pid"].tolist() == ["1001", "1001", "7", "8"]
    floats = cohort.to_pipeline_parquet(_frame(pid=[1001.0, np.nan, 7.0, 7.5]),
                                        tmp_path / "b.parquet", pid_as_str=True)
    got = pd.read_parquet(floats.path)["pid"].tolist()
    assert got[0] == "1001" and got[2] == "7" and got[3] == "7.5"
    assert pd.isna(got[1])


def test_legacy_path_logs_the_runners_own_episode_id_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="spacescans.cohort"):
        cohort.csv_to_parquet(COHORTS / "episode_id_col.csv", tmp_path / "a.parquet",
                              variant="bg_ndi_wi")
        cohort.csv_to_parquet(COHORTS / "episode_id_col.csv", tmp_path / "b.parquet",
                              variant="zcta5")
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0].endswith("row-index ids (Sprint 2 invariant).")
    assert msgs[1].endswith("row-index ids.")


@pytest.mark.parametrize("pid_as_str", [False, True])
def test_the_pipeline_adapter_reads_episode_id_as_geoid(pid_as_str, tmp_path):
    """The contract with spacescans.linkage.helpers._adapt_demo_conus."""
    info = cohort.to_pipeline_parquet(COHORTS / "fips.csv", tmp_path / "x.parquet",
                                      pid_as_str=pid_as_str)
    adapted = _adapt_demo_conus(pd.read_parquet(info.path))
    assert adapted.columns.tolist() == ["PATID", "start", "end", "long", "lat", "geoid"]
    assert adapted["geoid"].tolist() == list(range(info.rows))
    assert pd.api.types.is_datetime64_dtype(adapted["start"])
