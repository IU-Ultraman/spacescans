"""Registered datasets: the registry (root, records, names, lifecycle) and polygon tables.

Polygon tables need no ``[geo]`` extra, so the registry's own behaviour is
tested with them here; rasters and features have their own files. Every
refusal is checked for the absence of the file's values: messages carry
column names, counts and formats only (PHASE_D §2.2, §4.B).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from spacescans import custom
from spacescans.custom import registry as reg

# Distinctive cell values: no refusal message may contain any of them.
SECRET_KEY = "18097999917"
SECRET_VALUE = "4.2424242"


def _csv(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


@pytest.fixture
def root(tmp_path):
    return tmp_path / "datasets"


@pytest.fixture
def static_csv(tmp_path):
    return _csv(tmp_path / "pm.csv", "GEOID10,pm25,label\n18097000100,1.5,a\n"
                                     "18097000200,NA,b\n01001020100,2.25,c\n")


def _register_static(path, root, name="PM static", **kw):
    return custom.register(path, name=name, kind="polygon_table", temporal="static",
                           key={"boundary": "tract", "column": "GEOID10"}, root=root, **kw)


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------

def test_root_is_the_argument_then_the_environment_then_the_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(custom.DATASETS_DIR_ENV, str(tmp_path / "env"))
    assert custom.datasets_root(tmp_path / "arg") == (tmp_path / "arg").absolute()
    assert custom.datasets_root() == (tmp_path / "env").absolute()
    monkeypatch.delenv(custom.DATASETS_DIR_ENV)
    monkeypatch.setitem(sys.modules, "platformdirs", None)          # not installed
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = {"darwin": tmp_path / "home" / "Library" / "Application Support" / "spacescans",
                "linux": tmp_path / "xdg" / "spacescans"}.get(sys.platform)
    if expected is None:
        pytest.skip("the fallback is checked on macOS and Linux")
    assert custom.datasets_root() == expected / "datasets"


def test_the_fallback_matches_platformdirs_when_it_is_installed(monkeypatch, tmp_path):
    platformdirs = pytest.importorskip("platformdirs")
    monkeypatch.delenv(custom.DATASETS_DIR_ENV, raising=False)
    assert custom.datasets_root() == Path(platformdirs.user_data_dir("spacescans")) / "datasets"


def test_an_absent_root_lists_nothing_and_is_not_created(tmp_path):
    missing = tmp_path / "nowhere"
    assert custom.list_datasets(missing) == []
    with pytest.raises(custom.DatasetNotFoundError, match="holds no datasets"):
        custom.get_dataset("x", missing)
    assert not missing.exists()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_register_get_list_and_remove(static_csv, root):
    rec = _register_static(static_csv, root, metadata={
        "source": "EPA test surface", "version": "v1.2", "description": "Test PM.",
        "license_note": "CC BY 4.0", "visibility": "deployment", "owner": 42})
    assert re.fullmatch(r"[0-9a-f]{32}", rec.id)
    assert rec.kind == "polygon_table" and rec.temporal == "static"
    assert rec.key == {"boundary": "Tract", "column": "GEOID10"}
    assert rec.variables == [{"name": "pm25", "unit": None, "description": None}]
    assert (rec.source, rec.version, rec.description, rec.license_note) == (
        "EPA test surface", "v1.2", "Test PM.", "CC BY 4.0")
    assert rec.visibility == "deployment" and rec.owner == "42"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec.created_utc)
    assert rec.files == ["values.parquet"]
    assert rec.checksum == hashlib.sha256(static_csv.read_bytes()).hexdigest()
    assert rec.spacescans_version
    assert rec.variable_key == f"custom_{rec.id[:8]}"
    assert rec.directory == root.absolute() / rec.id
    stored = json.loads((rec.directory / "dataset.json").read_text())
    assert stored.pop("record_format") == 1
    assert stored == rec.to_dict()
    assert list(stored)[:16] == ["id", "name", "kind", "temporal", "key", "variables", "source",
                                 "version", "description", "license_note", "visibility", "owner",
                                 "created_utc", "files", "checksum", "spacescans_version"]

    for ref in ("PM static", "pm STATIC", rec.id, rec.variable_key, rec):
        assert custom.get_dataset(ref, root) == rec
    assert custom.list_datasets(root) == [rec]
    assert custom.DatasetRecord.from_dict(rec.to_dict(), root=root) == rec

    removed = custom.remove_dataset("pm static", root)
    assert removed == rec
    assert custom.list_datasets(root) == []
    assert not rec.directory.exists()
    with pytest.raises(custom.DatasetNotFoundError):
        custom.get_dataset(rec.id, root)


def test_default_visibility_is_private_and_the_description_is_generated(static_csv, root):
    rec = _register_static(static_csv, root)
    assert rec.visibility == "private" and rec.owner is None
    assert rec.description == "Registered static Census Tract (2010) table."


def test_names_are_unique_ignoring_case_and_overwrite_replaces(static_csv, root):
    first = _register_static(static_csv, root, name="PM")
    with pytest.raises(custom.DatasetExistsError, match="already exists"):
        _register_static(static_csv, root, name="pm")
    second = _register_static(static_csv, root, name="pm", overwrite=True)
    assert second.id != first.id
    assert [r.id for r in custom.list_datasets(root)] == [second.id]
    assert not first.directory.exists()
    assert sorted(p.name for p in root.iterdir()) == sorted([".registry.lock", second.id])


def test_concurrent_registrations_of_one_name_admit_one(static_csv, root):
    outcomes: list[str] = []

    def attempt():
        try:
            _register_static(static_csv, root, name="race")
            outcomes.append("ok")
        except custom.DatasetExistsError:
            outcomes.append("exists")

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["exists"] * 3 + ["ok"]
    assert len(custom.list_datasets(root)) == 1


@pytest.mark.parametrize("name,match", [
    ("", "give the dataset a name"),
    ("   ", "give the dataset a name"),
    ("x" * 81, "longer than 80"),
    ("tab\there", "control character"),
    ("0123456789abcdef0123456789abcdef", "looks like a dataset id"),
    ("custom_deadbeef", "looks like a dataset id"),
    (5, "a string"),
])
def test_bad_names_are_refused(static_csv, root, name, match):
    with pytest.raises(custom.DatasetError, match=match):
        _register_static(static_csv, root, name=name)


@pytest.mark.parametrize("kw,match", [
    ({"kind": "csv"}, "kind must be one of"),
    ({"temporal": "biweekly"}, "temporal must be one of 'static', 'yearly', 'daily' for a "
                               "polygon_table"),
    ({"metadata": {"licence": "x"}}, "unknown metadata field"),
    ({"metadata": {"visibility": "public"}}, "visibility must be one of"),
    ({"metadata": {"source": 3}}, "must be a string"),
    ({"metadata": {"description": "x" * 2001}}, "longer than 2000"),
    ({"metadata": ["source"]}, "metadata must be a mapping"),
])
def test_bad_kinds_and_metadata_are_refused(static_csv, root, kw, match):
    args = {"name": "x", "kind": "polygon_table", "temporal": "static", "key": "tract"} | kw
    with pytest.raises(custom.DatasetError, match=match):
        custom.register(static_csv, root=root, **args)
    assert custom.list_datasets(root) == []


def test_missing_files_are_refused(tmp_path, root):
    with pytest.raises(custom.DatasetError, match="file not found"):
        custom.register(tmp_path / "nope.csv", name="x", kind="polygon_table",
                        temporal="static", key="tract", root=root)
    with pytest.raises(custom.DatasetError, match="at least one file"):
        custom.register([], name="x", kind="polygon_table", temporal="static", key="tract",
                        root=root)


def test_a_failed_registration_leaves_nothing_behind(tmp_path, root):
    bad = _csv(tmp_path / "bad.csv", "GEOID10,pm25\n1809700010,1\n")
    with pytest.raises(custom.DatasetError):
        _register_static(bad, root)
    assert [p.name for p in root.iterdir()] == [".registry.lock"]


def test_a_broken_record_is_skipped_with_a_warning(static_csv, root):
    rec = _register_static(static_csv, root)
    broken = root / ("f" * 32)
    broken.mkdir()
    (broken / "dataset.json").write_text("{not json")
    with pytest.warns(UserWarning, match="unreadable dataset record"):
        assert custom.list_datasets(root) == [rec]


def test_update_changes_metadata_name_and_variable_text_but_not_the_data(static_csv, root):
    rec = _register_static(static_csv, root, name="PM")
    other = _register_static(static_csv, root, name="Other")
    with pytest.raises(custom.DatasetExistsError):
        custom.update_dataset("PM", name="other", root=root)
    new = custom.update_dataset(
        "PM", name="PM 2.5", metadata={"visibility": "deployment", "description": "New."},
        variables=[{"name": "pm25", "unit": "ug/m3", "description": "Fine particles"}],
        root=root)
    assert (new.id, new.files, new.checksum) == (rec.id, rec.files, rec.checksum)
    assert new.name == "PM 2.5" and new.visibility == "deployment" and new.description == "New."
    assert new.variables == [{"name": "pm25", "unit": "ug/m3", "description": "Fine particles"}]
    assert custom.get_dataset(rec.id, root) == new
    with pytest.raises(custom.DatasetError, match="has no variable"):
        custom.update_dataset(rec.id, variables=[{"name": "o3", "unit": "ppb"}], root=root)
    with pytest.raises(custom.DatasetError, match="only unit and description"):
        custom.update_dataset(rec.id, variables=[{"name": "pm25", "band": 2}], root=root)
    assert custom.get_dataset(other.id, root).name == "Other"


# ---------------------------------------------------------------------------
# polygon tables
# ---------------------------------------------------------------------------

def test_a_static_table_is_normalised_to_parquet(static_csv, root):
    rec = _register_static(static_csv, root)
    table = pq.read_table(rec.path("values.parquet"))
    assert table.schema.names == ["geo_key", "pm25"]
    assert str(table.schema.field("geo_key").type) == "string"
    assert str(table.schema.field("pm25").type) == "double"
    df = table.to_pandas()
    assert df["geo_key"].tolist() == ["01001020100", "18097000100", "18097000200"]   # sorted
    assert df["pm25"].tolist()[0] == 2.25 and pd.isna(df["pm25"].tolist()[2])
    d = rec.details
    assert (d["row_count"], d["distinct_keys"], d["boundary"]) == (3, 3, "Tract")
    assert d["values_present"] == {"pm25": 2}
    assert d["columns"] == {"pm25": "pm25"}
    assert d["originals"] == [{"name": "pm.csv", "sha256": rec.checksum,
                               "bytes": static_csv.stat().st_size}]
    assert set(d["prepared_sha256"]) == {"values.parquet"}


def test_yearly_and_daily_tables_keep_their_period(tmp_path, root):
    yearly = _csv(tmp_path / "y.csv", "GEOID,Year,v\n18097000100,2016,1\n18097000100,2015.0,2\n"
                                      "18097000200,2016,3\n")
    rec = custom.register(yearly, name="y", kind="polygon_table", temporal="yearly", key="tract",
                          root=root)
    assert rec.key == {"boundary": "Tract", "column": "GEOID", "year_column": "Year"}
    df = pd.read_parquet(rec.path("values.parquet"))
    assert list(df.columns) == ["geo_key", "year", "v"]
    assert df[["geo_key", "year"]].values.tolist() == [["18097000100", 2015], ["18097000100", 2016],
                                                       ["18097000200", 2016]]
    assert rec.details["coverage_years"] == [2015, 2016]

    daily = _csv(tmp_path / "d.csv",
                 "fips,day,pm25,o3\n"
                 "18097000100,2016-01-02,1,30\n18097000100,2016/01/01,N/A,31\n"
                 "18097000200,20160103,inf,NULL\n18097000200,01/04/2016,.,32\n")
    rec = custom.register(daily, name="d", kind="polygon_table", temporal="daily",
                          key={"boundary": "Tract", "column": "fips", "date_column": "day"},
                          variables=[{"name": "pm25", "unit": "ug/m3"},
                                     {"name": "ozone", "column": "o3", "unit": "ppb"}],
                          root=root)
    table = pq.read_table(rec.path("values.parquet"))
    assert table.schema.names == ["geo_key", "date", "pm25", "ozone"]
    assert str(table.schema.field("date").type) == "date32[day]"
    df = table.to_pandas()
    assert df["date"].tolist() == [date(2016, 1, 1), date(2016, 1, 2), date(2016, 1, 3),
                                   date(2016, 1, 4)]
    assert df["pm25"].isna().tolist() == [True, False, True, True]      # N/A, inf and "." missing
    assert df["ozone"].tolist()[:2] == [31.0, 30.0] and pd.isna(df["ozone"][2])
    assert rec.details["coverage_dates"] == ["2016-01-01", "2016-01-04"]
    assert rec.details["coverage_years"] == [2016, 2016]
    assert rec.variables == [{"name": "pm25", "unit": "ug/m3", "description": None},
                             {"name": "ozone", "unit": "ppb", "description": None}]
    assert rec.details["columns"] == {"pm25": "pm25", "ozone": "o3"}


def test_encodings_header_quirks_ragged_rows_and_parquet_inputs(tmp_path, root):
    latin = _csv(tmp_path / "l.csv", "GEOID10 ,name,v\n18097000100,Caf\u00e9,1\n"
                                     "18097000200,X\n\n", encoding="latin-1")
    rec = custom.register(latin, name="latin", kind="polygon_table", temporal="static",
                          key="tract", root=root)
    df = pd.read_parquet(rec.path("values.parquet"))
    assert df["geo_key"].tolist() == ["18097000100", "18097000200"]
    assert df["v"].tolist()[0] == 1.0 and pd.isna(df["v"].tolist()[1])

    bom = _csv(tmp_path / "b.csv", "\ufeffGEOID10,v\n18097000100,1\n")
    assert custom.register(bom, name="bom", kind="polygon_table", temporal="static",
                           key="tract", root=root).key["column"] == "GEOID10"

    pq_in = tmp_path / "p.parquet"
    pd.DataFrame({"ZCTA5CE10": ["46202", "02134"], "v": [1.0, None]}).to_parquet(pq_in)
    rec = custom.register(pq_in, name="pq", kind="polygon_table", temporal="static",
                          key="zcta", root=root)
    assert pd.read_parquet(rec.path("values.parquet"))["geo_key"].tolist() == ["02134", "46202"]


def test_default_variables_are_the_numeric_columns(tmp_path, root):
    path = _csv(tmp_path / "v.csv", "GEOID10,pm 2.5,name,ndvi,empty,date\n"
                                    "18097000100,1,a,0.5,,x\n18097000200,2,b,NA,,y\n")
    rec = custom.register(path, name="v", kind="polygon_table", temporal="static", key="tract",
                          root=root)
    assert [v["name"] for v in rec.variables] == ["pm_2_5", "ndvi"]
    assert rec.details["columns"] == {"pm_2_5": "pm 2.5", "ndvi": "ndvi"}
    none = _csv(tmp_path / "n.csv", "GEOID10,name\n18097000100,a\n")
    with pytest.raises(custom.DatasetError, match="no numeric value column"):
        custom.register(none, name="n", kind="polygon_table", temporal="static", key="tract",
                        root=root)


def _refusal(tmp_path, root, text, temporal="static", key="tract", **kw) -> str:
    path = _csv(tmp_path / f"r{abs(hash(text))}.csv", text)
    with pytest.raises(custom.DatasetError) as info:
        custom.register(path, name="r", kind="polygon_table", temporal=temporal, key=key,
                        root=root, **kw)
    message = str(info.value)
    for secret in (SECRET_KEY, SECRET_VALUE, "1809799991", "2016-13-45", "nonsense"):
        assert secret not in message
    assert custom.list_datasets(root) == []
    return message


@pytest.mark.parametrize("text,temporal,match", [
    (f"GEOID10,v\n1809799991,{SECRET_VALUE}\n{SECRET_KEY},1\n", "static",
     r"1 of 2 values in 'GEOID10' are not 11-digit Tract codes\. 1 of them is one digit short"),
    (f"GEOID10,v\n,{SECRET_VALUE}\n{SECRET_KEY},1\n", "static", r"1 of 2 rows have no value"),
    (f"GEOID10,v\n{SECRET_KEY},1\n{SECRET_KEY},{SECRET_VALUE}\n", "static",
     r"1 duplicate rows: each geography must appear once"),
    (f"GEOID10,year,v\n{SECRET_KEY},2016,1\n{SECRET_KEY},2016,2\n", "yearly",
     r"duplicate rows: each geography and year"),
    (f"GEOID10,year,v\n{SECRET_KEY},2016.5,1\n{SECRET_KEY},nonsense,2\n", "yearly",
     r"2 of 2 rows have no usable value in 'year' \(expected a whole-number year"),
    (f"GEOID10,date,v\n{SECRET_KEY},2016-13-45,1\n{SECRET_KEY},2016-01-01,2\n", "daily",
     r"1 of 2 rows have no usable value in 'date' \(expected a date"),
    (f"GEOID10,date,v\n{SECRET_KEY},2016-01-01,1\n{SECRET_KEY},20160101,2\n", "daily",
     r"duplicate rows: each geography and date must appear once"),
    (f"GEOID10,v,w\n{SECRET_KEY},nonsense,1\n18097000100,nonsense,2\n", "static",
     r"mostly non-numeric: v \(2 rows\)"),
])
def test_table_rules_are_enforced_without_quoting_the_file(tmp_path, root, text, temporal,
                                                           match):
    kw = {"variables": ["v"]} if "nonsense,1" in text else {}
    message = _refusal(tmp_path, root, text, temporal=temporal, **kw)
    assert re.search(match, message), message


def test_columns_and_keys_must_exist(tmp_path, root):
    msg = _refusal(tmp_path, root, f"FIPS_CODE,v\n{SECRET_KEY},1\n")
    assert "name the key column" in msg and "The file has: FIPS_CODE, v" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v\n{SECRET_KEY},1\n",
                   key={"boundary": "tract", "column": "tract"})
    assert "key column 'tract' is not in the file" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v\n{SECRET_KEY},1\n", temporal="daily")
    assert "a daily table needs a date column" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v\n{SECRET_KEY},1\n", variables=["pm25"])
    assert "value column 'pm25' is not in the file" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v,e\n{SECRET_KEY},1,\n", variables=["v", "e"])
    assert "no data at all: e" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v\n{SECRET_KEY},1\n", key="state")
    assert "unknown boundary 'state'" in msg
    msg = _refusal(tmp_path, root, f"GEOID10,v\n{SECRET_KEY},1\n",
                   key={"boundary": "tract", "geography": "x"})
    assert "unknown key field(s): geography" in msg
    msg = _refusal(tmp_path, root, "GEOID10,v\n")
    assert "header but no data rows" in msg


@pytest.mark.parametrize("variables,match", [
    (["date"], "is used by the linkage tables"),
    (["1st"], "must start with a letter"),
    (["v", "V"], "used twice"),
    ([{"name": "v", "colour": "red"}], "unknown variable field"),
    ([{"name": "v", "unit": "u" * 51}], "longer than 50"),
])
def test_variable_names_are_checked(tmp_path, root, variables, match):
    path = _csv(tmp_path / "v.csv", f"GEOID10,v,V\n{SECRET_KEY},1,2\n")
    with pytest.raises(custom.DatasetError, match=match):
        custom.register(path, name="v", kind="polygon_table", temporal="static", key="tract",
                        variables=variables, root=root)


def test_a_table_csv_that_is_not_a_csv_is_refused_without_its_content(tmp_path, root):
    path = tmp_path / "x.csv"
    path.write_bytes(b'GEOID10,v\n"' + SECRET_KEY.encode() + b',1\n')        # unterminated quote
    with pytest.raises(custom.DatasetError) as info:
        custom.register(path, name="x", kind="polygon_table", temporal="static", key="tract",
                        root=root)
    assert SECRET_KEY not in str(info.value)
    with pytest.raises(custom.DatasetError, match=r"a polygon table is a \.csv"):
        custom.register(_csv(tmp_path / "x.xlsx", "x"), name="x", kind="polygon_table",
                        temporal="static", key="tract", root=root)


# ---------------------------------------------------------------------------
# definitions and the link hook
# ---------------------------------------------------------------------------

def test_a_table_definition_is_linkable_and_guards_its_file(tmp_path, root):
    daily = _csv(tmp_path / "d.csv", "GEOID10,date,pm25\n18097000100,2016-01-02,1\n")
    rec = custom.register(daily, name="PM daily", kind="polygon_table", temporal="daily",
                          key="tract", variables=[{"name": "pm25", "unit": "ug/m3",
                                                   "description": "PM2.5"}], root=root)
    d = custom.dataset_definition("pm daily", root)
    assert d == rec.definition()
    assert (d["geometry"], d["temporal"], d["boundary"], d["spatial_method"]) == (
        "polygon", "daily", "Tract", "areal")
    assert (d["join_col"], d["key_col"], d["date_col"], d["year_col"]) == (
        "GEOID10", "geo_key", "date", None)
    assert d["variable_key"] == rec.variable_key and d["dataset_id"] == rec.id[:8]
    assert d["coverage_years"] == [2016, 2016] and d["coverage_dates"] == ["2016-01-02"] * 2
    assert d["value_units"] == {"pm25": "ug/m3"} and d["value_labels"] == {"pm25": "PM2.5"}
    assert d["display_unit"] == "ug/m3"
    assert d["registered"]["id"] == rec.id
    assert custom.catalog_entry(d)["temporal"] == "daily"
    checked = custom.check_definition(d, key=rec.variable_key, deep=True)
    assert checked["values_path"] == str(rec.path("values.parquet"))
    rows = custom.feature_rows(d)
    assert rows == [{"feature_name": "pm25", "short_description": "PM2.5",
                     "detailed_description": "Registered daily Census Tract (2010) table, "
                                             "2016-01-02 to 2016-01-02. Unit: ug/m3."}]
    rec.path("values.parquet").write_bytes(b"edited")
    with pytest.raises(custom.CustomExposomeError, match="changed since"):
        custom.check_definition(d, key=rec.variable_key, deep=True)


def test_check_definition_knows_the_daily_table_rules(static_csv, root):
    d = _register_static(static_csv, root).definition()
    with pytest.raises(custom.CustomExposomeError, match="date_col is set exactly"):
        custom.check_definition({**d, "temporal": "daily"}, files=False)
    with pytest.raises(custom.CustomExposomeError, match="static, yearly or daily"):
        custom.check_definition({**d, "temporal": "biweekly"}, files=False)
    json_round_trip = {**d, "date_col": {}, "value_units": [], "value_labels": []}
    assert custom.check_definition(json_round_trip, files=False)["date_col"] is None


def test_resolve_custom_accepts_names_ids_records_and_definitions(static_csv, tmp_path, root):
    a = _register_static(static_csv, root, name="A")
    b = _register_static(static_csv, root, name="B")
    loose = custom.define_polygon(static_csv, boundary="Tract", key_col="GEOID10",
                                  value_cols=["pm25"])
    out = custom.resolve_custom(["A", b.id, a, loose, b.variable_key], root)
    assert list(out) == [a.variable_key, b.variable_key, loose["variable_key"]]
    assert out[a.variable_key] == a.definition()
    assert custom.resolve_custom("A", root) == {a.variable_key: a.definition()}
    with pytest.raises(custom.DatasetNotFoundError, match="registered: 'A', 'B'"):
        custom.resolve_custom(["C"], root)
    with pytest.raises(custom.DatasetError, match="has no variable_key"):
        custom.resolve_custom([{"label": "x"}], root)
    with pytest.raises(custom.DatasetError, match="cannot use a int"):
        custom.resolve_custom([3], root)


def test_link_overlay_adds_listed_datasets_and_keeps_the_mapping_form(static_csv, root,
                                                                     monkeypatch):
    rec = _register_static(static_csv, root, name="A")
    monkeypatch.setenv(custom.DATASETS_DIR_ENV, str(root))
    keys, overlay, warns = custom.link_overlay(["A"], ["noise"])
    assert keys == ["noise", rec.variable_key] and list(overlay) == [rec.variable_key]
    assert warns == []
    keys, overlay, warns = custom.link_overlay({rec.variable_key: rec.definition()}, ["noise"])
    assert keys == ["noise"] and overlay == {}
    assert [w["code"] for w in warns] == ["custom_unused"]
    keys, overlay, _ = custom.link_overlay([rec, "A"], [rec.variable_key])
    assert keys == [rec.variable_key]
    assert custom.link_overlay(None, ["noise"]) == (["noise"], {}, [])
