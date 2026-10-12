"""Dataset definitions: polygon (CSV) and raster (GeoTIFF).

Ported from spacescans-web ``backend/tests/test_custom_exposomes.py`` (the
create/create_raster cases, minus storage and ownership, which stay in the
web), plus the cases only a library-less caller (R) needs: files used in place,
stable ids, default names.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from spacescans import custom
from spacescans.custom import CustomExposomeError

TRACT_A = "12073001100"
TRACT_B = "12073001200"


def _csv(rows):
    return "\n".join(rows).encode()


def _static_csv():
    return _csv(["tract,greenness,heat", f"{TRACT_A},0.41,88.2", f"{TRACT_B},0.55,86.0"])


def _yearly_csv():
    return _csv(["tract,year,greenness", f"{TRACT_A},2015,0.41", f"{TRACT_A},2016,0.44",
                 f"{TRACT_B},2015,0.55"])


def _define(content=None, **kw):
    params = dict(label="My greenness", description="", boundary="Tract", key_col="tract",
                  value_cols=["greenness", "heat"], year_col=None)
    params.update(kw)
    return custom.define_polygon(_static_csv() if content is None else content, **params)


# --------------------------------------------------------------------------
# polygon definitions
# --------------------------------------------------------------------------

def test_definition_is_catalog_shaped():
    m = _define()
    assert m["experiment"] == "custom"
    assert m["boundary"] == "Tract"
    assert m["spatial_method"] == "areal"
    assert m["temporal"] == "static"
    assert m["value_cols"] == ["greenness", "heat"]
    assert m["join_col"] == "GEOID10"          # the C3 weights table's column
    assert m["key_col"] == "tract"             # the user's own column
    assert "ontology_id" not in m              # absent, not null
    assert m["row_count"] == 2 and m["distinct_keys"] == 2
    assert m["variable_key"] == custom.variable_key(m["dataset_id"])
    assert m["sha256"] == hashlib.sha256(_static_csv()).hexdigest()
    assert m["description"] == "User-uploaded Tract values."
    assert m["data_source"] == "Uploaded by user: values.csv"


def test_the_private_fields_are_exactly_the_webs():
    """Pins the superset: the same private fields the web manifest carries."""
    m = _define()
    assert set(m) - set(custom.CATALOG_FIELDS) == {
        "geometry", "dataset_id", "variable_key", "owner_uid", "join_col", "key_col",
        "year_col", "value_labels", "value_units", "values_path", "row_count",
        "distinct_keys", "sha256", "uploaded_filename", "created_at",
    }


def test_catalog_entry_keeps_only_catalog_fields():
    entry = custom.catalog_entry(_define())
    assert set(entry) <= set(custom.CATALOG_FIELDS)
    assert entry["display_unit"] == "unitless" and entry["value_cols"] == ["greenness", "heat"]


def test_yearly_dataset_records_its_own_coverage_years():
    m = _define(_yearly_csv(), value_cols=["greenness"], year_col="year")
    assert m["temporal"] == "yearly"
    assert m["coverage_years"] == [2015, 2016]
    assert m["year_col"] == "year"


def test_static_dataset_claims_the_full_window():
    assert _define()["coverage_years"] == [2013, 2019]


def test_a_path_is_used_in_place(tmp_path):
    """No library: the C4 step reads the user's own file, by absolute path."""
    p = tmp_path / "greenness_2010.csv"
    p.write_bytes(_static_csv())
    m = custom.define_polygon(p, boundary="Tract", key_col="tract",
                              value_cols=["greenness", "heat"])
    assert m["values_path"] == str(p.resolve()) or Path(m["values_path"]) == p.absolute()
    assert m["uploaded_filename"] == "greenness_2010.csv"
    assert m["label"] == "greenness_2010"              # default name: the file's stem
    assert m["owner_uid"] is None


def test_the_id_is_stable_for_the_same_file_and_name():
    assert _define()["dataset_id"] == _define()["dataset_id"]
    assert _define()["dataset_id"] != _define(label="Another name")["dataset_id"]


def test_library_fields_are_recorded_as_given():
    m = _define(dataset_id="0badc0de", owner_uid=7, values_path="/lib/user-7/0badc0de/values.csv",
                uploaded_filename="mine.csv", created_at="2026-10-12T00:00:00+00:00")
    assert (m["dataset_id"], m["variable_key"], m["owner_uid"]) == \
        ("0badc0de", "custom_0badc0de", "7")
    assert m["values_path"] == "/lib/user-7/0badc0de/values.csv"
    assert m["data_source"] == "Uploaded by user: mine.csv"
    assert m["created_at"] == "2026-10-12T00:00:00+00:00"


