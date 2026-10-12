# src/spacescans/pipeline/loader.py
"""YAML config loading + Pydantic validation + path resolution."""
from __future__ import annotations
import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import yaml
from spacescans.config_resolution import (
    ConfigResolutionError, resolve_data_dir, resolve_output_dir, resolve_config,
)
from spacescans.models.config import DatasetConfig


def load_config_dict(
    raw: Mapping[str, Any],
    *,
    data_dir: str | None = None,
    output_dir: str | None = None,
) -> DatasetConfig:
    """Resolve all paths in an already-parsed config mapping and validate it.

    Same rules as load_config(): data_dir / output_dir overrides take priority
    over env vars and the mapping's own ``base_dir`` / ``base_output_dir``.
    The input is not modified.
    """
    if not isinstance(raw, Mapping):
        raise ConfigResolutionError(
            f"A pipeline config must be a mapping at the top level, got {type(raw).__name__}."
        )
    raw = copy.deepcopy(dict(raw))

    yaml_base_dir = raw.pop("base_dir", None)
    yaml_output_dir = raw.pop("base_output_dir", None)

    resolved_data = resolve_data_dir(cli_value=data_dir, yaml_value=yaml_base_dir)
    resolved_out = resolve_output_dir(cli_value=output_dir, yaml_value=yaml_output_dir)

    rewritten = resolve_config(raw, data_dir=resolved_data, output_dir=resolved_out)
    return DatasetConfig.model_validate(rewritten)


def load_config(
    path: str | Path,
    *,
    data_dir: str | None = None,
    output_dir: str | None = None,
) -> DatasetConfig:
    """Load YAML, resolve all paths, validate to DatasetConfig.

    data_dir / output_dir overrides take priority over env vars and yaml base_dir.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    return load_config_dict(raw, data_dir=data_dir, output_dir=output_dir)
