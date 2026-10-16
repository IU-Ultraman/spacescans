"""What a link ran with: versions, fingerprints, data receipts, cache keys, settings.

:func:`build` returns the ``provenance`` dict of a
:class:`~spacescans.orchestrate.link.LinkResult` (the bridge writes it to
``provenance.json``). It is meant for a methods section or an audit: enough to
say which code, native libraries, data release and data files produced a
result, and which cached weights were reused.

It never holds cohort values. The cohort appears only as counts (episodes,
patients) and the sha256 of the pipeline input file, whose first 8 hex digits
already name the web's legacy cache entries. Custom datasets appear by key,
geometry, boundary or grid hash and their recorded sha256, not by path.
"""
from __future__ import annotations

import importlib.metadata
import platform
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["LIBRARIES", "SCHEMA", "build", "data_receipts", "library_versions"]

SCHEMA = 1
#: Python distributions whose versions are recorded (None when not installed).
LIBRARIES: tuple[str, ...] = ("pandas", "numpy", "pyarrow", "duckdb", "pydantic", "pyyaml",
                              "geopandas", "shapely", "pyproj", "rasterio", "exactextract",
                              "pyogrio", "fiona", "netCDF4", "pyhdf", "pyreadr")


def library_versions(names: Sequence[str] = LIBRARIES) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in names:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def data_receipts(datasets: Sequence[str], data_dir: Path) -> dict[str, dict[str, Any]]:
    """``{dataset: {artifact: sha256 | None}}`` from the data dir's download receipts."""
    try:
        from spacescans.data import load_manifest
        from spacescans.data.receipts import read_receipt

        manifest = load_manifest()
    except Exception:  # noqa: BLE001 - provenance never fails a run
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key in datasets:
        ds = manifest.datasets.get(key)
        if ds is None:
            continue
        arts: dict[str, Any] = {}
        for name in ds.artifacts:
            try:
                rec = read_receipt(data_dir, name)
            except Exception:  # noqa: BLE001
                rec = None
            arts[name] = rec.get("sha256") if isinstance(rec, Mapping) else None
        out[key] = arts
    return out


def _custom_entry(definition: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"geometry": definition.get("geometry"),
                             "value_cols": list(definition.get("value_cols") or []),
                             "sha256": definition.get("sha256")}
    if definition.get("geometry") == "raster":
        entry["grid_hash"] = definition.get("grid_hash")
        entry["rasters"] = [{"year": r.get("year"), "sha256": r.get("sha256")}
                            for r in definition.get("rasters") or []]
    else:
        entry["boundary"] = definition.get("boundary")
        entry["year_col"] = definition.get("year_col")
    return entry


def _code() -> dict[str, Any]:
    import spacescans
    from spacescans._capabilities import code_fingerprint
    from spacescans.orchestrate.cache import step_code_fingerprint

    try:
        code_fp = code_fingerprint()
    except Exception:  # noqa: BLE001
        code_fp = None
    try:
        step_fp = step_code_fingerprint()
    except Exception:  # noqa: BLE001
        step_fp = None
    return {"version": spacescans.__version__, "code_fp": code_fp, "step_code_fp": step_fp}


def _native() -> dict[str, Any]:
    try:
        from spacescans.orchestrate.cache import native_versions

        return dict(native_versions())
    except Exception:  # noqa: BLE001
        return {}


def build(*, ctx: Any, groups: Mapping[str, Sequence[str]], overlay: Mapping[str, Any],
          steps: Sequence[Any], cache: Any, executor: str, threads: int | None,
          memory_limit: str | None, on_error: str, data_summary: Mapping[str, Any],
          cohort_info: Any, started: datetime, ended: datetime, seconds: float,
          failed: Sequence[str], skipped: Sequence[str], columns: Sequence[str],
          output_level: str = "episode", rows_without_pid: int = 0) -> dict[str, Any]:
    """The provenance of one link (see the module docstring).

    ``run.output_level`` records what one result row is: ``"participant"`` or
    ``"episode"``. ``cohort.rows_without_pid`` counts the input rows with a
    blank ``pid``, which a participant-level table leaves out.
    """
    data_dir = Path(ctx.data_dir)
    datasets = sorted({d for v in (data_summary.get("variables") or {}).values()
                       for d in v.get("datasets", [])} | set(data_summary.get("datasets") or {}))
    step_dicts = [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in steps]
    return {
        "schema": SCHEMA,
        "spacescans": _code(),
        "python": {"version": platform.python_version(), "implementation":
                   platform.python_implementation(), "executable": sys.executable},
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(), "platform": platform.platform()},
        "libraries": library_versions(),
        "native": _native(),
        "data": {"data_dir": str(data_dir), "data_release": data_summary.get("data_release"),
                 "variables": dict(data_summary.get("variables") or {}),
                 "datasets": dict(data_summary.get("datasets") or {}),
                 "receipts": data_receipts(datasets, data_dir)},
        "run": {"variables": [k for ks in groups.values() for k in ks],
                "experiments": {exp: list(ks) for exp, ks in groups.items()},
                "output_level": output_level,
                "buffer_m": ctx.buffer_m, "raster_res_m": ctx.raster_res_m,
                "threads": threads, "memory_limit": memory_limit, "executor": executor,
                "on_error": on_error, "run_dir": str(ctx.run_dir),
                "started_utc": started.isoformat(), "ended_utc": ended.isoformat(),
                "seconds": round(float(seconds), 3),
                "failed": list(failed), "skipped": list(skipped), "columns": list(columns)},
        "cache": (None if cache is None else
                  {"key_format": cache.key_format, "root": str(cache.root),
                   "keys": {s["name"]: s["cache_key"] for s in step_dicts if s.get("cache_key")}}),
        "cohort": {"episodes": cohort_info.rows, "patients": cohort_info.patients,
                   "rows_without_pid": int(rows_without_pid),
                   "input_sha256": cohort_info.sha256},
        "custom": {k: _custom_entry(d) for k, d in (overlay or {}).items()},
        "steps": step_dicts,
    }
