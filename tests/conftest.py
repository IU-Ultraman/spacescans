"""Shared test fixtures for spacescans tests."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_golden_compare():
    """tests/golden/golden_compare.py, imported once as ``golden_compare``.

    Importing it here, before any test runs, lets it keep the caller's SPACESCANS_GOLDEN_DIR,
    which _isolate_spacescans_env removes while each test runs.
    """
    if "golden_compare" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "golden_compare", Path(__file__).resolve().parent / "golden" / "golden_compare.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["golden_compare"] = module
        spec.loader.exec_module(module)
    return sys.modules["golden_compare"]


_golden_compare = _load_golden_compare()


@pytest.hookimpl(tryfirst=True)  # before -m deselects by marker
def pytest_collection_modifyitems(config, items):
    """Golden tests: the ``golden`` marker, or a request for the ``golden_dir`` fixture.

    The goldens are kept out of git. A golden test is skipped, not failed, unless
    SPACESCANS_GOLDEN_DIR names a golden copy (one with a manifest.json); CI also
    deselects them with ``-m "not golden"``.
    """
    reason = _golden_compare.golden_unavailable_reason()
    for item in items:
        if "golden_dir" in getattr(item, "fixturenames", ()) and not item.get_closest_marker("golden"):
            item.add_marker(pytest.mark.golden)
        if reason and item.get_closest_marker("golden"):
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    """The golden references named by SPACESCANS_GOLDEN_DIR; requesting it marks a test golden."""
    root = _golden_compare.golden_dir()
    if root is None:
        pytest.skip(_golden_compare.golden_unavailable_reason())
    return root


@pytest.fixture(autouse=True)
def _isolate_spacescans_env(monkeypatch):
    """Run every test without the caller's SPACESCANS_* environment variables.

    The package reads SPACESCANS_DATA_DIR and SPACESCANS_OUTPUT_DIR, and expands
    ${VAR} placeholders in config paths from the environment, so a value
    exported in the shell that runs pytest would change test results. A test
    that needs one sets it with monkeypatch.setenv; monkeypatch restores the
    caller's values afterwards. Opt-in suites read their variables at import
    (golden tests use golden_compare or the golden_dir fixture, which keep
    SPACESCANS_GOLDEN_DIR).
    """
    for name in [k for k in os.environ if k.startswith("SPACESCANS_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clean_env(monkeypatch):
    """Unset SPACESCANS_* env vars so tests don't leak through local config.

    Kept for the tests that request it by name; _isolate_spacescans_env now
    does this for every test.
    """
    for name in ("SPACESCANS_DATA_DIR", "SPACESCANS_OUTPUT_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield
