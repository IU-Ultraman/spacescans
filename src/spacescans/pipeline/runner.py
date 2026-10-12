# src/spacescans/pipeline/runner.py
"""Pipeline runner — the main entry point."""
from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from spacescans.engine.duckdb_engine import DuckDBEngine
from spacescans.models.config import DatasetConfig
from spacescans.pipeline.loader import load_config, load_config_dict
from spacescans.pipeline.registry import get_pattern, init


class Pipeline:
    def __init__(self, config: DatasetConfig):
        # init() is idempotent; calling it here lets Pipeline(DatasetConfig(...))
        # run without going through from_config().
        init()
        self.config = config
        self.engine = self._build_engine()

    @classmethod
    def from_config(
        cls,
        path: str | Path,
        *,
        data_dir: str | None = None,
        output_dir: str | None = None,
    ) -> "Pipeline":
        init()
        config = load_config(path, data_dir=data_dir, output_dir=output_dir)
        return cls(config)

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        data_dir: str | None = None,
        output_dir: str | None = None,
    ) -> "Pipeline":
        """Build a pipeline from a parsed config mapping (same rules as from_config)."""
        init()
        config = load_config_dict(raw, data_dir=data_dir, output_dir=output_dir)
        return cls(config)

    def run(self) -> Path:
        pattern_fn = get_pattern(self.config.linkage_pattern.value)
        return pattern_fn(self.config, self.engine)

    def _build_engine(self):
        engine = self.config.engine
        if engine.backend == "duckdb":
            return DuckDBEngine(
                threads=engine.threads,
                memory_limit=engine.memory_limit,
                temp_directory=engine.temp_directory,
            )
        raise ValueError(f"Unknown engine backend: {engine.backend}")
