"""The R package in r/ is released with the same version as the Python package."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESCRIPTION = REPO / "r" / "DESCRIPTION"


def _field(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    assert m, f"{name} missing from r/DESCRIPTION"
    return m.group(1).strip()


@pytest.mark.skipif(not DESCRIPTION.is_file(), reason="no R package in this checkout")
def test_r_package_version_matches_python():
    py = re.search(r'^version = "(.+)"$', (REPO / "pyproject.toml").read_text(), re.MULTILINE).group(1)
    desc = DESCRIPTION.read_text()
    assert _field(desc, "Version") == py
    assert _field(desc, "Config/spacescans/version") == py


@pytest.mark.skipif(not DESCRIPTION.is_file(), reason="no R package in this checkout")
def test_r_package_protocol_matches_bridge():
    from spacescans.bridge import PROTOCOL
    assert int(_field(DESCRIPTION.read_text(), "Config/spacescans/protocol")) == PROTOCOL
