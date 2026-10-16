"""Per-experiment recipes: the pipeline steps each web runner runs, as data.

Ported from the 11 runner modules in spacescans-web ``backend/app/experiments``
(web commit 3991c3b): the step constants and ``plan()`` of each runner, the
fields its ``render_yaml`` fills, its C3 cache tag and meta fields, its merge
table (``_PARQUET_MAP``), the FAQSD column rename, the cohort-conversion
variant it uses, and the pipeline modules its start-up probe greps.
``plan(experiment, keys)`` returns the same step list, in the same order, as
the runner's ``plan({"variables": keys, ...})``.

Render contract
---------------
A renderer turns a :class:`Step` plus ``run_dir``, ``cohort_parquet``,
``buffer_m``, ``raster_res_m`` and ``name_suffix`` into a config dict;
``yaml.safe_dump(cfg, sort_keys=False)`` of it is byte-identical to the
``pipeline_configs/<step>.yaml`` the web runner writes (the web passes
``name_suffix = f"_task_{task_dir.name[-8:]}"``, ``run_dir = task_dir`` and
``cohort_parquet = task_dir / "input.parquet"``). In this order:

1. ``cfg = yaml.safe_load(template(step.template))``
2. ``cfg["name"] = cfg["name"] + name_suffix``
3. ``cfg["buffer"]["patient_file"] = str(cohort_parquet)``
4. ``cfg["buffer"]["buffer_m"] = buffer_m`` - on every step, including the
   TIGER and NHD steps whose templates have no ``buffer_m`` (it is appended)
5. if ``step.uses_raster_res``: ``cfg["buffer"]["raster_res_m"] = raster_res_m``
6. for each ``path, producer`` in ``step.bind``: set the dotted ``path`` to
   ``str(run_dir / "output" / f"{producer}.parquet")``. Every parent mapping
   exists in the shipped templates; a missing one is a render error (the web
   raised ``RuntimeError`` for a missing ``source``/``exposure``).
7. for each ``path, value`` in ``step.fill``: set the dotted ``path`` to a
   (deep) copy of ``value``, creating missing parent mappings
8. if ``step.time_block == "always"``: ``cfg.setdefault("time", {})``; then if
   ``"time" in cfg``: ``cfg["time"]["output_grouping"]`` is set from the run's
   ``output_level`` (:data:`OUTPUT_GROUPING`): ``"patient"`` for
   ``"participant"`` (the default), ``"episode"`` for ``"episode"``. A step
   whose output a later step reads per episode (``step.episode_rows``: the ZBP
   linkage the CBP fallback completes) is ``"episode"`` at both levels and,
   at the participant level, also gets ``contributing_days: true``
9. ``cfg["output"]["path"] = str(run_dir / "output" / f"{step.name}.parquet")``

Setting an existing key keeps its place and a new key is appended, which is
why the order matters. The web checks the C3 cache before rendering and
writes no YAML on a hit. With ``output_level="episode"`` the result is the
web 0.2.7 YAML byte for byte (gate A0); the participant level differs from it
only in the ``time`` block of C4 steps. :func:`render_config` implements these rules;
:func:`spacescans.orchestrate.render.render` applies it to a step and a
``RunContext``, and ``spacescans.custom.render`` to a custom step.

Legacy C3 cache (the web's ``c3_cache``)
----------------------------------------
Only ``kind == "c3"`` steps are cached, under :func:`legacy_cache_key`:
``f"{sha256(input.parquet)[:8]}__{cache_tag}__b{buffer_m}m"`` plus
``f"__r{raster_res_m}m"`` when ``uses_raster_res``. The ``.meta.json``
sidecar records ``boundary = step.meta_boundary`` (``"BG"`` for the
``BG2020`` step, as bg_ndi_wi writes it) and carries ``raster_res_m`` only
when ``step.meta_raster_res`` (bg_ndi_wi and zcta5_cbp).
``spacescans.orchestrate.cache.WeightCache(root, key_format="legacy-v1")``
reads and writes these entries.

Merge
-----
Each experiment's partial merge reads ``output/<recipe.output_for(key)>``
for its keys in request order, renames columns by ``recipe.renames`` first
(FAQSD; the web rewrites that parquet in place, ``recipe.rename_in_place``),
keeps the variable's ``value_cols`` and writes ``result_<experiment>.csv``.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class _FrozenMap(dict):
    """A read-only ``dict``: recipe data is shared module state.

    Still a real ``dict`` (JSON-serialisable, ``dataclasses.asdict`` and
    ``copy.deepcopy`` work); only the mutating methods are blocked.
    """

    __slots__ = ()

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("recipe data is read-only; copy it with dict(...) first")

    __setitem__ = __delitem__ = __ior__ = _readonly  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _readonly  # type: ignore[assignment]

    def __reduce__(self) -> tuple:
        return (type(self), (dict(self),))

    def __copy__(self) -> _FrozenMap:
        return self

    def __deepcopy__(self, memo: dict) -> _FrozenMap:
        return type(self)(copy.deepcopy(dict(self), memo))


_C3, _C4 = "c3", "c4"
_TIME_BLOCKS = ("if_present", "always")


@dataclass(frozen=True)
class Step:
    """One pipeline run (``spacescans run <config>``) within an experiment.

    ``name`` is the stem of ``output/<name>.parquet`` and
    ``pipeline_configs/<name>.yaml`` and the log ``source``; ``template`` is a
    path under the packaged templates (``"c3/bg_us_demo.yaml"``). See the
    module docstring for how ``bind``, ``fill`` and ``time_block`` render and
    how ``cache_tag``, ``uses_raster_res``, ``meta_boundary`` and
    ``meta_raster_res`` form the legacy cache entry.

    ``dataset`` names the custom variable whose definition a custom C4 step
    (or a raster grid C3 step) was built from. ``materialise_values`` marks a
    custom raster C4 step: ``output/values_<dataset>.parquet`` (bound to
    ``exposure.file``) must be written from the C3 weights before rendering,
    as the web's ``custom._materialise_raster_values`` does.

    ``episode_rows`` marks a C4 step whose output another C4 step of the same
    source reads per episode (the ZBP linkage that ``cbp_fallback``
    completes): it renders one row per episode at every output level, with
    each value's contributing days at the participant level (render rule 8).
    """

    name: str
    template: str
    kind: str
    cache_tag: str | None = None
    uses_raster_res: bool = False
    bind: Mapping[str, str] = field(default_factory=_FrozenMap)
    fill: Mapping[str, Any] = field(default_factory=_FrozenMap)
    time_block: str = "if_present"
    meta_boundary: str | None = None
    meta_raster_res: bool = False
    dataset: str | None = None
    materialise_values: bool = False
    episode_rows: bool = False

    def __post_init__(self) -> None:
        if self.kind not in (_C3, _C4):
            raise ValueError(f"step {self.name!r}: kind must be 'c3' or 'c4', not {self.kind!r}")
        if self.time_block not in _TIME_BLOCKS:
            raise ValueError(f"step {self.name!r}: time_block must be one of {_TIME_BLOCKS}")
        if self.kind == _C4 and (self.cache_tag or self.uses_raster_res or self.meta_boundary):
            raise ValueError(f"step {self.name!r}: C4 steps are never cached or rasterised")
        if self.kind == _C3 and not self.cache_tag:
            raise ValueError(f"step {self.name!r}: a C3 step needs a cache tag")
        object.__setattr__(self, "bind", _FrozenMap(self.bind))
        object.__setattr__(self, "fill", _FrozenMap(self.fill))

    def __hash__(self) -> int:  # bind/fill are dicts; equal steps share these
        return hash((self.name, self.template, self.kind, self.cache_tag))

    # The web's PipelineStep spells these is_c3 / template_relpath.
    @property
    def is_c3(self) -> bool:
        return self.kind == _C3

    @property
    def template_relpath(self) -> str:
        return self.template

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready form (for the bridge and for golden files)."""
        return {
            "name": self.name, "template": self.template, "kind": self.kind,
            "cache_tag": self.cache_tag, "uses_raster_res": self.uses_raster_res,
            "bind": dict(self.bind), "fill": copy.deepcopy(dict(self.fill)),
            "time_block": self.time_block, "meta_boundary": self.meta_boundary,
            "meta_raster_res": self.meta_raster_res, "dataset": self.dataset,
            "materialise_values": self.materialise_values, "episode_rows": self.episode_rows,
        }


