"""Base install must allow `import spacescans` and access __version__."""
from importlib.metadata import version
from pathlib import Path

import pytest

DIST = "spacescans"


def test_can_import_top_level():
    import spacescans
    # __version__ is read back from the installed distribution's metadata, so
    # compare it with that instead of pinning a literal that every release bump
    # would break.
    assert spacescans.__version__ == version(DIST)


def test_installed_metadata_matches_pyproject():
    """A stale (editable) install reports the version it was installed at.

    Running newer sources under it makes __version__ wrong, so fail with the fix.
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("not running from a source checkout")
    tomllib = pytest.importorskip("tomllib")  # stdlib from Python 3.11
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert version(DIST) == declared, (
        f"installed {DIST} metadata says {version(DIST)}, pyproject.toml says "
        f"{declared}: reinstall the checkout (pip install -e .)"
    )


def test_pipeline_re_exported():
    from spacescans import Pipeline
    assert callable(Pipeline)


def test_resolve_config_re_exported():
    from spacescans import resolve_config
    assert callable(resolve_config)
