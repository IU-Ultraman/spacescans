# src/spacescans/_extras.py
"""Optional extras gating: clean errors when a feature needs `pip install [extra]`."""
from __future__ import annotations
import importlib.util
from typing import Sequence

INSTALL_DOCS_URL = "https://github.com/IU-Ultraman/spacescans#optional-extras"

# Which pip extra provides each optional third-party module. Used to explain an
# import failure that was not raised through `require()` (a bare
# ModuleNotFoundError from a module-level import).
EXTRA_FOR_MODULE = {
    "geopandas": "geo",
    "rasterio": "geo",
    "shapely": "geo",
    "exactextract": "geo",
    "pyogrio": "geo",
    "pyproj": "geo",
    "pyreadr": "rda",
    "pyhdf": "hdf4",
    "xarray": "nc",
    "netCDF4": "nc",
}


class MissingExtraError(ImportError):
    """Raised when an optional extra is needed but not installed.

    ``extra`` names the pip extra (for example ``"geo"``) and ``missing`` lists
    the modules that could not be imported, when known. Both are optional so
    ``MissingExtraError("message")`` keeps working.
    """

    def __init__(self, *args, extra: str | None = None, missing: Sequence[str] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.extra = extra
        self.missing = tuple(missing)


def extra_for_module(name: str | None) -> str | None:
    """Return the pip extra that provides top-level module ``name`` (or None)."""
    if not name:
        return None
    return EXTRA_FOR_MODULE.get(name.split(".")[0])


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
            f"  See: {INSTALL_DOCS_URL}",
            extra=extra,
            missing=missing,
        )
