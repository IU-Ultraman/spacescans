"""The registered-dataset bridge commands, as the R package calls them (a subprocess, JSON)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BRIDGE = [sys.executable, "-I", "-u", "-X", "utf8", "-m", "spacescans.bridge"]
SECRET = "18097999917"


def call(tmp: Path, command: str, args: dict | None = None):
    """Run one bridge command; return (rc, result)."""
    req = tmp / f"{command}.request.json"
    res = tmp / f"{command}.result.json"
    if res.exists():
        res.unlink()
    req.write_text(json.dumps({"protocol": 1, "command": command, "verbose": False,
                               "client": {"name": "pytest", "version": "0",
                                          "protocol_range": [1, 1]},
                               "args": args or {}}))
    env = {k: v for k, v in os.environ.items() if k != "SPACESCANS_DATASETS_DIR"}
    p = subprocess.run(BRIDGE + [command, "--request", str(req), "--result", str(res)],
                       capture_output=True, text=True, cwd=str(tmp), timeout=180, env=env,
                       check=False)
    return p.returncode, json.loads(res.read_text()) if res.exists() else None


def test_register_list_get_update_and_remove_through_the_bridge(tmp_path):
    csv = tmp_path / "pm.csv"
    csv.write_text("GEOID,date,pm25\n18097000100,2016-01-01,1.5\n18097000100,2016-01-02,NA\n")
    root = tmp_path / "datasets"
    # jsonlite shapes: a one-element list as a scalar, NULL as {}
    rc, res = call(tmp_path, "datasets-register", {
        "files": str(csv), "name": "PM daily", "kind": "polygon_table", "temporal": "daily",
        "key": {"boundary": "tract", "column": "GEOID", "date_column": {}},
        "variables": {"name": "pm25", "unit": "ug/m3", "description": {}},
        "metadata": {"source": "test", "owner": {}}, "root": str(root), "overwrite": False})
    assert rc == 0 and res["ok"], res
    ds = res["value"]["dataset"]
    assert ds["name"] == "PM daily" and ds["temporal"] == "daily" and ds["owner"] is None
    assert ds["variables"] == [{"name": "pm25", "unit": "ug/m3", "description": None}]
    assert Path(ds["path"]).is_dir() and res["value"]["key"] == ds["variable_key"]

    rc, res = call(tmp_path, "datasets-list", {"root": str(root)})
    assert rc == 0 and [d["id"] for d in res["value"]["datasets"]] == [ds["id"]]
    assert res["value"]["root"] == str(root)

    rc, res = call(tmp_path, "datasets-get", {"name": "pm daily", "root": str(root)})
    assert rc == 0
    definition, key = res["value"]["definition"], res["value"]["key"]
    assert definition["variable_key"] == key and definition["date_col"] == "date"
    # the definition survives the trip back (what ss_link would pass to link)
    rc, res = call(tmp_path, "custom-validate", {"kind": "check", "definition": definition,
                                                 "key": key, "deep": True})
    assert rc == 0 and res["value"]["key"] == key

    rc, res = call(tmp_path, "datasets-update", {"name": ds["id"], "new_name": "PM 2.5 daily",
                                                 "metadata": {"visibility": "deployment"},
                                                 "root": str(root)})
    assert rc == 0 and res["value"]["dataset"]["name"] == "PM 2.5 daily"
    assert res["value"]["dataset"]["visibility"] == "deployment"

    rc, res = call(tmp_path, "datasets-remove", {"name": key, "root": str(root)})
    assert rc == 0 and res["value"]["removed"]["id"] == ds["id"]
    rc, res = call(tmp_path, "datasets-get", {"name": "PM 2.5 daily", "root": str(root)})
    assert rc != 0 and res["error"]["kind"] == "config"
    assert "no dataset named" in res["error"]["message"]


def test_a_refused_registration_is_a_config_error_without_data_values(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(f"GEOID,v\n{SECRET},1\n{SECRET},2\n")
    rc, res = call(tmp_path, "datasets-register", {
        "files": [str(csv)], "name": "bad", "kind": "polygon_table", "temporal": "static",
        "key": "tract", "root": str(tmp_path / "datasets")})
    assert rc != 0 and res["error"]["kind"] == "config"
    assert "duplicate rows" in res["error"]["message"]
    assert SECRET not in json.dumps(res)
    rc, res = call(tmp_path, "datasets-register", {
        "files": ["relative.csv"], "name": "x", "kind": "polygon_table", "temporal": "static"})
    assert rc != 0 and "absolute path" in res["error"]["message"]


@pytest.mark.geo
def test_raster_periods_can_be_given_parallel_to_the_files(tmp_path, write_tif):
    pytest.importorskip("rasterio")
    import numpy as np
    files = [write_tif(tmp_path / f"{n}.tif", np.ones((4, 4)), west=-86.5, north=40.0)
             for n in ("a", "b")]
    rc, res = call(tmp_path, "datasets-register", {
        "files": [str(f) for f in files], "periods": ["2016-01-01", "2016-01-15"],
        "name": "pm", "kind": "raster", "temporal": "biweekly", "variables": "pm",
        "root": str(tmp_path / "datasets")})
    assert rc == 0, res
    assert res["value"]["dataset"]["details"]["coverage_dates"] == ["2016-01-01", "2016-01-28"]
