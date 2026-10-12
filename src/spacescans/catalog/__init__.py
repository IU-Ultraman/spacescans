"""spacescans.catalog - the exposure catalog the web app and the R package share.

What is in it (all package data; no exposure data):

* ``variables.json`` (+ ``variables.schema.json``): the 11 catalog variables;
  :func:`variables`, :func:`variable`, :func:`group`, :func:`experiments`.
* ``features.json``: the per-column feature dictionary; :func:`features`,
  :func:`feature_rows`, :func:`write_feature_csv`.
* ``templates/{c3,c4}``: the 25 pipeline YAML templates the runners render;
  :func:`templates`, :func:`template`.
* :mod:`spacescans.catalog.recipes`: each experiment's steps, cache tags,
  render fields and merge table as data; :func:`plan`, :func:`recipe`.

Everything here imports with the base install (no geo stack, no pandas).
The JSON files and templates are byte-for-byte copies of spacescans-web
(commit 3991c3b) so the web can switch to this package without changing a
result.
"""
from __future__ import annotations

from spacescans.catalog._features import (
    FEATURE_FIELDNAMES,
    Feature,
    feature_rows,
    features,
    write_feature_csv,
)
from spacescans.catalog._registry import (
    CatalogError,
    Variable,
    experiments,
    group,
    payload,
    schema,
    validate_payload,
    variable,
    variables,
)
from spacescans.catalog._templates import (
    template,
    template_bytes,
    template_hashes,
    template_text,
    templates,
    templates_dir,
)
from spacescans.catalog.recipes import (
    Recipe,
    Step,
    all_recipes,
    legacy_cache_key,
    plan,
    recipe,
)

__all__ = [
    "FEATURE_FIELDNAMES",
    "CatalogError",
    "Feature",
    "Recipe",
    "Step",
    "Variable",
    "all_recipes",
    "experiments",
    "feature_rows",
    "features",
    "group",
    "legacy_cache_key",
    "payload",
    "plan",
    "recipe",
    "schema",
    "template",
    "template_bytes",
    "template_hashes",
    "template_text",
    "templates",
    "templates_dir",
    "validate_payload",
    "variable",
    "variables",
    "write_feature_csv",
]
