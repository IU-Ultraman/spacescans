"""Render a step's pipeline config: the packaged template with the run's fields filled.

``render(step, ctx)`` is pure and gives exactly the dict the web's
``render_yaml`` builds, for every runner and the upload path (gate A0); the
rules are the render contract in :mod:`spacescans.catalog.recipes`, applied
by :func:`spacescans.catalog.recipes.render_config`. ``write_config`` writes
it where the web does, ``run_dir/pipeline_configs/<step>.yaml``, with
``yaml.safe_dump(cfg, sort_keys=False)``.

Callers check the C3 cache first and render only on a miss: the web writes no
YAML for a step served from the cache.

A custom raster dataset's C4 step reads a value table that must be
materialised from the C3 weights first (``Step.materialise_values``);
:func:`prepare` does that and returns the web's log records. Rendering itself
never reads or writes anything.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.catalog import recipes as _recipes
from spacescans.catalog import template as _template

if TYPE_CHECKING:
    from spacescans.catalog.recipes import Step
    from spacescans.orchestrate.context import RunContext

__all__ = ["dump", "prepare", "render", "render_template", "render_text", "write_config"]


def render_template(step: Step, template: Mapping[str, Any], ctx: Any) -> dict[str, Any]:
    """:func:`render` with the parsed template given by the caller.

    ``ctx`` is a :class:`~spacescans.orchestrate.context.RunContext` or any
    object with ``run_dir``, ``cohort_parquet``, ``buffer_m``,
    ``raster_res_m`` and ``name_suffix`` (``engine`` is optional).
    """
    return _recipes.render_config(
        step, template, run_dir=Path(ctx.run_dir), cohort_parquet=ctx.cohort_parquet,
        buffer_m=ctx.buffer_m, raster_res_m=ctx.raster_res_m, name_suffix=ctx.name_suffix,
        engine=getattr(ctx, "engine", None))


def render(step: Step, ctx: RunContext) -> dict[str, Any]:
    """The pipeline config of ``step`` in the run ``ctx`` (pure; equals the web's YAML).

    Custom steps must come from ``catalog.plan("custom", keys, overlay=ctx.overlay)``
    (or ``custom.plan``): their definition fields are part of the step.
    """
    return render_template(step, _template(step.template), ctx)


def dump(cfg: Mapping[str, Any]) -> str:
    """The YAML text the web writes for a rendered config."""
    import yaml

    return yaml.safe_dump(dict(cfg), sort_keys=False)


def render_text(step: Step, ctx: RunContext) -> str:
    """``dump(render(step, ctx))``."""
    return dump(render(step, ctx))


def write_config(step: Step, ctx: RunContext, *, config: Mapping[str, Any] | None = None) -> Path:
    """Write ``run_dir/pipeline_configs/<step.name>.yaml`` and return its path.

    ``config`` is an already rendered config (to render once for both a cache
    key and the file); by default the step is rendered here.
    """
    cfg = render(step, ctx) if config is None else config
    out = Path(ctx.run_dir) / "pipeline_configs" / f"{step.name}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump(cfg), encoding="utf-8")
    return out


def prepare(step: Step, ctx: RunContext) -> list[dict[str, str]]:
    """Run-time work a step needs before it renders; returns ``{level, source, msg}`` records.

    Only a custom raster C4 step (``materialise_values``) has any: its value
    table ``output/values_<key>.parquet`` is built from the C3 weights' grid
    ids, which must exist by then (``custom.prepare_step``). The records are
    the web's ``logs.jsonl`` lines for it.
    """
    if not getattr(step, "materialise_values", False):
        return []
    from spacescans.custom.steps import prepare_step

    return prepare_step(step, ctx, definitions=ctx.overlay or {})
