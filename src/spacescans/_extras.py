# src/spacescans/_extras.py
"""Optional extras gating: clean errors when a feature needs `pip install [extra]`."""
from __future__ import annotations
import importlib.util


class MissingExtraError(ImportError):
    """Raised when an optional extra is needed but not installed."""


def require(extra: str, *modules: str) -> None:
    """Raise MissingExtraError if any listed module is not importable.

    Use at the top of a module that depends on optional packages:

        from spacescans._extras import require
        require("geo", "geopandas", "rasterio")

    The error message tells the user exactly how to install the missing extra.
    """
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        raise MissingExtraError(
            f"This feature needs `pip install 'spacescans-pipeline[{extra}]'`.\n"
            f"  Missing module(s): {', '.join(missing)}\n"
            f"  See: https://github.com/xuguang-ai/spacescans-project#optional-extras"
        )
