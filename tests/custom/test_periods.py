"""Periods of dated files: years and dates from names, manifests, biweekly ends."""
from __future__ import annotations

from datetime import date

import pytest

from spacescans.custom import DatasetError
from spacescans.custom.periods import (
    Period,
    assign_periods,
    dates_in_name,
    parse_date,
    read_manifest,
    split_manifest,
    years_in_name,
)


@pytest.mark.parametrize("name,years", [
    ("ndvi_2016.tif", [2016]),
    ("ndvi_2016_v2.tif", [2016]),
    ("2016.tif", [2016]),
    ("pm_20160101.tif", []),          # inside an 8-digit date
    ("data_2015_2016.tif", [2015, 2016]),
    ("x_1899.tif", []),
    ("x_v12345.tif", []),
])
def test_years_in_names(name, years):
    assert years_in_name(name) == years


@pytest.mark.parametrize("name,dates", [
    ("pm_20160101.tif", [date(2016, 1, 1)]),
    ("pm_2016-01-16.tif", [date(2016, 1, 16)]),
    ("pm_2016_02_01.tif", [date(2016, 2, 1)]),
    ("pm_2016.02.29.tif", [date(2016, 2, 29)]),
    ("pm_20160101_20160115.tif", [date(2016, 1, 1), date(2016, 1, 15)]),
    ("pm_20161301.tif", []),                         # no month 13
    ("MOD13Q1.A2016017.h10v05.tif", [date(2016, 1, 17)]),
    ("MOD13Q1.A2016366.tif", [date(2016, 12, 31)]),
    ("MOD13Q1.A2015366.tif", []),                    # 2015 has 365 days
    ("pm_2016-0101.tif", []),                         # mixed separators
])
def test_dates_in_names(name, dates):
    assert dates_in_name(name) == dates


def test_parse_date_forms():
    assert parse_date("2016-01-02") == parse_date("2016/01/02") == parse_date("20160102") \
        == parse_date(date(2016, 1, 2)) == date(2016, 1, 2)
    assert parse_date("02/01/2016") is None and parse_date("1850-01-01") is None
    assert parse_date(20160102) is None


def test_yearly_periods_from_names_mapping_and_manifest():
    assert assign_periods([("a_2016.tif", None), ("a_2015.tif", None)], "yearly") == [
        Period(year=2016), Period(year=2015)]
    assert assign_periods([("a.tif", 2016), ("b.tif", "2017")], "yearly") == [
        Period(year=2016), Period(year=2017)]
    with pytest.raises(DatasetError, match="has no year in its name"):
        assign_periods([("a.tif", None)], "yearly")
    with pytest.raises(DatasetError, match="more than one year"):
        assign_periods([("a_2015_2016.tif", None)], "yearly")
    with pytest.raises(DatasetError, match="two files carry the year 2016"):
        assign_periods([("a_2016.tif", None), ("b_2016.tif", None)], "yearly")
    manifest = {"a.tif": {"year": 2014}, "b.tif": {"year": 2015}}
    assert assign_periods([("a.tif", None), ("b.tif", None)], "yearly", manifest) == [
        Period(year=2014), Period(year=2015)]
    with pytest.raises(DatasetError, match="not in the manifest"):
        assign_periods([("c.tif", None), ("d.tif", None)], "yearly", {"d.tif": {"year": 2014}})
    with pytest.raises(DatasetError, match="lists file\\(s\\) not given: b.tif"):
        assign_periods([("a.tif", None)], "yearly", manifest)


def test_daily_periods_are_single_days():
    out = assign_periods([("t_20160102.tif", None), ("t_20160101.tif", None)], "daily")
    assert out == [Period(start=date(2016, 1, 2), end=date(2016, 1, 2)),
                   Period(start=date(2016, 1, 1), end=date(2016, 1, 1))]
    with pytest.raises(DatasetError, match="two files start on 2016-01-01"):
        assign_periods([("a_20160101.tif", None), ("b_2016-01-01.tif", None)], "daily")
    with pytest.raises(DatasetError, match="more than one date"):
        assign_periods([("t_20160101_20160102.tif", None)], "daily")


