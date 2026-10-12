"""An optional run lock: one run at a time per directory.

The web runners hold ``flock(DATA_DIR/.run_lock, LOCK_EX | LOCK_NB)`` for a whole
task (``app/experiments/bg_ndi_wi.py:416-430``) and the task manager probes it
before spawning (``app/task_manager.py:480-498``). :class:`RunLock` is that lock
as a context manager, for any directory: the web adapter can keep using
``DATA_DIR/.run_lock``; other callers choose their own directory (never a
read-only data root). The OS drops the lock when the holder exits, even after
SIGKILL, so there is nothing stale to clean up.

``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. Stdlib only.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Union

__all__ = ["RUN_LOCK_BUSY", "RUN_LOCK_FILE", "RunLock", "RunLockBusy"]

PathLike = Union[str, "os.PathLike[str]"]

#: The lock file's name, as in the web (``DATA_DIR / ".run_lock"``).
RUN_LOCK_FILE = ".run_lock"
#: The web's message when another task holds the lock (status.json; the paper's
#: ``independent_impl_chain.sh`` greps for "run lock").
RUN_LOCK_BUSY = "another task acquired the run lock first; retry shortly"


class RunLockBusy(RuntimeError):
    """Another process holds the run lock. Bridge error kind ``locked``."""

    kind = "locked"

    def __init__(self, path: Path, holder: dict[str, Any] | None = None,
                 message: str = RUN_LOCK_BUSY):
        super().__init__(message)
        self.message = message
        self.path = Path(path)
        self.holder = holder
        self.hint = "wait for the other run to finish, then retry"
        self.details: dict[str, Any] = {"lock": str(self.path), "holder": holder}

    def to_dict(self) -> dict[str, Any]:
        """The bridge's error shape, like ``spacescans.data.errors.DataError.to_dict``."""
        return {"kind": self.kind, "message": self.message, "hint": self.hint,
                "details": self.details, "type": type(self).__name__}


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class RunLock:
    """Exclusive advisory lock on ``directory/name`` (default ``.run_lock``).

    ``with RunLock(d): ...`` takes it without waiting and raises :class:`RunLockBusy`
    (message :data:`RUN_LOCK_BUSY`) when another process holds it. ``acquire(timeout_s=)``
    can wait instead: None waits for ever, a number waits that many seconds.
    ``is_free()`` is the task manager's probe-and-release check.

    The directory is created if needed (``mkdir -p``). Like the web, the lock file
    is left in place and nothing is written into it. With ``record_holder=True``
    the holder's pid, host and start time go to a sibling ``<name>.holder.json``
    (removed on release), so a busy error can say who holds the lock.

    One lock per instance and process; it is not reentrant. Locking the same file
    through a second instance in the same process fails like another process would.
    """

    def __init__(self, directory: PathLike, *, name: str = RUN_LOCK_FILE,
                 record_holder: bool = False):
        if not name or os.sep in name or (os.altsep and os.altsep in name):
            raise ValueError(f"lock name must be a plain file name, got {name!r}")
        self.directory = Path(directory)
        self.path = self.directory / name
        self.holder_path = self.directory / f"{name}.holder.json"
        self.record_holder = record_holder
        self._fd: int | None = None

    def __repr__(self) -> str:
        return f"RunLock({str(self.path)!r}, held={self.held})"

    @property
    def held(self) -> bool:
        """True while this instance holds the lock."""
        return self._fd is not None

    def _open(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)

    def _holder(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.holder_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def acquire(self, *, timeout_s: float | None = 0.0, poll_s: float = 0.1) -> "RunLock":
        """Take the lock. ``timeout_s`` 0 (default) never waits, None waits for ever."""
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this RunLock")
        deadline = None if timeout_s is None else time.monotonic() + max(timeout_s, 0.0)
        fd = self._open()
        try:
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:  # BlockingIOError on POSIX, PermissionError on Windows
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RunLockBusy(self.path, self._holder()) from None
                    time.sleep(poll_s)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        if self.record_holder:
            info = {"pid": os.getpid(), "host": socket.gethostname(),
                    "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            try:
                self.holder_path.write_text(json.dumps(info), encoding="utf-8")
            except OSError:  # informational only
                pass
        return self

    def release(self) -> None:
        """Drop the lock (no-op when not held)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self.record_holder:
            try:
                self.holder_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def is_free(self) -> bool:
        """True when nobody holds the lock right now (takes and drops it at once).

        Like the web's ``_run_lock_free``, this only answers "is a run going on?";
        the answer can change straight after.
        """
        if self._fd is not None:
            return False
        fd = self._open()
        try:
            try:
                _try_lock(fd)
            except OSError:
                return False
            _unlock(fd)
            return True
        finally:
            os.close(fd)

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - safety net
        try:
            self.release()
        except Exception:
            pass
