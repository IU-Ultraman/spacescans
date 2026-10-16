"""The PyPI alias `spacescans` (packaging/spacescans-alias) moves with spacescans-pipeline.

It holds no code: its only content is the pin on spacescans-pipeline at the same version, so
a version bump that forgets it would publish an alias pointing at the wrong release.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    tomllib = pytest.importorskip("tomli")

REPO = Path(__file__).resolve().parents[1]
ALIAS = REPO / "packaging" / "spacescans-alias"

pytestmark = pytest.mark.skipif(not ALIAS.is_dir(), reason="no alias package in this checkout")


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def test_alias_pins_spacescans_pipeline_at_the_same_version():
    main = _project(REPO / "pyproject.toml")
    alias = _project(ALIAS / "pyproject.toml")
    assert alias["name"] == "spacescans"
    assert alias["version"] == main["version"]
    assert alias["dependencies"] == [f"{main['name']}=={main['version']}"]
    assert alias["license"] == main["license"] == "MIT"
    assert alias["requires-python"] == main["requires-python"]


def test_alias_has_no_code_and_the_same_license_text():
    data = tomllib.loads((ALIAS / "pyproject.toml").read_text())
    assert data["tool"]["setuptools"]["packages"] == []
    assert not [p for p in ALIAS.rglob("*.py")]
    assert (ALIAS / "LICENSE").read_text() == (REPO / "LICENSE").read_text()
    assert "spacescans-pipeline" in (ALIAS / "README.md").read_text()
