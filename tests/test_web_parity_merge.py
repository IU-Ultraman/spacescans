"""spacescans.orchestrate.merge reproduces the web's write_partial and fan_in.

Expected partials, result.csv files and low-match warnings come from the
web's own _merge.py (and faqsd.merge_results for the in-place rename) run
on synthetic C4 outputs with the real column names and dtypes; see
fixtures/web_parity/generate.py. CSV outputs are compared byte for byte.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow
import pyarrow.parquet as pq
import pytest

from spacescans.orchestrate import merge

FIX = Path(__file__).parent / "fixtures" / "web_parity"
MANIFEST = json.loads((FIX / "manifest.json").read_text())
SAME_ENV = (pd.__version__, pyarrow.__version__) == (MANIFEST["pandas"],
                                                     MANIFEST["pyarrow"])
CATALOG = {**json.loads((FIX / "variable_metadata.json").read_text())["variables"],
           **json.loads((FIX / "overlay.json").read_text())}
VALUE_COLS = {k: v["value_cols"] for k, v in CATALOG.items()}

MERGE_CASES = sorted(p.name for p in (FIX / "merge").iterdir() if p.is_dir())
FAN_IN_CASES = sorted(p.name for p in (FIX / "fan_in").iterdir() if p.is_dir())


def _copy_case(kind: str, name: str, tmp_path: Path) -> Path:
    dst = tmp_path / name
    shutil.copytree(FIX / kind / name, dst)
    return dst


@pytest.mark.parametrize("name", MERGE_CASES)
def test_partial_matches_web(name, tmp_path):
    run_dir = _copy_case("merge", name, tmp_path)
    spec = json.loads((run_dir / "case.json").read_text())
    expected_dir = FIX / "expected" / "merge" / name
    want = json.loads((expected_dir / "result.json").read_text())

    call = {"run_dir": run_dir, "experiment": spec["experiment"],
            "variables": spec["variables"], "parquet_map": spec["parquet_map"],
            "value_cols": VALUE_COLS, "renames": spec["renames"] or None,
            "rewrite_in_place": spec["rewrite_in_place"]}
    if "error" in want:
        with pytest.raises(Exception) as e:
            merge.partial_from_dir(**call)
        assert type(e.value).__name__ == want["error"]["type"]
        assert str(e.value) == want["error"]["message"]
        return

    merged, stats = merge.partial_from_dir(**call)
    out = run_dir / "output" / merge.partial_csv_name(spec["experiment"])
    out.parent.mkdir(exist_ok=True)       # the web's write_partial does this
    merged.to_csv(out, index=False)
    assert out.name == want["partial"]
    assert out.read_bytes() == (expected_dir / want["partial"]).read_bytes()

    got_warnings = ([{"level": "warning", **stats.as_log_fields()}]
                    if stats.low else [])
    assert got_warnings == want["warnings"]

    for fname, sha in want.get("rewritten", {}).items():
        got_t = pq.read_table(run_dir / "output" / fname)
        assert got_t.equals(pq.read_table(expected_dir / fname))
        if SAME_ENV:
            got_sha = hashlib.sha256((run_dir / "output" / fname).read_bytes()).hexdigest()
            assert got_sha == sha


@pytest.mark.parametrize("name", [n for n in MERGE_CASES if n != "numeric_pid"])
def test_merge_partial_on_frames_equals_the_dir_helper(name, tmp_path):
    """The pure function gives the same table without touching any file."""
    case = FIX / "merge" / name
    spec = json.loads((case / "case.json").read_text())
    keys = merge.episode_keys(pd.read_csv(case / "input.csv", dtype=str))
    outputs = [(v, pd.read_parquet(case / "output" / spec["parquet_map"][v]))
               for v in spec["variables"]]
    before = [df.copy() for _, df in outputs]
    merged, stats = merge.merge_partial(keys, outputs, VALUE_COLS,
                                        experiment=spec["experiment"],
                                        renames=spec["renames"] or None)
    via_dir, stats_dir = merge.partial_from_dir(
        _copy_case("merge", name, tmp_path), spec["experiment"], spec["variables"],
        spec["parquet_map"], VALUE_COLS, renames=spec["renames"] or None,
        rewrite_in_place=spec["rewrite_in_place"])
    pd.testing.assert_frame_equal(merged, via_dir)
    assert stats == stats_dir
    for (_, df), b in zip(outputs, before):
        pd.testing.assert_frame_equal(df, b)      # inputs untouched


@pytest.mark.parametrize("name", FAN_IN_CASES)
def test_fan_in_matches_web(name, tmp_path):
    run_dir = _copy_case("fan_in", name, tmp_path)
    spec = json.loads((run_dir / "case.json").read_text())
    expected_dir = FIX / "expected" / "fan_in" / name
    want = json.loads((expected_dir / "result.json").read_text())
    if "error" in want:
        with pytest.raises(Exception) as e:
            merge.fan_in_from_dir(run_dir, spec["experiments"])
        assert type(e.value).__name__ == want["error"]["type"]
        return
    df = merge.fan_in_from_dir(run_dir, spec["experiments"])
    out = run_dir / "output" / "result.csv"
    out.parent.mkdir(exist_ok=True)       # git keeps no empty output/ folder
    df.to_csv(out, index=False)
    assert out.read_bytes() == (expected_dir / "result.csv").read_bytes()


def test_fan_in_collision_suffixes_are_the_webs():
    header = (FIX / "expected" / "fan_in" / "input_collision" / "result.csv"
              ).read_text(encoding="utf-8").splitlines()[0].split(",")
    # input.csv already has `ndi`, so bg_ndi_wi's ndi is suffixed.
    assert "ndi" in header and "ndi_bg_ndi_wi_dup" in header
    twice = (FIX / "expected" / "fan_in" / "same_twice" / "result.csv"
             ).read_text().splitlines()[0].split(",")
    assert [c for c in twice if c.endswith("_noise2_dup")] == [
        "l50dba_exi_noise2_dup", "l50dba_imp_noise2_dup", "l50dba_nat_noise2_dup"]


# ---------------------------------------------------------------------------
# Semantics worth pinning beyond the fixtures
# ---------------------------------------------------------------------------


def _keys(n: int) -> pd.DataFrame:
    return merge.episode_keys(pd.DataFrame({"pid": [f"P{i}" for i in range(n)]}))


def _c4(pids, eids, **cols) -> pd.DataFrame:
    return pd.DataFrame({"PATID": pids, "geoid": eids, **cols})


def test_episode_keys_use_row_numbers_and_patid_alias():
    k = merge.episode_keys(pd.DataFrame({"PATID": ["b", "a"], "episode_id": [7, 7]}))
    assert k.columns.tolist() == ["pid", "episode_id"]
    assert k["episode_id"].tolist() == [0, 1]
    assert k["pid"].tolist() == ["b", "a"]


def test_single_variable_keeps_c4_row_order_and_absent_rows_stay_absent():
    keys = _keys(4)
    c4 = _c4(["P3", "P0"], [3, 0], v=[3.0, 0.0])
    merged, stats = merge.merge_partial(keys, [("x", c4)], {"x": ["v"]},
                                        experiment="e")
    assert merged["episode_id"].tolist() == [3, 0]
    assert stats.matched_n == 2 and stats.match_pct == 50.0 and stats.low


def test_first_variable_wins_on_a_repeated_column():
    keys = _keys(2)
    a = _c4(["P0", "P1"], [0, 1], v=[1.0, 2.0])
    b = _c4(["P0", "P1"], [0, 1], v=[9.0, 9.0], w=[5.0, 6.0])
    merged, _ = merge.merge_partial(keys, [("a", a), ("b", b)],
                                    {"a": ["v"], "b": ["v", "w"]}, experiment="e")
    assert merged.columns.tolist() == ["pid", "episode_id", "v", "w"]
    assert merged["v"].tolist() == [1.0, 2.0]


def test_all_missing_values_do_not_count_as_matched():
    keys = _keys(2)
    c4 = _c4(["P0", "P1"], [0, 1], v=[np.nan, 1.0])
    _, stats = merge.merge_partial(keys, [("x", c4)], {"x": ["v"]}, experiment="e")
    assert stats.matched_n == 1 and stats.match_pct == 50.0


def test_no_variables_means_keys_only_and_full_match():
    keys = _keys(3)
    merged, stats = merge.merge_partial(keys, [], {}, experiment="e")
    pd.testing.assert_frame_equal(merged, keys)
    assert stats.match_pct == 100.0 and not stats.low


def test_unknown_variable_raises_keyerror():
    with pytest.raises(KeyError):
        merge.merge_partial(_keys(1), [("nope", _c4(["P0"], [0], v=[1.0]))], {},
                            experiment="e")


def test_typed_fan_in_keeps_episode_id_and_honours_reserved_names():
    """The R path: typed tables, the user's other columns held back."""
    base = pd.DataFrame({"pid": ["P0", "P1", "P2"],
                         "startDate": pd.to_datetime(["2015-01-01"] * 3)})
    part_a = pd.DataFrame({"pid": ["P1", "P0"], "episode_id": [1, 0],
                           "age": [1.0, 2.0], "ndi": [0.5, -0.5]})
    part_b = pd.DataFrame({"pid": ["P0"], "episode_id": [0], "ndi": [9.0]})
    out = merge.fan_in(base, [("bg", part_a), ("custom", part_b)],
                       reserved=["age"], drop_episode_id=False)
    assert out.columns.tolist() == ["pid", "startDate", "episode_id", "age_bg_dup",
                                    "ndi", "ndi_custom_dup"]
    assert out["episode_id"].tolist() == [0, 1, 2]
    assert out["age_bg_dup"].tolist()[:2] == [2.0, 1.0]
    assert np.isnan(out["ndi_custom_dup"].iloc[1])
    assert out["startDate"].dtype == base["startDate"].dtype


