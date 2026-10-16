"""The pipeline steps of uploaded datasets: thin wrappers over the catalog's one Step model.

Unlike a shipped source, the custom path serves *many* datasets across *four*
boundary layers and any number of uploaded grids, so the two things every other
recipe fixes are looked up per variable instead:

  * the C3 step, taken from the boundary the dataset attaches to, reusing the
    shipped step name, template and cache tag (``TRACT_FARA`` for Tract, ``BG``
    for Block Group, ...). Reusing the tag is the point: a run that has already
    computed FARA's tract weights for this cohort and buffer reuses them.
    A RASTER dataset gets ``grid_weights`` with the uploaded GeoTIFF as the
    template grid instead, named and tagged by the grid's hash, so every year
    and every dataset on one grid share one C3.
  * the C4 step, rendered from ``c4/custom_areal.yaml`` with the dataset's own
    join column, value columns and table path: ``static_areal`` without a year
    column, ``yearly_areal`` with one. Both read a plain table through
    ``read_table`` because the rendered config sets no ``plugin:``. For a
    raster, the value table is first materialised from the C3 weights' grid
    ids (:func:`prepare_step`); from there C4 cannot tell a raster from a
    polygon.

Since tier-2 phase B there is one step model: :class:`Step` *is*
:class:`spacescans.catalog.recipes.Step`, :func:`plan` is
``catalog.plan("custom", ...)``, :func:`render` applies the catalog's render
contract (:func:`spacescans.catalog.recipes.render_config`) and
:func:`legacy_cache_key` is the orchestrate cache's ``legacy-v1`` key. The
functions here keep the web runner's names and call shapes (``c4_step_for(key)``,
``render(step, template, ctx, variables=..., definitions=...)``) for the web
adapter and existing callers.

Ported from spacescans-web ``app/experiments/custom.py`` (lines 105-325 and
349-415). The source-grep probe ``_sanity_check_pipeline_supports_areal_episode``
is not ported: inside the package the linkage code is the same release.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from spacescans.catalog import recipes as _recipes
from spacescans.catalog.recipes import Step
from spacescans.custom.definition import is_raster
from spacescans.custom.keys import TASK_CUSTOM_KEY

# boundary -> (C3 step name, C3 template, cache tag), from the catalog's custom
# recipe: the step name and tag are the shipped recipes' on purpose so the C3
# cache is shared with them.
BOUNDARY_C3: dict[str, tuple[str, str, str]] = {
    boundary: (step.name, step.template, step.cache_tag)
    for boundary, step in (
        (b, _recipes.recipe(_recipes.CUSTOM_EXPERIMENT).steps[name])
        for b, name in _recipes.CUSTOM_BOUNDARY_C3.items()
    )
}

C4_TEMPLATE = _recipes.CUSTOM_C4_TEMPLATE
C3_GRID_TEMPLATE = _recipes.CUSTOM_GRID_TEMPLATE
GRID_TAG_PREFIX = _recipes.CUSTOM_GRID_TAG_PREFIX
GRID_STEP_PREFIX = _recipes.CUSTOM_GRID_STEP_PREFIX
#: Every template the custom path renders, relative to the template root.
TEMPLATES = tuple(sorted({t for _n, t, _g in BOUNDARY_C3.values()} | {C4_TEMPLATE, C3_GRID_TEMPLATE}))
# A dataset with no coverage_years falls back to the shipped variables' window.
_DEFAULT_YEARS = tuple(_recipes.CUSTOM_DEFAULT_YEARS)


@runtime_checkable
class RenderContext(Protocol):
    """What :func:`render` reads from a run context.

    :class:`spacescans.orchestrate.RunContext` has these fields (and more); so
    does :class:`Context`, a minimal stand-in that needs no data directory.
    """

    run_dir: Path
    cohort_parquet: Path
    buffer_m: int
    raster_res_m: int
    name_suffix: str


@dataclass(frozen=True)
class Context:
    """Minimal run context: where a run writes, and the task's buffer settings.

    ``run_dir/output/<step>.parquet`` is every step's output; the web passes
    ``task_dir``, ``task_dir/"input.parquet"`` and ``f"_task_{task_dir.name[-8:]}"``.
    """

    run_dir: Path
    cohort_parquet: Path
    buffer_m: int = 270
    raster_res_m: int = 25
    name_suffix: str = ""


# --------------------------------------------------------------------------
# definitions of a selection
# --------------------------------------------------------------------------

def definitions_from_config(config: Mapping[str, Any]) -> dict[str, dict]:
    """A web task config's snapshot of the custom datasets it selected."""
    snapshot = config.get(TASK_CUSTOM_KEY) or {}
    if not isinstance(snapshot, dict):
        raise ValueError("config.custom_variables is not a mapping")  # noqa: TRY004  (web parity)
    return snapshot


