"""The run context: where one run reads and writes, and its buffer settings.

A :class:`RunContext` is everything :func:`spacescans.orchestrate.render.render`
and :class:`spacescans.orchestrate.cache.WeightCache` need besides the step.
The web passes ``run_dir = task_dir``, ``cohort_parquet = task_dir /
"input.parquet"`` and ``name_suffix = f"_task_{task_dir.name[-8:]}"``
(:meth:`RunContext.from_task_dir`); R passes a private run folder and no
suffix.

It satisfies :class:`spacescans.custom.RenderContext` under the same attribute
names (``run_dir``, ``cohort_parquet``, ``buffer_m``, ``raster_res_m``,
``name_suffix``), so ``custom.render`` and ``custom.prepare_step`` accept it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The ``engine`` keys a context may set (EngineConfig's DuckDB resource caps and the
#: number of worker processes).
ENGINE_KEYS: tuple[str, ...] = ("threads", "memory_limit", "temp_directory", "workers")


@dataclass(frozen=True)
class RunContext:
    """One run's paths and buffer settings.

    ``run_dir``: every step writes ``output/<step>.parquet`` and
    ``pipeline_configs/<step>.yaml`` under it. ``cohort_parquet``: the
    pipeline input (``episode_id``, ``pid``, coordinates, dates).
    ``data_dir``: the data root the templates' relative paths resolve against.
    ``overlay``: custom-exposome definitions keyed by variable key (the web's
    ``config["custom_variables"]``). ``engine``: DuckDB resource caps
    (``threads``, ``memory_limit``, ``temp_directory``) and worker processes
    (``workers``) written into every rendered config; ``None`` leaves the YAML
    unchanged.

    ``buffer_m`` and ``raster_res_m`` are used as given: the web passes its
    config's JSON values, and ``270`` and ``270.0`` render (and key) differently.

    ``output_level``: ``"participant"`` (the default; every C4 step writes one
    row per participant) or ``"episode"`` (one row per location episode, the
    web 0.2.7 configs); see ``catalog.recipes.render_config`` rule 8.
    """

    run_dir: Path
    cohort_parquet: Path
    data_dir: Path
    buffer_m: int = 270
    raster_res_m: int = 25
    name_suffix: str = ""
    overlay: Mapping[str, dict] | None = None
    engine: Mapping[str, Any] | None = None
    output_level: str = "participant"

    def __post_init__(self) -> None:
        for name in ("run_dir", "cohort_parquet", "data_dir"):
            value = getattr(self, name)
            if value is None:
                raise TypeError(f"RunContext.{name} is required")
            object.__setattr__(self, name, Path(os.fspath(value)))
        if not isinstance(self.name_suffix, str):
            raise TypeError("RunContext.name_suffix must be a string")
        if self.overlay is not None and not isinstance(self.overlay, Mapping):
            raise TypeError("RunContext.overlay must be a mapping of definitions")
        from spacescans.catalog.recipes import check_output_level

        check_output_level(self.output_level)
        if self.engine is not None:
            if not isinstance(self.engine, Mapping):
                raise TypeError("RunContext.engine must be a mapping")
            unknown = sorted(set(self.engine) - set(ENGINE_KEYS))
            if unknown:
                raise ValueError(f"RunContext.engine: unknown key(s) {unknown}; "
                                 f"allowed: {list(ENGINE_KEYS)}")

    @classmethod
    def from_task_dir(cls, task_dir: str | os.PathLike, *, data_dir: str | os.PathLike,
                      buffer_m: Any = 270, raster_res_m: Any = 25,
                      overlay: Mapping[str, dict] | None = None,
                      output_level: str = "participant") -> RunContext:
        """The context the web runners use for ``task_dir``."""
        task = Path(os.fspath(task_dir))
        return cls(run_dir=task, cohort_parquet=task / "input.parquet", data_dir=data_dir,
                   buffer_m=buffer_m, raster_res_m=raster_res_m,
                   name_suffix=f"_task_{task.name[-8:]}", overlay=overlay,
                   output_level=output_level)

    def output_path(self, step_name: str) -> Path:
        """``run_dir/output/<step_name>.parquet``."""
        return self.run_dir / "output" / f"{step_name}.parquet"

    def config_path(self, step_name: str) -> Path:
        """``run_dir/pipeline_configs/<step_name>.yaml`` (the web's file name)."""
        return self.run_dir / "pipeline_configs" / f"{step_name}.yaml"
