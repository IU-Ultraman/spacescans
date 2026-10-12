"""orchestrate.locks.RunLock: the web's DATA_DIR/.run_lock as a context manager."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from spacescans.orchestrate.locks import RUN_LOCK_BUSY, RUN_LOCK_FILE, RunLock, RunLockBusy

pytestmark = pytest.mark.skipif(os.name != "posix", reason="flock semantics are POSIX")

SRC = Path(__file__).resolve().parents[2] / "src"


def _python(code: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *args],
                          capture_output=True, text=True, env=env, timeout=60, **kwargs)


_TRY_ACQUIRE = """
    import sys
    from spacescans.orchestrate.locks import RunLock, RunLockBusy
    try:
        RunLock(sys.argv[1]).acquire()
    except RunLockBusy as exc:
        print("busy", exc.kind, exc)
    else:
        print("acquired")
"""

# app/task_manager.py:_run_lock_free (web 3991c3b), verbatim apart from the path.
_WEB_PROBE = """
    import fcntl, os, sys
    from pathlib import Path
    lock_path = Path(sys.argv[1]) / ".run_lock"
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("busy")
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            print("free")
    finally:
        os.close(fd)
"""


def test_the_lock_is_exclusive_across_processes_and_shared_with_the_web_probe(tmp_path):
    lock = RunLock(tmp_path / "state")
    assert lock.path == tmp_path / "state" / RUN_LOCK_FILE == tmp_path / "state" / ".run_lock"
    with lock:
        assert lock.held and lock.path.is_file()
        out = _python(_TRY_ACQUIRE, str(tmp_path / "state")).stdout.strip()
        assert out == f"busy locked {RUN_LOCK_BUSY}"
        assert _python(_WEB_PROBE, str(tmp_path / "state")).stdout.strip() == "busy"
    assert not lock.held
    assert _python(_WEB_PROBE, str(tmp_path / "state")).stdout.strip() == "free"
    assert _python(_TRY_ACQUIRE, str(tmp_path / "state")).stdout.strip() == "acquired"
    assert lock.path.read_bytes() == b""     # like the web: the file stays, nothing in it


def test_a_lock_held_by_the_web_runner_code_blocks_runlock(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import fcntl, os, sys, time
            from pathlib import Path
            lock_path = Path(sys.argv[1]) / ".run_lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.touch()
            lock_fd = os.open(str(lock_path), os.O_RDWR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("held", flush=True)
            time.sleep(60)
        """), str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        lock = RunLock(tmp_path)
        assert not lock.is_free()
        with pytest.raises(RunLockBusy) as info:
            lock.acquire()
        assert str(info.value) == RUN_LOCK_BUSY and "run lock" in str(info.value)
        holder.send_signal(signal.SIGKILL)     # the OS drops a dead holder's lock
        holder.wait(10)
        assert lock.is_free()
        with lock:
            assert lock.held
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_a_second_instance_in_the_same_process_is_busy(tmp_path):
    with RunLock(tmp_path):
        other = RunLock(tmp_path)
        assert not other.is_free()
        with pytest.raises(RunLockBusy):
            other.acquire()
    assert RunLock(tmp_path).is_free()


def test_acquire_can_wait_for_the_holder(tmp_path):
    first = RunLock(tmp_path).acquire()
    threading.Timer(0.3, first.release).start()
    t0 = time.monotonic()
    second = RunLock(tmp_path).acquire(timeout_s=5)
    assert 0.2 <= time.monotonic() - t0 < 3
    second.release()


def test_a_timeout_raises_busy_after_waiting(tmp_path):
    with RunLock(tmp_path):
        t0 = time.monotonic()
        with pytest.raises(RunLockBusy):
            RunLock(tmp_path).acquire(timeout_s=0.3, poll_s=0.05)
        assert time.monotonic() - t0 >= 0.3


def test_holder_record_is_optional_and_reported(tmp_path):
    lock = RunLock(tmp_path, record_holder=True).acquire()
    holder = json.loads(lock.holder_path.read_text())
    assert holder["pid"] == os.getpid() and holder["host"] and holder["since"]
    with pytest.raises(RunLockBusy) as info:
        RunLock(tmp_path).acquire()
    err = info.value.to_dict()
    assert err == {"kind": "locked", "message": RUN_LOCK_BUSY,
                   "hint": "wait for the other run to finish, then retry",
                   "details": {"lock": str(tmp_path / ".run_lock"), "holder": holder},
                   "type": "RunLockBusy"}
    lock.release()
    assert not lock.holder_path.exists()
    with RunLock(tmp_path):                  # the default writes no holder file
        assert not lock.holder_path.exists()


def test_misuse_is_refused(tmp_path):
    with pytest.raises(ValueError):
        RunLock(tmp_path, name="a/b")
    lock = RunLock(tmp_path).acquire()
    with pytest.raises(RuntimeError, match="already held"):
        lock.acquire()
    lock.release()
    lock.release()                           # releasing twice is a no-op
