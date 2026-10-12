"""spacescans — environmental exposure linkage pipeline."""
from __future__ import annotations
import sys

if sys.version_info < (3, 10):
    raise ImportError(
        f"spacescans-pipeline requires Python 3.10+, got "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}."
    )

# Single source of truth is pyproject.toml; read it back via package metadata
# so the version never drifts between the two files.
from importlib.metadata import version as _pkg_version, PackageNotFoundError

try:
    __version__ = _pkg_version("spacescans-pipeline")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+local"

# Lazy public re-exports — imported on attribute access to keep base install
# import-light (avoids touching pipeline/runner.py until needed).
def __getattr__(name: str):
    if name == "Pipeline":
        from spacescans.pipeline.runner import Pipeline
        return Pipeline
    if name == "resolve_config":
        from spacescans.config_resolution import resolve_config
        return resolve_config
    if name == "capabilities":
        from spacescans._capabilities import capabilities
        return capabilities
    raise AttributeError(f"module 'spacescans' has no attribute {name!r}")


__all__ = ["Pipeline", "resolve_config", "capabilities", "__version__"]