@dataclass(frozen=True)
class Recipe:
    """Everything source-specific about one experiment (one web runner).

    ``steps`` holds every step the experiment can run, in run order; ``plan``
    keeps that order. ``needs`` lists, per variable, the steps it requires
    (shared C3 steps are listed by every variable that needs them and run
    once). ``outputs`` maps a variable to the parquet its partial merge reads.
    ``cohort_variant`` names the cohort conversion the runner uses
    (:data:`COHORT_STR_COLUMNS`). ``episode_grouping_modules`` are the
    pipeline modules whose source the runner greps for ``output_grouping``
    before running (to be replaced by ``spacescans.capabilities()``).

    The ``custom`` experiment is ``dynamic``: its steps come from the
    custom-exposome definitions passed to :func:`plan` (``steps`` holds only
    the four shared boundary C3 steps), and ``linkage_report`` asks for the
    per-column linkage-rate report the web logs before merging.
    """

    experiment: str
    steps: Mapping[str, Step]
    needs: Mapping[str, tuple[str, ...]]
    outputs: Mapping[str, str]
    renames: Mapping[str, str] = field(default_factory=_FrozenMap)
    rename_in_place: bool = False
    cohort_variant: str = "zcta5"
    episode_grouping_modules: tuple[str, ...] = ()
    linkage_report: bool = False
    dynamic: bool = False

    def __post_init__(self) -> None:
        for attr in ("steps", "needs", "outputs", "renames"):
            object.__setattr__(self, attr, _FrozenMap(getattr(self, attr)))
        if self.cohort_variant not in COHORT_STR_COLUMNS:
            raise ValueError(f"{self.experiment}: unknown cohort variant {self.cohort_variant!r}")
        for name, step in self.steps.items():
            if name != step.name:
                raise ValueError(f"{self.experiment}: step registered as {name!r} is {step.name!r}")
        for key, names in self.needs.items():
            unknown = [n for n in names if n not in self.steps]
            if unknown:
                raise ValueError(f"{self.experiment}.{key}: needs unknown step(s) {unknown}")
            if key not in self.outputs:
                raise ValueError(f"{self.experiment}.{key}: no output parquet")

    def __hash__(self) -> int:
        return hash(self.experiment)

    @property
    def variables(self) -> tuple[str, ...]:
        """The catalog keys this experiment accepts (empty for ``custom``)."""
        return tuple(self.needs)

    def output_for(self, key: str) -> str:
        """The parquet file name (under ``output/``) the merge reads for ``key``."""
        if self.dynamic:
            return f"c4_{key}.parquet"
        try:
            return self.outputs[key]
        except KeyError:
            raise KeyError(f"{key!r} is not a variable of experiment {self.experiment!r}") from None

    def output_map(self, keys: Sequence[str]) -> dict[str, str]:
        """``{key: parquet}`` for ``keys`` - the web's ``parquet_map``."""
        return {key: self.output_for(key) for key in _key_list(keys)}


