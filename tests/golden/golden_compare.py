"""How parity tests compare new outputs with the tier-2 golden references.

The goldens under tests/golden/web-0.2.7/ were produced by the 0.2.7 web runners (see
README.md). Every track that checks a new implementation against them should use the helpers
here, so there is one definition of "the same output".

Rules
-----
* **Row order is not part of the contract.** Several runners write their per-experiment
  partial CSV in DuckDB hash order (noise, vnl, custom, ...; `_merge.write_partial` keeps the
  C4 row order). Both sides are therefore put in canonical order first: rows sorted by
  ``int(episode_id)``, ties broken by the remaining columns' raw text from left to right.
  The header and the column order are left alone, and each row keeps its original text byte
  for byte. ``canonical_result_csv`` implements this; ``result_<exp>.sorted.csv`` in each
  golden directory is its output.
* **Keys and shape are exact.** Column names and order, the set of (pid, episode_id) rows,
  every non-numeric column and the missing-value mask must match exactly.
* **Numeric values are compared with a tolerance** that depends on where the new side ran:
  ``SAME_ENV`` (the frozen image spacescans-web-backend:frozen-2026-10-12) or ``CROSS_ENV``
  (another environment, for example the macOS uv environment the R package installs).
  A value passes when ``abs(a - b) <= atol + rtol * abs(b)`` (numpy.isclose), b being the
  golden value.
* ``result.csv`` and ``feature_dictionary.csv`` follow the input row order (left join on the
  cohort), so they are compared as written, with the same per-cell rules.

pandas and numpy are needed only by the frame comparisons; the canonical sort is stdlib only.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SORT_RULE = ("rows sorted by int(episode_id), ties broken by the remaining columns' raw text "
             "from left to right; header and column order unchanged; each row's original text "
             "kept byte for byte")

KEY_COLUMNS = ("pid", "PATID", "episode_id", "geoid", "grid_id", "GEOID10", "GEOID", "year")


@dataclass(frozen=True)
class Tolerance:
    rtol: float
    atol: float
    why: str


# Same image, same data root. web-0.2.7/determinism.json measures how much two cold passes of
# the 0.2.7 runners differ, which is the floor any reimplementation can be held to. The slack
# is for multi-threaded DuckDB sums, whose order may differ between engines (design section 6,
# A4: accept rel <= 1e-12 once the difference is traced to summation order).
SAME_ENV = Tolerance(rtol=1e-12, atol=1e-12,
                     why="same frozen image and data: exact expected; slack covers summation order")
# Another environment (different GDAL/GEOS/PROJ, pandas, DuckDB builds): design section 6, gate B.
CROSS_ENV = Tolerance(rtol=1e-9, atol=1e-9,
                      why="different native libraries: design gate B (weights abs 1e-9, values rel 1e-9)")


# ---------------------------------------------------------------------------
# locating the goldens, and the A0 YAML normalisation (stdlib only)
# ---------------------------------------------------------------------------

def golden_root() -> Path:
    """tests/golden/web-0.2.7, or $SPACESCANS_GOLDEN_DIR (e.g. a fresh regeneration)."""
    return Path(os.environ.get("SPACESCANS_GOLDEN_DIR", Path(__file__).resolve().parent / "web-0.2.7"))


def load_manifest(root: Path | None = None) -> dict:
    return json.loads(((root or golden_root()) / "manifest.json").read_text())


def normalise_yaml(text: str, task_dir: str) -> str:
    """Gate A0: replace the task-dir prefix with <TASK> and the `_task_<last 8 chars>` name
    suffix with `_task_<S>`; what remains must be byte-identical."""
    return re.sub(r"_task_\S{8}$", "_task_<S>", text.replace(task_dir, "<TASK>"), flags=re.M)


# ---------------------------------------------------------------------------
# canonical row order (stdlib only)
# ---------------------------------------------------------------------------

def _records(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        raise ValueError("empty CSV")
    header, body = lines[0], lines[1:]
    # One physical line per record: refuse quoted fields that span lines.
    for i, line in enumerate(body, start=2):
        if len(list(csv.reader([line]))) != 1:
            raise ValueError(f"line {i} is not a single CSV record")
    return header, body


def canonical_result_csv(text: str, *, key: str = "episode_id") -> str:
    """Return ``text`` with its data rows in canonical order (see ``SORT_RULE``)."""
    header, body = _records(text)
    cols = next(csv.reader([header]))
    if key not in cols:
        raise ValueError(f"no {key!r} column in {cols}")
    k = cols.index(key)
    if body and not body[-1].endswith(("\n", "\r")):
        body[-1] += "\n"                       # a moved last line must still end its record

    def sort_key(line: str):
        fields = next(csv.reader([line]))
        return (int(fields[k]), [f for j, f in enumerate(fields) if j != k])

    return header + "".join(sorted(body, key=sort_key))


def canonical_result_file(src: Path, dst: Path) -> Path:
    dst.write_bytes(canonical_result_csv(src.read_bytes().decode("utf-8")).encode("utf-8"))
    return dst


# ---------------------------------------------------------------------------
# frame comparisons (pandas / numpy)
# ---------------------------------------------------------------------------

def read_text_frame(path: Path):
    """Read a result CSV keeping every cell as text ("" for missing)."""
    import pandas as pd
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _as_float(series):
    import numpy as np
    import pandas as pd
    s = series.replace("", np.nan) if series.dtype == object else series
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)


def _numeric_text(series) -> bool:
    import pandas as pd
    s = series[series != ""]
    if s.empty:
        return False
    return bool(pd.to_numeric(s, errors="coerce").notna().all())


def compare_text_frames(expected, actual, *, tol: Tolerance = SAME_ENV,
                        keys: Sequence[str] = ("pid", "episode_id")) -> list[str]:
    """Compare two text frames row by row; return the list of problems (empty = equal).

    Both frames must already be in the same row order (canonical for partials, input order
    for result.csv).
    """
    import numpy as np
    problems: list[str] = []
    if list(expected.columns) != list(actual.columns):
        return [f"columns differ: expected {list(expected.columns)}, got {list(actual.columns)}"]
    if len(expected) != len(actual):
        return [f"row count differs: expected {len(expected)}, got {len(actual)}"]
    for col in expected.columns:
        e, a = expected[col].reset_index(drop=True), actual[col].reset_index(drop=True)
        if col in keys or not (_numeric_text(e) or _numeric_text(a)):
            bad = (e != a).to_numpy()
            if bad.any():
                i = int(np.flatnonzero(bad)[0])
                problems.append(f"{col}: {int(bad.sum())} cell(s) differ, first at row {i}: "
                                f"{e.iloc[i]!r} != {a.iloc[i]!r}")
            continue
        ef, af = _as_float(e), _as_float(a)
        en, an = np.isnan(ef), np.isnan(af)
        if (en != an).any():
            i = int(np.flatnonzero(en != an)[0])
            problems.append(f"{col}: missing-value mask differs ({int((en != an).sum())} cell(s)), "
                            f"first at row {i}: {e.iloc[i]!r} vs {a.iloc[i]!r}")
            continue
        ok = np.isclose(af, ef, rtol=tol.rtol, atol=tol.atol, equal_nan=True)
        if not ok.all():
            d = np.abs(af - ef)
            i = int(np.nanargmax(np.where(ok, -np.inf, d)))
            problems.append(f"{col}: {int((~ok).sum())} value(s) outside rtol={tol.rtol} "
                            f"atol={tol.atol}; worst row {i}: {ef[i]!r} vs {af[i]!r}")
    return problems


def compare_result_csv(expected_path: Path, actual_path: Path, *, tol: Tolerance = SAME_ENV,
                       canonical: bool = True) -> list[str]:
    """Compare a new result CSV with a golden one. ``canonical`` sorts both sides first
    (per-experiment partials); pass False for result.csv, which follows input order."""
    import pandas as pd
    e_text = expected_path.read_text()
    a_text = actual_path.read_text()
    if canonical:
        e_text, a_text = canonical_result_csv(e_text), canonical_result_csv(a_text)
    if e_text == a_text:
        return []
    e = pd.read_csv(io.StringIO(e_text), dtype=str, keep_default_na=False)
    a = pd.read_csv(io.StringIO(a_text), dtype=str, keep_default_na=False)
    return compare_text_frames(e, a, tol=tol)


def canonical_table(df):
    """Sort a C3/C4 table into canonical order: non-float columns first (in column order),
    then float columns, all ascending, missing values last."""
    import pandas as pd
    floats = [c for c in df.columns if pd.api.types.is_float_dtype(df[c])]
    others = [c for c in df.columns if c not in floats]
    return df.sort_values(others + floats, kind="mergesort", na_position="last",
                          ignore_index=True)


def compare_tables(expected, actual, *, tol: Tolerance = SAME_ENV) -> list[str]:
    """Compare two C3/C4 parquet tables (as DataFrames) after ``canonical_table``."""
    import numpy as np
    import pandas as pd
    if list(expected.columns) != list(actual.columns):
        return [f"columns differ: expected {list(expected.columns)}, got {list(actual.columns)}"]
    if len(expected) != len(actual):
        return [f"row count differs: expected {len(expected)}, got {len(actual)}"]
    e, a = canonical_table(expected), canonical_table(actual)
    problems = []
    for col in e.columns:
        if pd.api.types.is_float_dtype(e[col]) and pd.api.types.is_float_dtype(a[col]):
            ef, af = e[col].to_numpy(float), a[col].to_numpy(float)
            if (np.isnan(ef) != np.isnan(af)).any():
                problems.append(f"{col}: missing-value mask differs")
                continue
            ok = np.isclose(af, ef, rtol=tol.rtol, atol=tol.atol, equal_nan=True)
            if not ok.all():
                problems.append(f"{col}: {int((~ok).sum())} value(s) outside tolerance; "
                                f"max abs diff {float(np.nanmax(np.abs(af - ef))):.3e}")
        else:
            es = e[col].astype(str).to_numpy()
            as_ = a[col].astype(str).to_numpy()
            if (es != as_).any():
                problems.append(f"{col}: {int((es != as_).sum())} cell(s) differ")
    return problems


def max_diffs(expected, actual, columns: Iterable[str]) -> dict[str, dict]:
    """Max absolute and relative difference per float column (aligned frames)."""
    import numpy as np
    out = {}
    for col in columns:
        ef = np.asarray(expected[col], dtype=float)
        af = np.asarray(actual[col], dtype=float)
        both = ~np.isnan(ef) & ~np.isnan(af)
        d = np.abs(af - ef)[both]
        denom = np.maximum(np.abs(ef[both]), np.finfo(float).tiny)
        out[col] = {
            "cells": int(both.sum()),
            "differing": int((d != 0).sum()),
            "max_abs": float(d.max()) if d.size else 0.0,
            "max_rel": float((d / denom).max()) if d.size else 0.0,
            "nan_mask_equal": bool((np.isnan(ef) == np.isnan(af)).all()),
        }
        if math.isnan(out[col]["max_abs"]):
            out[col]["max_abs"] = None
    return out