def test_bytes_without_a_path_have_no_values_path():
    assert _define()["values_path"] is None


def test_units_are_per_column_and_the_catalog_unit_is_derived():
    m = _define(_csv(["tract,greenness,heat", f"{TRACT_A},0.4,88", f"{TRACT_B},0.5,86"]),
                units={"greenness": "index", "heat_index": "°F"})
    assert m["value_units"] == {"greenness": "index"}      # heat_index is not selected
    assert m["display_unit"] == "index"


def test_distinct_units_join_in_column_order():
    m = _define(units={"heat": "F", "greenness": "index"})
    assert m["value_units"] == {"greenness": "index", "heat": "F"}
    assert m["display_unit"] == "index / F"


def test_same_unit_twice_is_listed_once():
    assert _define(units={"greenness": "index", "heat": "index"})["display_unit"] == "index"


def test_no_units_becomes_unitless():
    m = _define()
    assert m["display_unit"] == "unitless" and m["value_units"] == {}


def test_per_column_unit_is_kept_as_typed_and_the_catalog_unit_transliterated():
    m = _define(units={"greenness": "  µg/m³ ", "heat": "°F"})
    assert m["value_units"] == {"greenness": "µg/m³", "heat": "°F"}
    assert m["display_unit"] == "ug/m3 / degF"


def test_labels_are_kept_only_for_selected_columns():
    m = _define(labels={"greenness": "Greenness (NDVI)", "other": "x", "heat": "  "})
    assert m["value_labels"] == {"greenness": "Greenness (NDVI)"}


def test_a_name_in_any_script_is_kept():
    assert _define(label="绿地指数")["label"] == "绿地指数"


def test_unnamed_dataset_is_rejected():
    with pytest.raises(CustomExposomeError, match="give the dataset a name"):
        _define(label="   ")


def test_bytes_need_a_name():
    with pytest.raises(CustomExposomeError, match="give the dataset a name"):
        custom.define_polygon(_static_csv(), boundary="Tract", key_col="tract",
                              value_cols=["greenness"])


def test_overlong_name_is_rejected():
    with pytest.raises(CustomExposomeError, match="longer than 80"):
        _define(label="x" * 81)


def test_the_name_is_checked_before_the_file():
    """As in the web: an unnamed upload of a bad file reports the name first."""
    with pytest.raises(CustomExposomeError, match="give the dataset a name"):
        _define(b"", label="")


def test_validation_errors_surface_from_define():
    with pytest.raises(CustomExposomeError, match="12-digit"):
        _define(boundary="BG")


# --------------------------------------------------------------------------
# raster definitions
# --------------------------------------------------------------------------

def _cells(rows=4, cols=5, offset=0):
    return np.array([[r * 10 + c + offset for c in range(cols)] for r in range(rows)])


def _stack(n=3):
    return np.stack([_cells(offset=100 * i) for i in range(n)])


@pytest.fixture
def tif(tmp_path, write_tif):
    def _make(name, array, **kw) -> Path:
        return write_tif(tmp_path / name, array, **kw)
    return _make


@pytest.mark.geo
def test_raster_static(tif):
    p = tif("s.tif", _cells())
    m = custom.define_raster([("ndvi.tif", p.read_bytes(), None)], label="My raster",
                             value_col="ndvi", value_label="Greenness", value_unit="index",
                             paths=["/lib/user-1/ab12cd34/rasters/static.tif"])
    assert m["geometry"] == "raster"
    assert m["boundary"] == "Point" and m["spatial_method"] == "grid"
    assert m["temporal"] == "static" and m["year_col"] is None
    assert m["join_col"] == "grid_id" and m["key_col"] == "grid_id"
    assert m["value_cols"] == ["ndvi"]
    assert m["value_units"] == {"ndvi": "index"} and m["display_unit"] == "index"
    assert m["value_labels"] == {"ndvi": "Greenness"}
    assert m["values_path"] is None                    # materialised per run
    assert len(m["rasters"]) == 1 and m["rasters"][0]["year"] is None
    assert m["rasters"][0]["path"].endswith("rasters/static.tif")
    assert m["rasters"][0]["uploaded_filename"] == "ndvi.tif"
    assert m["grid"]["width"] == 5 and m["grid"]["height"] == 4
    assert m["row_count"] == 20
    assert m["coverage_years"] == [2013, 2019]
    assert m["description"] == "User-uploaded 0.01° raster."
    assert "ontology_id" not in m


