"""spacescans.cohort reproduces the web's upload check and csv_to_parquet.

Expected outputs come from running the web's own functions (commit in
fixtures/web_parity/manifest.json) on the synthetic cohorts in
fixtures/web_parity/cohorts; see generate.py there.

Parity is defined within one environment, so the strength of the parquet
check depends on how close this one is to the generator's:

* always: column names, row count, arrow type family and every value;
* same pandas major version: the exact arrow schema and pandas metadata
  (pandas 3 stores text as large_string, pandas 2 as string), and every
  value exactly except floats, which may be one unit in the last place
  apart (pandas parses some decimals differently on Linux x86_64 and macOS
  arm64);
* same pandas and pyarrow versions on the generator's platform: every float
  bit for bit and the sha256 of the file bytes, which the legacy C3 cache
  key is built from.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow
import pyarrow.parquet as pq
import pytest

from spacescans import cohort

FIX = Path(__file__).parent / "fixtures" / "web_parity"
MANIFEST = json.loads((FIX / "manifest.json").read_text())
PLATFORM = f"{sys.platform}-{platform.machine()}"
SAME_ENV = (pd.__version__, pyarrow.__version__, PLATFORM) == (MANIFEST["pandas"],
                                                               MANIFEST["pyarrow"],
                                                               MANIFEST["platform"])
SAME_PANDAS_MAJOR = pd.__version__.split(".")[0] == MANIFEST["pandas"].split(".")[0]

PARQUET_CASES = sorted((FIX / "expected" / "parquet").glob("*.json"))
UPLOAD_CASES = sorted((FIX / "expected" / "upload").glob("*.json"))


# One deliberate difference from the web 0.2.7 output these fixtures record (PRIV-2,
# 2026-10-14): 0.2.7's unrecognised-date-format message began with the file's first date;
# spacescans gives the same text without it.
_QUOTED_DATE = re.compile(r"^Unrecognized date format: (?:'[^']*'|\"[^\"]*\")\.")


def _as_now(message: str) -> str:
    """The web's 0.2.7 error text as spacescans words it today."""
    return _QUOTED_DATE.sub("Unrecognized date format.", message)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _family(type_name: str) -> str:
    """Arrow type up to what differs between pandas majors."""
    if type_name in ("string", "large_string"):
        return "string"
    if type_name.startswith("timestamp"):
        return "timestamp"
    return type_name


def _values(t: pyarrow.Table) -> pd.DataFrame:
    """Decoded table with datetimes in one unit (pandas 2: ns, pandas 3: us)."""
    df = t.to_pandas()
    for c in df.columns:
        if pd.api.types.is_datetime64_dtype(df[c]):
            df[c] = df[c].astype("datetime64[ns]")
    return df


def _assert_equal_floats_to_one_ulp(got: pyarrow.Table, want: pyarrow.Table) -> None:
    assert got.schema.equals(want.schema)
    for name in want.column_names:
        g, w = got.column(name), want.column(name)
        if pyarrow.types.is_floating(w.type):
            assert g.is_null().equals(w.is_null()), name
            np.testing.assert_array_max_ulp(g.to_numpy(), w.to_numpy(), maxulp=1)
        else:
            assert g.equals(w), name


def assert_parquet_like_web(actual: Path, rec: dict) -> None:
    expected = FIX / "expected" / "parquet" / rec["blob"]
    got_t, want_t = pq.read_table(actual), pq.read_table(expected)
    assert got_t.num_rows == rec["rows"]
    assert [[f.name, _family(str(f.type))] for f in got_t.schema] == \
        [[n, _family(t)] for n, t in rec["schema"]]
    pd.testing.assert_frame_equal(_values(got_t), _values(want_t), check_dtype=False)
    if SAME_PANDAS_MAJOR:
        assert [[f.name, str(f.type)] for f in got_t.schema] == rec["schema"]
        if SAME_ENV:
            assert got_t.equals(want_t)
        else:
            _assert_equal_floats_to_one_ulp(got_t, want_t)
        # pandas' own metadata (dtypes, index) must match too, version aside.
        got_md = json.loads(got_t.schema.metadata[b"pandas"])
        want_md = json.loads(want_t.schema.metadata[b"pandas"])
        for md in (got_md, want_md):
            md.pop("creator", None)
            md.pop("pandas_version", None)
        assert got_md == want_md
    if SAME_ENV:
        assert _sha256(actual) == rec["sha256"]


@pytest.mark.parametrize("case", PARQUET_CASES, ids=lambda p: p.stem)
def test_csv_to_parquet_matches_web(case, tmp_path):
    rec = json.loads(case.read_text())
    src = FIX / "cohorts" / f"{rec['cohort']}.csv"
    dst = tmp_path / "input.parquet"
    if "error" in rec:
        with pytest.raises(ValueError) as e:
            cohort.csv_to_parquet(src, dst, variant=rec["variant"])
        assert str(e.value) == _as_now(rec["error"]["message"])
        return
    info = cohort.csv_to_parquet(src, dst, variant=rec["variant"])
    assert_parquet_like_web(dst, rec)
    assert info.sha256 == _sha256(dst)
    assert info.rows == rec["rows"]
    assert info.path == dst


@pytest.mark.parametrize("case", [c for c in PARQUET_CASES
                                  if "error" not in json.loads(c.read_text())],
                         ids=lambda p: p.stem)
def test_to_pipeline_parquet_legacy_is_csv_to_parquet(case, tmp_path):
    """The design's entry point, called the way the web adapter will."""
    rec = json.loads(case.read_text())
    src = FIX / "cohorts" / f"{rec['cohort']}.csv"
    dst = tmp_path / "input.parquet"
    cohort.to_pipeline_parquet(src, dst, variant=rec["variant"], pid_as_str=False)
    assert_parquet_like_web(dst, rec)