def _ends(names):
    return [(p.start.isoformat(), p.end.isoformat())
            for p in assign_periods([(n, None) for n in names], "biweekly")]


def test_biweekly_semi_monthly_periods_end_on_the_15th_and_month_end():
    assert _ends(["x_20160101.tif", "x_20160116.tif", "x_20160201.tif", "x_20160216.tif"]) == [
        ("2016-01-01", "2016-01-15"), ("2016-01-16", "2016-01-31"),
        ("2016-02-01", "2016-02-15"), ("2016-02-16", "2016-02-29")]


def test_biweekly_14_day_periods_and_gaps():
    assert _ends(["x_20160101.tif", "x_20160115.tif", "x_20160129.tif"]) == [
        ("2016-01-01", "2016-01-14"), ("2016-01-15", "2016-01-28"),
        ("2016-01-29", "2016-02-11")]
    # a missing file leaves its days uncovered instead of stretching the previous period
    assert _ends(["x_20160101.tif", "x_20160301.tif"]) == [
        ("2016-01-01", "2016-01-16"), ("2016-03-01", "2016-03-15")]


def test_biweekly_start_and_end_in_the_name_and_from_a_manifest():
    assert _ends(["x_20160101_20160110.tif", "x_20160111_20160124.tif"]) == [
        ("2016-01-01", "2016-01-10"), ("2016-01-11", "2016-01-24")]
    manifest = {"a.tif": {"start": date(2016, 1, 1), "end": date(2016, 1, 31)},
                "b.tif": {"start": date(2016, 2, 1)}}
    out = assign_periods([("a.tif", None), ("b.tif", None)], "biweekly", manifest)
    assert [(p.start.day, p.end.isoformat()) for p in out] == [(1, "2016-01-31"),
                                                               (1, "2016-02-15")]
    with pytest.raises(DatasetError, match="overlap"):
        assign_periods([("a.tif", (date(2016, 1, 1), date(2016, 1, 20))),
                        ("b.tif", date(2016, 1, 15))], "biweekly")
    with pytest.raises(DatasetError, match="a biweekly period of 40 days"):
        assign_periods([("a.tif", (date(2016, 1, 1), date(2016, 2, 9)))], "biweekly")


def test_read_manifest(tmp_path):
    path = tmp_path / "m.csv"
    path.write_text("﻿File,Start,End\nsub/a.tif,2016-01-01,2016-01-15\nb.tif,2016/01/16,\n",
                    encoding="utf-8")
    assert read_manifest(path, "biweekly") == {
        "a.tif": {"start": date(2016, 1, 1), "end": date(2016, 1, 15)},
        "b.tif": {"start": date(2016, 1, 16)}}
    path.write_text("file,year\na.tif,2016\na.tif,2017\n")
    with pytest.raises(DatasetError, match="a.tif is listed twice"):
        read_manifest(path, "yearly")
    path.write_text("file,date,end\na.tif,2016-01-01,2016-01-02\n")
    with pytest.raises(DatasetError, match="only used by biweekly"):
        read_manifest(path, "daily")
    path.write_text("name,when\na.tif,2016\n")
    with pytest.raises(DatasetError, match="needs a 'year' column"):
        read_manifest(path, "yearly")
    path.write_text("file,year\na.tif,later\n")
    with pytest.raises(DatasetError, match="line 2 has no year"):
        read_manifest(path, "yearly")


def test_split_manifest(tmp_path):
    a, m = tmp_path / "a.tif", tmp_path / "m.csv"
    assert split_manifest([a, m], (".tif",)) == ([a], m)
    with pytest.raises(DatasetError, match="at most one manifest"):
        split_manifest([a, m, tmp_path / "n.csv"], (".tif",))
    with pytest.raises(DatasetError, match="expected .tif files"):
        split_manifest([tmp_path / "a.png"], (".tif",))
