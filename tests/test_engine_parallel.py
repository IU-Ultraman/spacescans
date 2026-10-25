"""engine.parallel: the process map behind ``engine.workers``.

A spawned worker imports the function it runs. Builtins can be imported there; a test
module imported by pytest's importlib mode cannot, so these tests map builtins.
"""
from __future__ import annotations

import pytest

from spacescans.engine.parallel import map_ordered, resolve_workers


@pytest.mark.parametrize("value,expected", [(None, 1), (1, 1), (4, 4)])
def test_resolve_workers(value, expected):
    assert resolve_workers(value) == expected


@pytest.mark.parametrize("value", [0, -2, 1.5, True, "2"])
def test_resolve_workers_refuses_anything_but_a_positive_int(value):
    with pytest.raises(ValueError, match="workers must be a positive integer"):
        resolve_workers(value)


def test_one_worker_is_the_plain_loop_in_order():
    seen: list[int] = []
    assert map_ordered(abs, [-3, 1, -2], 1, on_done=seen.append) == [3, 1, 2]
    assert seen == [0, 1, 2]


def test_workers_keep_the_input_order_and_report_every_item():
    items = list(range(-20, 0))
    seen: list[int] = []
    assert map_ordered(abs, items, 3, on_done=seen.append) == [abs(i) for i in items]
    assert sorted(seen) == list(range(len(items)))


def test_a_worker_error_is_raised_in_the_caller():
    with pytest.raises(ValueError, match="invalid literal"):
        map_ordered(int, ["1", "2", "x", "4"], 2)


def test_fewer_items_than_workers():
    assert map_ordered(abs, [-1], 4) == [1]
    assert map_ordered(abs, [], 4) == []
