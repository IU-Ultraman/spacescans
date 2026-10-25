"""InProcessExecutor: Pipeline.from_dict (U1) with DuckDB engine overrides (U4).

The geo test runs the bundled quickstart C3 step through both executors and requires
the same weights, which also checks ``${SPACESCANS_DATA_DIR}`` means the step's data
dir in both modes (#11) whatever the caller's environment says.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from importlib import resources
from pathlib import Path

import pytest
import yaml

from spacescans.orchestrate.events import Event
from spacescans.orchestrate.execute import (
    Executor,
    InProcessExecutor,
    SubprocessExecutor,
    execute_step,
    make_executor,
)


def _write_yaml(path: Path, cfg: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def _quickstart(tmp_path: Path, out: Path) -> tuple[Path, Path]:
    """The bundled quickstart C3 config, reading ``${SPACESCANS_DATA_DIR}``, and its data dir."""
    data = tmp_path / "sample"
    data.mkdir(parents=True, exist_ok=True)
    for entry in resources.files("spacescans.resources.data").iterdir():
        if entry.is_file() and not entry.name.startswith("__"):
            (data / entry.name).write_bytes(entry.read_bytes())
    text = (resources.files("spacescans.resources.configs.quickstart") / "end_to_end.yaml"
            ).read_text()
    cfg = yaml.safe_load(text.replace("${SPACESCANS_QUICKSTART_DATA_DIR}", "${SPACESCANS_DATA_DIR}"))
    cfg["output"]["path"] = str(out)
    return _write_yaml(tmp_path / "pipeline_configs" / "c3_quickstart.yaml", cfg), data


def test_prepare_fills_data_dir_and_engine_overrides(tmp_path):
    cfg = _write_yaml(tmp_path / "c3.yaml", {
        "name": "x", "linkage_pattern": "boundary_overlap_fast", "geometry_type": "polygon",
        "source": {"file": "${SPACESCANS_DATA_DIR}/BG/a.shp", "join_col": "GEOID10"},
        "buffer": {"patient_file": "/r/input.parquet", "buffer_m": 270},
        "engine": {"backend": "duckdb"},
        "output": {"path": "/r/output/c3.parquet", "format": "parquet"},
    })
    plain = InProcessExecutor().prepare(cfg, tmp_path / "data")
    assert plain["source"]["file"] == f"{tmp_path / 'data'}/BG/a.shp"
    assert plain["engine"] == {"backend": "duckdb"}         # None leaves the YAML as rendered
    raw = InProcessExecutor(threads=2, memory_limit="1GB", temp_directory=tmp_path / "spill",
                            workers=3).prepare(cfg, tmp_path / "data")
    assert raw["engine"] == {"backend": "duckdb", "threads": 2, "memory_limit": "1GB",
                             "temp_directory": str(tmp_path / "spill"), "workers": 3}
    assert isinstance(InProcessExecutor(), Executor)
    assert isinstance(make_executor("inprocess", threads=1), InProcessExecutor)
    assert make_executor("inprocess", workers=2).workers == 2
    with pytest.raises(ValueError):
        InProcessExecutor(threads=0)
    for bad in (0, 1.5, True):
        with pytest.raises(ValueError, match="workers"):
            InProcessExecutor(workers=bad)


class _FakePipe:
    """Stands in for a built Pipeline; ``behaviour`` decides what run() does."""

    def __init__(self, behaviour, raw):
        self.behaviour = behaviour
        self.raw = raw
        self.interrupted = threading.Event()
        pipe = self

        class _Conn:
            def interrupt(self):
                pipe.interrupted.set()

        self.engine = type("E", (), {"_conn": _Conn()})()

    def run(self):
        return self.behaviour(self)


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Patch Pipeline.from_dict; returns a dict to put the run() behaviour in."""
    from spacescans.pipeline import runner

    box: dict = {}

    def from_dict(cls, raw, *, data_dir=None, output_dir=None):
        box["raw"], box["data_dir"], box["output_dir"] = raw, data_dir, output_dir
        box["pipe"] = _FakePipe(box["behaviour"], raw)
        return box["pipe"]

    monkeypatch.setattr(runner.Pipeline, "from_dict", classmethod(from_dict))
    return box


def _minimal_config(tmp_path: Path) -> Path:
    return _write_yaml(tmp_path / "pipeline_configs" / "c3_x.yaml",
                       {"name": "x", "linkage_pattern": "grid_weights",
                        "output": {"path": "rel/c3_x.parquet"}})


def test_output_lines_progress_and_rc_like_the_cli(tmp_path, fake_pipeline):
    def behaviour(pipe):
        print("[overlap_fast] tile 1/2 ( 50.0%)")
        print("partial ", end="")
        print("line\r\nnext")
        return Path("/r/output/c3_x.parquet")

    fake_pipeline["behaviour"] = behaviour
    cfg = _minimal_config(tmp_path)
    events: list[Event] = []
    run = execute_step(InProcessExecutor(), cfg, experiment="x", step="c3_x",
                       data_dir=tmp_path / "data", cwd=tmp_path / "run", on_event=events.append)
    assert run.rc == 0 and run.ok and run.maxrss_bytes and run.maxrss_bytes > 0
    assert [e.kind for e in events] == ["spawn", "line", "progress", "line", "line", "line",
                                        "step_end"]
    assert events[0].text == (f"spawning c3_x: <in-process> run --data-dir {tmp_path / 'data'} "
                              f"{cfg}")
    assert [e.text for e in events if e.kind == "line"] == [
        "[overlap_fast] tile 1/2 ( 50.0%)", "partial line", "next",
        "Output: /r/output/c3_x.parquet"]
    assert events[-1].text == "step c3_x exit code 0"
    # A relative output path resolves under cwd/output, as `spacescans run` started in cwd would.
    assert fake_pipeline["output_dir"] == str(tmp_path / "run" / "output")
    assert fake_pipeline["data_dir"] == str(tmp_path / "data")


