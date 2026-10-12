"""Fixtures for spacescans.data tests: no real network, ever.

* ``server``: a local HTTP server on 127.0.0.1 that serves in-memory files
  with ``Range`` support, and can misbehave on request (drop the connection
  mid-body, ignore Range, send a bogus Content-Range, fail with 5xx, answer
  with an HTML page). It also emulates a SharePoint "anyone with the link"
  folder share: the share link redirects with an access cookie, and
  ``_layouts/15/download.aspx?SourceUrl=`` serves files only with that cookie.
* ``archive``: a small .tar.gz shaped like the distribution archives
  (members with 0777 modes, AppleDouble sidecars, a .DS_Store, __MACOSX/).
* ``mini_manifest``: a manifest dict pointing at the server.
"""
from __future__ import annotations

import copy
import hashlib
import http.server
import io
import socket
import tarfile
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SHARE_PATH = "/:f:/g/personal/tester_example_edu/ShareToken123"
FOLDER = "/personal/tester_example_edu/Documents/spacescans-pipeline-data"
COOKIE = "FedAuth=cookie-value-1/+=="


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_tar(members: list[tuple[str, bytes | None, int]]) -> bytes:
    """members: (name, data or None for a directory, mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = 1_700_000_000
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SHP = b"shp-bytes-" * 5000          # 50 kB
DBF = b"dbf-bytes-" * 300
PRJ = b'GEOGCS["GCS_North_American_1983"]'
MANIFEST_TXT = b"county boundaries test archive\n"


def county_members() -> list[tuple[str, bytes | None, int]]:
    base = "County/C3/tl_2010_us_county10"
    return [
        ("County", None, 0o777),
        ("County/C3", None, 0o700),
        (base, None, 0o777),
        ("County/C3/MANIFEST.txt", MANIFEST_TXT, 0o644),
        (f"{base}/tl_2010_us_county10.shp", SHP, 0o777),
        (f"{base}/._tl_2010_us_county10.shp", b"\x00\x05\x16\x07appledouble", 0o777),
        (f"{base}/tl_2010_us_county10.dbf", DBF, 0o700),
        (f"{base}/._tl_2010_us_county10.dbf", b"\x00\x05\x16\x07appledouble", 0o777),
        (f"{base}/tl_2010_us_county10.prj", PRJ, 0o777),
        (f"{base}/.DS_Store", b"\x00\x00\x00\x01Bud1", 0o644),
        ("__MACOSX/County/._C3", b"junk", 0o644),
    ]


@dataclass
class Archive:
    name: str
    data: bytes
    sha256: str
    payload_bytes: int
    files: int


@pytest.fixture
def archive() -> Archive:
    data = make_tar(county_members())
    payload = [d for n, d, _ in county_members()
               if d is not None and not n.split("/")[-1].startswith("._")
               and not n.endswith(".DS_Store") and not n.startswith("__MACOSX")]
    return Archive(name="county_test_v1.tar.gz", data=data, sha256=sha256(data),
                   payload_bytes=sum(len(d) for d in payload), files=len(payload))


PANEL = bytes(range(256)) * 400     # 102,400 bytes, a stand-in for an .Rda panel


@dataclass
class Behaviour:
    ignore_range: bool = False
    drop_after: int | None = None      # close the connection after N body bytes ...
    drops: int = 0                     # ... this many times
    fail_status: list[int] = field(default_factory=list)   # statuses to return first, in order
    bogus_total: int = 0               # serve 206 with a wrong Content-Range total this many times
    html_once: bool = False            # answer the next download with an HTML page
    require_cookie: bool = True        # sharepoint download.aspx needs the share cookie
    cookie_value: str = COOKIE
    expire_cookie_once: bool = False   # reject the first valid cookie with 403


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def do_HEAD(self):
        self._handle(head=True)

    def do_GET(self):
        self._handle(head=False)

    def _handle(self, head: bool) -> None:
        srv = self.server
        srv.requests.append({"method": self.command, "path": self.path,
                             "headers": {k.lower(): v for k, v in self.headers.items()}})
        b = srv.behaviour
        parsed = urllib.parse.urlsplit(self.path)
        # SharePoint share link -> redirect + cookie
        if parsed.path == SHARE_PATH:
            loc = (f"/personal/tester_example_edu/_layouts/15/onedrive.aspx?id="
                   f"{urllib.parse.quote(FOLDER, safe='')}&ga=1")
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Set-Cookie", f"{b.cookie_value}; path=/; secure; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if b.fail_status:
            code = b.fail_status.pop(0)
            self._send_simple(code, b"error")
            return
        name = None
        if parsed.path.endswith("/_layouts/15/download.aspx"):
            q = urllib.parse.parse_qs(parsed.query)
            src = (q.get("SourceUrl") or [""])[0]
            if not src.startswith(FOLDER + "/"):
                self._send_simple(404, b"not found")
                return
            if b.require_cookie:
                cookie = self.headers.get("Cookie") or ""
                if b.cookie_value not in cookie:
                    self._send_simple(403, b"forbidden")
                    return
                if b.expire_cookie_once:
                    b.expire_cookie_once = False
                    self._send_simple(403, b"expired")
                    return
            name = src[len(FOLDER) + 1:]
        elif parsed.path.startswith("/files/"):
            name = urllib.parse.unquote(parsed.path[len("/files/"):])
        if name is None or name not in srv.files:
            self._send_simple(404, b"not found")
            return
        if b.html_once:
            b.html_once = False
            body = b"<html><body>Sign in to continue</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = srv.files[name]
        start = 0
        rng = self.headers.get("Range")
        status = 200
        if rng and not b.ignore_range and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            s, _, e = spec.partition("-")
            start = int(s)
            end = int(e) if e else len(data) - 1
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
            body = data[start:end + 1]
        else:
            body = data
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            total = len(data)
            if b.bogus_total:
                b.bogus_total -= 1
                total = len(data) + 7
            self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{total}")
        self.end_headers()
        if head:
            return
        if b.drop_after is not None and b.drops > 0 and len(body) > b.drop_after:
            b.drops -= 1
            self.wfile.write(body[:b.drop_after])
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True
            return
        self.wfile.write(body)

    def _send_simple(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RangeServer:
    def __init__(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.files = {}
        self.httpd.requests = []
        self.httpd.behaviour = Behaviour()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def files_url(self) -> str:
        return self.base + "/files"

    @property
    def share_url(self) -> str:
        return self.base + SHARE_PATH + "?e=abc"

    @property
    def files(self) -> dict:
        return self.httpd.files

    @property
    def behaviour(self) -> Behaviour:
        return self.httpd.behaviour

    @property
    def requests(self) -> list:
        return self.httpd.requests

    def downloads(self) -> list:
        return [r for r in self.requests if "download.aspx" in r["path"] or r["path"].startswith("/files/")]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = RangeServer()
    try:
        yield s
    finally:
        s.close()


def build_manifest(mirror: dict, archive: Archive) -> dict:
    return {
        "schema_version": 1,
        "data_release": "2099.01",
        "mirrors": {"test": mirror},
        "lists": {"years": {"range": [2013, 2015]}, "parts": ["shp", "dbf", "prj"]},
        "artifacts": {
            archive.name: {
                "kind": "tar", "status": "available", "mirror": "test",
                "sha256": archive.sha256, "bytes": len(archive.data),
                "extracted_bytes": archive.payload_bytes + 100, "payload_bytes": archive.payload_bytes,
                "files": archive.files, "roots": ["County/C3"],
            },
            "panel.Rda": {
                "kind": "bare", "status": "available", "mirror": "test",
                "sha256": sha256(PANEL), "bytes": len(PANEL), "dest": "NDI/C4",
            },
            "labels.csv": {
                "kind": "bare", "status": "pending_upload", "mirror": "test",
                "sha256": sha256(b"a,b\n1,2\n"), "bytes": 8, "dest": "FARA/C4",
                "interim": "copy it from a spacescans-web checkout",
            },
        },
        "datasets": {
            "county_boundaries": {
                "title": "test counties", "acquire": "mirror", "artifacts": [archive.name],
                "provider": "Census", "license": "public domain", "redistribution": "allowed",
                "attribution": "Credit the Census Bureau.",
                "layout": [{"rule": "files", "path": "County/C3/tl_2010_us_county10/tl_2010_us_county10.{ext}",
                            "vars": {"ext": "@parts"}}],
            },
            "ndi_panel": {
                "title": "test panel", "acquire": "mirror", "artifacts": ["panel.Rda"],
                "provider": "project", "license": "derived", "redistribution": "allowed",
                "attribution": "Cite us.",
            },
            "fara_labels": {
                "title": "labels", "acquire": "mirror", "artifacts": ["labels.csv"],
                "provider": "project", "license": "derived", "redistribution": "allowed",
                "attribution": "Cite us.",
            },
            "vnl": {
                "title": "night lights", "acquire": "provider-manual", "provider": "EOG",
                "license": "CC BY 4.0", "redistribution": "allowed with attribution",
                "attribution": "Credit EOG.",
                "terms": {"id": "eog-vnl", "url": "https://example.org/licence.pdf",
                          "summary": "account required", "account": True},
                "instructions": ["Download it from EOG yourself."],
                "layout": [{"rule": "one_per", "dir": "VNL/C3",
                            "regex": "^VNL_v21_npp_(?P<year>\\d{4})_global_.*\\.tif$",
                            "group": "year", "values": "@years", "warn_other": "*.tif"}],
            },
        },
        "variables": {
            "tiger_proximity": {"requires": ["county_boundaries"]},
            "ndi": {"requires": ["county_boundaries", "ndi_panel"]},
            "fara_tract": {"requires": ["fara_labels"]},
            "vnl": {"requires": ["vnl"]},
        },
        "boundaries": {"County": "county_boundaries"},
    }


@pytest.fixture
def mini_manifest(server, archive) -> dict:
    server.files[archive.name] = archive.data
    server.files["panel.Rda"] = PANEL
    return build_manifest({"type": "http", "url": server.files_url}, archive)


@pytest.fixture
def share_manifest(server, archive) -> dict:
    server.files[archive.name] = archive.data
    server.files["panel.Rda"] = PANEL
    return build_manifest({"type": "sharepoint-share", "url": server.share_url, "range": True}, archive)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("SPACESCANS_DATA_DIR", raising=False)
    monkeypatch.delenv("SPACESCANS_DATA_MIRROR", raising=False)
    d = tmp_path / "ss-data"
    return d


class Plenty:
    free = 10 ** 15


def plenty(_path):
    return Plenty()


def fast(**extra):
    """kwargs that make fetch() quick and roomy in tests."""
    kw = {"sleep": lambda s: None, "headroom_bytes": 0, "disk_usage": plenty}
    kw.update(extra)
    return kw


class _Helpers:
    sha256 = staticmethod(sha256)
    make_tar = staticmethod(make_tar)
    county_members = staticmethod(county_members)
    build_manifest = staticmethod(build_manifest)
    fast = staticmethod(fast)
    deepcopy = staticmethod(copy.deepcopy)
    PANEL = PANEL
    SHP = SHP
    COOKIE = COOKIE


@pytest.fixture
def h() -> _Helpers:
    """Helper functions and constants (importable test helpers do not work
    under --import-mode=importlib, so they come as a fixture)."""
    return _Helpers()
