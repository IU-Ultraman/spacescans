"""The golden skip rule: tests/conftest.py and golden_compare.golden_unavailable_reason().

These need no goldens. They pin how a missing, misnamed or present copy is treated, and
that the caller's SPACESCANS_GOLDEN_DIR survives the per-test environment isolation.
"""
from __future__ import annotations

import golden_compare as gc  # imported by tests/conftest.py at startup
import pytest


@pytest.fixture
def caller_env(monkeypatch):
    """Set what the caller exported; the variable itself is gone inside tests."""
    monkeypatch.delenv(gc.GOLDEN_ENV, raising=False)

    def set_(value):
        monkeypatch.setattr(gc, "_CALLER_GOLDEN_DIR", value)
    return set_


def test_unset_means_skip(caller_env):
    caller_env(None)
    assert "is not set" in gc.golden_unavailable_reason()
    assert gc.golden_dir() is None


def test_a_directory_without_a_manifest_means_skip(caller_env, tmp_path):
    caller_env(str(tmp_path))
    assert "no manifest.json" in gc.golden_unavailable_reason()
    assert gc.golden_dir() is None


def test_a_copy_with_a_manifest_is_used_inside_tests(caller_env, tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    caller_env(str(tmp_path))
    assert gc.golden_unavailable_reason() is None
    assert gc.golden_dir() == tmp_path == gc.golden_root()


def test_a_value_set_inside_a_test_wins(caller_env, monkeypatch, tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    caller_env("/nonexistent/goldens")
    monkeypatch.setenv(gc.GOLDEN_ENV, str(tmp_path))
    assert gc.golden_dir() == tmp_path


def test_golden_tests_carry_the_marker_and_skip_without_goldens(request):
    """Every collected test that requests golden_dir is marked golden; without goldens, skipped."""
    items = [i for i in request.session.items if "golden_dir" in getattr(i, "fixturenames", ())]
    for item in items:
        assert item.get_closest_marker("golden"), item.nodeid
    if gc.golden_unavailable_reason():
        for item in [i for i in request.session.items if i.get_closest_marker("golden")]:
            assert item.get_closest_marker("skip"), item.nodeid


def test_golden_dir_names_a_golden_copy(golden_dir):
    """A golden test (through the fixture): skipped here unless SPACESCANS_GOLDEN_DIR is set."""
    assert (golden_dir / "manifest.json").is_file()
    assert golden_dir == gc.golden_root()