def test_an_exception_is_rc_1_with_the_cli_error_line(tmp_path, fake_pipeline):
    def behaviour(pipe):
        print("working")
        raise RuntimeError("boom")

    fake_pipeline["behaviour"] = behaviour
    seen: list[str] = []
    run = InProcessExecutor().run(_minimal_config(tmp_path), data_dir=tmp_path, cwd=tmp_path,
                                  on_line=seen.append, cancel=None)
    assert run.rc == 1 and seen == ["working", "Error: boom"] and run.tail == seen


def test_prints_from_other_threads_are_not_captured(tmp_path, fake_pipeline, capsys):
    def behaviour(pipe):
        for i in range(3):
            print(f"step line {i}")
        return Path("/r/out.parquet")

    fake_pipeline["behaviour"] = behaviour
    seen: list[str] = []

    def on_line(line: str) -> None:   # a consumer that echoes to stdout
        seen.append(line)
        print(f"echo: {line}")

    run = InProcessExecutor().run(_minimal_config(tmp_path), data_dir=tmp_path, cwd=tmp_path,
                                  on_line=on_line, cancel=None)
    assert run.rc == 0
    assert seen == ["step line 0", "step line 1", "step line 2", "Output: /r/out.parquet"]
    assert capsys.readouterr().out.count("echo: ") == 4


def test_cancel_interrupts_duckdb_and_returns_after_the_grace(tmp_path, fake_pipeline):
    def behaviour(pipe):
        print("started")
        pipe.interrupted.wait(30)   # a step that only stops when DuckDB is interrupted
        raise RuntimeError("INTERRUPT Error: Interrupted!")

    fake_pipeline["behaviour"] = behaviour
    cancel = threading.Event()
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line)
        cancel.set()

    run = InProcessExecutor(cancel_grace_s=5.0).run(
        _minimal_config(tmp_path), data_dir=tmp_path, cwd=tmp_path, on_line=on_line,
        cancel=cancel)
    assert fake_pipeline["pipe"].interrupted.is_set()
    assert run.cancelled and run.rc == 1                 # the step stopped itself in time
    assert seen == ["started", "Error: INTERRUPT Error: Interrupted!"]


def test_a_step_that_ignores_cancel_is_abandoned_after_the_grace(tmp_path, fake_pipeline):
    release = threading.Event()

    def behaviour(pipe):
        print("started")
        release.wait(30)            # deaf to the interrupt
        return Path("/r/out.parquet")

    fake_pipeline["behaviour"] = behaviour
    t0 = time.monotonic()
    run = InProcessExecutor(timeout_s=0.3, cancel_grace_s=0.5).run(
        _minimal_config(tmp_path), data_dir=tmp_path, cwd=tmp_path, on_line=lambda _l: None,
        cancel=None)
    elapsed = time.monotonic() - t0
    release.set()
    for worker in threading.enumerate():   # let the abandoned step finish inside this test
        if worker.name == "spacescans-inprocess-step":
            worker.join(5)
    assert run.timed_out and not run.cancelled and run.rc == -15
    assert elapsed < 3.0


@pytest.mark.geo
@pytest.mark.extras
def test_both_executors_give_the_same_weights_on_the_bundled_sample(tmp_path, monkeypatch):
    pytest.importorskip("geopandas")
    pytest.importorskip("exactextract")
    import pandas as pd

    # A caller environment pointing elsewhere must not change what either mode reads (#11).
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(tmp_path / "not-the-data"))
    results = {}
    for mode, executor in (("subprocess", SubprocessExecutor()),
                           ("inprocess", InProcessExecutor(threads=1, memory_limit="1GB"))):
        out = tmp_path / mode / "output" / "c3_quickstart.parquet"
        cfg, data = _quickstart(tmp_path / mode, out)
        events: list[Event] = []
        run = execute_step(executor, cfg, experiment="quickstart", step="c3_quickstart",
                           data_dir=data, cwd=tmp_path / mode, on_event=events.append)
        assert run.rc == 0, run.tail
        assert events[-2].text == f"Output: {out}"
        results[mode] = pd.read_parquet(out)
    a, b = results["subprocess"], results["inprocess"]
    assert len(a) > 0
    sort = list(a.columns)
    pd.testing.assert_frame_equal(a.sort_values(sort, ignore_index=True),
                                  b.sort_values(sort, ignore_index=True),
                                  check_exact=False, rtol=1e-12, atol=1e-12)


@pytest.mark.geo
@pytest.mark.extras
def test_engine_overrides_reach_duckdb(tmp_path, monkeypatch):
    pytest.importorskip("geopandas")
    from spacescans.pipeline import runner

    seen: dict = {}

    def run(self):
        seen.update(self.engine.settings())
        return Path("/r/out.parquet")

    monkeypatch.setattr(runner.Pipeline, "run", run)
    cfg, data = _quickstart(tmp_path, tmp_path / "out.parquet")
    result = InProcessExecutor(threads=1, memory_limit="1GB", temp_directory=tmp_path / "spill"
                               ).run(cfg, data_dir=data, cwd=tmp_path, on_line=lambda _l: None,
                                     cancel=None)
    assert result.rc == 0, result.tail
    assert seen["threads"] == "1"
    assert seen["temp_directory"] == str(tmp_path / "spill")
    assert seen["memory_limit"] not in ("", None)
