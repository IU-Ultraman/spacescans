"""The feature dictionary rows and CSV are the web's, byte for byte."""
from __future__ import annotations

import pytest

from spacescans import catalog


def test_feature_rows_equal_the_web_rows_for(feature_record):
    assert catalog.feature_rows(feature_record["columns"]) == feature_record["rows"]


def test_feature_csv_is_byte_identical(feature_record, tmp_path):
    path = catalog.write_feature_csv(feature_record["columns"], tmp_path / "sub" / "fd.csv")
    assert path.read_bytes() == feature_record["csv"].encode("utf-8")


def test_features_cover_every_catalog_value_column():
    feats = catalog.features()
    assert len(feats) == 58
    for var in catalog.variables().values():
        for col in var.value_cols:
            assert col in feats, f"{var.key}.{col} has no dictionary entry"
    f = feats["faqsd_pm25"]
    assert f.name == "faqsd_pm25" and f.label and f.definition


def test_feature_rows_accept_any_iterable():
    assert catalog.feature_rows(iter(["ndi"])) == catalog.feature_rows(["ndi"])
    assert catalog.feature_rows(["not_a_feature"]) == []


@pytest.mark.parametrize("header", [catalog.FEATURE_FIELDNAMES])
def test_header_names(header):
    assert header == ("feature_name", "short_description", "detailed_description")