# ---------------------------------------------------------------------------
# shared tables
# ---------------------------------------------------------------------------

#: Optional cohort columns read as ``str`` so leading zeros survive, per
#: cohort-conversion variant: bg_ndi_wi's ``csv_to_parquet`` (bg_ndi_wi.py:81)
#: and zcta5_cbp's (zcta5_cbp.py:90), which every other runner imports.
COHORT_STR_COLUMNS: Mapping[str, tuple[str, ...]] = _FrozenMap({
    "bg_ndi_wi": ("state_fips", "county_fips", "tract_geoid", "bg_geoid"),
    "zcta5": ("state_fips", "county_fips", "tract_geoid", "bg_geoid", "zcta5"),
})

CUSTOM_EXPERIMENT = "custom"
#: Boundary of a custom polygon dataset -> the shipped C3 step it reuses
#: (step name, template and cache tag are the shipped runners', so the C3
#: cache is shared with them). custom.py:70-75.
CUSTOM_BOUNDARY_C3: Mapping[str, str] = _FrozenMap({
    "Tract": "c3_tract_us",
    "BG": "c3_bg",
    "ZCTA5": "c3_zcta5",
    "County": "c3_county_us",
})
CUSTOM_C4_TEMPLATE = "c4/custom_areal.yaml"
CUSTOM_GRID_TEMPLATE = "c3/custom_grid.yaml"
CUSTOM_GRID_TAG_PREFIX = "CUSTOMGRID_"
CUSTOM_GRID_STEP_PREFIX = "c3_customgrid_"
#: Years a custom dataset without ``coverage_years`` is linked over.
CUSTOM_DEFAULT_YEARS = (2013, 2019)


