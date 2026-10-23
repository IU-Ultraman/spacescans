# tests/test_extras_gating.py
"""Verify _extras.require() raises clean errors for missing modules."""
import pytest
from spacescans._extras import require, MissingExtraError


def test_require_passes_when_module_present():
    require("dev", "pytest")   # pytest is always present in test env


def test_require_raises_for_missing_module():
    with pytest.raises(MissingExtraError) as exc_info:
        require("nope_extra", "this_module_does_not_exist")
    msg = str(exc_info.value)
    assert "pip install 'spacescans[nope_extra]'" in msg
    assert "this_module_does_not_exist" in msg


def test_require_lists_all_missing_modules():
    with pytest.raises(MissingExtraError) as exc_info:
        require("foo", "missing_a", "missing_b")
    msg = str(exc_info.value)
    assert "missing_a" in msg and "missing_b" in msg


def test_missing_extra_error_subclasses_importerror():
    """MissingExtraError must be catchable as ImportError for compat."""
    assert issubclass(MissingExtraError, ImportError)
