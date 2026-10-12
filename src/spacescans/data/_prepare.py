"""``prepare_templates()``: regenerate the two grid template rasters locally.

Both rasters are grids only (every cell 0); the C3 ``grid_weights`` step
reads their geometry, never their values:

* ``ACAG/C3/acag_template.tif``: ACAG V5.NA.05's North America grid,
  11,800 x 5,400 cells of 0.01 degree, edges lon -170..-52, lat 68..14,
  uint8, LZW, 512 x 512 tiles. Written north-up because exactextract rejects
  a bottom-up transform; the ACAG reader flips its ascending-latitude arrays
  to match. A port of spacescans-web ``backend/scripts/make_acag_template.py``.
* ``TEMIS/C3/temis_template.tif``: the TEMIS 0.25 degree global grid,
  1,440 x 720 cells, int16, uncompressed, band interleave: the sanitised
  template of spacescans-web commit bba55fa (same geometry as the original,
  values zeroed so no TEMIS data is redistributed).

The output must equal the manifest's sha256 byte for byte (rasterio 1.5 /
GDAL 3.12 reproduce both). A file that does not match is not placed: status
would report it as corrupt. On success a receipt (origin ``prepare``) is
written, so ``status()`` reports the template as ready. Needs the ``geo``
extra (rasterio, numpy).
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data._status import StatusReport, sha256_file, status
from spacescans.data.errors import DataCorruptError, UsageError
from spacescans.data.extract import place_file
from spacescans.data.lock import DataLock
from spacescans.data.manifest import coerce_manifest
from spacescans.data.paths import resolve_data_dir, state_dir
from spacescans.data.receipts import write_receipt

EventFn = Callable[[dict], None]


@dataclass(frozen=True)
class TemplateSpec:
    width: int
    height: int
    dtype: str
    transform: tuple[float, float, float, float, float, float]   # Affine(a, b, c, d, e, f)
    options: dict[str, Any]
    rows_per_write: int | None = None    # stream zeros in row blocks (None: one write)
    crs: str = "EPSG:4326"


TEMPLATES: dict[str, TemplateSpec] = {
    "acag_template.tif": TemplateSpec(
        width=11_800, height=5_400, dtype="uint8",
        transform=(0.01, 0.0, -170.0, 0.0, -0.01, 68.0),
        options={"compress": "lzw", "tiled": True, "blockxsize": 512, "blockysize": 512},
        rows_per_write=512),
    "temis_template.tif": TemplateSpec(
        width=1_440, height=720, dtype="int16",
        transform=(0.25, 0.0, -180.0, 0.0, -0.25, 90.0),
        options={"interleave": "band"}),
}


def write_template(name: str, path: str | os.PathLike[str]) -> Path:
    """Write template ``name`` (a key of :data:`TEMPLATES`) to ``path``."""
    from spacescans._extras import require

    require("geo", "rasterio", "numpy")
    import numpy as np
    import rasterio
    from rasterio.transform import Affine

    spec = TEMPLATES[name]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out, "w", driver="GTiff", height=spec.height, width=spec.width, count=1,
                       dtype=spec.dtype, crs=spec.crs, transform=Affine(*spec.transform),
                       **spec.options) as dst:
        step = spec.rows_per_write or spec.height
        for row0 in range(0, spec.height, step):   # stream zeros; never hold 63 MB
            h = min(step, spec.height - row0)
            dst.write(np.zeros((h, spec.width), dtype=spec.dtype), 1,
                      window=((row0, row0 + h), (0, spec.width)))
    return out


@dataclass
class PreparedItem:
    name: str
    target: str
    result: str           # generated | present | mismatch
    sha256: str = ""
    bytes: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "target": self.target, "result": self.result,
                "sha256": self.sha256, "bytes": self.bytes, "detail": self.detail}


@dataclass
class PrepareReport:
    data_dir: str
    items: list[PreparedItem] = field(default_factory=list)
    status: StatusReport | None = None

    @property
    def ok(self) -> bool:
        return not any(i.result == "mismatch" for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"data_dir": self.data_dir, "ok": self.ok, "items": [i.to_dict() for i in self.items],
                "status": self.status.to_dict() if self.status is not None else None}


def _gdal_versions() -> str:
    try:
        import rasterio
        return f"rasterio {rasterio.__version__}, GDAL {rasterio.__gdal_version__}"
    except (ImportError, AttributeError):  # pragma: no cover - rasterio was importable a moment ago
        return "unknown GDAL"


def prepare_templates(data_dir: str | os.PathLike[str] | None = None, *,
                      names: Sequence[str] | None = None, force: bool = False,
                      manifest: Any = None, on_event: EventFn | None = None) -> PrepareReport:
    """Generate the template rasters into the data directory (see module docstring).

    ``names`` limits it to some of the manifest's ``generate: templates``
    artifacts. A template already in place with the right sha256 is left
    alone unless ``force``. Raises :class:`DataCorruptError` (with
    ``.report``) when a generated file does not match its sha256, and
    :class:`spacescans._extras.MissingExtraError` without the geo extra.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    emit = on_event or (lambda e: None)
    arts = [a for a in man.artifacts.values() if a.generate == "templates"]
    if names is not None:
        unknown = [n for n in names if n not in {a.name for a in arts}]
        if unknown:
            raise UsageError(f"not a template spacescans can generate: {unknown}; "
                             f"known: {[a.name for a in arts]}",
                             details={"unknown": unknown, "known": [a.name for a in arts]})
        arts = [a for a in arts if a.name in names]
    missing_spec = [a.name for a in arts if a.name not in TEMPLATES]
    if missing_spec:  # pragma: no cover - a manifest/package mismatch
        raise UsageError(f"no generator for {missing_spec}")
    report = PrepareReport(data_dir=str(root))
    root.mkdir(parents=True, exist_ok=True)
    tmp = state_dir(root) / "tmp" / f"prepare-{os.getpid()}"
    with DataLock(root, purpose="prepare"):
        try:
            for art in arts:
                dest = root / art.relpath
                if not force and dest.is_file() and dest.stat().st_size == art.bytes \
                        and sha256_file(dest) == art.sha256:
                    report.items.append(PreparedItem(art.name, art.relpath, "present",
                                                     art.sha256, art.bytes))
                    continue
                emit({"ev": "prepare", "artifact": art.name, "target": art.relpath})
                out = write_template(art.name, tmp / art.name)
                sha, size = sha256_file(out), out.stat().st_size
                if sha != art.sha256 or size != art.bytes:
                    report.items.append(PreparedItem(
                        art.name, art.relpath, "mismatch", sha, size,
                        f"generated {size:,} bytes, sha256 {sha[:16]}...; the manifest has "
                        f"{art.bytes:,} bytes, {art.sha256[:16]}... ({_gdal_versions()}). "
                        "Not placed; fetch it from the mirror instead"))
                    continue
                place_file(out, dest)
                write_receipt(root, art, files=[(art.relpath, size, sha)],
                              source="spacescans data prepare templates", origin="prepare",
                              data_release=man.data_release)
                report.items.append(PreparedItem(art.name, art.relpath, "generated", sha, size))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                tmp.parent.rmdir()
            except OSError:
                pass
    report.status = status(datasets=list(dict.fromkeys(
        k for a in arts for k in man.datasets_of_artifact(a.name))), data_dir=root, manifest=man)
    if not report.ok:
        bad = [i for i in report.items if i.result == "mismatch"]
        err = DataCorruptError(
            "generated template(s) differ from the published bytes: "
            + "; ".join(f"{i.name}: {i.detail}" for i in bad),
            hint="fetch the templates from the mirror (spacescans data fetch --dataset acag_template "
                 "--dataset temis_template) or copy them from a spacescans-web checkout",
            details={"mismatch": [i.to_dict() for i in bad]})
        err.report = report  # type: ignore[attr-defined]
        raise err
    return report