def test_variants_differ_only_on_zcta5(tmp_path):
    """bg_ndi_wi infers zcta5 (losing leading zeros); zcta5 keeps it text."""
    src = FIX / "cohorts" / "fips.csv"
    a = cohort.read_upload_csv(src, variant="bg_ndi_wi")
    b = cohort.read_upload_csv(src, variant="zcta5")
    assert pd.api.types.is_integer_dtype(a["zcta5"].dropna().astype("Int64")) \
        and not b["zcta5"].dropna().map(type).eq(int).any()
    assert b["zcta5"].dropna().map(type).eq(str).all()
    assert b["zcta5"].dropna().str.len().eq(5).all()
    assert (b["zcta5"].dropna().str[0] == "0").any()     # zeros kept
    for c in ("state_fips", "county_fips", "tract_geoid", "bg_geoid"):
        assert a[c].equals(b[c])


def test_unknown_variant_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown cohort variant"):
        cohort.csv_to_parquet(FIX / "cohorts" / "iso.csv", tmp_path / "x.parquet",
                              variant="tract")


@pytest.mark.parametrize("case", UPLOAD_CASES, ids=lambda p: p.stem)
def test_check_upload_matches_web(case):
    rec = json.loads(case.read_text(encoding="utf-8"))
    content = (FIX / "cohorts" / f"{rec['cohort']}.csv").read_bytes()
    if "error" in rec:
        with pytest.raises(ValueError) as e:
            cohort.check_upload(content, rec["filename"])
        assert type(e.value).__name__ in ("CohortError", rec["error"]["type"])
        assert str(e.value) == _as_now(rec["error"]["message"])
        # The web deletes input.csv on these errors; the caller does that now.
        assert rec["input_csv_exists"] is False
        return
    got = cohort.check_upload(content, rec["filename"])
    assert json.loads(json.dumps(got.summary)) == rec["summary"]
    assert rec["meta_data_summary"] == rec["summary"]
    if rec["input_csv_changed"]:
        assert got.renamed_id_from is not None
        assert got.rewritten_text == rec["input_csv_text"]
    else:
        assert got.rewritten_text is None


def test_check_upload_rejects_a_utf8_bom_like_the_web():
    """Pinned web behaviour (an Excel "CSV UTF-8" export): the BOM sticks to
    the first header, so pid reads as missing. Kept for parity; see the
    branch report for the proposed post-cutover fix."""
    content = (FIX / "cohorts" / "bom.csv").read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    with pytest.raises(cohort.CohortError) as e:
        cohort.check_upload(content, "bom.csv")
    assert e.value.code == "missing_columns"


# ---------------------------------------------------------------------------
# Date resolution: the web's tests/test_cohort_dates.py cases, ported
# ---------------------------------------------------------------------------


def _df(start, end):
    return pd.DataFrame({"startDate": start, "endDate": end})


def test_us_slash_dates_parse_month_first():
    out = cohort.parse_date_columns(_df(["8/19/2017"], ["12/1/2016"]))
    assert out["startDate"].iloc[0] == pd.Timestamp("2017-08-19")
    assert out["endDate"].iloc[0] == pd.Timestamp("2016-12-01")


def test_ambiguous_dates_resolve_month_first():
    out = cohort.parse_date_columns(_df(["3/4/2017"], ["5/6/2017"]))
    assert out["startDate"].iloc[0] == pd.Timestamp("2017-03-04")


@pytest.mark.parametrize("value", [
    "8/19/17", "8-19-2017", "2017/08/19", "19-Aug-2017", "Aug 19, 2017",
    "19/8/2017", "2017-08-19 00:00:00",
])
def test_only_two_formats_are_accepted(value):
    with pytest.raises(ValueError, match="Unrecognized date format") as e:
        cohort.parse_date_columns(_df([value], [value]))
    assert e.value.code == "date_format"


def test_one_format_is_shared_across_columns():
    with pytest.raises(ValueError, match="Unrecognized date format"):
        cohort.parse_date_columns(_df(["8/19/2017"], ["2017-09-20"]))


def test_mixed_formats_within_a_column_are_rejected():
    with pytest.raises(ValueError, match="Unrecognized date format"):
        cohort.parse_date_columns(_df(["8/19/2017", "2017-09-20"],
                                      ["9/1/2017", "9/2/2017"]))


def test_blank_cells_become_nat_not_errors():
    out = cohort.parse_date_columns(_df(["8/19/2017", ""], ["9/20/2017", "9/21/2017"]))
    assert out["startDate"].iloc[0] == pd.Timestamp("2017-08-19")
    assert pd.isna(out["startDate"].iloc[1])


def test_parse_date_columns_returns_the_same_frame():
    df = _df(["2017-08-19"], ["2017-09-20"])
    assert cohort.parse_date_columns(df) is df
    assert cohort.parse_date_columns(pd.DataFrame({"x": [1]})).columns.tolist() == ["x"]


def test_detect_prefers_iso_and_defaults_to_it():
    assert cohort.detect_date_format([pd.Series(["2017-08-19"])]) == "%Y-%m-%d"
    assert cohort.detect_date_format([pd.Series([None, ""])]) == "%Y-%m-%d"


def test_error_names_both_accepted_forms():
    with pytest.raises(ValueError) as e:
        cohort.parse_date_columns(_df(["19-Aug-2017"], ["20-Aug-2017"]))
    msg = str(e.value)
    assert "2017-08-19" in msg and "8/19/2017" in msg
    assert "%" not in msg