def _c3(name: str, template: str, tag: str, *, raster: bool, meta_boundary: str | None = None,
        meta_raster_res: bool = False, time_block: str = "if_present") -> Step:
    return Step(name=name, template=template, kind=_C3, cache_tag=tag, uses_raster_res=raster,
                meta_boundary=meta_boundary or tag, meta_raster_res=meta_raster_res,
                time_block=time_block)


def _c4(name: str, template: str, *, episode_rows: bool = False, **bind: str) -> Step:
    # keyword "source__file" -> dotted path "source.file"
    return Step(name=name, template=template, kind=_C4, episode_rows=episode_rows,
                bind={k.replace("__", "."): v for k, v in bind.items()})


def _recipe(experiment: str, steps: Sequence[Step], needs: Mapping[str, Sequence[str]],
            outputs: Mapping[str, str], **kwargs: Any) -> Recipe:
    return Recipe(experiment=experiment, steps={s.name: s for s in steps},
                  needs={k: tuple(v) for k, v in needs.items()}, outputs=outputs, **kwargs)


def _pair(experiment: str, key: str, c3: Step, c4: Step, probe: str, **kwargs: Any) -> Recipe:
    """A runner with one variable, one C3 and one C4 (eight of the eleven)."""
    return _recipe(experiment, [c3, c4], {key: (c3.name, c4.name)},
                   {key: f"{c4.name}.parquet"},
                   episode_grouping_modules=(probe,), **kwargs)


_LINKAGE = "spacescans.linkage."
# The tract weights are one table for FARA, FAQSD and custom Tract datasets.
_TRACT = _c3("c3_tract_us", "c3/tract_us_demo.yaml", "TRACT_FARA", raster=True)