def test_fan_in_without_reserved_names_is_the_web_rule():
    base = pd.DataFrame({"pid": ["P0"]})
    part = pd.DataFrame({"pid": ["P0"], "episode_id": [0], "age": [1.0]})
    out = merge.fan_in(base, [("bg", part)])
    assert out.columns.tolist() == ["pid", "age"]


def test_reserved_rename_that_would_duplicate_is_an_error():
    base = pd.DataFrame({"pid": ["P0"], "age_bg_dup": [1]})
    part = pd.DataFrame({"pid": ["P0"], "episode_id": [0], "age": [1.0]})
    with pytest.raises(ValueError, match="duplicate"):
        merge.fan_in(base, [("bg", part)], reserved=["age"])


def test_numeric_pids_merge_once_ids_are_text_on_both_sides():
    """The web's numeric-pid failure (fixture numeric_pid) disappears when
    the cohort's pid is text, as with to_pipeline_parquet(pid_as_str=True)."""
    keys = merge.episode_keys(pd.DataFrame({"pid": ["00042", "1001"]}))
    c4 = _c4(["1001", "00042"], [1, 0], v=[1.0, 2.0])
    merged, stats = merge.merge_partial(keys, [("x", c4)], {"x": ["v"]},
                                        experiment="e")
    assert stats.match_pct == 100.0
    assert merged.set_index("episode_id").loc[0, "pid"] == "00042"
