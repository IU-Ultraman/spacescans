"""Run directories under one per-user root, with an owner record and a sweep.

A :func:`~spacescans.orchestrate.link.link` that is given no ``run_dir``
creates one under ``<user cache>/runs`` (phase-C decision 2, 2026-10-13),
where the user cache is the directory R's
``tools::R_user_dir("spacescans", "cache")`` names, so R and Python runs
share one root (:func:`user_cache_root`): ``$R_USER_CACHE_DIR``, else
``$XDG_CACHE_HOME``, else ``~/Library/Caches/org.R-project.R`` on macOS,
``%LOCALAPPDATA%/R/cache`` on Windows and ``~/.cache`` elsewhere, followed by
``R/spacescans``. The root and every run directory are created with mode 0700.

A run directory holds cohort data (``input.parquet``, the C3 and C4 outputs),
so one whose owner died (an R session killed with SIGKILL, a crashed Python
process, a reboot) must not stay behind: each directory gets an ``owner.json``
with the owning process's pid, start time and host, and :func:`sweep` deletes
(never archives) the directories whose owner no longer exists. R runs the same
sweep at package load, in ``ss_doctor()`` and ``ss_cache_clear()``; the bridge
runs it in ``cache-info``, ``cache-clear`` and ``runs-sweep``; ``link`` runs
it before creating its own directory.

Liveness (:func:`owner_alive`): the pid must exist and, when its start time
can be read, must have started within :data:`START_TIME_TOLERANCE_S` of the
recorded one, so a reused pid counts as a dead owner. A record from another
host, or without a usable pid, cannot be judged and is left alone (``unknown``);
so is a directory without a readable record. Nothing here reads a run
directory's contents, and the report names directories only.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "OWNER_FILE", "OWNER_SCHEMA", "RUNS_DIRNAME", "START_TIME_TOLERANCE_S", "SweepReport",
    "create_run_dir", "host_name", "owner_alive", "pid_exists", "process_start_time",
    "read_owner", "run_dir_or_temp", "runs_root", "sweep", "user_cache_root", "write_owner",
]

OWNER_FILE = "owner.json"
OWNER_SCHEMA = 1
RUNS_DIRNAME = "runs"
#: A process whose start time differs from the record by more than this is another process.
START_TIME_TOLERANCE_S = 2.0
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# the per-user root (R's tools::R_user_dir("spacescans", "cache"))
# ---------------------------------------------------------------------------

def user_cache_root() -> Path:
    """The directory ``tools::R_user_dir("spacescans", "cache")`` gives in R.

    ``$R_USER_CACHE_DIR``, else ``$XDG_CACHE_HOME``, else the platform's user
    cache (``~/Library/Caches/org.R-project.R``, ``%LOCALAPPDATA%/R/cache``,
    ``~/.cache``), then ``R/spacescans``. The path need not exist.
    """
    env = os.environ.get("R_USER_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
    if env:
        base = Path(env).expanduser()
    elif sys.platform == "win32":  # pragma: no cover - not a supported platform
        local = os.environ.get("LOCALAPPDATA")
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "R" / "cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "org.R-project.R"
    else:
        base = Path.home() / ".cache"
    return base / "R" / "spacescans"


def runs_root(root: str | os.PathLike | None = None) -> Path:
    """``<root>/runs``: where run directories without a caller-given path live.

    ``root`` defaults to :func:`user_cache_root`; R passes the same root.
    """
    base = Path(os.fspath(root)).expanduser() if root is not None else user_cache_root()
    return base / RUNS_DIRNAME


def host_name() -> str:
    """The machine's node name (``uname -n``), what R's ``Sys.info()[["nodename"]]`` gives."""
    try:
        return platform.node()
    except Exception:  # noqa: BLE001 - no name is better than a crash
        return ""


# ---------------------------------------------------------------------------
# process liveness
# ---------------------------------------------------------------------------

def pid_exists(pid: int) -> bool:
    """Whether a process with ``pid`` exists (another user's process counts as existing)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _boot_time_linux() -> float | None:
    """``btime`` of ``/proc/stat``: the wall-clock boot time, which moves when the clock is stepped."""
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as f:
            return float(next(int(line.split()[1]) for line in f if line.startswith("btime ")))
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _boot_id_linux() -> str | None:
    """The kernel's id of this boot (``/proc/sys/kernel/random/boot_id``)."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _start_time_linux(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            stat = f.read()
        # field 22 (starttime, clock ticks since boot) counted after the ")" that ends comm
        fields = stat[stat.rindex(")") + 2:].split()
        ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    btime = _boot_time_linux()
    return None if btime is None else btime + ticks / os.sysconf("SC_CLK_TCK")


def _start_time_darwin(pid: int) -> float | None:
    """``kinfo_proc.kp_proc.p_starttime`` through ``sysctl``, as the ps R package reads it."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        ctl_kern, kern_proc, kern_proc_pid = 1, 14, 1
        mib = (ctypes.c_int * 4)(ctl_kern, kern_proc, kern_proc_pid, int(pid))
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value < 16:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
            return None
        # struct kinfo_proc starts with struct extern_proc, whose first member is the
        # union holding struct timeval p_starttime {long tv_sec; int tv_usec}.
        tv_sec, tv_usec = struct.unpack_from("@qi", buf.raw, 0)
        if tv_sec <= 0:
            return None
        return tv_sec + tv_usec / 1e6
    except Exception:  # noqa: BLE001 - ctypes or layout surprises: fall back to ps
        return None


def _start_time_ps(pid: int) -> float | None:
    """``ps -o lstart= -p <pid>`` (one-second resolution), where nothing better exists."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=10, check=False, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def process_start_time(pid: int) -> float | None:
    """When process ``pid`` started, as Unix seconds; None when unknown or no such process."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        t = _start_time_linux(pid)
    elif sys.platform == "darwin":
        t = _start_time_darwin(pid)
    else:  # pragma: no cover - other platforms
        t = None
    return t if t is not None else _start_time_ps(pid)


def owner_alive(owner: Mapping[str, Any], *, host: str | None = None) -> bool | None:
    """Whether the process recorded in ``owner`` still runs.

    True: it exists and its start time matches the record (within
    :data:`START_TIME_TOLERANCE_S`) or cannot be read. False: no such pid, or
    the pid now belongs to a process started at another time. None: the
    record is from another host or has no usable pid, so nothing can be said.
    """
    recorded_host = owner.get("host")
    here = host_name() if host is None else host
    if recorded_host and here and str(recorded_host) != here:
        return None
    pid = owner.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not pid_exists(pid):
        return False
    recorded = owner.get("start_time")
    now = process_start_time(pid)
    if not (_number(recorded) and now is not None):
        return True
    # a pid started at another time than the record says is another process (pid reuse)
    if abs(now - float(recorded)) <= START_TIME_TOLERANCE_S:
        return True
    # Linux computes a start time as boot time + ticks, and its boot time moves with every
    # step of the wall clock: a record that carries its boot id and boot time is compared
    # by the time since boot instead (another boot means the recorded process is gone).
    rec_boot_id, rec_btime = owner.get("boot_id"), owner.get("boot_time")
    if _LINUX and isinstance(rec_boot_id, str) and _number(rec_btime):
        cur_btime = _boot_time_linux()
        if _boot_id_linux() == rec_boot_id and cur_btime is not None:
            return abs((now - cur_btime) - (float(recorded) - float(rec_btime))) \
                <= START_TIME_TOLERANCE_S
    return False


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---------------------------------------------------------------------------
# owner records and run directories
# ---------------------------------------------------------------------------

def write_owner(run_dir: str | os.PathLike, *, pid: int | None = None,
                client: str | None = None) -> dict[str, Any]:
    """Write ``run_dir/owner.json`` for the process ``pid`` (default: this one); return it.

    ``client`` names the program (``spacescans 0.3.0 link``, ``spacescans-r
    0.3.0``). The record holds the pid, its start time, the host and the time
    of writing (on Linux also the boot id and boot time, see
    :func:`owner_alive`): never a cohort value.
    """
    from spacescans import __version__

    pid = os.getpid() if pid is None else int(pid)
    record = {
        "schema": OWNER_SCHEMA,
        "pid": pid,
        "start_time": process_start_time(pid),
        "host": host_name(),
        "client": client or f"spacescans {__version__}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if _LINUX:   # see owner_alive: Linux start times move when the wall clock is stepped
        boot_id, btime = _boot_id_linux(), _boot_time_linux()
        if boot_id and btime is not None:
            record.update(boot_id=boot_id, boot_time=btime)
    path = Path(run_dir) / OWNER_FILE
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{OWNER_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
            f.write("\n")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return record


def read_owner(run_dir: str | os.PathLike) -> dict[str, Any] | None:
    """The ``owner.json`` of ``run_dir``, or None when absent or unreadable."""
    try:
        data = json.loads((Path(run_dir) / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _mkdir_private(path: Path) -> None:
    from spacescans.orchestrate.cache import _mkdir_private as mk

    mk(path)


def create_run_dir(root: str | os.PathLike | None = None, *, prefix: str = "link-",
                   pid: int | None = None, client: str | None = None,
                   sweep_first: bool = True) -> Path:
    """A new private run directory under :func:`runs_root` with its owner record.

    The root (and its parents) are created 0700 when missing; the directory
    itself is created by ``mkdtemp`` (0700, unique name ``<prefix><random>``).
    With ``sweep_first`` the root is swept of dead owners first (a failing
    sweep never prevents the run).
    """
    base = runs_root(root)
    _mkdir_private(base)
    with contextlib.suppress(OSError):   # an existing root is made private too, as R does
        os.chmod(base, _DIR_MODE)
    if sweep_first:
        with contextlib.suppress(Exception):
            sweep(root)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    try:
        os.chmod(path, _DIR_MODE)
        write_owner(path, pid=pid, client=client)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)   # a directory without a record is never swept
        raise
    return path


def run_dir_or_temp(*, prefix: str = "link-", pid: int | None = None,
                    client: str | None = None) -> tuple[Path, str | None]:
    """:func:`create_run_dir` under the per-user root, or a private temp directory.

    When the per-user root cannot be created (a read-only or missing home
    directory) the run still goes ahead, as R's ``ss_link()`` does, in a
    ``mkdtemp`` directory (0700) of the system temp dir, and the second value
    says so: no sweep will remove that directory. It is None otherwise.
    """
    try:
        return create_run_dir(prefix=prefix, pid=pid, client=client), None
    except (OSError, RuntimeError) as exc:
        path = Path(tempfile.mkdtemp(prefix=f"spacescans-{prefix}"))
        return path, (f"could not create the per-user run directory root "
                      f"({type(exc).__name__}); the run uses {path}, which no sweep removes: "
                      f"delete it when you no longer need it")


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

@dataclass
class SweepReport:
    """What :func:`sweep` found under ``root``: directory names, never contents.

    ``removed``: run directories whose owner is gone (deleted, or only listed
    with ``dry_run``); ``kept``: owners alive; ``unknown``: no record, another
    host or no usable pid (left alone); ``bytes``: what the removed ones held;
    ``errors``: directories that could not be judged or removed.
    """

    root: str
    dry_run: bool = False
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "dry_run": self.dry_run, "removed": list(self.removed),
                "kept": len(self.kept), "unknown": list(self.unknown), "bytes": self.bytes,
                "errors": list(self.errors)}

    def message(self) -> str | None:
        """One line about removed directories, or None when nothing was removed."""
        if not self.removed:
            return None
        n = len(self.removed)
        verb = "would remove" if self.dry_run else "removed"
        return (f"{verb} {n} run director{'y' if n == 1 else 'ies'} left by processes that no "
                f"longer exist under {self.root} ({self.bytes / 1e6:.1f} MB)")


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def sweep(root: str | os.PathLike | None = None, *, dry_run: bool = False) -> SweepReport:
    """Delete the run directories under :func:`runs_root` whose owner process is gone.

    Only immediate subdirectories (not symlinks) holding an ``owner.json`` are
    judged; the rest is left alone and listed as ``unknown``. A root that does
    not exist is an empty report; nothing is created.
    """
    base = runs_root(root)
    report = SweepReport(root=str(base), dry_run=dry_run)
    if not base.is_dir():
        return report
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError as exc:
        report.errors.append(f"{base}: {type(exc).__name__}")
        return report
    here = host_name()
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        owner = read_owner(path)
        if owner is None:
            report.unknown.append(entry.name)
            continue
        try:
            alive = owner_alive(owner, host=here)
        except Exception as exc:  # noqa: BLE001 - judge nothing we cannot read
            report.errors.append(f"{entry.name}: {type(exc).__name__}")
            continue
        if alive is None:
            report.unknown.append(entry.name)
        elif alive:
            report.kept.append(entry.name)
        else:
            size = _dir_bytes(path)
            if not dry_run:
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    if not path.exists():       # another sweep removed it first
                        continue
                    report.errors.append(f"{entry.name}: {type(exc).__name__}")
                    continue
            report.removed.append(entry.name)
            report.bytes += size
    return report
