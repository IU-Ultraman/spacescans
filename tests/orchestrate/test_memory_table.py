"""The gate-M memory table and the out-of-memory hint that quotes it (contract 4.5).

``spacescans/orchestrate/memory_table.csv`` is written by ``tools/memory_gate.py report``;
``docs/memory-<date>.csv`` holds the full measurements it was derived from. A
``step_failed`` with exit code -9/137 says how much memory the source needed at the
nearest measured cohort size, both in ``StepFailed.hint`` and in the bridge's
classification of it.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from spacescans import catalog
from spacescans.orchestrate import StepFailed
from spacescans.orchestrate import memory as mem

REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "src" / "spacescans" / "orchestrate" / "memory_table.csv"


def test_the_packaged_table_loads_and_covers_every_catalog_source():
    rows = mem.table()
    assert rows, "memory_table.csv is empty or unreadable"
    experiments = {r.experiment for r in rows}
    expected = {catalog.variable(v).experiment for v in catalog.variables()}
    assert expected <= experiments, expected - experiments
    assert "custom" in experiments            # the two upload cases
    for r in rows:
        assert r.episodes > 0 and r.peak_gib > 0 and r.outcome in ("ok", "oom", "failed")
        assert r.container_gib > 0 and r.source and r.measured


def test_the_table_is_the_report_tool_csv_byte_for_byte():
    """What the hint reads is exactly what the tool wrote (no hand edits)."""
    with open(TABLE, newline="") as f:
        header = next(csv.reader(f))
    assert header == ["experiment", "case", "episodes", "peak_gib", "peak_step", "wall_s",
                      "outcome", "container_gib", "source", "measured"]
    with open(TABLE, newline="") as f:
        n = sum(1 for _ in csv.DictReader(f))
    assert n == len(mem.table())


@pytest.mark.parametrize("episodes,expected", [
    (None, 100000),        # unknown size: the largest measured
    (1001, 1001),
    (3000, 1001),          # log-nearest: 3,000 is nearer to 1,001 than to 10,002
    (3200, 10002),
    (50000, 100000),
    (10**7, 100000),
])
def test_nearest_measured_size_on_a_log_scale(episodes, expected):
    rows = mem.nearest("noise", episodes)
    assert rows and {r.episodes for r in rows} == {expected}


def test_nearest_is_empty_for_an_unmeasured_source():
    assert mem.nearest("not_a_source", 10) == ()
    assert mem.oom_note("not_a_source", 10) is None
    assert mem.oom_note(None, 10) is None


def test_oom_note_quotes_the_measured_need():
    note = mem.oom_note("acag", 100000)
    assert note is not None and "acag" in note and "100,000 episodes" in note and "GiB" in note
    note = mem.oom_note("noise", 25000, step="c4_noise")
    assert "noise" in note and "this cohort has 25,000 episodes" in note


def test_oom_note_names_the_container_when_the_measured_run_was_killed():
    killed = [r for r in mem.table() if r.outcome == "oom"]
    assert killed, "the table records the 100k FAQSD/TEMIS kills"
    r = killed[0]
    note = mem.oom_note(r.experiment, r.episodes)
    assert "needed more than" in note and f"{r.container_gib:.0f} GiB container" in note


def test_step_failed_hint_carries_the_note_only_when_out_of_memory():
    oom = StepFailed("c4_tract_faqsd", -9, ["TAIL-LINE"], experiment="faqsd", episodes=100000)
    assert oom.oom and "measured: faqsd" in oom.hint and "100,000 episodes" in oom.hint
    d = oom.to_dict()
    assert d["details"]["oom"] is True and d["details"]["episodes"] == 100000
    assert "TAIL-LINE" not in d["message"] and "TAIL-LINE" not in d["hint"]   # the tail stays a tail

    plain = StepFailed("c4_tract_faqsd", 3, ["x"], experiment="faqsd", episodes=100000)
    assert "measured" not in plain.hint and plain.to_dict()["details"]["oom"] is False
    timed = StepFailed("c4_tract_faqsd", -9, ["x"], experiment="faqsd", timed_out=True)
    assert "measured" not in timed.hint and not timed.oom
    unknown = StepFailed("c4_x", 137, [], experiment="not_a_source")
    assert unknown.oom and unknown.hint.endswith("more memory")   # no note, no dangling separator


def test_bridge_classification_keeps_the_note_and_the_size():
    from spacescans.bridge.errors import classify

    b = classify(StepFailed("c4_temis", 137, ["x"], experiment="temis", episodes=100000))
    assert b.kind == "step_failed" and b.details["oom"] is True
    assert b.details["episodes"] == 100000
    assert "measured: temis" in b.hint and "100,000 episodes" in b.hint
    # R's own step_failed hint replaces the Python one and quotes this field instead
    assert b.details["memory_note"] == mem.oom_note("temis", 100000, step="c4_temis")
    assert b.details["memory_note"] in b.hint
    b = classify(StepFailed("c4_temis", 2, ["x"], experiment="temis", episodes=100000))
    assert "measured" not in (b.hint or "")


def test_run_source_hands_the_cohort_size_to_a_failing_step(tmp_path):
    """The size comes from the input parquet's footer, never from reading the values."""
    import pandas as pd

    from spacescans.orchestrate import run as run_mod

    pd.DataFrame({"episode_id": range(7)}).to_parquet(tmp_path / "input.parquet", index=False)
    assert run_mod._episode_count(tmp_path / "input.parquet") == 7
    assert run_mod._episode_count(tmp_path / "missing.parquet") is None


