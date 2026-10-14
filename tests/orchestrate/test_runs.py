"""orchestrate.runs: run directories under the per-user root, owner records and the sweep.

Phase-C decision 2: a ``link`` without ``run_dir`` creates its directory under
``tools::R_user_dir("spacescans", "cache")/runs`` (as R computes it) with an
``owner.json`` (pid, start time, host), and a sweep removes the directories whose
owner process no longer exists: a dead pid, or a pid that now belongs to a process
started at another time. A live owner is kept, and so is anything the sweep cannot
judge (another host, no record). The conftest points ``R_USER_CACHE_DIR`` at a temp
dir, so nothing here touches the real user cache.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from spacescans.orchestrate import runs


def _dead_process() -> tuple[int, float | None]:
    """The pid of a process that has exited (and its start time while it lived)."""
    p = subprocess.Popen([sys.executable, "-c", "import os, time; print(os.getpid()); time.sleep(0.4)"],
                         stdout=subprocess.PIPE, text=True)
    start = runs.process_start_time(p.pid)
    p.wait(timeout=30)
    return p.pid, start


def _run_dir(root: Path, name: str, owner: dict | None, *, payload: bytes = b"x" * 1000) -> Path:
    d = runs.runs_root(root) / name
    d.mkdir(parents=True)
    (d / "input.parquet").write_bytes(payload)
    if owner is not None:
        (d / runs.OWNER_FILE).write_text(json.dumps(owner))
    return d


def _owner(**over) -> dict:
    base = {"schema": 1, "pid": os.getpid(), "start_time": runs.process_start_time(os.getpid()),
            "host": runs.host_name(), "client": "test", "created_at": "2026-10-13T00:00:00+00:00"}
    base.update(over)
    return base


# ---------------------------------------------------------------------------------------------
# the root
# ---------------------------------------------------------------------------------------------

def test_user_cache_root_follows_r_user_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("R_USER_CACHE_DIR", str(tmp_path / "rcache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert runs.user_cache_root() == tmp_path / "rcache" / "R" / "spacescans"
    monkeypatch.delenv("R_USER_CACHE_DIR")
    assert runs.user_cache_root() == tmp_path / "xdg" / "R" / "spacescans"
    monkeypatch.delenv("XDG_CACHE_HOME")
    default = runs.user_cache_root()
    if sys.platform == "darwin":
        assert default == Path.home() / "Library" / "Caches" / "org.R-project.R" / "R" / "spacescans"
    elif sys.platform.startswith("linux"):
        assert default == Path.home() / ".cache" / "R" / "spacescans"
    assert runs.runs_root(tmp_path / "x") == tmp_path / "x" / "runs"
    assert runs.runs_root() == default / "runs"


# ---------------------------------------------------------------------------------------------
# liveness
# ---------------------------------------------------------------------------------------------

def test_process_start_time_and_pid_exists():
    me = os.getpid()
    assert runs.pid_exists(me) and runs.pid_exists(1)
    t = runs.process_start_time(me)
    assert t is not None and 0 < t <= __import__("time").time()
    dead, start_while_alive = _dead_process()
    assert start_while_alive is not None
    assert not runs.pid_exists(dead)
    assert runs.process_start_time(dead) is None
    assert runs.process_start_time(0) is None and not runs.pid_exists(0)


def test_owner_alive_rules():
    assert runs.owner_alive(_owner()) is True
    # the same pid started at another time is another process (pid reuse)
    assert runs.owner_alive(_owner(start_time=_owner()["start_time"] - 3600)) is False
    # a start time the record could not read is accepted
    assert runs.owner_alive(_owner(start_time=None)) is True
    dead, start = _dead_process()
    assert runs.owner_alive(_owner(pid=dead, start_time=start)) is False
    # another host, or no usable pid: nothing can be said
    assert runs.owner_alive(_owner(host="some-other-host.example")) is None
    assert runs.owner_alive(_owner(pid=None)) is None
    assert runs.owner_alive(_owner(pid="12")) is None
    assert runs.owner_alive(_owner(pid=True)) is None
    assert runs.owner_alive({}) is None


# ---------------------------------------------------------------------------------------------
# records and directories
# ---------------------------------------------------------------------------------------------

def test_create_run_dir_is_private_and_owned(tmp_path):
    d = runs.create_run_dir(tmp_path / "root", prefix="link-", client="pytest")
    assert d.parent == tmp_path / "root" / "runs" and d.name.startswith("link-")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(d.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "root").stat().st_mode) == 0o700
    owner = runs.read_owner(d)
    assert owner["schema"] == runs.OWNER_SCHEMA and owner["pid"] == os.getpid()
    assert owner["host"] == runs.host_name() and owner["client"] == "pytest"
    assert abs(owner["start_time"] - runs.process_start_time(os.getpid())) < 1e-6
    assert stat.S_IMODE((d / runs.OWNER_FILE).stat().st_mode) == 0o600
    assert set(owner) == {"schema", "pid", "start_time", "host", "client", "created_at"}
    # the record names a process, never cohort values
    assert runs.read_owner(tmp_path) is None
    (tmp_path / runs.OWNER_FILE).write_text("[1, 2]")
    assert runs.read_owner(tmp_path) is None


def test_an_existing_root_is_made_private(tmp_path):
    base = runs.runs_root(tmp_path / "root")
    base.mkdir(parents=True)
    os.chmod(base, 0o755)
    d = runs.create_run_dir(tmp_path / "root")
    assert stat.S_IMODE(base.stat().st_mode) == 0o700 and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_create_run_dir_can_name_another_owner(tmp_path):
    """The bridge records its client: the client reads the results after the bridge exits."""
    dead, _start = _dead_process()
    d = runs.create_run_dir(tmp_path / "root", pid=dead, client="bridge for R", sweep_first=False)
    assert runs.read_owner(d)["pid"] == dead
    rep = runs.sweep(tmp_path / "root")
    assert rep.removed == [d.name] and not d.exists()


# ---------------------------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------------------------

def test_sweep_removes_dead_owners_keeps_live_ones_and_counts(tmp_path):
    root = tmp_path / "root"
    dead, dead_start = _dead_process()
    gone = _run_dir(root, "link-gone", _owner(pid=dead, start_time=dead_start), payload=b"y" * 2048)
    reused = _run_dir(root, "link-reused", _owner(start_time=_owner()["start_time"] - 7200))
    live = _run_dir(root, "link-live", _owner())
    foreign = _run_dir(root, "link-foreign", _owner(pid=dead, host="elsewhere.example"))
    bare = _run_dir(root, "link-bare", None)
    (runs.runs_root(root) / "not-a-dir.txt").write_text("x")
    other = tmp_path / "elsewhere"
    other.mkdir()
    (runs.runs_root(root) / "link-symlink").symlink_to(other, target_is_directory=True)

    rep = runs.sweep(root, dry_run=True)
    assert rep.removed == ["link-gone", "link-reused"] and rep.dry_run
    assert gone.exists() and reused.exists()

    rep = runs.sweep(root)
    assert rep.removed == ["link-gone", "link-reused"]
    assert rep.kept == ["link-live"]
    assert rep.unknown == ["link-bare", "link-foreign"]
    assert rep.bytes == 2048 + len(json.dumps(_owner(pid=dead, start_time=dead_start))) \
        + 1000 + len(json.dumps(_owner(start_time=_owner()["start_time"] - 7200)))
    assert rep.errors == []
    assert not gone.exists() and not reused.exists()
    assert live.exists() and foreign.exists() and bare.exists() and other.exists()
    d = rep.to_dict()
    assert d["kept"] == 1 and d["removed"] == ["link-gone", "link-reused"] and d["root"] == str(runs.runs_root(root))
    assert "removed 2 run directories" in rep.message() and str(runs.runs_root(root)) in rep.message()
    # nothing in the report but directory names and counts
    assert "input.parquet" not in json.dumps(d)
    # a second sweep finds nothing to do and says nothing
    again = runs.sweep(root)
    assert again.removed == [] and again.message() is None and again.kept == ["link-live"]


def test_sweep_of_a_missing_root_creates_nothing(tmp_path):
    rep = runs.sweep(tmp_path / "never")
    assert rep.removed == [] and rep.kept == [] and rep.unknown == []
    assert not (tmp_path / "never").exists()


def test_sweep_leaves_unreadable_records_alone(tmp_path):
    root = tmp_path / "root"
    d = _run_dir(root, "link-broken", None)
    (d / runs.OWNER_FILE).write_text("{not json")
    rep = runs.sweep(root)
    assert rep.unknown == ["link-broken"] and d.exists()


# ---------------------------------------------------------------------------------------------
# link without run_dir
# ---------------------------------------------------------------------------------------------

def test_link_without_run_dir_uses_the_per_user_root_and_sweeps_first(tmp_path, monkeypatch):
    from spacescans.orchestrate import link as link_fn

    link_mod = sys.modules["spacescans.orchestrate.link"]
    monkeypatch.setattr(link_mod, "_check_data", lambda groups, overlay, data_dir: (
        {"data_dir": str(data_dir), "data_release": "test", "variables": {}, "datasets": {}}, []))
    monkeypatch.setenv("R_USER_CACHE_DIR", str(tmp_path / "ucache"))
    root = tmp_path / "ucache" / "R" / "spacescans"
    dead, dead_start = _dead_process()
    stale = _run_dir(root, "link-stale", _owner(pid=dead, start_time=dead_start))

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from test_link_unit import COHORT, FakeExecutor
    finally:
        sys.path.pop(0)
    res = link_fn(COHORT, ["noise"], data_dir=tmp_path / "data", executor=FakeExecutor())
    try:
        assert res.run_dir.parent == root / "runs" and res.run_dir.name.startswith("link-")
        assert stat.S_IMODE(res.run_dir.stat().st_mode) == 0o700
        owner = runs.read_owner(res.run_dir)
        assert owner["pid"] == os.getpid() and owner["client"].endswith(" link")
        assert not stale.exists()                      # swept before the run
        assert pd.read_parquet(res.run_dir / "input.parquet").shape[0] == len(COHORT)
    finally:
        import shutil
        shutil.rmtree(res.run_dir, ignore_errors=True)
    # a caller-given run_dir is used as given and gets no owner record
    given = tmp_path / "mine"
    res2 = link_fn(COHORT, ["noise"], data_dir=tmp_path / "data", run_dir=given, executor=FakeExecutor())
    assert res2.run_dir == given and runs.read_owner(given) is None


def test_orchestrate_exports_the_runs_api():
    import spacescans.orchestrate as o

    assert o.sweep_run_dirs is runs.sweep and o.create_run_dir is runs.create_run_dir
    assert o.runs_root is runs.runs_root and o.user_cache_root is runs.user_cache_root
    assert o.SweepReport is runs.SweepReport


@pytest.mark.skipif(sys.platform != "darwin", reason="the sysctl read is macOS only")
def test_darwin_start_time_agrees_with_ps():
    me = os.getpid()
    via_sysctl = runs._start_time_darwin(me)
    via_ps = runs._start_time_ps(me)
    assert via_sysctl is not None and via_ps is not None
    assert abs(via_sysctl - via_ps) < 1.5          # ps prints whole seconds
