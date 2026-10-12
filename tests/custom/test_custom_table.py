"""CSV uploads: validation rules, preview, units and keys.

Ported from spacescans-web ``backend/tests/test_custom_exposomes.py`` (keys,
validation, preview and unit cases). Every rule stands for a defect that would
otherwise surface as an empty join twenty minutes into a run.
"""
from __future__ import annotations

import re

import pytest

from spacescans import custom
from spacescans.custom import CustomExposomeError

TRACT_A = "12073001100"
TRACT_B = "12073001200"


def _csv(rows: list[str]) -> bytes:
    return "\n".join(rows).encode()


def _static_csv() -> bytes:
    return _csv(["tract,greenness,heat", f"{TRACT_A},0.41,88.2", f"{TRACT_B},0.55,86.0"])


def _yearly_csv() -> bytes:
    return _csv(["tract,year,greenness", f"{TRACT_A},2015,0.41", f"{TRACT_A},2016,0.44",
                 f"{TRACT_B},2015,0.55"])


def _validate(content, **kw):
    params = dict(boundary="Tract", key_col="tract", value_cols=["v"], year_col=None)
    params.update(kw)
    return custom.validate_csv(content, **params)


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

def test_variable_key_round_trips():
    key = custom.variable_key("ab12cd34")
    assert key == "custom_ab12cd34"
    assert custom.dataset_id_from_key(key) == "ab12cd34"
    assert custom.is_custom_key(key)


@pytest.mark.parametrize("key", ["ndi", "custom_", "custom_NOTHEX", "custom_ab12"])
def test_non_custom_keys_are_rejected(key):
    assert custom.dataset_id_from_key(key) is None
    assert not custom.is_custom_key(key)


def test_variable_key_matches_the_catalog_key_pattern():
    """The catalog schema pins keys to ^[a-z][a-z0-9_]*$."""
    assert re.fullmatch(r"^[a-z][a-z0-9_]*$", custom.variable_key("0a1b2c3d"))


def test_derive_dataset_id_is_the_webs_first_candidate():
    """web: base = sha256(content + name + uid); candidate k = sha256(f"{base}:{k}")[:8]."""
    import hashlib
    content, name, uid = b"tract,v\n", "My data", 7
    base = hashlib.sha256(content + name.encode() + str(uid).encode()).hexdigest()
    for k in (0, 1, 5):
        expected = hashlib.sha256(f"{base}:{k}".encode()).hexdigest()[:8]
        assert custom.derive_dataset_id(content, name, salt=str(uid), attempt=k) == expected
    plain = custom.derive_dataset_id(content, name)
    assert custom.is_dataset_id(plain) and plain == custom.derive_dataset_id(content, name)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_static_csv_validates():
    facts = _validate(_static_csv(), value_cols=["greenness", "heat"])
    assert facts == {"row_count": 2, "coverage_years": [0, 0], "distinct_keys": 2}


def test_yearly_csv_reports_its_year_range():
    facts = _validate(_yearly_csv(), value_cols=["greenness"], year_col="year")
    assert facts["coverage_years"] == [2015, 2016]
    assert facts["row_count"] == 3


def test_a_path_validates_like_its_bytes(tmp_path):
    p = tmp_path / "values.csv"
    p.write_bytes(_yearly_csv())
    assert _validate(str(p), value_cols=["greenness"], year_col="year") == \
        _validate(_yearly_csv(), value_cols=["greenness"], year_col="year")
    assert custom.preview_csv(p) == custom.preview_csv(_yearly_csv())


def test_an_unreadable_path_is_a_user_error(tmp_path):
    with pytest.raises(CustomExposomeError, match="cannot read"):
        custom.preview_csv(tmp_path / "missing.csv")


def test_numeric_fips_is_rejected_with_a_leading_zero_hint():
    """The FAQSD defect: a spreadsheet drops the leading zero and the join
    silently matches nothing. 11-digit tract codes starting with 0 become 10."""
    content = _csv(["tract,v", "1073001100,0.4", "6073001100,0.5"])
    with pytest.raises(CustomExposomeError) as exc:
        _validate(content)
    assert "11-digit" in str(exc.value)
    assert "leading zero" in str(exc.value)


def test_wrong_boundary_width_is_rejected():
    """Tract codes offered as a Block Group dataset are 12-digit there."""
    with pytest.raises(CustomExposomeError, match="12-digit"):
        _validate(_static_csv(), boundary="BG", value_cols=["greenness"])


def test_duplicate_keys_are_rejected():
    """A duplicate geography multiplies rows in the C4 join."""
    content = _csv(["tract,v", f"{TRACT_A},0.4", f"{TRACT_A},0.5"])
    with pytest.raises(CustomExposomeError, match="duplicate"):
        _validate(content)


def test_duplicates_are_per_year_when_a_year_column_is_declared():
    ok = _csv(["tract,year,v", f"{TRACT_A},2015,0.4", f"{TRACT_A},2016,0.5"])
    _validate(ok, year_col="year")
    bad = _csv(["tract,year,v", f"{TRACT_A},2015,0.4", f"{TRACT_A},2015,0.5"])
    with pytest.raises(CustomExposomeError, match="duplicate"):
        _validate(bad, year_col="year")