@pytest.mark.geo
def test_raster_paths_are_used_in_place(tif):
    p = tif("ndvi_static.tif", _cells())
    m = custom.define_raster([(p, None)], value_cols=["ndvi"], bands=[1])
    assert m["label"] == "ndvi_static"
    assert m["rasters"][0]["path"] == str(p.absolute())
    assert m["rasters"][0]["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert m["rasters"][0]["bytes"] == p.stat().st_size
    assert m["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.geo
def test_raster_ids_match_for_bytes_and_paths(tif):
    """The id hashes the files' bytes however they were handed in."""
    a, b = tif("2016.tif", _cells()), tif("2017.tif", _cells(offset=100))
    by_path = custom.define_raster([(a, 2016), (b, 2017)], label="x")
    by_bytes = custom.define_raster([("2016.tif", a.read_bytes(), 2016),
                                     ("2017.tif", b.read_bytes(), 2017)], label="x")
    assert by_path["dataset_id"] == by_bytes["dataset_id"]
    assert by_path["sha256"] == by_bytes["sha256"]


@pytest.mark.geo
def test_raster_yearly_shares_one_grid(tif):
    m = custom.define_raster([("a.tif", tif("y16.tif", _cells()).read_bytes(), 2016),
                              ("b.tif", tif("y17.tif", _cells(offset=100)).read_bytes(), 2017)],
                             label="Yearly raster", value_col="ndvi",
                             paths=["/r/2016.tif", "/r/2017.tif"])
    assert m["temporal"] == "yearly" and m["year_col"] == "year"
    assert m["coverage_years"] == [2016, 2017]
    assert [r["year"] for r in m["rasters"]] == [2016, 2017]
    assert m["rasters"][0]["path"] == "/r/2016.tif"
    assert m["description"] == "User-uploaded 0.01° raster, 2016–2017."


@pytest.mark.geo
def test_rasters_are_listed_by_year(tif):
    m = custom.define_raster([(tif("b.tif", _cells()), 2017), (tif("a.tif", _cells()), 2016)],
                             label="x")
    assert [r["year"] for r in m["rasters"]] == [2016, 2017]
    assert m["data_source"] == "Uploaded by user: a.tif, b.tif"


@pytest.mark.geo
def test_raster_rejects_a_grid_mismatch_between_years(tif):
    with pytest.raises(CustomExposomeError, match="different grid"):
        custom.define_raster([(tif("a.tif", _cells()), 2016),
                              (tif("b.tif", _cells(), res=0.02), 2017)], label="x")


@pytest.mark.geo
def test_raster_rejects_duplicate_years(tif):
    with pytest.raises(CustomExposomeError, match="same year"):
        custom.define_raster([(tif("a.tif", _cells()), 2016), (tif("b.tif", _cells()), 2016)],
                             label="x")


@pytest.mark.geo
def test_raster_requires_a_year_on_every_file_when_several(tif):
    with pytest.raises(CustomExposomeError, match="assign a year to every file"):
        custom.define_raster([(tif("a.tif", _cells()), 2016), (tif("b.tif", _cells()), None)],
                             label="x")


@pytest.mark.geo
def test_raster_rejects_an_unusable_year(tif):
    with pytest.raises(CustomExposomeError, match="unusable year"):
        custom.define_raster([(tif("a.tif", _cells()), 1850), (tif("b.tif", _cells()), 2016)],
                             label="x")


def test_raster_needs_at_least_one_file():
    with pytest.raises(CustomExposomeError, match="upload at least one GeoTIFF"):
        custom.define_raster([], label="x")


@pytest.mark.geo
def test_raster_rejects_a_bad_column_name(tif):
    with pytest.raises(CustomExposomeError, match="value column name"):
        custom.define_raster([(tif("a.tif", _cells()), None)], label="x", value_col="my value!")


@pytest.mark.geo
def test_raster_rejects_a_missing_band(tif):
    with pytest.raises(CustomExposomeError, match="band 2 does not exist"):
        custom.define_raster([(tif("a.tif", _cells()), None)], label="x", band=2)


@pytest.mark.geo
def test_raster_surfaces_the_inspection_error_with_the_filename(tif):
    content = tif("s.tif", _cells(), bottom_up=True).read_bytes()
    with pytest.raises(CustomExposomeError, match=r"south\.tif: .*bottom-up"):
        custom.define_raster([("south.tif", content, None)], label="x")


@pytest.mark.geo
def test_raster_with_several_bands_names_one_column_each(tif):
    p = tif("p.tif", _stack(), nodata=-9999)
    m = custom.define_raster(
        [("pollutants.tif", p.read_bytes(), None)], label="Pollutants",
        bands=[3, 1], value_cols=["no2", "pm25"],
        labels={"no2": "Nitrogen dioxide", "pm25": "  "},
        units={"no2": "ppb", "pm25": "µg/m³", "other": "x"},
    )
    assert m["value_cols"] == ["no2", "pm25"]
    assert m["bands"] == [3, 1] and m["band"] == 3
    assert m["band_nodata"] == [-9999.0, -9999.0]
    assert m["value_labels"] == {"no2": "Nitrogen dioxide"}
    assert m["value_units"] == {"no2": "ppb", "pm25": "µg/m³"}
    assert m["display_unit"] == "ppb / ug/m3"


@pytest.mark.geo
def test_band_columns_default_when_unnamed(tif):
    p = tif("p.tif", _stack())
    assert custom.define_raster([(p, None)], bands=[2])["value_cols"] == ["value"]
    assert custom.define_raster([(p, None)], bands=[3, 1])["value_cols"] == ["band3", "band1"]


@pytest.mark.geo
def test_the_single_band_arguments_still_work(tif):
    p = tif("a.tif", _stack(2))
    m = custom.define_raster([("a.tif", p.read_bytes(), None)], label="x",
                             value_col=" ndvi ", band=2, value_label="Greenness",
                             value_unit="index")
    assert m["value_cols"] == ["ndvi"] and m["bands"] == [2] and m["band"] == 2
    assert m["value_labels"] == {"ndvi": "Greenness"} and m["value_units"] == {"ndvi": "index"}


@pytest.mark.geo
@pytest.mark.parametrize("bands,cols,match", [
    ([], [], "select at least one band"),
    ([1, 1], ["a", "b"], "a band is selected twice"),
    ([0], ["a"], "band numbers start at 1"),
    ([1, 2], ["a"], "2 band\\(s\\) but 1 column name"),
    ([1, 2], ["pm25", "PM25"], "used twice: PM25, pm25"),
    ([1], ["year"], "already used by result.csv"),
    ([1], ["episode_id"], "already used by result.csv"),
    ([1], ["2pm"], "value column name"),
    ([1, 4], ["a", "b"], "band 4 does not exist \\(the file has 3\\)"),
])
def test_raster_rejects_bad_band_selections(tif, bands, cols, match):
    p = tif("a.tif", _stack())
    with pytest.raises(CustomExposomeError, match=match):
        custom.define_raster([("a.tif", p.read_bytes(), None)], label="x",
                             bands=bands, value_cols=cols)


@pytest.mark.geo
def test_every_yearly_file_must_have_every_selected_band(tif):
    with pytest.raises(CustomExposomeError, match=r"2017\.tif: band 3 does not exist"):
        custom.define_raster([("2016.tif", tif("a.tif", _stack(3)).read_bytes(), 2016),
                              ("2017.tif", tif("b.tif", _stack(2)).read_bytes(), 2017)],
                             label="x", bands=[1, 3], value_cols=["a", "c"])


def test_too_many_bands_is_refused_before_reading_files():
    n = custom.MAX_VALUE_COLS + 1
    with pytest.raises(CustomExposomeError, match="exceeds the limit"):
        custom.define_raster([("a.tif", b"not read", None)], label="x",
                             bands=list(range(1, n + 1)), value_cols=[f"c{i}" for i in range(n)])


def test_too_many_files_is_refused():
    with pytest.raises(CustomExposomeError, match="31 files exceeds the limit of 30"):
        custom.define_raster([(f"{y}.tif", b"x", y) for y in range(1990, 2021)], label="x")


@pytest.mark.geo
def test_preview_raster_reports_a_user_error(tif):
    with pytest.raises(CustomExposomeError, match="bottom-up") as exc:
        custom.preview_raster(tif("s.tif", _cells(), bottom_up=True).read_bytes())
    assert type(exc.value) is CustomExposomeError       # not the RasterError subclass
    meta = custom.preview_raster(tif("ok.tif", _cells()))
    assert meta["band_count"] == 1 and len(meta["grid_hash"]) == 8


def test_raster_bands_reads_old_single_band_definitions():
    d = {"value_cols": ["ndvi"], "band": 1, "grid": {"nodata": -5.0}}
    assert custom.raster_bands(d) == (["ndvi"], [1], [-5.0])


def test_raster_bands_refuses_a_band_column_mismatch():
    d = {"value_cols": ["no2", "pm25"], "bands": [1], "band_nodata": [None]}
    with pytest.raises(ValueError, match="1 band\\(s\\) for 2 value column"):
        custom.raster_bands(d)
