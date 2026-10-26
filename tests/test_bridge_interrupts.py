"""SIGINT and SIGTERM while the bridge waits for its worker thread.

From CPython 3.13 on, a KeyboardInterrupt raised inside ``threading.Event.wait`` can leave
the event's lock held; the worker's ``done.set()`` and every later wait on the event then
block for ever. On 3.13 an exception raised by a signal handler can also skip the ``except``
meant to catch it. So ``run_captured`` and ``run_cancellable`` never let a signal handler
raise while they wait: they keep what it raised and raise it themselves.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
import traceback

import pytest

from spacescans.bridge.main import _on_sigterm, _Terminated
from spacescans.bridge.worker import run_cancellable, run_captured

RUNNERS = ["run_captured", "run_cancellable"]
SIGNALS = [pytest.param(signal.SIGINT, KeyboardInterrupt, id="SIGINT"),
           pytest.param(signal.SIGTERM, _Terminated, id="SIGTERM")]


@pytest.fixture(autouse=True)
def bridge_handlers():
    """The handlers ``main()`` runs commands with; an interrupt leaves both signals ignored."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _on_sigterm)
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def run(runner: str, fn, cancel: threading.Event | None = None):
    if runner == "run_cancellable":
        return run_cancellable(fn, lambda line: None, cancel=cancel or threading.Event(), grace_s=2)
    return run_captured(fn, lambda line: None)


def interrupt(runner: str, sig: int) -> tuple[BaseException, list[str]]:
    """Run work that waits until stopped, send ``sig`` after 0.3 s; return what was raised."""
    stop = threading.Event()
    steps: list[str] = []

    def work():
        while not stop.is_set():
            time.sleep(0.01)
        steps.append("stopped")

    timer = threading.Timer(0.3, signal.raise_signal, (sig,))
    timer.start()
    try:
        run(runner, work, cancel=stop)
    except BaseException as e:
        return e, steps
    finally:
        stop.set()  # run_captured leaves its worker running
        timer.cancel()
    pytest.fail(f"{runner} returned although it was interrupted")


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("sig,exc", SIGNALS)
def test_the_runner_raises_the_interrupt_itself_not_inside_a_wait(runner, sig, exc):
    e, steps = interrupt(runner, sig)
    assert isinstance(e, exc)
    inside = [f"{f.name}:{f.lineno}" for f in traceback.extract_tb(e.__traceback__)
              if f.filename == threading.__file__]
    assert inside == []
    if runner == "run_cancellable":
        assert steps == ["stopped"]  # the work was stopped before the interrupt went on


@pytest.mark.parametrize("runner", RUNNERS)
def test_both_signals_stay_ignored_after_an_interrupt(runner):
    """A second Ctrl-C must not stop the bridge from writing its result."""
    interrupt(runner, signal.SIGINT)
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_handlers_are_back_after_a_run(runner):
    assert run(runner, lambda: "done") == ("done", [])
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert signal.getsignal(signal.SIGTERM) is _on_sigterm


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_handler_that_does_not_raise_does_not_stop_the_work(runner):
    calls = []
    signal.signal(signal.SIGINT, lambda signum, frame: calls.append(signum))
    timer = threading.Timer(0.2, signal.raise_signal, (signal.SIGINT,))
    timer.start()

    def work():
        time.sleep(0.6)
        return "done"

    assert run(runner, work) == ("done", [])
    assert calls == [signal.SIGINT]


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_runner_works_outside_the_main_thread(runner):
    """Only the main thread may set signal handlers, and no handler runs in another thread."""
    out = []
    thread = threading.Thread(target=lambda: out.append(run(runner, lambda: "done")))
    thread.start()
    thread.join(10)
    assert out == [("done", [])]


STRESS = r'''
import faulthandler, json, os, random, signal, sys, threading, time

from spacescans.bridge import worker
from spacescans.bridge.main import _on_sigterm, _Terminated

# The main thread goes round its waiting code every 0.3 ms instead of every 50 ms, so that the
# signals land at every point of it. An Event.wait with a timeout, which the main thread must
# not use, is shortened the same way. No child processes here: the sweep's ps is skipped.
worker._POLL_S = 0.0003
_event_wait = threading.Event.wait
threading.Event.wait = lambda self, timeout=None: _event_wait(
    self, timeout if timeout is None else min(timeout, 0.0003))
worker.kill_descendants = lambda: []

wanted = []  # the signal a round asks for once its work has started


def shooter():
    n = 0
    while True:
        if not wanted:
            time.sleep(0.0001)
            continue
        sig = wanted.pop()
        time.sleep(random.uniform(0, 0.002))
        if n % 2:
            os.kill(os.getpid(), sig)  # to the process: usually the main thread takes it
        else:
            signal.raise_signal(sig)   # to this thread: the main thread sees it at its next check
        n += 1


def main(rounds):
    threading.Thread(target=shooter, daemon=True).start()
    outcomes = {}
    for i in range(rounds):
        # a round that hangs prints every thread's stack and exits with status 1
        faulthandler.dump_traceback_later(10, exit=True)
        sig, expected = ((signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, _Terminated))[i % 2]
        cancellable = i % 4 >= 2
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, _on_sigterm)
        stop = threading.Event()
        steps = []

        def work(sig=sig, stop=stop, steps=steps):
            wanted.append(sig)
            while not stop.is_set():
                time.sleep(0.0002)
            steps.append("stopped")

        try:
            if cancellable:
                worker.run_cancellable(work, lambda line: None, cancel=stop, grace_s=5)
            else:
                worker.run_captured(work, lambda line: None)
            outcome = "returned"
        except BaseException as e:
            if not isinstance(e, expected):
                outcome = type(e).__name__
            else:
                outcome = "ok" if not cancellable or steps == ["stopped"] else "not stopped"
        stop.set()
        # one worker at a time: its redirect_stdout and catch_warnings are process-wide
        while threading.active_count() > 2:
            time.sleep(0.0002)
        faulthandler.cancel_dump_traceback_later()
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if (i + 1) % 250 == 0 or i + 1 == rounds:
            print(json.dumps({"rounds": i + 1, "outcomes": outcomes}), file=sys.__stdout__, flush=True)


main(int(sys.argv[1]))
'''


def test_thousands_of_signals_at_random_moments(tmp_path):
    """Each round: one SIGINT or SIGTERM at a random moment while a runner waits.

    Before the fix, 2,000 rounds hung on Python 3.13 in most runs, and on 3.14 and 3.15 some
    interrupts came out as RuntimeError.
    """
    rounds = 2000
    script = tmp_path / "stress.py"
    script.write_text(STRESS)
    p = subprocess.Popen([sys.executable, "-I", "-u", str(script), str(rounds)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    progress = [json.loads(line) for line in out.splitlines()]
    assert p.returncode == 0, f"{progress[-1:]}\n{err[-4000:]}"
    assert progress[-1] == {"rounds": rounds, "outcomes": {"ok": rounds}}
