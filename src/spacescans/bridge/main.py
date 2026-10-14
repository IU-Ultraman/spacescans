"""Entry point: parse argv, split stdout, run one command, write the result, exit."""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from spacescans.bridge.errors import BridgeError, classify, format_tb
from spacescans.bridge.events import EventStream
from spacescans.bridge.protocol import (
    EXIT_ERROR,
    EXIT_NO_RESULT,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE,
    PROTOCOL,
    envelope,
    write_json_atomic,
)


class _Terminated(BaseException):
    """Raised in the main thread by the SIGTERM handler."""


def _on_sigterm(signum, frame):  # pragma: no cover - exercised through subprocess tests
    raise _Terminated()


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[spacescans.bridge] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _split_stdout() -> io.TextIOWrapper | None:
    """Keep the real stdout for events only; point fd 1 at stderr.

    After this, print() calls and C libraries writing to fd 1 end up in the
    human log instead of corrupting the event stream.
    """
    try:
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        ev_fd = os.dup(1)  # non-inheritable (PEP 446): step subprocesses never see it
    except OSError:
        return None
    os.dup2(2, 1)
    return io.TextIOWrapper(io.FileIO(ev_fd, "w", closefd=True), encoding="utf-8",
                            line_buffering=True, write_through=True)


def _read_request(path: str | None, command: str) -> dict[str, Any]:
    if path is None:
        return {"protocol": PROTOCOL, "args": {}}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise BridgeError("protocol", f"cannot read request file {path}: {e}", exit_code=EXIT_USAGE) from e
    try:
        req = json.loads(text)
    except json.JSONDecodeError as e:
        raise BridgeError("protocol", f"request file {path} is not valid JSON: {e}",
                          exit_code=EXIT_USAGE) from e
    if not isinstance(req, dict):
        raise BridgeError("protocol", "the request must be a JSON object", exit_code=EXIT_USAGE)
    proto = req.get("protocol", PROTOCOL)
    if proto != PROTOCOL:
        raise BridgeError("protocol", f"request protocol {proto!r} is not supported; this bridge "
                          f"speaks protocol {PROTOCOL}", details={"bridge_protocol": PROTOCOL,
                                                                  "request_protocol": proto},
                          exit_code=EXIT_USAGE)
    client = req.get("client") or {}
    if not isinstance(client, dict):
        raise BridgeError("protocol", "'client' must be an object", exit_code=EXIT_USAGE)
    rng = client.get("protocol_range")
    if rng is not None:
        ok_shape = (isinstance(rng, list) and len(rng) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool) for x in rng))
        if not ok_shape:
            raise BridgeError("protocol", "'client.protocol_range' must be [min, max]", exit_code=EXIT_USAGE)
        if not rng[0] <= PROTOCOL <= rng[1]:
            raise BridgeError(
                "protocol",
                f"{client.get('name') or 'the client'} {client.get('version') or ''} accepts protocol "
                f"{rng[0]}-{rng[1]}, but this spacescans bridge speaks protocol {PROTOCOL}",
                hint="install matching versions of the client and spacescans",
                details={"bridge_protocol": PROTOCOL, "client_range": rng}, exit_code=EXIT_USAGE)
    cmd = req.get("command")
    if cmd is not None and cmd != command:
        raise BridgeError("protocol", f"the request is for command {cmd!r} but {command!r} was run",
                          exit_code=EXIT_USAGE)
    args = req.get("args")
    if args is None:
        req["args"] = {}
    elif not isinstance(args, dict):
        raise BridgeError("protocol", "'args' must be an object", exit_code=EXIT_USAGE)
    return req


DATA_DIR_ENV = "SPACESCANS_DATA_DIR"
#: Set by a client whose bridge must not outlive it (R, except for a job meant
#: to survive the session): the client's pid.
PARENT_ENV = "SPACESCANS_BRIDGE_PARENT"
#: The client's pid when ``$SPACESCANS_BRIDGE_PARENT`` named this process's parent
#: (set by :func:`watch_parent`); a run directory the bridge creates for ``link`` is
#: owned by it, since the client reads the results after the bridge has exited.
client_pid: int | None = None


def watch_parent(interval_s: float = 1.0) -> threading.Thread | None:
    """Stop the bridge, as on SIGTERM, once the client that started it is gone.

    Only with ``$SPACESCANS_BRIDGE_PARENT`` naming this process's parent at
    start-up: when the parent dies (the bridge is re-parented), the bridge
    sends itself SIGTERM, so the running command is cancelled, its step
    processes are stopped and the result says ``interrupted``. Without this
    a killed R session would leave the bridge and its pipeline steps running.
    The variable is removed so step subprocesses never see it.
    """
    global client_pid
    raw = os.environ.pop(PARENT_ENV, "")
    try:
        expected = int(raw)
    except ValueError:
        return None
    if expected <= 1 or os.getppid() != expected:
        return None
    client_pid = expected

    def loop() -> None:
        while True:
            time.sleep(interval_s)
            if os.getppid() != expected:
                _log(f"the client process {expected} is gone; stopping")
                os.kill(os.getpid(), signal.SIGTERM)
                return

    thread = threading.Thread(target=loop, name="spacescans-bridge-parent-watch", daemon=True)
    thread.start()
    return thread


