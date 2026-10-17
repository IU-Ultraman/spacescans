"""Refuse uploaded files that GDAL/OGR would read as a VRT (format confusion).

An uploaded dataset is validated by its file extension and then opened by its content with
every GDAL/OGR driver enabled. GDAL's ``VRT`` (raster) and OGR's ``OGR_VRT`` (vector) drivers
follow a ``<SourceFilename>`` / ``<SrcDataSource>`` to an arbitrary local path or a
``/vsicurl/<url>``. So a file named ``.tif`` / ``.gpkg`` / ``.geojson`` / ``.zip`` whose bytes
are VRT XML would be read as a VRT: the referenced server file's values flow into the exposure
column the uploader downloads (arbitrary local file read), or the server makes an outbound
request to an attacker-chosen URL (SSRF). Review finding R2-1.

Defence: reject any file whose header holds a VRT marker, and (for a zipped Shapefile) any zip
that carries a ``.vrt`` / ``.ovf`` member or VRT bytes in a member. Raster opens additionally
pin the ``GTiff`` driver (see :func:`spacescans.custom.raster._open`). This is enforced in the
package, which both the package API and the web backend use to validate and read uploads.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

#: Lower-cased XML markers of a GDAL raster VRT or an OGR vector VRT.
_VRT_MARKERS = (b"<vrtdataset", b"<ogrvrtdatasource", b"<ogrvrtlayer", b"<ogrvrtunionlayer")
_SNIFF_BYTES = 1 << 16
_MSG = ("the file is a GDAL/OGR VRT (virtual dataset), which can make the server read other "
        "files or make network requests; upload the data itself — a GeoTIFF, GeoPackage, "
        "GeoJSON or zipped Shapefile — not a VRT")


def _has_marker(blob: bytes) -> bool:
    low = blob.lower()
    return any(m in low for m in _VRT_MARKERS)


def _head(source: bytes | bytearray | memoryview | str | Path) -> bytes | None:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:_SNIFF_BYTES])
    try:
        with open(source, "rb") as fh:
            return fh.read(_SNIFF_BYTES)
    except OSError:
        return None


def reject_format_confusion(source: Any, error_cls: type[Exception]) -> None:
    """Raise ``error_cls`` if ``source`` (bytes or a path) is, or hides, a GDAL/OGR VRT."""
    head = _head(source)
    if head is not None and _has_marker(head):
        raise error_cls(_MSG)
    # A zipped Shapefile is a container: GDAL may pick a .vrt inside it over the .shp.
    if isinstance(source, (str, Path)) and str(source).lower().endswith(".zip"):
        _reject_vrt_in_zip(source, error_cls)


def _reject_vrt_in_zip(path: str | Path, error_cls: type[Exception]) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if any(n.lower().endswith((".vrt", ".ovf")) for n in names):
                raise error_cls(_MSG)
            for name in names:
                if name.endswith("/"):
                    continue
                try:
                    with zf.open(name) as member:
                        if _has_marker(member.read(_SNIFF_BYTES)):
                            raise error_cls(_MSG)
                except (OSError, zipfile.BadZipFile, RuntimeError):
                    continue
    except (zipfile.BadZipFile, OSError):
        return   # not a readable zip; the reader will reject it with its own message
