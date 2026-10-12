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
    data.verify(["ndi"], data_dir="~/ss-data")

CLI: ``spacescans data status|fetch|verify|adopt``.
"""
from __future__ import annotations

from spacescans.data._adopt import AdoptReport, adopt
from spacescans.data._fetch import FetchPlan, FetchReport, fetch, plan
from spacescans.data._status import (
    USABLE,
    DatasetStatus,
    StatusReport,
    VariableStatus,
    status,
)
from spacescans.data._verify import VerifyReport, verify
from spacescans.data.errors import (
    DataCorruptError,
    DataError,
    DataMissingError,
    DiskSpaceError,
    LockedError,
    ManifestError,
    NetworkError,
    NotOnMirrorError,
    TermsRequiredError,
    UnsafeArchiveError,
    UsageError,
)
from spacescans.data.extract import (
    DATASET_DIRS,
    ExtractResult,
    extract_archive,
    extract_tar,
    is_macos_metadata,
    validate_member,
)
from spacescans.data.lock import DataLock
from spacescans.data.manifest import (
    MANIFEST_PATH,
    SCHEMA_PATH,
    Artifact,
    Dataset,
    Manifest,
    load_manifest,
)
from spacescans.data.paths import cloud_sync_warning, resolve_data_dir


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
    "DATASET_DIRS",
    "MANIFEST_PATH",
    "SCHEMA_PATH",
    "USABLE",
    "AdoptReport",
    "Artifact",
    "DataCorruptError",
    "DataError",
    "DataLock",
    "DataMissingError",
    "Dataset",
    "DatasetStatus",
    "DiskSpaceError",
    "ExtractResult",
    "FetchPlan",
    "FetchReport",
    "LockedError",
    "Manifest",
    "ManifestError",
    "NetworkError",
    "NotOnMirrorError",
    "StatusReport",
    "TermsRequiredError",
    "UnsafeArchiveError",
    "UsageError",
    "VariableStatus",
    "VerifyReport",
    "adopt",
    "boundary_datasets",
    "cloud_sync_warning",
    "extract_archive",
    "extract_tar",
    "fetch",
    "is_macos_metadata",
    "load_manifest",
    "plan",
    "resolve_data_dir",
    "status",
    "terms",
    "validate_member",
    "verify",
]
