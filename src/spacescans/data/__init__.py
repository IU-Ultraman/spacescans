"""spacescans.data — get the exposure data into a data directory, and check it.

No data ships with spacescans: this module knows where each dataset comes
from (``manifest.json``), what a complete copy looks like on disk, and how to
download the project-mirrored parts resumably. Provider datasets come with
instructions and licence notes; FAQSD (EPA), TEMIS (KNMI) and ACAG (WashU)
are downloaded from their providers only when the call accepts their terms
(``accept_terms``), never on the user's behalf. VNL needs an EOG login: it
is imported from files the user downloaded (``import_files``).

Standard library only: importing this module (and calling ``status``) works
on a base install, without geopandas or rasterio.

    from spacescans import data
    data.status(["ndi", "walkability"], data_dir="~/ss-data")
    data.fetch(["ndi", "walkability"], data_dir="~/ss-data")
    data.verify(["ndi"], data_dir="~/ss-data")
    data.requirements(["ndi", "faqsd"])            # datasets and artifacts, no data dir
    data.fetch(["faqsd"], data_dir="~/ss-data", accept_terms=["epa-faqsd"])

CLI: ``spacescans data status|fetch|verify|adopt|import|requirements|prepare|mirror-check``.
"""
from __future__ import annotations

from spacescans.data._adopt import AdoptReport, adopt
from spacescans.data._fetch import FetchPlan, FetchReport, fetch, plan
from spacescans.data._import import ImportReport, import_files
from spacescans.data._mirror import MirrorCheck, MirrorCheckReport, mirror_check
from spacescans.data._prepare import (
    TEMPLATES,
    PrepareReport,
    prepare_templates,
    write_template,
)
from spacescans.data._providers import RemoteFile, parse_box_page, remote_files
from spacescans.data._requirements import (
    DatasetNeed,
    Requirements,
    VariableRequirements,
    requirements,
)
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
    "TEMPLATES",
    "USABLE",
    "AdoptReport",
    "Artifact",
    "DataCorruptError",
    "DataError",
    "DataLock",
    "DataMissingError",
    "Dataset",
    "DatasetNeed",
    "DatasetStatus",
    "DiskSpaceError",
    "ExtractResult",
    "FetchPlan",
    "FetchReport",
    "ImportReport",
    "LockedError",
    "Manifest",
    "ManifestError",
    "MirrorCheck",
    "MirrorCheckReport",
    "NetworkError",
    "NotOnMirrorError",
    "PrepareReport",
    "RemoteFile",
    "Requirements",
    "StatusReport",
    "TermsRequiredError",
    "UnsafeArchiveError",
    "UsageError",
    "VariableRequirements",
    "VariableStatus",
    "VerifyReport",
    "adopt",
    "boundary_datasets",
    "cloud_sync_warning",
    "extract_archive",
    "extract_tar",
    "fetch",
    "import_files",
    "is_macos_metadata",
    "load_manifest",
    "mirror_check",
    "parse_box_page",
    "plan",
    "prepare_templates",
    "remote_files",
    "requirements",
    "resolve_data_dir",
    "status",
    "terms",
    "validate_member",
    "verify",
    "write_template",
]
