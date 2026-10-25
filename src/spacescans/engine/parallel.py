"""Worker processes for the Python side of a pipeline step (``engine.workers``).

DuckDB already runs on the cores it is allowed (``engine.threads``). Rasterising,
exactextract and NetCDF decoding run in Python and hold one core each; a step whose
``engine.workers`` is above 1 hands such independent pieces of work to that many
worker processes. Results come back in input order, so a step writes the same output
whatever the number of workers.

The processes are spawned, never forked: the step process already runs DuckDB's
threads, and forking a multi-threaded process can deadlock the child. A spawned worker
imports the function it runs, so that function must be defined at module level in the
package. Workers belong to the step's process group, which the executors stop as a whole
when a run is cancelled.
"""
from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def resolve_workers(value: Any) -> int:
    """``engine.workers`` as a process count: None (the default) means 1."""
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"workers must be a positive integer or None, got {value!r}")
    return value


def map_ordered(fn: Callable[[T], R], items: Sequence[T], workers: int = 1, *,
                on_done: Callable[[int], None] | None = None) -> list[R]:
    """``[fn(item) for item in items]``, computed by up to ``workers`` processes.

    With ``workers`` of 1 or fewer than two items this is the plain loop in this
    process. ``on_done(i)`` runs in this process each time item ``i`` is finished (in
    completion order, for progress lines). The first exception a worker raises is
    raised here, after the work not yet started has been cancelled.
    """
    if workers <= 1 or len(items) < 2:
        out = []
        for i, item in enumerate(items):
            out.append(fn(item))
            if on_done is not None:
                on_done(i)
        return out
    results: list[Any] = [None] * len(items)
    pool = ProcessPoolExecutor(max_workers=min(workers, len(items)),
                               mp_context=multiprocessing.get_context("spawn"))
    try:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()
            if on_done is not None:
                on_done(i)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results
