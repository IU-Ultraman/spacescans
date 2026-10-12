"""spacescans.data — get the exposure data into a data directory, and check it.

No data ships with spacescans: this module knows where each dataset comes
from (``manifest.json``) and what a complete copy looks like on disk.
Standard library only.
"""
from __future__ import annotations

from spacescans.data.errors import (DataCorruptError, DataError, DataMissingError, DiskSpaceError,
                                    LockedError, ManifestError, NetworkError, NotOnMirrorError,
                                    TermsRequiredError, UnsafeArchiveError, UsageError)
from spacescans.data.manifest import (Artifact, Dataset, Manifest, load_manifest, MANIFEST_PATH,
                                      SCHEMA_PATH)
from spacescans.data.paths import cloud_sync_warning, resolve_data_dir
