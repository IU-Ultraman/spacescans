"""Provider downloads: which files a provider-scripted dataset consists of.

``remote_files()`` turns a dataset's ``download`` block into
:class:`RemoteFile` items (URL, target under the data root, expected size when
the provider says). The download itself goes through the fetch engine
(:func:`spacescans.data._fetch._stream`), which checks sizes, file signatures
and sha256 and retries; nothing here writes to the data directory.

Methods (manifest ``download.method``):

``files``  one URL per expansion of the dataset's ``files`` layout rule
           (EPA FAQSD: 14 ``.txt.gz`` files from rsigserver, which ignores
           Range, so every attempt starts from byte 0);
``daily``  one URL per calendar day of the ``daily`` layout rule, minus its
           known gaps (KNMI TEMIS on CloudFront: 4 variables x 7 years);
``box``    WashU Box shared folders (ACAG). Box's folder API rejects
           anonymous calls and folder downloads are disabled, so each folder's
           HTML page is paged with ``?page=N``: the page embeds its listing as
           JSON (``Box.postStreamData = {...}``, older pages
           ``Box.prefetchedData``). Files are downloaded one by one through
           ``index.php?rm=box_download_shared_file&shared_name=...&file_id=f_...``
           and routed to their folders by the dataset's ``import`` rules.
"""
from __future__ import annotations

import datetime as _dt
import http.client
import itertools
import json
import ssl
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from spacescans.data.errors import DataMissingError, NetworkError
from spacescans.data.manifest import Dataset, Manifest
from spacescans.data.transport import Transport

MAX_PAGES = 500
_BOX_MARKERS = ("Box.postStreamData", "Box.prefetchedData")
_FOLDER_KEYS = ("/app-api/enduserapp/shared-folder", "/app-api/enduserapp/folder")


class ProviderFormatError(NetworkError):
    """A provider page did not look as expected (the site changed?)."""


@dataclass(frozen=True)
class RemoteFile:
    url: str                       # where to GET it
    target: str                    # relative path of the placed file
    name: str                      # file name as published (``.gz`` included)
    bytes: int | None = None       # download size the provider's listing gives, if any
    reference_bytes: int | None = None  # download size the project saw (a warning when it differs)


def _files(man: Manifest, ds: Dataset) -> Iterator[RemoteFile]:
    dl = ds.download
    for rule in ds.layout:
        if rule.get("rule") != "files":
            continue
        for target, binding in man.expand(rule["path"], rule.get("vars")):
            name = target.rsplit("/", 1)[-1] + (".gz" if dl.gunzip else "")
            yield RemoteFile(url=dl.url.format(**binding), target=target, name=name,
                             reference_bytes=dl.reference_download_bytes.get(target))


def _days(year: int) -> Iterator[_dt.date]:
    d = _dt.date(year, 1, 1)
    while d.year == year:
        yield d
        d += _dt.timedelta(days=1)


