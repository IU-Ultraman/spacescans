"""An exclusive, advisory lock on a data directory's downloads.

``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows, on
``<data_dir>/.spacescans/lock``. The OS releases it when the holder exits,
even after a crash or SIGKILL, so there are no stale locks to clean up. It
only serialises fetches; pipeline runs never need it because every file is
moved into place atomically.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from spacescans.data.errors import LockedError
from spacescans.data.paths import state_dir


class DataLock:
    """Context manager: ``with DataLock(root): ...``. Never blocks."""

    def __init__(self, root: Path, *, purpose: str = "fetch") -> None:
        self.root = Path(root)
        self.purpose = purpose
        self.path = state_dir(self.root) / "lock"
        self.info_path = state_dir(self.root) / "lock.json"
        self._fh = None

    def acquire(self) -> DataLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            _try_lock(fh)
        except OSError:
            fh.close()
            holder = self._holder()
            who = (f" (pid {holder.get('pid')} on {holder.get('host')}, "
                   f"{holder.get('purpose')} since {holder.get('since')})") if holder else ""
            raise LockedError(
                f"another spacescans process is downloading into {self.root}{who}",
                hint="wait for it to finish, then re-run; partial downloads resume",
                details={"lock": str(self.path), "holder": holder},
            ) from None
        self._fh = fh
        info = {"pid": os.getpid(), "host": socket.gethostname(), "purpose": self.purpose,
                "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        try:
            self.info_path.write_text(json.dumps(info), encoding="utf-8")
        except OSError:  # pragma: no cover - informational only
            pass
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self.info_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def _holder(self) -> dict | None:
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def __enter__(self) -> DataLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