_RECIPE_LIST: tuple[Recipe, ...] = (
    # bg_ndi_wi.py: steps 41-50, plan() 55, render_yaml() 122, merge_results() 264,
    # _cache_key() 323, cache meta 585
    _recipe(
        "bg_ndi_wi",
        [
            _c3("c3_bg", "c3/bg_us_demo.yaml", "BG", raster=True, meta_raster_res=True),
            # NDI is dual-vintage: 2020-Census BG weights for exposure rows with
            # bg_vintage=2020. The cache meta of this step says "BG", not "BG2020".
            _c3("c3_bg_2020", "c3/bg_us_2020_demo.yaml", "BG2020", raster=True,
                meta_boundary="BG", meta_raster_res=True),
            _c4("c4_ndi", "c4/bg_ndi_demo.yaml",
                source__file="c3_bg", source_2020__file="c3_bg_2020"),
            _c4("c4_wi", "c4/bg_wi_demo.yaml", source__file="c3_bg"),
        ],
        {"ndi": ("c3_bg", "c3_bg_2020", "c4_ndi"), "walkability": ("c3_bg", "c4_wi")},
        {"ndi": "c4_ndi.parquet", "walkability": "c4_wi.parquet"},
        cohort_variant="bg_ndi_wi",
    ),
    # zcta5_cbp.py: steps 38-71, plan() 74, render_yaml() 115, _cache_key() 193,
    # merge_results() 226, cache meta 406
    _recipe(
        "zcta5_cbp",
        [
            _c3("c3_zcta5", "c3/zcta5_us_demo.yaml", "ZCTA5", raster=True, meta_raster_res=True),
            _c3("c3_county", "c3/county_us_demo.yaml", "COUNTY", raster=True,
                meta_raster_res=True),
            # ZBP per episode: the fallback below reads it per episode at both output
            # levels (with contributing days for the participant means).
            _c4("c4_zcta5_cbp", "c4/zcta5_cbp_demo.yaml", episode_rows=True,
                source__file="c3_zcta5"),
            # County CBP for episodes whose ZIP has no ZBP row; it reads the ZBP
            # C4 output as exposure.zbp_file (one C4 feeds another).
            _c4("c4_county_cbp", "c4/county_cbp_demo.yaml",
                source__file="c3_county", exposure__zbp_file="c4_zcta5_cbp"),
        ],
        {"cbp_zcta5": ("c3_zcta5", "c3_county", "c4_zcta5_cbp", "c4_county_cbp")},
        # the final value is the fallback's output
        {"cbp_zcta5": "c4_county_cbp.parquet"},
    ),
    # tiger_proximity.py: steps 46-62, plan() 84, render_yaml() 99, _cache_key() 163
    _pair("tiger_proximity", "tiger_proximity",
          _c3("c3_tiger_roads", "c3/tiger_roads_demo.yaml", "BG_TIGER2", raster=False),
          _c4("c4_tiger_roads", "c4/tiger_roads_demo.yaml", exposure__file="c3_tiger_roads"),
          _LINKAGE + "precomputed_areal_linkage"),
    # nhd_bluespace.py: steps 44-60, plan() 88, render_yaml() 103, _cache_key() 168
    _pair("nhd_bluespace", "nhd_bluespace",
          _c3("c3_nhd_bluespace", "c3/nhd_demo.yaml", "BG_NHD", raster=False),
          _c4("c4_nhd_bluespace", "c4/nhd_bluespace_demo.yaml",
              exposure__file="c3_nhd_bluespace"),
          _LINKAGE + "precomputed_static_linkage"),
    # noise.py: steps 53-69, plan() 93, render_yaml() 108, _cache_key() 176
    _pair("noise", "noise",
          _c3("c3_noise", "c3/noise_grid_demo.yaml", "BG_NOISE", raster=False),
          _c4("c4_noise", "c4/noise_demo.yaml", source__file="c3_noise"),
          _LINKAGE + "static_areal_linkage"),
    # vnl.py: steps 56-72, plan() 96, render_yaml() 111, _cache_key() 178; the tag is
    # BG_VNL1 since the grid_id_offset fix
    _pair("vnl", "vnl",
          _c3("c3_vnl", "c3/vnl_grid_demo.yaml", "BG_VNL1", raster=False),
          _c4("c4_vnl", "c4/vnl_demo.yaml", source__file="c3_vnl"),
          _LINKAGE + "gridded_linkage"),
    # temis.py: steps 53-69, plan() 93, render_yaml() 108, _cache_key() 176
    _pair("temis", "temis",
          _c3("c3_temis", "c3/temis_grid_demo.yaml", "BG_TEMIS", raster=False),
          _c4("c4_temis", "c4/temis_demo.yaml", source__file="c3_temis"),
          _LINKAGE + "gridded_linkage"),
    # fara_tract.py: steps 50-66, plan() 89, render_yaml() 104, _cache_key() 168
    _pair("fara_tract", "fara_tract", _TRACT,
          _c4("c4_tract_fara", "c4/tract_fara_demo.yaml", source__file="c3_tract_us"),
          _LINKAGE + "fara_linkage"),
    # acag.py: steps 53-69, plan() 91, render_yaml() 106, _cache_key() 174
    _pair("acag", "acag",
          _c3("c3_acag", "c3/acag_grid_demo.yaml", "BG_ACAG", raster=False),
          _c4("c4_acag", "c4/acag_demo.yaml", source__file="c3_acag"),
          _LINKAGE + "acag_linkage"),
    # faqsd.py: steps 52-68, plan() 90, render_yaml() 104, _cache_key() 168;
    # merge_results() 203 renames o3/pm25 by rewriting the C4 parquet in place
    _pair("faqsd", "faqsd", _TRACT,
          _c4("c4_tract_faqsd", "c4/tract_faqsd_demo.yaml", source__file="c3_tract_us"),
          "spacescans.plugins.readers.faqsd",
          renames={"o3": "faqsd_o3", "pm25": "faqsd_pm25"}, rename_in_place=True),
    # custom.py: BOUNDARY_C3 70, c3_step_for() 131, plan() 166, render_yaml() 188,
    # _cache_key() 349, _boundary_tag_for_step() 374; steps built per dataset by plan()
    _recipe(
        CUSTOM_EXPERIMENT,
        [
            _c3("c3_tract_us", "c3/tract_us_demo.yaml", "TRACT_FARA", raster=True,
                time_block="always"),
            _c3("c3_bg", "c3/bg_us_demo.yaml", "BG", raster=True, time_block="always"),
            _c3("c3_zcta5", "c3/zcta5_us_demo.yaml", "ZCTA5", raster=True, time_block="always"),
            _c3("c3_county_us", "c3/county_us_demo.yaml", "COUNTY", raster=True,
                time_block="always"),
        ],
        {}, {},
        episode_grouping_modules=(_LINKAGE + "static_areal_linkage",
                                  _LINKAGE + "yearly_areal_linkage"),
        linkage_report=True, dynamic=True,
    ),
)