def definition_for(definitions: Mapping[str, Mapping[str, Any]], var_key: str) -> Mapping[str, Any]:
    try:
        return definitions[var_key]
    except KeyError:
        raise ValueError(
            f"{var_key!r} has no definition in config.custom_variables — the "
            "task config was written without resolving it"
        ) from None


def grid_tag(definition: Mapping[str, Any]) -> str:
    return f"{GRID_TAG_PREFIX}{definition['grid_hash']}"


# --------------------------------------------------------------------------
# steps and plan (the catalog's)
# --------------------------------------------------------------------------

def c3_step_for(definition: Mapping[str, Any] | str) -> Step:
    """The C3 step a dataset needs (``catalog.recipes.custom_c3_step``).

    Polygons (or a bare boundary name) reuse the shipped boundary steps; a
    raster gets one per distinct grid, named by the grid's hash.
    """
    return _recipes.custom_c3_step(definition)


def c4_step_for(var_key: str, definition: Mapping[str, Any] | None = None) -> Step:
    """The C4 step of one variable (``catalog.recipes.custom_c4_step``).

    Without its definition (the web's ``c4_step_for(var_key)``) the step has
    only its name, template and dataset; :func:`render` completes it from the
    definitions it is given.
    """
    if definition is not None:
        return _recipes.custom_c4_step(var_key, definition)
    return Step(name=f"c4_{var_key}", template=C4_TEMPLATE, kind="c4", time_block="always",
                dataset=var_key)


def plan(variables: Sequence[str], definitions: Mapping[str, Mapping[str, Any]]) -> list[Step]:
    """One C3 per distinct boundary or grid, then one C4 per variable, in order.

    ``catalog.plan("custom", variables, overlay=definitions)``: two datasets
    on the same boundary (or grid) share a single C3 step; the C4 steps run
    against the one weights parquet it produces.
    """
    return _recipes.plan(_recipes.CUSTOM_EXPERIMENT, variables, overlay=definitions)


def parquet_map(variables: Sequence[str]) -> dict[str, str]:
    """Variable -> the C4 output file name, for the per-source merge."""
    return _recipes.recipe(_recipes.CUSTOM_EXPERIMENT).output_map(variables)


def first_definition_for_grid(variables: Sequence[str], definitions: Mapping[str, Mapping[str, Any]],
                              c3_step_name: str) -> Mapping[str, Any]:
    """The first selected raster whose grid renders ``c3_step_name``."""
    for var_key in variables:
        d = definition_for(definitions, var_key)
        if is_raster(d) and c3_step_for(d).name == c3_step_name:
            return d
    raise ValueError(f"no raster dataset in the selection renders {c3_step_name}")


def output_path(ctx: RenderContext, step_name: str) -> Path:
    return Path(ctx.run_dir) / "output" / f"{step_name}.parquet"


def values_path(ctx: RenderContext, var_key: str) -> Path:
    """Where a raster dataset's materialised C4 value table lives in a run."""
    return Path(ctx.run_dir) / "output" / f"values_{var_key}.parquet"


def weights_path(ctx: RenderContext, var_key: str) -> Path:
    """Where a dataset's run-specific C4 weights live (daily polygon tables, features)."""
    return Path(ctx.run_dir) / "output" / f"weights_{var_key}.parquet"


# --------------------------------------------------------------------------
# kinds the web's custom runner never had (registered datasets, phase D)
# --------------------------------------------------------------------------

#: Time structures linked through the ``gridded`` pattern (a date or a period per row).
DATED = ("daily", "biweekly")


def is_features(definition: Mapping[str, Any]) -> bool:
    """A features dataset: the value is the distance to the nearest feature."""
    return definition.get("geometry") == "features"