def test_blank_geography_is_rejected():
    content = _csv(["tract,v", f"{TRACT_A},0.4", ",0.5"])
    with pytest.raises(CustomExposomeError, match="no value in"):
        _validate(content)


def test_mostly_non_numeric_value_column_is_rejected():
    content = _csv(["tract,v", f"{TRACT_A},high", f"{TRACT_B},low"])
    with pytest.raises(CustomExposomeError, match="non-numeric"):
        _validate(content)


def test_blanks_and_na_count_as_missing_not_as_parse_failures():
    content = _csv(["tract,v", f"{TRACT_A},0.4", f"{TRACT_B},NA", "12073001300,",
                    "12073001400,0.6"])
    assert _validate(content)["row_count"] == 4


def test_value_column_with_no_data_at_all_is_rejected():
    content = _csv(["tract,v,empty", f"{TRACT_A},0.4,", f"{TRACT_B},0.5,"])
    with pytest.raises(CustomExposomeError, match="no data at all"):
        _validate(content, value_cols=["v", "empty"])


def test_missing_column_names_are_reported_with_what_the_file_has():
    with pytest.raises(CustomExposomeError) as exc:
        _validate(_static_csv(), key_col="GEOID", value_cols=["greenness"])
    assert "GEOID" in str(exc.value) and "tract" in str(exc.value)


def test_a_column_cannot_be_both_key_and_value():
    with pytest.raises(CustomExposomeError, match="used twice"):
        _validate(_static_csv(), value_cols=["tract"])


def test_unusable_year_is_rejected():
    content = _csv(["tract,year,v", f"{TRACT_A},last year,0.4"])
    with pytest.raises(CustomExposomeError, match="unusable year"):
        _validate(content, year_col="year")


def test_header_only_file_is_rejected():
    with pytest.raises(CustomExposomeError, match="no data rows"):
        _validate(b"tract,v\n")


def test_empty_file_is_rejected():
    with pytest.raises(CustomExposomeError, match="file is empty"):
        _validate(b"")


def test_no_value_columns_is_rejected():
    with pytest.raises(CustomExposomeError, match="at least one value column"):
        _validate(_static_csv(), value_cols=[])


def test_too_many_value_columns_is_rejected():
    with pytest.raises(CustomExposomeError, match="exceeds the limit of 40"):
        _validate(_static_csv(), value_cols=[f"c{i}" for i in range(41)])


def test_unknown_boundary_is_rejected():
    with pytest.raises(CustomExposomeError, match="unknown boundary"):
        _validate(_static_csv(), boundary="Parcel", value_cols=["greenness"])


def test_bom_in_the_header_is_stripped():
    content = "﻿tract,v\n12073001100,0.4\n".encode("utf-8")
    _validate(content)


def test_latin1_file_is_read():
    content = "tract,v,note\n12073001100,0.4,caf\xe9\n".encode("latin-1")
    assert _validate(content)["row_count"] == 1


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

def test_preview_flags_which_columns_look_numeric():
    result = custom.preview_csv(_static_csv())
    by_name = {c["name"]: c for c in result["columns"]}
    assert set(by_name) == {"tract", "greenness", "heat"}
    assert by_name["greenness"]["numeric"] is True
    assert by_name["tract"]["numeric"] is True   # a geoid is digits; the UI names it
    assert by_name["greenness"]["range"] == [0.41, 0.55]
    assert by_name["heat"]["range"] == [86.0, 88.2]
    assert result["row_count"] == 2
    assert result["sample_rows"][0]["greenness"] == "0.41"


def test_preview_range_is_numeric_not_lexical():
    """A string sort would put "10" before "9"."""
    result = custom.preview_csv(_csv(["tract,v", f"{TRACT_A},9", f"{TRACT_B},10"]))
    col = next(c for c in result["columns"] if c["name"] == "v")
    assert col["range"] == [9.0, 10.0]


def test_preview_text_column_has_no_range():
    result = custom.preview_csv(_csv(["tract,label", f"{TRACT_A},high", f"{TRACT_B},low"]))
    col = next(c for c in result["columns"] if c["name"] == "label")
    assert col["numeric"] is False and "range" not in col


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------

def test_units_are_transliterated_for_the_catalog():
    assert custom.clean_unit("  µg/m³ ") == "ug/m3"
    assert custom.clean_unit("°F") == "degF"
    assert custom.clean_unit("☃") == "unitless"        # nothing printable left
    assert custom.unit_as_typed("  µg/m³ ") == "µg/m³"


def test_distinct_units_join_in_column_order():
    assert custom.summarise_units(["a", "b", "c"], {"c": "F", "a": "index", "b": "index"}) \
        == "index / F"
    assert custom.summarise_units(["a"], {}) == "unitless"


@pytest.mark.xfail(strict=True, reason=(
    "parity with the web: a >50-char summary ends in U+2026, which the catalog's "
    "printable-ASCII display_unit pattern rejects; fix after the web cutover"))
def test_a_long_unit_summary_stays_printable_ascii():
    cols = [f"c{i}" for i in range(8)]
    summary = custom.summarise_units(cols, {c: f"unit{c}xx" for c in cols})
    assert len(summary) <= 50 and re.fullmatch(r"[\x20-\x7e]+", summary)