_RECIPES: Mapping[str, Recipe] = _FrozenMap({r.experiment: r for r in _RECIPE_LIST})


def all_recipes() -> dict[str, Recipe]:
    """Every recipe by experiment: the 10 catalog experiments in catalog order, then ``custom``."""
    return dict(_RECIPES)


def recipe(experiment: str) -> Recipe:
    """The recipe of one experiment; ``KeyError`` when there is none."""
    try:
        return _RECIPES[experiment]
    except KeyError:
        raise KeyError(
            f"no recipe for experiment {experiment!r} (known: {sorted(_RECIPES)})"
        ) from None


# ---------------------------------------------------------------------------
# custom exposomes (custom.py: c3_step_for, c4_step_for, plan, render_yaml)
# ---------------------------------------------------------------------------

def is_raster(definition: Mapping[str, Any]) -> bool:
    """A raster custom dataset (one GeoTIFF grid) rather than a polygon one."""
    return definition.get("geometry") == "raster"


def custom_c3_step(definition: Mapping[str, Any] | str, *, dataset: str | None = None) -> Step:
    """The C3 step a custom dataset needs (the web's ``custom.c3_step_for``).

    A polygon dataset (or a bare boundary name) reuses the shipped boundary
    step; a raster dataset gets one step per distinct grid, named and tagged by
    the grid's hash, whose template grid is the dataset's first raster.
    """
    if isinstance(definition, str):
        definition = {"boundary": definition}
    if is_raster(definition):
        grid_hash = definition["grid_hash"]
        tag = f"{CUSTOM_GRID_TAG_PREFIX}{grid_hash}"
        return Step(
            name=f"{CUSTOM_GRID_STEP_PREFIX}{grid_hash}", template=CUSTOM_GRID_TEMPLATE,
            kind=_C3, cache_tag=tag, uses_raster_res=False,
            fill={"source.file": definition["rasters"][0]["path"],
                  "buffer.grid_id_offset": 0},
            time_block="always", meta_boundary=tag, meta_raster_res=False, dataset=dataset,
        )
    boundary = definition["boundary"]
    try:
        name = CUSTOM_BOUNDARY_C3[boundary]
    except KeyError:
        raise ValueError(
            f"custom exposome on unsupported boundary {boundary!r}; "
            f"expected one of {sorted(CUSTOM_BOUNDARY_C3)}"
        ) from None
    return _RECIPES[CUSTOM_EXPERIMENT].steps[name]


def custom_c4_step(key: str, definition: Mapping[str, Any]) -> Step:
    """The C4 step of one custom dataset (the C4 branch of custom.render_yaml, as data).

    Kinds the web's runner never had (daily and biweekly datasets, feature
    distances) come from :func:`spacescans.custom.steps.kind_c4_step`.
    """
    from spacescans.custom.steps import kind_c4_step  # lazy: spacescans.custom imports this module

    step = kind_c4_step(key, definition)
    if step is not None:
        return step
    c3 = custom_c3_step(definition)
    raster = is_raster(definition)
    year_col = definition.get("year_col")
    years = definition.get("coverage_years") or list(CUSTOM_DEFAULT_YEARS)
    bind = {"source.file": c3.name}
    fill: dict[str, Any] = {
        "linkage_pattern": "yearly_areal" if year_col else "static_areal",
        "source.join_col": definition["join_col"],
    }
    if raster:
        bind["exposure.file"] = f"values_{key}"
    else:
        fill["exposure.file"] = str(definition["values_path"])
    fill["exposure.join_col"] = definition["key_col"]
    fill["exposure.value_cols"] = list(definition["value_cols"])
    if year_col:
        fill["exposure.year_col"] = year_col
    fill["time.years"] = list(range(int(years[0]), int(years[1]) + 1))
    fill["time.temporal_resolution"] = "yearly"
    fill["time.temporal_mode"] = "yearly"
    return Step(name=f"c4_{key}", template=CUSTOM_C4_TEMPLATE, kind=_C4, bind=bind, fill=fill,
                time_block="always", dataset=key, materialise_values=raster)