def is_dated(definition: Mapping[str, Any]) -> bool:
    """A daily polygon table, or a daily or biweekly raster."""
    return (definition.get("geometry") in ("polygon", "raster")
            and definition.get("temporal") in DATED)


def needs_c3(definition: Mapping[str, Any]) -> bool:
    """Whether the dataset has a C3 step (features have none: distances are computed per run)."""
    return not is_features(definition)


def _years(definition: Mapping[str, Any]) -> list[int]:
    years = definition.get("coverage_years") or list(_DEFAULT_YEARS)
    return list(range(int(years[0]), int(years[1]) + 1))


def kind_c4_step(var_key: str, definition: Mapping[str, Any]) -> Step | None:
    """The C4 step of a daily/biweekly or features dataset; None for the web's kinds.

    ``catalog.recipes.custom_c4_step`` asks this first. Both kinds read
    tables written just before the step renders (:func:`prepare_step`), so
    the step has ``materialise_values`` set:

    * daily polygon table, daily or biweekly raster: ``gridded`` (the TEMIS
      pattern) over ``output/values_<key>.parquet`` keyed by ``grid_id`` with
      ``date`` or ``start_date``/``end_date``; its weights are the raster's
      grid C3 output, or for a table ``output/weights_<key>.parquet`` (the
      boundary C3 output with the geography code as ``grid_id``);
    * features: ``static_areal`` / ``yearly_areal`` over the per-location
      distances ``output/values_<key>.parquet`` (``geoid[, year], <col>``)
      through one-to-one weights ``output/weights_<key>.parquet``, so each
      cohort row's value is its own distance, averaged over its days (and,
      for yearly data, weighted by the days in each year).
    """
    if is_features(definition):
        yearly = bool(definition.get("year_col"))
        bind = {"source.file": f"weights_{var_key}", "exposure.file": f"values_{var_key}"}
        fill: dict[str, Any] = {
            "linkage_pattern": "yearly_areal" if yearly else "static_areal",
            "source.join_col": "geoid",
            "exposure.join_col": "geoid",
            "exposure.value_cols": list(definition["value_cols"]),
        }
        if yearly:
            fill["exposure.year_col"] = "year"
        fill["time.years"] = _years(definition)
        fill["time.temporal_resolution"] = "yearly"
        fill["time.temporal_mode"] = "yearly"
    elif is_dated(definition):
        raster = is_raster(definition)
        c3 = _recipes.custom_c3_step(definition)
        bind = {"source.file": c3.name if raster else f"weights_{var_key}",
                "exposure.file": f"values_{var_key}"}
        fill = {
            "linkage_pattern": "gridded",
            "source.join_col": "grid_id",
            "exposure.join_col": "grid_id",
            "exposure.value_cols": list(definition["value_cols"]),
        }
        if definition["temporal"] == "daily":
            fill["exposure.date_col"] = "date"
        else:
            fill["exposure.start_col"] = "start_date"
            fill["exposure.end_col"] = "end_date"
        fill["time.years"] = _years(definition)
        fill["time.temporal_resolution"] = definition["temporal"]
        fill["time.temporal_mode"] = "yearly"
    else:
        return None
    return Step(name=f"c4_{var_key}", template=C4_TEMPLATE, kind="c4", bind=bind, fill=fill,
                time_block="always", dataset=var_key, materialise_values=True)


def _template_of(step: Any) -> str:
    return getattr(step, "template", None) or step.template_relpath