def _daily(man: Manifest, ds: Dataset) -> Iterator[RemoteFile]:
    dl = ds.download
    for rule in ds.layout:
        if rule.get("rule") != "daily":
            continue
        gaps = set(rule.get("known_gaps") or ())
        names = list((rule.get("vars") or {}).keys())
        pools = [man.resolve_values(rule["vars"][k]) for k in names]
        for combo in itertools.product(*pools) if pools else [()]:
            binding = dict(zip(names, combo))
            for year in (int(y) for y in man.resolve_values(rule["years"])):
                for day in _days(year):
                    stamp = day.strftime("%Y%m%d")
                    if stamp in gaps:
                        continue
                    fields = dict(binding, year=f"{year:04d}", mm=f"{day.month:02d}",
                                  dd=f"{day.day:02d}", yyyymmdd=stamp)
                    target = rule["path"].format(**fields)
                    yield RemoteFile(url=dl.url.format(**fields), target=target,
                                     name=target.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# WashU Box shared folders

@dataclass(frozen=True)
class BoxItem:
    type: str          # "file" | "folder"
    id: str
    name: str
    size: int | None = None


def _find_listing(obj: Any) -> Mapping[str, Any] | None:
    """The folder listing dict inside Box's page data."""
    if isinstance(obj, Mapping):
        for key in _FOLDER_KEYS:
            if isinstance(obj.get(key), Mapping) and isinstance(obj[key].get("items"), list):
                return obj[key]
        items = obj.get("items")
        if isinstance(items, list) and items and all(isinstance(i, Mapping) for i in items) and any(
                "typedID" in i or ("type" in i and "name" in i) for i in items):
            return obj
        for v in obj.values():
            found = _find_listing(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_listing(v)
            if found is not None:
                return found
    return None


def parse_box_page(html: str) -> tuple[list[BoxItem], int | None]:
    """(items on this page, page count if the page says) from a Box shared-folder page."""
    data = None
    for marker in _BOX_MARKERS:
        i = html.find(marker)
        if i < 0:
            continue
        j = html.find("=", i + len(marker))
        if j < 0:
            continue
        rest = html[j + 1:].lstrip()
        try:
            data, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            continue
        break
    if data is None:
        raise ProviderFormatError(
            "the Box page holds no folder listing (Box.postStreamData not found); "
            "Box may have changed its page format",
            hint="download the files by hand and run spacescans data import acag <files or folders>")
    listing = _find_listing(data)
    if listing is None:
        return [], None
    items: list[BoxItem] = []
    for raw in listing.get("items") or ():
        typed = str(raw.get("typedID") or "")
        kind = raw.get("type") or {"f": "file", "d": "folder"}.get(typed[:1], "")
        ident = raw.get("id") or (typed[2:] if "_" in typed else "")
        name = raw.get("name")
        if kind not in ("file", "folder") or not ident or not isinstance(name, str):
            continue
        size = raw.get("itemSize", raw.get("size"))
        items.append(BoxItem(type=kind, id=str(ident), name=name,
                             size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None))
    pages = listing.get("pageCount")
    return items, (int(pages) if isinstance(pages, int) and pages > 0 else None)


class BoxLister:
    """List the files under a path of a Box shared folder by paging its HTML."""

    def __init__(self, base: str, transport: Transport, *, max_attempts: int = 5,
                 backoff: Callable[[int], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.base = base.rstrip("/")
        self.transport = transport
        self.max_attempts = max_attempts
        self.backoff = backoff or (lambda n: float(min(60, 2 ** max(0, n - 1))))
        self.sleep = sleep or time.sleep

    def page_url(self, share: str, folder_id: str | None, page: int) -> str:
        path = f"/s/{share}" + (f"/folder/{folder_id}" if folder_id else "")
        return f"{self.base}{path}?page={page}"

    def download_url(self, share: str, file_id: str) -> str:
        q = urllib.parse.urlencode({"rm": "box_download_shared_file", "shared_name": share,
                                    "file_id": f"f_{file_id}"})
        return f"{self.base}/index.php?{q}"

    def _get(self, url: str) -> str:
        last: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self.transport.open(url) as resp:
                    if resp.status in (404, 410):
                        raise DataMissingError(
                            f"the provider's shared folder is gone (HTTP {resp.status}): {url}",
                            hint="download the files by hand and run spacescans data import",
                            details={"url": url, "status": resp.status})
                    if resp.status == 200:
                        return resp.read().decode("utf-8", "replace")
                    last = NetworkError(f"HTTP {resp.status} listing {url}")
            except (TimeoutError, OSError, http.client.HTTPException, ssl.SSLError) as e:
                last = e
            if attempt < self.max_attempts:
                self.sleep(self.backoff(attempt))
        raise NetworkError(f"could not list {url} after {self.max_attempts} attempts: {last}",
                           hint="re-run later; files already downloaded are kept",
                           details={"url": url})

    def folder(self, share: str, folder_id: str | None) -> list[BoxItem]:
        """Every item of one folder, all pages."""
        out: list[BoxItem] = []
        seen: set[tuple[str, str]] = set()
        page = 1
        while page <= MAX_PAGES:
            items, pages = parse_box_page(self._get(self.page_url(share, folder_id, page)))
            new = [i for i in items if (i.type, i.id) not in seen]
            for i in new:
                seen.add((i.type, i.id))
            out.extend(new)
            if pages is not None:
                if page >= pages:
                    break
            elif not new:
                break
            page += 1
        return out

    def files(self, share: str, path: str) -> list[BoxItem]:
        """The files in ``path`` (folder names separated by ``/``) of a share."""
        folder_id: str | None = None
        walked: list[str] = []
        for part in [p for p in path.split("/") if p]:
            items = self.folder(share, folder_id)
            match = next((i for i in items if i.type == "folder" and i.name == part), None)
            if match is None:
                where = "/".join(walked) or "(top)"
                raise DataMissingError(
                    f"folder {part!r} not found under {where} in Box share {share}",
                    hint="the provider may have reorganised the share; download by hand and run "
                         "spacescans data import acag <files or folders>",
                    details={"share": share, "path": path})
            folder_id = match.id
            walked.append(part)
        return [i for i in self.folder(share, folder_id) if i.type == "file"]


def _box(man: Manifest, ds: Dataset, transport: Transport, **retry: Any) -> Iterator[RemoteFile]:
    dl = ds.download
    lister = BoxLister(dl.base, transport, **retry)
    seen: set[str] = set()
    for share in dl.shares:
        paths = [p for tpl in share["paths"] for p, _ in man.expand(tpl, share.get("vars"))]
        for path in paths:
            for item in lister.files(share["name"], path):
                target = route(man, ds, item.name)[0]
                if target is None or target in seen:
                    continue
                seen.add(target)
                yield RemoteFile(url=lister.download_url(share["name"], item.id), target=target,
                                 name=item.name, bytes=item.size)


def route(man: Manifest, ds: Dataset, name: str) -> tuple[str | None, str, bytes]:
    """(target, reason when None, expected signature) for a file name, by import rules."""
    reason = "name does not match"
    for rule in ds.imports:
        years = man.resolve_values(rule.years) if rule.years is not None else None
        target, why = rule.route(name, years)
        if target is not None:
            return target, "", bytes.fromhex(rule.magic) if rule.magic else b""
        reason = why if why != "name does not match" else reason
    return None, reason, b""


def remote_files(man: Manifest, ds: Dataset, *, transport: Transport, **retry: Any) -> Iterator[RemoteFile]:
    """The files of a provider-scripted dataset (Box listing makes requests)."""
    if ds.download is None:
        raise ValueError(f"{ds.key} has no download block")
    method = ds.download.method
    if method == "files":
        return _files(man, ds)
    if method == "daily":
        return _daily(man, ds)
    return _box(man, ds, transport, **retry)
