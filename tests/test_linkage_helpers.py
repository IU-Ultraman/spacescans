"""Unit tests for spacescans.linkage.helpers.resolve_output_grouping.

The helper takes its `config` argument untyped (mirroring `load_patients`
in the same module) and only reads `config.time` / `config.time.output_grouping`.
Tests therefore use lightweight `SimpleNamespace` stubs instead of fully
constructing `DatasetConfig`, which would require unrelated required fields
(linkage_pattern, geometry_type, source) and obscure the contract under test.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from spacescans.linkage.helpers import resolve_output_grouping


def _config(time):
    return SimpleNamespace(time=time)


def test_resolve_output_grouping_raises_on_none_time():
    config = _config(time=None)
    with pytest.raises(ValueError, match="time block"):
        resolve_output_grouping(config)


def test_resolve_output_grouping_raises_on_invalid():
    config = _config(time=SimpleNamespace(output_grouping="weekly"))
    with pytest.raises(ValueError, match="weekly"):
        resolve_output_grouping(config)


@pytest.mark.parametrize("grouping", ["patient", "episode"])
def test_resolve_output_grouping_returns_literal(grouping: str):
    config = _config(time=SimpleNamespace(output_grouping=grouping))
    assert resolve_output_grouping(config) == grouping