def _custom_plan(keys: list[str], overlay: Mapping[str, Any] | None) -> list[Step]:
    definitions = overlay or {}
    if not isinstance(definitions, Mapping):
        raise ValueError("config.custom_variables is not a mapping")  # noqa: TRY004  (web parity)

    def definition(key: str) -> Mapping[str, Any]:
        try:
            return definitions[key]
        except KeyError:
            raise ValueError(
                f"{key!r} has no definition in config.custom_variables — the task "
                "config was written without resolving it"
            ) from None

    steps: list[Step] = []
    seen: set[str] = set()
    for key in keys:  # one C3 per distinct boundary or grid, first use first
        if definition(key).get("geometry") == "features":
            continue  # no C3: distances are computed in the run (spacescans.custom.features)
        c3 = custom_c3_step(definition(key), dataset=key)
        if c3.name not in seen:
            seen.add(c3.name)
            steps.append(c3)
    for key in keys:  # then one C4 per variable, in request order
        steps.append(custom_c4_step(key, definition(key)))
    return steps


# ---------------------------------------------------------------------------
# plan and cache key
# ---------------------------------------------------------------------------

def _key_list(keys: Sequence[str] | None) -> list[str]:
    if keys is None:
        return []
    if isinstance(keys, (str, bytes)):
        raise TypeError("keys must be a sequence of variable keys, not a single string")
    return list(keys)


def plan(experiment: str, keys: Sequence[str], *,
         overlay: Mapping[str, Any] | None = None) -> list[Step]:
    """The ordered steps that run ``keys`` (one experiment's share of a selection).

    Same list, order and errors as the web runner's ``plan()``: every C3 before
    the C4 steps that read it, a shared C3 once, NDI's extra 2020 block-group
    weights only when ``ndi`` is selected, C4 steps in the recipe's order
    whatever the request order, and zcta5_cbp's fixed four steps. For
    ``custom``, ``overlay`` holds the datasets' definitions (the task's
    ``custom_variables``); C4 steps then follow the request order.

    Raises ``ValueError`` for an empty selection or a key the experiment does
    not run, ``KeyError`` for an unknown experiment.
    """
    rec = recipe(experiment)
    keys = _key_list(keys)
    if not keys:
        raise ValueError("at least one variable must be selected")
    if rec.dynamic:
        return _custom_plan(keys, overlay)
    unknown = [k for k in keys if k not in rec.needs]
    if unknown:
        raise ValueError(f"unknown variable(s): {', '.join(unknown)}")
    wanted = {name for k in keys for name in rec.needs[k]}
    return [step for name, step in rec.steps.items() if name in wanted]


def legacy_cache_key(step: Step, input_sha256: str, *, buffer_m: Any, raster_res_m: Any) -> str:
    """The web's C3 cache key (``legacy-v1``) for ``step``.

    ``input_sha256`` is the hex sha256 of the task's ``input.parquet`` bytes;
    ``buffer_m``/``raster_res_m`` are formatted as given (the web formats the
    config's JSON values, so ``270`` and ``270.0`` give different keys).

    A thin wrapper: the format is defined once, in
    :func:`spacescans.orchestrate.cache.legacy_key` (``WeightCache("legacy-v1")``).
    """
    from spacescans.orchestrate.cache import legacy_key

    if not step.is_c3 or not step.cache_tag:
        raise ValueError(f"step {step.name!r} is not a cached C3 step")
    return legacy_key(step.cache_tag, input_sha256, buffer_m=buffer_m,
                      raster_res_m=raster_res_m, uses_raster_res=step.uses_raster_res)


# ---------------------------------------------------------------------------
# render (the contract in the module docstring, as code)
# ---------------------------------------------------------------------------

#: ``engine`` keys a renderer may set (EngineConfig's DuckDB resource caps).
ENGINE_KEYS: tuple[str, ...] = ("threads", "memory_limit", "temp_directory")

