"""The pipeline steps of uploaded datasets: plan, render, cache tags.

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

:func:`render` is a pure function from a parsed template to the config dict;
writing it (``yaml.safe_dump(cfg, sort_keys=False)``) gives the web's YAML byte
for byte. Templates come from the caller (the catalog's copies of the web
``configs/``), so this module reads no template files.

Ported from spacescans-web ``app/experiments/custom.py`` (lines 105-325 and
349-415). The source-grep probe ``_sanity_check_pipeline_supports_areal_episode``
is not ported: inside the package the linkage code is the same release.
"""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from spacescans.custom.definition import is_raster
from spacescans.custom.keys import TASK_CUSTOM_KEY

# boundary -> (C3 step name, C3 template, cache tag). Step name and tag are the
# shipped recipes' on purpose so the C3 cache is shared with them.
BOUNDARY_C3: dict[str, tuple[str, str, str]] = {
    "Tract": ("c3_tract_us", "c3/tract_us_demo.yaml", "TRACT_FARA"),
    "BG": ("c3_bg", "c3/bg_us_demo.yaml", "BG"),
    "ZCTA5": ("c3_zcta5", "c3/zcta5_us_demo.yaml", "ZCTA5"),
    "County": ("c3_county_us", "c3/county_us_demo.yaml", "COUNTY"),
}

C4_TEMPLATE = "c4/custom_areal.yaml"
C3_GRID_TEMPLATE = "c3/custom_grid.yaml"
GRID_TAG_PREFIX = "CUSTOMGRID_"
GRID_STEP_PREFIX = "c3_customgrid_"
#: Every template the custom path renders, relative to the template root.
TEMPLATES = tuple(sorted({t for _n, t, _g in BOUNDARY_C3.values()} | {C4_TEMPLATE, C3_GRID_TEMPLATE}))
# A dataset with no coverage_years falls back to the shipped variables' window.
_DEFAULT_YEARS = (2013, 2019)


@dataclass(frozen=True)
class Step:
    """One pipeline step of the custom path.

    Field names follow the design's ``catalog.Step`` (name, template, kind,
    cache_tag, uses_raster_res). ``variable`` is the custom key a C4 step
    links; ``source_step`` is the C3 step whose weights it reads (the
    ``bind`` ``{"source.file": source_step}`` of a catalog step).
    ``is_c3`` and ``template_relpath`` keep the web's ``PipelineStep`` API.
    """

    name: str
    template: str
    kind: Literal["c3", "c4"]
    cache_tag: str | None = None
    uses_raster_res: bool = False
    variable: str | None = None
    source_step: str | None = None

    @property
    def is_c3(self) -> bool:
        return self.kind == "c3"

    @property
    def template_relpath(self) -> str:
        return self.template


class RenderContext(Protocol):
    """What :func:`render` reads from a run context.

    The design's ``orchestrate.RunContext`` has these fields; so does
    :class:`Context`, a minimal stand-in until that exists.
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
        raise ValueError("config.custom_variables is not a mapping")
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
# steps and plan
# --------------------------------------------------------------------------

def c3_step_for(definition: Mapping[str, Any] | str) -> Step:
    """The C3 step a dataset needs. Polygons reuse the shipped boundary steps;
    a raster gets one per distinct grid, named by the grid's hash."""
    if isinstance(definition, str):            # a bare boundary name
        definition = {"boundary": definition}
    if is_raster(definition):
        return Step(
            name=f"{GRID_STEP_PREFIX}{definition['grid_hash']}",
            template=C3_GRID_TEMPLATE, kind="c3",
            cache_tag=grid_tag(definition), uses_raster_res=False,
        )
    boundary = definition["boundary"]
    try:
        name, template, tag = BOUNDARY_C3[boundary]
    except KeyError:
        raise ValueError(
            f"custom exposome on unsupported boundary {boundary!r}; "
            f"expected one of {sorted(BOUNDARY_C3)}"
        ) from None
    return Step(name=name, template=template, kind="c3", cache_tag=tag, uses_raster_res=True)


def c4_step_for(var_key: str, definition: Mapping[str, Any] | None = None) -> Step:
    """The C4 step of one variable; with its definition, ``source_step`` is set."""
    return Step(
        name=f"c4_{var_key}", template=C4_TEMPLATE, kind="c4", variable=var_key,
        source_step=None if definition is None else c3_step_for(definition).name,
    )


def plan(variables: Sequence[str], definitions: Mapping[str, Mapping[str, Any]]) -> list[Step]:
    """One C3 per distinct boundary or grid, then one C4 per variable, in order.

    Two datasets on the same boundary (or grid) share a single C3 step; the C4
    steps run against the one weights parquet it produces.
    """
    if not variables:
        raise ValueError("at least one variable must be selected")
    steps: list[Step] = []
    seen_c3: set[str] = set()
    for var_key in variables:
        c3 = c3_step_for(definition_for(definitions, var_key))
        if c3.name not in seen_c3:
            seen_c3.add(c3.name)
            steps.append(c3)
    for var_key in variables:
        steps.append(c4_step_for(var_key, definition_for(definitions, var_key)))
    return steps


def parquet_map(variables: Sequence[str]) -> dict[str, str]:
    """Variable -> the C4 output file name, for the per-source merge."""
    return {v: f"{c4_step_for(v).name}.parquet" for v in variables}


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