def test_an_upload_quotes_the_measured_case_of_the_same_kind():
    """``custom`` has two measured cases: a grid step quotes the raster case, c3_bg the BG one."""
    rows = {r.case: r for r in mem.nearest("custom", 10002)}
    assert {"custom_raster", "custom_bg_csv"} <= set(rows)
    grid, bg = rows["custom_raster"], rows["custom_bg_csv"]
    note = mem.oom_note("custom", 10002, step="c3_customgrid_0badf00d")
    assert f"about {grid.peak_gib:.1f} GiB" in note and f"(peak in step {grid.peak_step})" in note
    note = mem.oom_note("custom", 10002, step="c3_bg")
    assert f"about {bg.peak_gib:.1f} GiB" in note and "(peak in step c3_bg)" in note


def test_the_note_never_breaks_the_error_path(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("broken table")

    monkeypatch.setattr(mem, "oom_note", boom)
    exc = StepFailed("c4_temis", -9, ["x"], experiment="temis", episodes=100000)
    assert exc.memory_note is None and exc.hint.endswith("more memory")
    from spacescans.bridge.errors import classify

    b = classify(exc)
    assert b.kind == "step_failed" and b.details["oom"] is True and "memory" in b.hint


class _KilledExecutor:
    """Every step is killed by the kernel (rc -9), as the out-of-memory killer does."""

    def command(self, config, data_dir):
        return ["killed", str(config)]

    def run(self, config, *, data_dir, cwd, on_line, cancel=None):
        from spacescans.orchestrate import StepRun

        return StepRun(rc=-9, seconds=0.01, maxrss_bytes=None, tail=["Killed"])


def test_link_warning_for_an_out_of_memory_source_carries_the_note(tmp_path, monkeypatch):
    """What R's ss_link prints for a failed source is the link warning's message."""
    import importlib

    import pandas as pd

    from spacescans.orchestrate import link

    link_mod = importlib.import_module("spacescans.orchestrate.link")
    monkeypatch.setattr(link_mod, "_check_data", lambda groups, overlay, data_dir: (
        {"data_dir": str(data_dir), "data_release": "test", "variables": {}, "datasets": {}}, []))
    cohort = pd.DataFrame({"pid": ["A1", "B2", "C3"],
                           "startDate": ["2014-01-01", "2015-03-01", "2013-06-01"],
                           "endDate": ["2015-12-31", "2019-12-31", "2017-01-01"],
                           "longitude": [-86.15, -87.62, -84.39],
                           "latitude": [39.77, 41.88, 33.75]})
    res = link(cohort, ["noise"], data_dir=tmp_path / "data", run_dir=tmp_path / "run",
               executor=_KilledExecutor())
    assert res.failed == ["noise"]
    err = res.errors[0]
    assert (err["kind"], err["rc"]) == ("step_failed", -9)
    expected = mem.oom_note("noise", 3, step=err["step"])
    assert expected and expected in err["hint"]
    w = next(w for w in res.warnings if w["code"] == "step_failed")
    assert w["message"].startswith(f"step {err['step']} failed: exit code -9")
    assert expected in w["message"] and "this cohort has 3 episodes" in w["message"]
