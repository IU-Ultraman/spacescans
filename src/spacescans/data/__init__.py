"""spacescans.data — get the exposure data into a data directory, and check it.

No data ships with spacescans: this module knows where each dataset comes
from (``manifest.json``), what a complete copy looks like on disk, and how to
download the project-mirrored parts resumably. Provider datasets (VNL, TEMIS,
ACAG, FAQSD) come with instructions and licence notes; their terms are never
accepted on the user's behalf.

Standard library only: importing this module (and calling ``status``) works
on a base install, without geopandas or rasterio.

    from spacescans import data
    data.status(["ndi", "walkability"], data_dir="~/ss-data")
    data.fetch(["ndi", "walkability"], data_dir="~/ss-data")

CLI: ``spacescans data status|fetch``.
"""
from __future__ import annotations

from spacescans.data.errors import (DataCorruptError, DataError, DataMissingError, DiskSpaceError,
                                    LockedError, ManifestError, NetworkError, NotOnMirrorError,
                                    TermsRequiredError, UnsafeArchiveError, UsageError)
from spacescans.data.extract import (DATASET_DIRS, ExtractResult, extract_archive, extract_tar,
                                     is_macos_metadata, validate_member)
from spacescans.data._fetch import FetchPlan, FetchReport, fetch, plan
from spacescans.data.lock import DataLock
from spacescans.data.manifest import (Artifact, Dataset, Manifest, load_manifest, MANIFEST_PATH,
                                      SCHEMA_PATH)
from spacescans.data.paths import cloud_sync_warning, resolve_data_dir
from spacescans.data._status import USABLE, DatasetStatus, StatusReport, VariableStatus, status


def terms(dataset: str, *, manifest=None) -> dict:
    """Licence, attribution, terms and instructions for one dataset."""
    from spacescans.data.manifest import coerce_manifest

    ds = coerce_manifest(manifest).dataset(dataset)
    return {"dataset": ds.key, "title": ds.title, "provider": ds.provider,
            "license": ds.license, "redistribution": ds.redistribution,
            "attribution": ds.attribution, "source_url": ds.source_url,
            "acquire": ds.acquire, "terms": ds.terms.to_dict() if ds.terms else None,
            "instructions": list(ds.instructions)}


def boundary_datasets(*, manifest=None) -> dict[str, str]:
    """Custom-upload boundary layer -> the dataset that provides its C3 shapes."""
    from spacescans.data.manifest import coerce_manifest

    return dict(coerce_manifest(manifest).boundaries)


__all__ = [
    "status", "plan", "fetch", "terms", "boundary_datasets", "resolve_data_dir",
    "load_manifest", "extract_archive", "extract_tar", "is_macos_metadata", "validate_member",
    "cloud_sync_warning",
    "StatusReport", "DatasetStatus", "VariableStatus", "FetchPlan", "FetchReport",
    "ExtractResult", "Manifest", "Artifact", "Dataset", "DataLock",
    "USABLE", "DATASET_DIRS", "MANIFEST_PATH", "SCHEMA_PATH",
    "DataError", "UsageError", "ManifestError", "DataMissingError", "NotOnMirrorError",
    "DataCorruptError", "UnsafeArchiveError", "TermsRequiredError", "NetworkError",
    "DiskSpaceError", "LockedError",
]
