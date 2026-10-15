"""spacescans.orchestrate.coverage reproduces task_manager.compute_coverage.

Expected results come from the web's compute_coverage run on the synthetic
cohorts with the web catalog plus a synthetic custom overlay (including a
static non-CONUS entry and one without a ``temporal`` key); see
fixtures/web_parity/generate.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from spacescans import catalog, cohort
from spacescans.orchestrate import coverage

FIX = Path(__file__).parent / "fixtures" / "web_parity"
# The web catalog the expected results came from is the package's (same sha256,
# fixtures/web_parity/manifest.json); the fixture keeps no copy of it.
CATALOG = {**catalog.payload()["variables"],
           **json.loads((FIX / "overlay.json").read_text())}
CASES = sorted((FIX / "expected" / "coverage").glob("*.json"))


# One deliberate difference from the web 0.2.7 output the coverage fixtures record (PRIV-2,
# 2026-10-14): 0.2.7's unrecognised-date-format message began with the file's first date;
# spacescans gives the same text without it.
_QUOTED_DATE = re.compile(r"^Unrecognized date format: (?:'[^']*'|\"[^\"]*\")\.")


def _as_now(message: str) -> str:
    """The web's 0.2.7 error text as spacescans words it today."""
    return _QUOTED_DATE.sub("Unrecognized date format.", message)


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x))


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.stem)
def test_coverage_matches_web(case, tmp_path):
    rec = json.loads(case.read_text(encoding="utf-8"))
    src = (FIX / "cohorts" / f"{rec['cohort']}.csv") if rec["with_input"] \
        else tmp_path / "input.csv"
    if "error" in rec:
        exc = {"KeyError": KeyError, "FileNotFoundError": FileNotFoundError,
               "ValueError": ValueError}[rec["error"]["type"]]
        with pytest.raises(exc) as e:
            coverage.compute_coverage(src, rec["variables"], CATALOG)
        assert str(e.value.args[0]) == _as_now(rec["error"]["args0"])
        return
    got = coverage.compute_coverage(src, rec["variables"], CATALOG)
    assert _jsonable(got) == rec["result"]


@pytest.mark.parametrize("case", [c for c in CASES
                                  if "error" not in json.loads(c.read_text())],
                         ids=lambda p: p.stem)
def test_coverage_from_a_table_equals_from_the_csv(case):
    rec = json.loads(case.read_text(encoding="utf-8"))
    df = coverage.read_coverage_csv(FIX / "cohorts" / f"{rec['cohort']}.csv")
    got = coverage.compute_coverage(df, rec["variables"], CATALOG)
    assert _jsonable(got) == rec["result"]


def test_typed_dates_give_the_same_coverage():
    """R sends calendar dates (datetime.date after parquet), pandas users
    datetime64 or text: all three agree with the CSV path."""
    want = coverage.compute_coverage(FIX / "cohorts" / "iso.csv", list(CATALOG), CATALOG)
    text = pd.read_csv(FIX / "cohorts" / "iso.csv")
    as_dt = text.assign(startDate=pd.to_datetime(text["startDate"]),
                        endDate=pd.to_datetime(text["endDate"]))
    as_date = as_dt.assign(startDate=as_dt["startDate"].dt.date,
                           endDate=as_dt["endDate"].dt.date)
    for df in (text, as_dt, as_date):
        before = df.copy()
        assert coverage.compute_coverage(df, list(CATALOG), CATALOG) == want
        pd.testing.assert_frame_equal(df, before)       # caller's table untouched


def test_timezone_aware_dates_are_refused():
    df = pd.read_csv(FIX / "cohorts" / "iso.csv")
    df["startDate"] = pd.to_datetime(df["startDate"]).dt.tz_localize("UTC")
    df["endDate"] = pd.to_datetime(df["endDate"]).dt.tz_localize("UTC")
    with pytest.raises(cohort.CohortError) as e:
        coverage.compute_coverage(df, ["ndi"], CATALOG)
    assert e.value.code == "date_timezone"


def test_catalog_objects_with_raw_are_accepted():
    @dataclass(frozen=True)
    class Variable:          # the shape spacescans.catalog.Variable will have
        key: str
        raw: dict

    objs = {k: Variable(k, v) for k, v in CATALOG.items()}
    src = FIX / "cohorts" / "outside.csv"
    assert coverage.compute_coverage(src, ["ndi", "noise"], objs) == \
        coverage.compute_coverage(src, ["ndi", "noise"], CATALOG)


def test_counts_are_rows_not_distinct_patients():
    df = pd.DataFrame({"pid": ["A", "A", "B"],
                       "startDate": ["2015-01-01", "2016-01-01", "2015-06-01"],
                       "endDate": ["2015-12-31", "2016-12-31", "2015-12-31"],
                       "longitude": [-86.1, -86.2, -149.9],
                       "latitude": [39.7, 39.8, 61.2]})
    out = coverage.compute_coverage(df, ["fara_tract"], CATALOG)["variables"]["fara_tract"]
    assert out["patients_in_time_window"] == 3
    assert out["patients_in_region"] == 2
    assert out["coverage_pct"] == 66.67
    assert out["warnings"] == ["33% of patients fall outside the CONUS coverage region"]
