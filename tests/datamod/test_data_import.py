"""import_files(): hand-downloaded provider files go where the readers look."""
from __future__ import annotations

import gzip

import pytest

from spacescans.cli import main
from spacescans.data import UsageError, import_files

VNL13 = "VNL_v21_npp_2013_global_vcmcfg_c202205302300.average_masked.dat.tif"
VNL14 = "VNL_v21_npp_2014_global_vcmslcfg_c202205302300.average_masked.dat.tif"


def test_vnl_tif_gz_is_unpacked_into_place(tmp_path, data_dir):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / (VNL13 + ".gz")).write_bytes(gzip.compress(b"tif-2013" * 100))
    (dl / VNL14).write_bytes(b"tif-2014")
    (dl / "VNL_v22_something_else.tif").write_bytes(b"x")
    rep = import_files("vnl", sorted(dl.iterdir()), data_dir=data_dir)
    res = {i.source.rsplit("/", 1)[-1]: (i.result, i.target) for i in rep.items}
    assert res[VNL13 + ".gz"] == ("placed", f"VNL/C3/{VNL13}")
    assert res[VNL14] == ("placed", f"VNL/C3/{VNL14}")
    assert res["VNL_v22_something_else.tif"][0] == "rejected"
    assert (data_dir / "VNL/C3" / VNL13).read_bytes() == b"tif-2013" * 100
    assert not list(data_dir.rglob("*.part")) and not rep.ok
    d = rep.status.dataset("vnl")
    assert d.status == "incomplete" and d.missing[0] == "VNL/C3: no file for year 2015"
    again = import_files("vnl", [dl / VNL14], data_dir=data_dir)
    assert again.items[0].result == "skipped_same_size"


def test_temis_daily_files_land_in_their_variable_and_year_folders(tmp_path, data_dir):
    f = tmp_path / "uvief20160101.hdf"
    f.write_bytes(bytes.fromhex("0e031301"))
    g = tmp_path / "uvdec20161231.hdf"
    g.write_bytes(bytes.fromhex("0e031301"))
    rep = import_files("temis_uv_raw", [f, g], data_dir=data_dir, move=True)
    assert [i.target for i in rep.items] == ["TEMIS/C4/raw/uvief/2016/uvief20160101.hdf",
                                            "TEMIS/C4/raw/uvdec/2016/uvdec20161231.hdf"]
    assert not f.exists() and (data_dir / "TEMIS/C4/raw/uvief/2016/uvief20160101.hdf").is_file()


def test_faqsd_txt_gz(tmp_path, data_dir):
    f = tmp_path / "2016_pm25_daily_average.txt.gz"
    f.write_bytes(gzip.compress(b"Date,Loc_Label1,Latitude,Longitude,Prediction,SEpred\n"))
    rep = import_files("faqsd", [f], data_dir=data_dir)
    assert rep.items[0].target == "FAQSD/C4/2016_pm25_daily_average.txt"
    assert (data_dir / rep.items[0].target).read_bytes().startswith(b"Date,")


def test_refusals(tmp_path, data_dir):
    with pytest.raises(UsageError, match="project mirror"):
        import_files("noise_rasters", [tmp_path / "x.tif"], data_dir=data_dir)
    with pytest.raises(UsageError, match="place them by hand"):
        import_files("acag", [tmp_path / "x.nc"], data_dir=data_dir)
    bad = tmp_path / (VNL13 + ".gz")
    bad.write_bytes(b"not gzip")
    from spacescans.data import DataCorruptError
    with pytest.raises(DataCorruptError, match="could not unpack"):
        import_files("vnl", [bad], data_dir=data_dir)
    assert not (data_dir / "VNL/C3" / VNL13).exists()


def test_cli_import(tmp_path, data_dir, capsys):
    f = tmp_path / VNL14
    f.write_bytes(b"t")
    rc = main(["data", "import", "vnl", str(f), "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and f"-> VNL/C3/{VNL14}: placed" in out and "vnl: incomplete" in out
