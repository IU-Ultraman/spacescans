"""SubprocessExecutor and execute_step against a fake pipeline CLI (fake_pipeline_cli.py).

Covers what the web's run_pipeline_step does (spawn line, every output line under the
step's source, exit-code line, its level), and what tier 2 adds: the step's whole
process group is stopped on cancel or timeout (SIGTERM, SIGKILL after 5 s), peak RSS
from wait4, SPACESCANS_DATA_DIR set to the data dir (#11), no process outliving run().
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spacescans.orchestrate.events import Event, LogWriter, tee
from spacescans.orchestrate.execute import (
    DRAIN_S,
    KILL_GRACE_S,
    TAIL_LINES,
    Executor,
    SubprocessExecutor,
    command_line,
    default_prefix,
    execute_step,
    make_executor,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and wait4 are POSIX")

FAKE = Path(__file__).with_name("fake_pipeline_cli.py")
STEP = "c3_fake"


def fake(**kwargs) -> SubprocessExecutor:
    return SubprocessExecutor(cmd_prefix=[sys.executable, str(FAKE)], **kwargs)


def spec_file(tmp_path: Path, **spec) -> Path:
    path = tmp_path / "pipeline_configs" / f"{STEP}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec))
    return path


@pytest.fixture
def dirs(tmp_path):
    run_dir, data_dir = tmp_path / "run", tmp_path / "data"
    run_dir.mkdir()
    data_dir.mkdir()
    return run_dir, data_dir


class Recorder:
    def __init__(self):
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def lines(self) -> list[str]:
        return [e.text for e in self.events if e.kind == "line"]

    def value(self, prefix: str) -> int:
        """The integer after ``prefix`` in the first output line that starts with it."""
        for line in self.lines():
            if line.startswith(prefix + " "):
                return int(line.split()[1])
        raise AssertionError(f"no {prefix!r} line in {self.lines()}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS: a group of zombies awaiting their reaper
        return True
    return True


def gone(pid: int, *, group: bool = False, within: float = 3.0) -> bool:
    check = group_alive if group else alive
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not check(pid):
            return True
        time.sleep(0.05)
    return not check(pid)


def logs(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "logs.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------------------
# command and environment
# ---------------------------------------------------------------------------

def test_default_command_is_this_interpreter_running_the_package():
    assert default_prefix() == (sys.executable, "-I", "-u", "-m", "spacescans")
    ex = SubprocessExecutor()
    assert ex.command(Path("/r/pipeline_configs/c3_bg.yaml"), Path("/data")) == [
        sys.executable, "-I", "-u", "-m", "spacescans",
        "run", "--data-dir", "/data", "/r/pipeline_configs/c3_bg.yaml"]
    assert isinstance(ex, Executor)
    assert command_line(ex, Path("/r/c.yaml"), Path("/d")) == (
        f"{sys.executable} -I -u -m spacescans run --data-dir /d /r/c.yaml")


def test_executor_arguments_are_checked():
    with pytest.raises(ValueError):
        SubprocessExecutor(cmd_prefix=[])
    with pytest.raises(ValueError):
        SubprocessExecutor(timeout_s=0)
    assert isinstance(make_executor("subprocess"), SubprocessExecutor)
    with pytest.raises(ValueError):
        make_executor("threads")
    ex = fake()
    assert make_executor(ex) is ex


def test_child_gets_data_dir_variable_cwd_env_changes_and_its_own_group(dirs, monkeypatch):
    run_dir, data_dir = dirs
    monkeypatch.setenv("SPACESCANS_DATA_DIR", "/somewhere/else")   # the caller's value loses (#11)
    monkeypatch.setenv("SS_EXEC_TEST_DROP", "inherited")
    monkeypatch.setenv("SS_EXEC_TEST_KEEP", "inherited")
    ex = fake(env={"SS_EXEC_TEST_ADD": "added", "SS_EXEC_TEST_DROP": None})
    cfg = spec_file(run_dir, report=True,
                    report_env=["SS_EXEC_TEST_ADD", "SS_EXEC_TEST_DROP", "SS_EXEC_TEST_KEEP"])
    rec = Recorder()
    run = execute_step(ex, cfg, experiment="noise", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=rec)
    assert run.rc == 0, run.tail
    lines = rec.lines()
    assert f"env SPACESCANS_DATA_DIR={data_dir}" in lines
    assert f"data-dir {data_dir}" in lines
    assert "env SS_EXEC_TEST_ADD=added" in lines
    assert "env SS_EXEC_TEST_DROP=<unset>" in lines
    assert "env SS_EXEC_TEST_KEEP=inherited" in lines
    assert Path(rec.lines()[2].split(" ", 1)[1]).resolve() == run_dir.resolve()   # "cwd ..."
    pid, pgid = rec.value("pid"), rec.value("pgid")
    assert pid == pgid != os.getpgid(0)   # a new session: the step leads its own group


# ---------------------------------------------------------------------------
# output, progress and the web's log lines
# ---------------------------------------------------------------------------

def test_lines_progress_and_exit_are_logged_with_the_web_strings(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, lines=[
        "[overlap_fast] reading 51 shapefiles",
        "[overlap_fast] tile 5/10 ( 50.0%) elapsed= 0.01m",
        "",
        "   indented summary line",
        "[nhd_proximity] tile 1/4 ( 25.0%)",
        "[overlap]   10/10 (100.0%)",
        "Output: /tmp/x/c3_fake.parquet",
    ], stderr="a warning on stderr")
    ex = fake()
    rec = Recorder()
    run = execute_step(ex, cfg, experiment="noise", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=tee(LogWriter.for_run_dir(run_dir), rec), index=0, total=2)
    assert run.rc == 0 and run.ok and not run.cancelled and not run.timed_out
    assert run.seconds > 0 and run.maxrss_bytes and run.maxrss_bytes > 1_000_000
    assert rec.kinds() == ["spawn", "line", "line", "progress", "line", "line", "progress",
                           "line", "progress", "line", "line", "step_end"]
    cmd = f"{sys.executable} {FAKE} run --data-dir {data_dir} {cfg}"
    spawn, end = rec.events[0], rec.events[-1]
    assert spawn.text == f"spawning {STEP}: {cmd}" and spawn.data == {"cmd": cmd}
    progress = [e for e in rec.events if e.kind == "progress"]
    assert [e.frac for e in progress] == [0.5, 0.25, 1.0]
    assert progress[0].text == "[overlap_fast] tile 5/10 ( 50.0%) elapsed= 0.01m"
    assert progress[0].overall == 0.25
    assert end.text == f"step {STEP} exit code 0"
    assert end.data["rc"] == 0 and end.data["maxrss_bytes"] == run.maxrss_bytes
    assert end.data["cancelled"] is False and end.data["timed_out"] is False
    assert {(e.experiment, e.step, e.index, e.total) for e in rec.events} == {("noise", STEP, 0, 2)}

    records = logs(run_dir)
    assert all(list(r) == ["ts", "level", "source", "msg"] for r in records)
    assert all(datetime.fromisoformat(r["ts"]).tzinfo == timezone.utc for r in records)
    assert [(r["level"], r["source"], r["msg"]) for r in records] == [
        ("info", "runner", f"spawning {STEP}: {cmd}"),
        ("info", STEP, "[overlap_fast] reading 51 shapefiles"),
        ("info", STEP, "[overlap_fast] tile 5/10 ( 50.0%) elapsed= 0.01m"),
        ("info", STEP, "   indented summary line"),
        ("info", STEP, "[nhd_proximity] tile 1/4 ( 25.0%)"),
        ("info", STEP, "[overlap]   10/10 (100.0%)"),
        ("info", STEP, "Output: /tmp/x/c3_fake.parquet"),
        ("info", STEP, "a warning on stderr"),
        ("info", "runner", f"step {STEP} exit code 0"),
    ]


def test_carriage_returns_split_lines_like_the_web_text_mode(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, raw="a\rb\r\n\nc")
    rec = Recorder()
    execute_step(fake(), cfg, experiment="x", step=STEP, data_dir=data_dir, cwd=run_dir,
                 on_event=rec)
    assert rec.lines() == ["a", "b", "c"]


def test_a_failed_step_logs_error_and_keeps_a_bounded_tail(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, lines=[f"line {i}" for i in range(80)], rc=3)
    run = execute_step(fake(), cfg, experiment="x", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=LogWriter.for_run_dir(run_dir))
    assert run.rc == 3 and not run.ok
    assert run.tail == [f"line {i}" for i in range(80 - TAIL_LINES, 80)]
    last = logs(run_dir)[-1]
    assert (last["level"], last["source"], last["msg"]) == ("error", "runner",
                                                            f"step {STEP} exit code 3")


def test_a_step_that_cannot_start_is_rc_127_or_126_not_an_exception(dirs, tmp_path):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir)
    seen: list[str] = []
    ex = SubprocessExecutor(cmd_prefix=[str(tmp_path / "no-such-python")])
    run = ex.run(cfg, data_dir=data_dir, cwd=run_dir, on_line=seen.append, cancel=None)
    assert run.rc == 127 and run.maxrss_bytes is None
    assert run.tail == seen and seen[0].startswith(f"could not start {tmp_path / 'no-such-python'}")
    not_exec = tmp_path / "not-executable"
    not_exec.write_text("#!/bin/sh\n")
    not_exec.chmod(0o644)
    run = SubprocessExecutor(cmd_prefix=[str(not_exec)]).run(
        cfg, data_dir=data_dir, cwd=run_dir, on_line=lambda _l: None, cancel=None)
    assert run.rc == 126


def test_maxrss_comes_from_wait4_for_the_step_alone(dirs):
    run_dir, data_dir = dirs
    small = fake().run(spec_file(run_dir, lines=["x"]), data_dir=data_dir, cwd=run_dir,
                       on_line=lambda _l: None, cancel=None)
    big = fake().run(spec_file(run_dir, alloc_mb=256), data_dir=data_dir, cwd=run_dir,
                     on_line=lambda _l: None, cancel=None)
    assert small.rc == big.rc == 0
    assert 1_000_000 < small.maxrss_bytes < 200 * 2**20
    assert big.maxrss_bytes >= 256 * 2**20
    assert big.maxrss_bytes - small.maxrss_bytes >= 200 * 2**20


# ---------------------------------------------------------------------------
# cancel, timeout, and nothing left behind
# ---------------------------------------------------------------------------

def _cancel_when_started(cancel: threading.Event, marks: dict) -> Recorder:
    """A recorder that sets ``cancel`` once the step has printed its pid and grandchild."""
    rec = Recorder()

    def on_event(ev: Event) -> None:
        rec(ev)
        if ev.kind == "line" and ev.text.startswith("grandchild ") and not cancel.is_set():
            marks["pid"], marks["grandchild"] = rec.value("pid"), rec.value("grandchild")
            marks["t"] = time.monotonic()
            cancel.set()
    rec.on_event = on_event
    return rec


@pytest.mark.slow
def test_cancel_kills_a_term_ignoring_step_and_its_group_within_6_s(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, report=True, ignore_term=True, grandchild=True, sleep=120)
    cancel, marks = threading.Event(), {}
    rec = _cancel_when_started(cancel, marks)
    run = execute_step(fake(), cfg, experiment="x", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=tee(rec.on_event, LogWriter.for_run_dir(run_dir)), cancel=cancel)
    elapsed = time.monotonic() - marks["t"]
    assert run.cancelled and not run.timed_out
    assert run.rc == -signal.SIGKILL                    # SIGTERM ignored: SIGKILL after grace
    assert KILL_GRACE_S - 0.25 <= elapsed < 6.0, elapsed
    assert gone(marks["grandchild"]) and gone(marks["pid"])
    assert gone(marks["pid"], group=True)               # the whole process group
    assert rec.events[-1].data["cancelled"] is True
    last = logs(run_dir)[-1]
    assert (last["level"], last["msg"]) == ("error", f"step {STEP} exit code -9")


def test_cancel_stops_a_cooperative_step_and_its_group_at_once(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, report=True, grandchild=True, sleep=120)
    cancel, marks = threading.Event(), {}
    rec = _cancel_when_started(cancel, marks)
    run = execute_step(fake(), cfg, experiment="x", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=rec.on_event, cancel=cancel)
    elapsed = time.monotonic() - marks["t"]
    assert run.cancelled and run.rc == -signal.SIGTERM
    assert elapsed < 2.0, elapsed
    assert gone(marks["grandchild"]) and gone(marks["pid"], group=True)


def test_timeout_stops_the_step_group(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, report=True, grandchild=True, sleep=120)
    rec = Recorder()
    run = execute_step(fake(timeout_s=1.0), cfg, experiment="x", step=STEP, data_dir=data_dir,
                       cwd=run_dir, on_event=rec)
    assert run.timed_out and not run.cancelled
    assert run.rc == -signal.SIGTERM
    assert 1.0 <= run.seconds < 3.0, run.seconds
    assert gone(rec.value("grandchild")) and gone(rec.value("pid"), group=True)


def test_cancel_set_before_the_start_starts_nothing(dirs):
    run_dir, data_dir = dirs
    cancel = threading.Event()
    cancel.set()
    seen: list[str] = []
    run = fake().run(spec_file(run_dir, report=True), data_dir=data_dir, cwd=run_dir,
                     on_line=seen.append, cancel=cancel)
    assert run.cancelled and run.rc == -signal.SIGTERM and seen == [] and run.seconds == 0.0


def test_an_on_line_exception_kills_the_group_before_it_propagates(dirs):
    run_dir, data_dir = dirs
    cfg = spec_file(run_dir, report=True, ignore_term=True, grandchild=True, sleep=120)
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line)
        if line.startswith("grandchild "):
            raise RuntimeError("consumer failed")

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="consumer failed"):
        fake().run(cfg, data_dir=data_dir, cwd=run_dir, on_line=on_line, cancel=None)
    assert time.monotonic() - t0 < 5.0
    pid = int(next(s for s in seen if s.startswith("pid ")).split()[1])
    grandchild = int(seen[-1].split()[1])
    assert gone(grandchild) and gone(pid, group=True)


def test_processes_a_finished_step_leaves_behind_are_killed(dirs):
    run_dir, data_dir = dirs
    # The step exits 0 but its grandchild keeps the output pipe open.
    cfg = spec_file(run_dir, report=True, grandchild=True, lines=["done"])
    rec = Recorder()
    t0 = time.monotonic()
    run = execute_step(fake(), cfg, experiment="x", step=STEP, data_dir=data_dir, cwd=run_dir,
                       on_event=rec)
    assert run.rc == 0 and not run.cancelled
    assert DRAIN_S - 0.25 <= time.monotonic() - t0 < DRAIN_S + 3.0
    assert gone(rec.value("grandchild")) and gone(rec.value("pid"), group=True)
