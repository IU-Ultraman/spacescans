"""Where mirrored artifacts are downloaded from.

``SharePointShare`` resolves an anonymous "anyone with the link" folder share
the way ``fetch_distribution.sh`` does (request the share link without
following the redirect, keep the access cookie, read the folder path from
the ``id=`` parameter of ``Location``), but downloads through
``<site>/_layouts/15/download.aspx?SourceUrl=<folder>/<name>``, which honours
``Range`` (verified 2026-10-12: 206 with ``Content-Range``), so interrupted
downloads resume. The ``_api/.../$value`` endpoint the script used ignores
Range. The cookie is re-minted on 401/403 or when SharePoint answers with an
HTML sign-in page instead of the file.

``HttpMirror`` is any static server holding the files under one base URL
(an institutional mirror, or the test suite's local server).
``FolderMirror`` is a local folder holding the files (a USB disk, a copy of
the OneDrive folder, or a spacescans-web ``pipeline-data`` tree, where a bare
file sits under its ``dest`` folder).
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from spacescans.data.errors import NetworkError, NotOnMirrorError
from spacescans.data.manifest import Artifact, Mirror
from spacescans.data.transport import Transport

MIRROR_ENV = "SPACESCANS_DATA_MIRROR"


class Source:
    kind = "source"
    range = True

    def describe(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def url(self, art: Artifact) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def refresh(self) -> bool:
        """Forget cached credentials; True if a retry could now succeed."""
        return False


class HttpMirror(Source):
    kind = "http"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def describe(self) -> str:
        return self.base_url

    def url(self, art: Artifact) -> str:
        return f"{self.base_url}/{urllib.parse.quote(art.name)}"


class SharePointShare(Source):
    kind = "sharepoint-share"

    def __init__(self, share_url: str, transport: Transport) -> None:
        self.share_url = share_url
        self.transport = transport
        self._folder: str | None = None
        self._base: str | None = None

    def describe(self) -> str:
        return self.share_url

    def resolve(self) -> tuple[str, str]:
        """(site base URL, folder server-relative path); mints the cookie."""
        if self._folder is not None and self._base is not None:
            return self._base, self._folder
        resp = None
        for method in ("HEAD", "GET"):
            resp = self.transport.open(self.share_url, method=method, follow_redirects=False)
            try:
                if method == "GET" or resp.status != 405:
                    location = resp.header("Location")
                    status = resp.status
                    break
            finally:
                resp.close()
        if status in (404, 410):
            raise NotOnMirrorError(
                f"the mirror share link is gone (HTTP {status}): {self.share_url}",
                hint=f"ask the project for a current mirror link; set {MIRROR_ENV} or pass --mirror",
            )
        if not location:
            raise NetworkError(
                f"could not resolve the share link (HTTP {status}, no redirect): {self.share_url}; "
                "is it still shared with 'anyone with the link'?",
                hint="re-run later; if it persists, the share may have been withdrawn",
            )
        loc = urllib.parse.urljoin(self.share_url, location)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
        folder = (query.get("id") or [None])[0]
        if not folder:
            raise NetworkError(f"unexpected share redirect (no folder id): {loc}")
        parts = urllib.parse.urlsplit(self.share_url)
        site = "/".join(folder.strip("/").split("/")[:2])
        self._base = f"{parts.scheme}://{parts.netloc}/{site}"
        self._folder = folder.rstrip("/")
        return self._base, self._folder

    def url(self, art: Artifact) -> str:
        base, folder = self.resolve()
        source = urllib.parse.quote(f"{folder}/{art.name}", safe="/")
        return f"{base}/_layouts/15/download.aspx?SourceUrl={source}"

    def refresh(self) -> bool:
        self._folder = self._base = None
        self.transport.cookies.clear()
        return True


class FolderMirror(Source):
    kind = "folder"

    def __init__(self, folder: str | os.PathLike[str]) -> None:
        self.folder = Path(folder).expanduser().resolve()

    def describe(self) -> str:
        return str(self.folder)

    def path(self, art: Artifact) -> Path | None:
        candidates = [self.folder / art.name]
        if art.kind == "bare" and art.dest:
            candidates.append(self.folder / art.dest / art.name)
        for c in candidates:
            if c.is_file():
                return c
        return None

    def url(self, art: Artifact) -> str:
        p = self.path(art)
        return p.as_uri() if p else (self.folder / art.name).as_uri()


def source_for(spec: str | os.PathLike[str] | Mirror, transport: Transport) -> Source:
    """A Source from a manifest Mirror, a URL or a local folder path."""
    if isinstance(spec, Mirror):
        if spec.type == "sharepoint-share":
            return SharePointShare(spec.url, transport)
        if spec.type == "http":
            return HttpMirror(spec.url)
        return FolderMirror(spec.url)
    s = os.fspath(spec)
    if s.startswith(("http://", "https://")):
        if "sharepoint.com/:f:" in s or "/:f:/" in s:
            return SharePointShare(s, transport)
        return HttpMirror(s)
    if s.startswith("file://"):
        s = urllib.parse.unquote(urllib.parse.urlsplit(s).path)
    p = Path(s).expanduser()
    if not p.is_dir():
        raise NotOnMirrorError(f"mirror folder not found: {p}")
    return FolderMirror(p)
