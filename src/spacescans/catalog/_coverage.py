"""``catalog.coverage``: the pre-run coverage estimate, by catalog key (design section 2.1).

A thin wrapper over :func:`spacescans.orchestrate.compute_coverage` (the port of
the web's ``task_manager.compute_coverage``), which takes the variables'
catalog entries; this looks them up, custom overlay included. pandas is
imported only when it is called, so importing the catalog stays light.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any


def coverage(cohort: Any, keys: Sequence[str], *,
             overlay: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """How many cohort episodes each variable can cover, before linking.

    ``cohort`` is a table or a CSV path; ``overlay`` adds custom definitions.
    Returns the web's ``/coverage`` shape: ``{"row_count", "variables": {key:
    {"coverage_years", "patients_in_time_window", "patients_in_region",
    "patients_covered", "coverage_pct", "warnings", "boundary",
    "display_unit", "temporal"}}}``. The ``patients_*`` counts are episodes
    (cohort rows), as in the web. ``KeyError`` names unknown keys.
    """
    from spacescans.catalog._registry import variables
    from spacescans.orchestrate.coverage import compute_coverage

    if isinstance(keys, (str, bytes)):
        raise TypeError("keys must be a sequence of variable keys, not a single string")
    if not isinstance(cohort, (str, os.PathLike)):
        import pandas as pd

        if not isinstance(cohort, pd.DataFrame):
            raise TypeError("cohort must be a DataFrame or a CSV path")
    return compute_coverage(cohort, list(keys), variables(overlay=overlay))