def apply_data_dir(args: dict) -> str | None:
    """Make ``$SPACESCANS_DATA_DIR`` agree with the request's ``data_dir`` (mismatch #11).

    The pipeline expands ``${SPACESCANS_DATA_DIR}`` from the environment but
    resolves relative paths against ``data_dir``; clients pass their whole
    environment through, so the two could name different directories. With a
    ``data_dir`` in the request the bridge sets the variable to it for this
    process and every step subprocess; without one the environment's value
    (if any) is the data root for both.
    """
    value = args.get("data_dir") if isinstance(args, dict) else None
    if isinstance(value, str) and value and os.path.isabs(value):
        os.environ[DATA_DIR_ENV] = value
        return value
    return None


def main(argv: list[str] | None = None) -> int:
    t0 = time.perf_counter()
    parser = argparse.ArgumentParser(
        prog="python -m spacescans.bridge",
        description=f"spacescans bridge, protocol {PROTOCOL}: run one command from a JSON request.")
    parser.add_argument("command", help="hello | catalog | coverage | data-status | data-plan | "
                                        "data-verify | data-fetch | data-import | data-terms | "
                                        "custom-validate | link | cache-info | cache-clear | run | "
                                        "resolve | schema | templates | quickstart | yaml-load | "
                                        "yaml-dump | capabilities | ping")
    parser.add_argument("--request", help="request JSON file (optional for commands without args)")
    parser.add_argument("--result", help="where to write the result JSON (default: a final 'result' event)")
    parser.add_argument("--verbose", action="store_true", help="also emit raw pipeline lines as events")
    ns = parser.parse_args(argv)

    from spacescans.bridge.commands import COMMANDS, Context

    stream = _split_stdout()
    events = EventStream(stream)
    previous_term = signal.signal(signal.SIGTERM, _on_sigterm)
    watch_parent()
    command = ns.command
    value: Any = None
    warns: list = []
    error: dict | None = None
    code = EXIT_OK
    hard_exit = False
    try:
        try:
            request = _read_request(ns.request, command)
            if command not in COMMANDS:
                raise BridgeError("protocol", f"unknown command {command!r}",
                                  details={"commands": sorted(COMMANDS)}, exit_code=EXIT_USAGE)
            events.verbose = bool(ns.verbose or request.get("verbose"))
            events.emit("start", command=command, pid=os.getpid(), protocol=PROTOCOL)
            apply_data_dir(request["args"])
            client = request.get("client") if isinstance(request.get("client"), dict) else {}
            ctx = Context(events=events, verbose=events.verbose, client=client)
            value, warns = COMMANDS[command](request["args"], ctx)
        except (KeyboardInterrupt, _Terminated) as e:
            # Ignore further signals while we write the result.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            which = "SIGTERM" if isinstance(e, _Terminated) else "SIGINT"
            code = EXIT_SIGTERM if isinstance(e, _Terminated) else EXIT_SIGINT
            err = BridgeError("interrupted", f"interrupted by {which}", details={"signal": which})
            error = {**err.to_dict(), "traceback": format_tb(e)}
            hard_exit = True  # a worker thread may still be inside a C call
            _log(f"{command}: interrupted by {which}")
        except BridgeError as e:
            error, code = e.to_dict(), e.exit_code
            _log(f"{command}: {e.kind}: {e.message}")
        except BaseException as e:
            err = classify(e)
            error, code = err.to_dict(), EXIT_ERROR
            _log(f"{command}: {err.kind}: {err.message}\n{error['traceback']}")

        result = envelope(command, ok=error is None, value=value, warnings=warns, error=error,
                          elapsed_s=time.perf_counter() - t0)
        try:
            if ns.result:
                write_json_atomic(Path(ns.result), result)
            else:  # keep stdout JSON Lines: the result travels as one more event
                events.emit("result", result=result)
        except Exception as e:
            _log(f"cannot write the result to {ns.result}: {type(e).__name__}: {e}")
            code = EXIT_NO_RESULT
        events.emit("done", ok=error is None, exit=code)
        events.close()
    finally:
        signal.signal(signal.SIGTERM, previous_term if previous_term is not None else signal.SIG_DFL)
    if hard_exit:
        try:
            sys.stderr.flush()
        finally:
            os._exit(code)
    return code