def _template_of(step: Any) -> str:
    return getattr(step, "template", None) or step.template_relpath


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(step: Any, template: Mapping[str, Any], ctx: RenderContext, *,
           variables: Sequence[str], definitions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The pipeline config of one step: the template with run and dataset fields filled.

    Pure: ``template`` is the parsed template (not modified), and nothing is
    read or written. For a raster C4 step the value table must already be at
    :func:`values_path` (see :func:`prepare_step`).
    """
    cfg = copy.deepcopy(dict(template))
    cfg["name"] = f"{cfg['name']}{ctx.name_suffix}"
    cfg["buffer"]["patient_file"] = str(ctx.cohort_parquet)
    cfg["buffer"]["buffer_m"] = ctx.buffer_m

    if step.is_c3 and _template_of(step) == C3_GRID_TEMPLATE:
        # A raster dataset: the uploaded GeoTIFF is the template grid. Any
        # dataset on this grid (this one, or another year of it) rendered the
        # same step, so the first raster of the first such dataset will do.
        definition = first_definition_for_grid(variables, definitions, step.name)
        cfg["source"]["file"] = definition["rasters"][0]["path"]
        cfg["buffer"]["grid_id_offset"] = 0
    elif step.is_c3:
        # The boundary templates are the shipped ones; source.file stays
        # relative so the pipeline CLI resolves it against --data-dir.
        # raster_res_m drives boundary_overlap_fast's rasterisation and is part
        # of the cache key, so it is injected exactly as the shipped polygon
        # recipes do, or this step would both mis-render and miss their weights.
        cfg["buffer"]["raster_res_m"] = ctx.raster_res_m
    else:
        var_key = step.name[len("c4_"):]
        definition = definition_for(definitions, var_key)
        c3_parquet = output_path(ctx, c3_step_for(definition).name)
        if is_raster(definition):
            values = values_path(ctx, var_key)
        else:
            values = definition["values_path"]

        cfg["linkage_pattern"] = (
            "yearly_areal" if definition.get("year_col") else "static_areal"
        )
        cfg["source"] = {
            "file": str(c3_parquet),
            "join_col": definition["join_col"],
        }
        exposure = {
            "file": str(values),
            "join_col": definition["key_col"],
            "value_cols": list(definition["value_cols"]),
        }
        if definition.get("year_col"):
            exposure["year_col"] = definition["year_col"]
        cfg["exposure"] = exposure

        years = definition.get("coverage_years") or list(_DEFAULT_YEARS)
        cfg.setdefault("time", {})
        cfg["time"]["years"] = list(range(int(years[0]), int(years[1]) + 1))
        cfg["time"]["temporal_resolution"] = "yearly"
        cfg["time"]["temporal_mode"] = "yearly"

    # Never optional: "patient" collapses a patient's episodes into one row and
    # the merge then cannot find geoid to rename to episode_id.
    cfg.setdefault("time", {})
    cfg["time"]["output_grouping"] = "episode"
    cfg["output"]["path"] = str(output_path(ctx, step.name))
    return cfg


def prepare_step(step: Any, ctx: RenderContext, *,
                 definitions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    """Run-time work a step needs before it renders; returns log records.

    Only a raster dataset's C4 step needs any: its value table is materialised
    from the C3 weights' grid ids (which must exist by then) into
    :func:`values_path`. Each record is ``{"level", "source", "msg"}``, the
    arguments of one web ``logs.jsonl`` line.
    """
    if step.is_c3:
        return []
    var_key = step.name[len("c4_"):]
    definition = definition_for(definitions, var_key)
    if not is_raster(definition):
        return []
    from spacescans.custom.values import materialise_values
    result = materialise_values(
        definition,
        c3_parquet=output_path(ctx, c3_step_for(definition).name),
        out_path=values_path(ctx, var_key),
    )
    return [{"level": "info", "source": step.name, "msg": result.message}]


# --------------------------------------------------------------------------
# C3 cache: tags and the web's legacy key
# --------------------------------------------------------------------------

def boundary_tag_for_step(step: Any) -> str | None:
    """The cache tag of a custom C3 step, by name; None for any other step."""
    if step.name.startswith(GRID_STEP_PREFIX):
        return GRID_TAG_PREFIX + step.name[len(GRID_STEP_PREFIX):]
    for _boundary, (name, _template, tag) in BOUNDARY_C3.items():
        if name == step.name:
            return tag
    return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def legacy_cache_key(input_parquet: str | Path, tag: str, *, buffer_m: Any,
                     raster_res_m: Any = None) -> str:
    """The web's C3 cache key (legacy-v1): ``<sha8>__<tag>__b<buffer>m[__r<res>m]``.

    Byte for byte the format the shipped polygon recipes use, so a custom
    Tract variable reuses FARA's cached weights. A grid tag (``CUSTOMGRID_``)
    gets no ``__r`` part: grid_weights does not rasterise the buffer, like the
    shipped grid sources; the grid hash in the tag keeps two grids apart.
    The uploaded values never enter the key: C4 is never cached.
    ``buffer_m``/``raster_res_m`` are formatted as given (270 vs 270.0 differ).
    """
    sha = _sha256_file(Path(input_parquet))
    if tag.startswith(GRID_TAG_PREFIX):
        return f"{sha[:8]}__{tag}__b{buffer_m}m"
    if raster_res_m is None:
        raise ValueError(f"cache tag {tag!r} needs raster_res_m")
    return f"{sha[:8]}__{tag}__b{buffer_m}m__r{raster_res_m}m"