#: The output levels a link can return: one row per participant (the default)
#: or one row per location episode (an input row).
OUTPUT_LEVELS: tuple[str, ...] = ("participant", "episode")
DEFAULT_OUTPUT_LEVEL = "participant"
#: The C4 ``time.output_grouping`` each output level renders (render rule 8).
OUTPUT_GROUPING: Mapping[str, str] = _FrozenMap({"participant": "patient", "episode": "episode"})


def check_output_level(output_level: Any) -> str:
    """``output_level`` if it is one of :data:`OUTPUT_LEVELS`, else ``ValueError``."""
    if output_level not in OUTPUT_LEVELS:
        raise ValueError(f"output_level must be one of {', '.join(map(repr, OUTPUT_LEVELS))}, "
                         f"got {output_level!r}")
    return output_level


def _set_path(cfg: dict, dotted: str, value: Any, *, create: bool, step: Step) -> None:
    *parents, leaf = dotted.split(".")
    node = cfg
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            if not create:
                raise ValueError(f"step {step.name!r}: template {step.template!r} has no mapping "
                                 f"{part!r} for {dotted!r}")
            child = node[part] = {}
        node = child
    node[leaf] = value


def render_config(step: Step, template: Mapping[str, Any], *, run_dir: Any, cohort_parquet: Any,
                  buffer_m: Any, raster_res_m: Any, name_suffix: str = "",
                  engine: Mapping[str, Any] | None = None,
                  output_level: str = DEFAULT_OUTPUT_LEVEL) -> dict[str, Any]:
    """Apply the render contract (module docstring, rules 1-9) to a parsed template.

    Pure: ``template`` is not modified and nothing is read or written.
    ``yaml.safe_dump(result, sort_keys=False)`` is the web 0.2.7 YAML byte for
    byte when ``engine`` is None and ``output_level`` is ``"episode"``.
    ``engine`` (R only) sets ``engine.threads`` / ``memory_limit`` /
    ``temp_directory`` for every value that is not None. ``output_level``
    (``"participant"``, the default, or ``"episode"``) sets the C4 row
    (rule 8). Raises ``ValueError`` for an unknown ``output_level`` and when
    the template lacks a mapping a ``bind`` path needs (the web raised for a
    missing ``source``/``exposure``).
    """
    from pathlib import Path

    check_output_level(output_level)
    run_dir = Path(run_dir)
    cfg = copy.deepcopy(dict(template))
    for top in ("buffer", "output"):
        if not isinstance(cfg.get(top), dict):
            raise ValueError(  # noqa: TRY004  (a template error, like the bind check below)
                f"step {step.name!r}: template {step.template!r} has no {top!r} mapping")
    cfg["name"] = f"{cfg['name']}{name_suffix}"
    cfg["buffer"]["patient_file"] = str(cohort_parquet)
    cfg["buffer"]["buffer_m"] = buffer_m
    if step.uses_raster_res:
        cfg["buffer"]["raster_res_m"] = raster_res_m
    for path, producer in step.bind.items():
        _set_path(cfg, path, str(run_dir / "output" / f"{producer}.parquet"), create=False,
                  step=step)
    for path, value in step.fill.items():
        _set_path(cfg, path, copy.deepcopy(value), create=True, step=step)
    if step.time_block == "always":
        cfg.setdefault("time", {})
    if "time" in cfg:
        episode_rows = step.episode_rows or output_level == "episode"
        cfg["time"]["output_grouping"] = "episode" if episode_rows else OUTPUT_GROUPING[output_level]
        if step.episode_rows and output_level == "participant":
            cfg["time"]["contributing_days"] = True
    if engine:
        unknown = sorted(set(engine) - set(ENGINE_KEYS))
        if unknown:
            raise ValueError(f"unknown engine option(s) {unknown}; allowed: {list(ENGINE_KEYS)}")
        settings = {k: engine[k] for k in ENGINE_KEYS if engine.get(k) is not None}
        if settings:
            block = cfg.get("engine")
            if not isinstance(block, dict):
                block = cfg["engine"] = {}
            block.update(settings)
    cfg["output"]["path"] = str(run_dir / "output" / f"{step.name}.parquet")
    return cfg
