"""Shared test fixtures for spacescans tests."""
from __future__ import annotations
import os
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_spacescans_env(monkeypatch):
    """Run every test without the caller's SPACESCANS_* environment variables.

    The package reads SPACESCANS_DATA_DIR and SPACESCANS_OUTPUT_DIR, and expands
    ${VAR} placeholders in config paths from the environment, so a value
    exported in the shell that runs pytest would change test results. A test
    that needs one sets it with monkeypatch.setenv; monkeypatch restores the
    caller's values afterwards.
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