def _planned(step: Any, variables: Sequence[str],
             definitions: Mapping[str, Mapping[str, Any]]) -> Step:
    """The catalog step that ``step`` (looked up by name) is in this selection.

    The web derives everything from the definitions at render time, so this
    does too: a step built without its definition, or a web ``PipelineStep``,
    renders like the planned one.
    """
    if step.is_c3 and _template_of(step) == C3_GRID_TEMPLATE:
        definition = first_definition_for_grid(variables, definitions, step.name)
        return _recipes.custom_c3_step(definition)
    if step.is_c3:
        shipped = _recipes.recipe(_recipes.CUSTOM_EXPERIMENT).steps.get(step.name)
        if shipped is not None:
            return shipped
        # Any other C3 renders as the web's custom runner renders a boundary step.
        return dataclasses.replace(step, uses_raster_res=True, time_block="always")
    var_key = step.name[len("c4_"):]
    return _recipes.custom_c4_step(var_key, definition_for(definitions, var_key))


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(step: Any, template: Mapping[str, Any], ctx: RenderContext, *,
           variables: Sequence[str], definitions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The pipeline config of one step: the template with run and dataset fields filled.

    Pure: ``template`` is the parsed template (not modified), and nothing is
    read or written. For a raster C4 step the value table must already be at
    :func:`values_path` (see :func:`prepare_step`). Same result as
    ``orchestrate.render`` of the planned step.
    """
    return _recipes.render_config(
        _planned(step, variables, definitions), template, run_dir=Path(ctx.run_dir),
        cohort_parquet=ctx.cohort_parquet, buffer_m=ctx.buffer_m,
        raster_res_m=ctx.raster_res_m, name_suffix=ctx.name_suffix,
        engine=getattr(ctx, "engine", None))


def prepare_step(step: Any, ctx: RenderContext, *,
                 definitions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    """Run-time work a step needs before it renders; returns log records.

    A raster dataset's C4 step: its value table is materialised from the C3
    weights' grid ids (which must exist by then) into :func:`values_path`.
    A daily or biweekly dataset's: the ``gridded`` inputs
    (:func:`spacescans.custom.dated.materialise_dated`). A features
    dataset's: the per-location distances and their weights
    (:func:`spacescans.custom.features.materialise_features`). Each record is
    ``{"level", "source", "msg"}``, the arguments of one web ``logs.jsonl``
    line.
    """
    if step.is_c3:
        return []
    var_key = step.name[len("c4_"):]
    definition = definition_for(definitions, var_key)
    if is_features(definition):
        from spacescans.custom.features import materialise_features
        result = materialise_features(definition, cohort_parquet=ctx.cohort_parquet,
                                      values_out=values_path(ctx, var_key),
                                      weights_out=weights_path(ctx, var_key))
    elif is_dated(definition):
        from spacescans.custom.dated import materialise_dated
        result = materialise_dated(definition,
                                   c3_parquet=output_path(ctx, c3_step_for(definition).name),
                                   cohort_parquet=ctx.cohort_parquet,
                                   values_out=values_path(ctx, var_key),
                                   weights_out=weights_path(ctx, var_key))
    elif is_raster(definition):
        from spacescans.custom.values import materialise_values
        result = materialise_values(
            definition,
            c3_parquet=output_path(ctx, c3_step_for(definition).name),
            out_path=values_path(ctx, var_key),
        )
    else:
        return []
    return [{"level": "info", "source": step.name, "msg": result.message}]


# --------------------------------------------------------------------------
# C3 cache: tags and the web's legacy key
# --------------------------------------------------------------------------

def boundary_tag_for_step(step: Any) -> str | None:
    """The cache tag of a custom C3 step, by name; None for any other step."""
    if step.name.startswith(GRID_STEP_PREFIX):
        return GRID_TAG_PREFIX + step.name[len(GRID_STEP_PREFIX):]
    for name, _template, tag in BOUNDARY_C3.values():
        if name == step.name:
            return tag
    return None


def legacy_cache_key(input_parquet: str | Path, tag: str, *, buffer_m: Any,
                     raster_res_m: Any = None) -> str:
    """The web's C3 cache key (legacy-v1): ``<sha8>__<tag>__b<buffer>m[__r<res>m]``.

    A web wrapper over :func:`spacescans.orchestrate.cache.legacy_key` (the
    format ``WeightCache("legacy-v1")`` uses), so a custom Tract variable
    reuses FARA's cached weights. A grid tag (``CUSTOMGRID_``) gets no ``__r``
    part: grid_weights does not rasterise the buffer, like the shipped grid
    sources; the grid hash in the tag keeps two grids apart. The uploaded
    values never enter the key: C4 is never cached. ``buffer_m``/``raster_res_m``
    are formatted as given (270 vs 270.0 differ).
    """
    from spacescans.orchestrate.cache import legacy_key, sha256_file

    return legacy_key(tag, sha256_file(Path(input_parquet)), buffer_m=buffer_m,
                      raster_res_m=raster_res_m,
                      uses_raster_res=not tag.startswith(GRID_TAG_PREFIX))
