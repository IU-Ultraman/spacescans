"""Provider downloads (FAQSD, TEMIS, ACAG) against local servers only.

The local server emulates the three providers:

* EPA rsigserver (``/rsig/rsigserver?data/FAQSD/outputs/<f>.txt.gz``): ignores
  ``Range`` always and can drop the connection mid-body;
* KNMI's CloudFront (``/uv/v2.0/<yyyy>/<mm>/<f>.hdf``): honours ``Range``;
  a missing object answers 403, as S3-backed CloudFront does;
* WashU Box shared folders: ``/s/<share>[/folder/<id>]?page=N`` HTML pages
  embedding ``Box.postStreamData = {...}`` (2 items per page here), and
  ``/index.php?rm=box_download_shared_file&shared_name=..&file_id=f_<id>``
  redirecting to a Range-capable ``/boxcloud/<id>``.

The autouse guard in conftest refuses any non-loopback connection.
"""
from __future__ import annotations

import datetime as dt
import gzip
import http.server
import json
import socket
import threading
import urllib.parse
from dataclasses import dataclass, field

import pytest

from spacescans.data import (
    DataCorruptError,
    DataMissingError,
    TermsRequiredError,
    fetch,
    parse_box_page,
    plan,
    status,
)
from spacescans.data._fetch import provider_receipt_path

HDF4 = bytes.fromhex("0e031301")
HDF5 = bytes.fromhex("894844460d0a1a0a")


@dataclass
class PState:
    files: dict = field(default_factory=dict)          # url path -> bytes
    box: dict = field(default_factory=dict)            # (share, folder id or None) -> items
    blobs: dict = field(default_factory=dict)          # box file id -> bytes
    requests: list = field(default_factory=list)
    drops: dict = field(default_factory=dict)          # url path -> (cut after N bytes, times left)
    ignore_range: bool = False                         # also for Range-capable paths
    page_size: int = 2
    marker: str = "Box.postStreamData"
    page_count: bool = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._go(head=True)

    def do_GET(self):
        self._go(head=False)

    def _simple(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _go(self, head):
        st: PState = self.server.st
        st.requests.append({"path": self.path, "range": self.headers.get("Range")})
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/rsig/rsigserver":
            data = st.files.get(u.query)
            if data is None:
                return self._simple(404, b"no such file")
            return self._body(data, key=u.query, ranges=False, ctype="application/x-gzip")
        if u.path.startswith("/uv/"):
            data = st.files.get(u.path)
            if data is None:
                return self._simple(403, b"<Error>AccessDenied</Error>", "application/xml")
            return self._body(data, key=u.path, ranges=True)
        if u.path.startswith("/s/"):
            parts = u.path.split("/")
            share = parts[2]
            folder = parts[4] if len(parts) > 4 and parts[3] == "folder" else None
            items = st.box.get((share, folder))
            if items is None:
                return self._simple(404, b"<html>gone</html>", "text/html")
            page = int((urllib.parse.parse_qs(u.query).get("page") or ["1"])[0])
            n = st.page_size
            pages = max(1, -(-len(items) // n))
            listing = {"items": items[(page - 1) * n:page * n], "pageNumber": page}
            if st.page_count:
                listing["pageCount"] = pages
            data = {"/app-api/enduserapp/shared-item": {"itemType": "folder", "sharedName": share},
                    "/app-api/enduserapp/shared-folder": listing}
            html = (f"<!DOCTYPE html><html><head><script>var x = 1;\n{st.marker} = "
                    f"{json.dumps(data)};\nBox.other = {{}};</script></head><body></body></html>")
            return self._simple(200, html.encode(), "text/html; charset=utf-8")
        if u.path == "/index.php":
            q = urllib.parse.parse_qs(u.query)
            fid = (q.get("file_id") or [""])[0]
            if q.get("rm") != ["box_download_shared_file"] or not fid.startswith("f_"):
                return self._simple(400, b"bad request")
            return self._simple(302, b"", extra={"Location": f"/boxcloud/{fid[2:]}"})
        if u.path.startswith("/boxcloud/"):
            data = st.blobs.get(u.path.rsplit("/", 1)[-1])
            if data is None:
                return self._simple(404, b"nope")
            return self._body(data, key=u.path, ranges=True)
        return self._simple(404, b"not found")

    def _body(self, data, *, key, ranges, ctype="application/octet-stream"):
        st: PState = self.server.st
        rng = self.headers.get("Range")
        start, status_ = 0, 200
        if ranges and rng and not st.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            status_ = 206
        body = data[start:]
        self.send_response(status_)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if status_ == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        cut, left = st.drops.get(key, (None, 0))
        if cut is not None and left > 0 and len(body) > cut:
            st.drops[key] = (cut, left - 1)
            self.wfile.write(body[:cut])
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True
            return
        self.wfile.write(body)


class _Quiet(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


@pytest.fixture
def prov():
    srv = _Quiet(("127.0.0.1", 0), _Handler)
    srv.st = PState()
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    host, port = srv.server_address[:2]
    srv.st.base = f"http://{host}:{port}"
    try:
        yield srv.st
    finally:
        srv.shutdown()
        srv.server_close()


def _base_manifest(h, archive, base):
    man = h.build_manifest({"type": "http", "url": base + "/files"}, archive)
    man["lists"]["kinds"] = ["ozone_daily_8hour_maximum", "pm25_daily_average"]
    return man


def _add_faqsd(man, base, *, reference=None):
    man["datasets"]["faqsd"] = {
        "title": "faqsd", "acquire": "provider-scripted", "provider": "US EPA",
        "license": "US federal public domain", "redistribution": "allowed", "attribution": "Credit EPA.",
        "terms": {"id": "epa-faqsd", "url": "https://example.org/faqsd", "summary": "public domain"},
        "instructions": ["get it from EPA"], "estimate": {"download_bytes": 2000, "disk_bytes": 9000},
        "download": {"method": "files", "range": False, "gunzip": True, "magic": "1f8b",
                     "url": base + "/rsig/rsigserver?data/FAQSD/outputs/{year}_{kind}.txt.gz",
                     "reference_download_bytes": reference or {}},
        "layout": [{"rule": "files", "path": "FAQSD/C4/{year}_{kind}.txt",
                    "vars": {"year": [2016], "kind": "@kinds"}}],
    }
    man["variables"]["faqsd"] = {"requires": ["tract", "faqsd"]} if "tract" in man["datasets"] \
        else {"requires": ["faqsd"]}
    return man


def _faqsd_files(prov):
    out = {}
    for kind in ("ozone_daily_8hour_maximum", "pm25_daily_average"):
        text = ("Date,Loc_Label1,Latitude,Longitude,Prediction,SEpred\n" +
                "".join(f"Jan-{d:02d}-2016,1{d:010d},30.0,-85.0,{d}.5,0.1\n" for d in range(1, 30)) * 200
                ).encode()
        out[f"data/FAQSD/outputs/2016_{kind}.txt.gz"] = (text, gzip.compress(text, mtime=0))
    for key, (_, gz) in out.items():
        prov.files[key] = gz
    return out


# --------------------------------------------------------------------------- FAQSD

def test_faqsd_needs_its_terms_and_never_downloads_without_them(prov, h, archive, data_dir):
    man = _add_faqsd(_base_manifest(h, archive, prov.base), prov.base)
    _faqsd_files(prov)
    with pytest.raises(TermsRequiredError) as e:
        fetch(["faqsd"], data_dir=data_dir, manifest=man, **h.fast())
    assert e.value.kind == "terms_required"
    assert [t["id"] for t in e.value.details["terms"]] == ["epa-faqsd"]
    assert "--accept-terms epa-faqsd" in e.value.hint
    assert prov.requests == [] and not (data_dir / "FAQSD").exists()
    dry = fetch(["faqsd"], data_dir=data_dir, manifest=man, dry_run=True)
    assert not dry.ok and dry.plan.terms_required[0]["dataset"] == "faqsd" and dry.plan.providers == []
    p = plan(["faqsd"], data_dir=data_dir, manifest=man, accept_terms=["epa-faqsd"])
    assert [e["dataset"] for e in p.providers] == ["faqsd"] and p.terms_required == []
    assert p.terms_used[0]["id"] == "epa-faqsd" and p.download_bytes == 2000
    st = status(["faqsd"], data_dir=data_dir, manifest=man)
    d = st.dataset("faqsd")
    assert d.status == "action_required" and d.scripted
    assert d.next == "spacescans data fetch --dataset faqsd --accept-terms epa-faqsd"
    step = next(s for s in st.next_steps if s.get("dataset") == "faqsd")
    assert step["action"] == "manual" and step["accept_terms"] == ["epa-faqsd"]
    assert prov.requests == []


def test_faqsd_without_range_restarts_from_zero_then_gunzips(prov, h, archive, data_dir):
    files = _faqsd_files(prov)
    key = "data/FAQSD/outputs/2016_pm25_daily_average.txt.gz"
    gz = prov.files[key]
    prov.drops[key] = (len(gz) // 2, 2)                  # two cut connections
    man = _add_faqsd(_base_manifest(h, archive, prov.base), prov.base,
                     reference={"FAQSD/C4/2016_ozone_daily_8hour_maximum.txt": 7})
    events = []
    rep = fetch(["faqsd"], data_dir=data_dir, manifest=man, accept_terms=["epa-faqsd"],
                on_event=events.append, **h.fast())
    assert rep.ok, rep.to_dict()
    item = rep.items[0]
    assert item.provider and item.artifact == "faqsd" and item.outcome == "downloaded"
    assert item.files == 2 and item.attempts == 4         # 1 + (1 + 2 retries)
    hits = [r for r in prov.requests if r["path"].endswith("2016_pm25_daily_average.txt.gz")]
    assert len(hits) == 3 and all(r["range"] is None for r in hits)   # never a Range request
    for k, (text, _) in files.items():
        name = k.rsplit("/", 1)[-1][:-3]
        assert (data_dir / "FAQSD/C4" / name).read_bytes() == text
    assert not list(data_dir.rglob("*.part")) and not list((data_dir / ".spacescans/downloads").rglob("*.gz*"))
    assert any("the project's download was 7" in w for w in item.warnings)
    receipt = json.loads(provider_receipt_path(data_dir, "faqsd").read_text())
    assert receipt["origin"] == "provider-fetch" and receipt["terms"] == "epa-faqsd"
    assert [f[0] for f in receipt["files"]] == ["FAQSD/C4/2016_ozone_daily_8hour_maximum.txt",
                                               "FAQSD/C4/2016_pm25_daily_average.txt"]
    assert receipt["files"][1][1] == len(files[key][0]) and receipt["downloads"][
        "FAQSD/C4/2016_pm25_daily_average.txt"]["bytes"] == len(gz)
    kinds = [e["ev"] for e in events]
    assert "provider_start" in kinds and kinds.count("retry") == 2 and kinds[-1] == "provider_end"
    assert rep.status.variable("faqsd").status == "ready"
    acks = json.loads((data_dir / ".spacescans/acknowledgements.json").read_text())
    assert acks[-1]["terms_id"] == "epa-faqsd"
    # A re-run downloads nothing: the files are in place.
    n = len(prov.requests)
    again = fetch(["faqsd"], data_dir=data_dir, manifest=man, accept_terms=["epa-faqsd"], **h.fast())
    assert again.ok and again.items == [] and len(prov.requests) == n


def test_faqsd_partial_rerun_and_corrupt_gzip(prov, h, archive, data_dir):
    files = _faqsd_files(prov)
    bad = sorted(files)[1]
    gz = files[bad][1]
    prov.files[bad] = gz[: len(gz) // 2]                  # a truncated gzip, served in full
    man = _add_faqsd(_base_manifest(h, archive, prov.base), prov.base)
    with pytest.raises(DataCorruptError) as e:
        fetch(["faqsd"], data_dir=data_dir, manifest=man, accept_terms=["epa-faqsd"], **h.fast())
    assert e.value.details["count"] == 1 and "could not unpack" in e.value.details["failed"][0]["message"]
    rep = e.value.report
    assert rep.items[0].files == 1 and rep.items[0].outcome == "failed"
    assert (data_dir / "FAQSD/C4/2016_ozone_daily_8hour_maximum.txt").is_file()
    assert not (data_dir / "FAQSD/C4/2016_pm25_daily_average.txt").exists()
    assert not list(data_dir.rglob("*.part"))
    prov.files[bad] = gz                                  # fixed upstream: the re-run fetches only it
    prov.requests.clear()
    rep = fetch(["faqsd"], data_dir=data_dir, manifest=man, accept_terms=["epa-faqsd"], **h.fast())
    assert rep.ok and rep.items[0].files == 1 and rep.items[0].skipped == 1
    assert [r["path"].rsplit("/", 1)[-1] for r in prov.requests] == ["2016_pm25_daily_average.txt.gz"]
    receipt = json.loads(provider_receipt_path(data_dir, "faqsd").read_text())
    assert len(receipt["files"]) == 2                     # merged across the two calls


# --------------------------------------------------------------------------- TEMIS

def _add_temis(man, base):
    man["datasets"]["uv"] = {
        "title": "uv", "acquire": "provider-scripted", "provider": "KNMI", "license": "credit",
        "redistribution": "not granted", "attribution": "Credit KNMI/ESA TEMIS.",
        "terms": {"id": "knmi-temis", "url": "https://example.org/temis", "summary": "credit; no redistribution"},
        "instructions": ["get it"], "estimate": {"download_bytes": 365 * 20, "disk_bytes": 365 * 20},
        "download": {"method": "daily", "range": True, "magic": "0e031301",
                     "url": base + "/uv/v2.0/{year}/{mm}/{var}{yyyymmdd}.hdf"},
        "layout": [{"rule": "daily", "path": "TEMIS/C4/raw/{var}/{year}/{var}{yyyymmdd}.hdf",
                    "vars": {"var": ["uvief"]}, "years": [2016], "known_gaps": ["20160229"],
                    "magic": "0e031301"}],
    }
    man["variables"]["temis"] = {"requires": ["uv"]}
    return man


def _days_2016():
    d = dt.date(2016, 1, 1)
    while d.year == 2016:
        if d != dt.date(2016, 2, 29):
            yield d
        d += dt.timedelta(days=1)


def test_temis_daily_files_skip_present_days_and_resume_with_range(prov, h, archive, data_dir):
    man = _add_temis(_base_manifest(h, archive, prov.base), prov.base)
    todo = {dt.date(2016, 1, 1), dt.date(2016, 3, 1), dt.date(2016, 12, 31)}
    for d in _days_2016():
        payload = HDF4 + f"uv-{d:%Y%m%d}".encode() * 50
        if d in todo:
            prov.files[f"/uv/v2.0/2016/{d:%m}/uvief{d:%Y%m%d}.hdf"] = payload
        else:
            p = data_dir / f"TEMIS/C4/raw/uvief/2016/uvief{d:%Y%m%d}.hdf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
    key = "/uv/v2.0/2016/03/uvief20160301.hdf"
    prov.drops[key] = (100, 1)
    rep = fetch(["temis"], data_dir=data_dir, manifest=man, accept_terms=["knmi-temis"], **h.fast())
    assert rep.ok, rep.to_dict()
    assert rep.items[0].files == 3 and rep.items[0].skipped == 362
    paths = [r["path"] for r in prov.requests]
    assert sorted(paths) == sorted(f"/uv/v2.0/2016/{d:%m}/uvief{d:%Y%m%d}.hdf" for d in [
        dt.date(2016, 1, 1), dt.date(2016, 3, 1), dt.date(2016, 3, 1), dt.date(2016, 12, 31)])
    retry = [r for r in prov.requests if r["path"] == key][1]
    assert retry["range"] == "bytes=100-"                 # resumed within the call, not restarted
    assert not any("20160229" in p for p in paths)        # the known gap is never requested
    assert (data_dir / "TEMIS/C4/raw/uvief/2016/uvief20160301.hdf").read_bytes() == prov.files[key]
    assert rep.status.dataset("uv").status == "ready"


def test_temis_missing_day_and_error_page(prov, h, archive, data_dir):
    man = _add_temis(_base_manifest(h, archive, prov.base), prov.base)
    for d in _days_2016():
        p = data_dir / f"TEMIS/C4/raw/uvief/2016/uvief{d:%Y%m%d}.hdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(HDF4 + b"x")
    (data_dir / "TEMIS/C4/raw/uvief/2016/uvief20160704.hdf").unlink()   # not on the server: 403
    (data_dir / "TEMIS/C4/raw/uvief/2016/uvief20160705.hdf").unlink()
    prov.files["/uv/v2.0/2016/07/uvief20160705.hdf"] = b"<html>maintenance</html>"  # octet-stream lie
    with pytest.raises(DataCorruptError) as e:
        fetch(["temis"], data_dir=data_dir, manifest=man, accept_terms=["knmi-temis"], **h.fast())
    failed = {f["file"]: f["kind"] for f in e.value.details["failed"]}
    assert failed == {"uvief20160704.hdf": "data_missing", "uvief20160705.hdf": "data_corrupt"}
    assert not (data_dir / "TEMIS/C4/raw/uvief/2016/uvief20160705.hdf").exists()
    assert len([r for r in prov.requests if "20160704" in r["path"]]) == 1    # 403: no retries
    assert status(["temis"], data_dir=data_dir, manifest=man).dataset("uv").status == "incomplete"


# --------------------------------------------------------------------------- ACAG (Box)

ACAG_RX = ("^V5NA05(?:\\.02)?\\.Hybrid(?P<species>PM25|BC|DUST|NH4|NO3|OM|SO4|SS)(?:-(?P=species))?"
           "(?P<variant>\\.FromBiomass)?\\.xNorthAmerica\\.(?P<year>\\d{4})\\d{3}-\\d{7}\\.nc$")


def _add_acag(man, base):
    man["lists"]["years13"] = [2013]
    man["datasets"]["acag"] = {
        "title": "acag", "acquire": "provider-scripted", "provider": "WashU ACAG", "license": "see page",
        "redistribution": "not confirmed", "attribution": "Cite the paper.",
        "terms": {"id": "acag-v5na05", "url": "https://example.org/acag", "summary": "cite; do not redistribute"},
        "instructions": ["get it"], "estimate": {"download_bytes": 6000, "disk_bytes": 6000},
        "import": [{"regex": ACAG_RX, "target": "ACAG/C4/xNorthAmerica/{species}{variant}/BiWeekly/{name}",
                    "map": {"variant": {"": "", ".FromBiomass": "_bm"}}, "years": "@years13",
                    "magic": "894844460d0a1a0a"}],
        "download": {"method": "box", "base": base, "range": True, "magic": "894844460d0a1a0a",
                     "shares": [{"name": "plainshare", "paths": ["GWRPM25/xNorthAmerica/BiWeekly"]},
                                {"name": "bmshare", "paths": ["NetCDF/{species}/xNorthAmerica/BiWeekly"],
                                 "vars": {"species": ["BC"]}}]},
        "layout": [{"rule": "count", "dir": "ACAG/C4/xNorthAmerica/{dir}/BiWeekly", "glob": "*.nc",
                    "vars": {"dir": ["PM25", "BC_bm"]}, "expect": 3, "regex": "\\.(\\d{7})-(\\d{7})\\.nc$",
                    "magic": "894844460d0a1a0a"}],
    }
    man["variables"]["acag"] = {"requires": ["acag"]}
    return man


def _box_tree(prov):
    """Two shares shaped like the WashU ones; 3 files of 2013 per folder plus noise."""
    def folder(i, name):
        return {"typedID": f"d_{i}", "type": "folder", "id": i, "name": name}

    def file(i, name, data):
        prov.blobs[str(i)] = data
        return {"typedID": f"f_{i}", "type": "file", "id": i, "name": name, "itemSize": len(data)}

    periods = ["2013001-2013014", "2013015-2013028", "2013029-2013042"]
    plain = [file(100 + k, f"V5NA05.HybridPM25.xNorthAmerica.{p}.nc", HDF5 + b"pm25-" + p.encode() * 40)
             for k, p in enumerate(periods)]
    plain.append(file(110, "V5NA05.HybridPM25.xNorthAmerica.2012001-2012014.nc", HDF5 + b"old"))
    plain.append(file(111, "README.png", b"\x89PNG"))
    prov.box[("plainshare", None)] = [folder(2, "GWRPM25"), file(112, "about.txt", b"hi")]
    prov.box[("plainshare", "2")] = [folder(3, "xNorthAmerica"), folder(4, "xGlobal")]
    prov.box[("plainshare", "3")] = [folder(5, "Monthly"), folder(6, "BiWeekly")]
    prov.box[("plainshare", "6")] = plain
    bm = [file(200 + k, f"V5NA05.02.HybridBC-BC.FromBiomass.xNorthAmerica.{p}.nc",
               HDF5 + b"bcbm-" + p.encode() * 40) for k, p in enumerate(periods)]
    prov.box[("bmshare", None)] = [folder(20, "NetCDF")]
    prov.box[("bmshare", "20")] = [folder(21, "OM"), folder(22, "BC")]
    prov.box[("bmshare", "22")] = [folder(23, "xNorthAmerica")]
    prov.box[("bmshare", "23")] = [folder(24, "BiWeekly")]
    prov.box[("bmshare", "24")] = bm
    return plain[:3], bm


def test_box_listing_pages_and_routes_files_by_name(prov, h, archive, data_dir):
    plain, bm = _box_tree(prov)
    man = _add_acag(_base_manifest(h, archive, prov.base), prov.base)
    prov.drops["/boxcloud/101"] = (50, 1)                  # one cut: resumed with Range
    rep = fetch(["acag"], data_dir=data_dir, manifest=man, accept_terms=["acag-v5na05"], **h.fast())
    assert rep.ok, rep.to_dict()
    assert rep.items[0].files == 6
    for it in plain:
        p = data_dir / "ACAG/C4/xNorthAmerica/PM25/BiWeekly" / it["name"]
        assert p.read_bytes() == prov.blobs[str(it["id"])]
    for it in bm:
        assert (data_dir / "ACAG/C4/xNorthAmerica/BC_bm/BiWeekly" / it["name"]).is_file()
    pages = [r["path"] for r in prov.requests if r["path"].startswith("/s/plainshare/folder/6")]
    assert pages == ["/s/plainshare/folder/6?page=1", "/s/plainshare/folder/6?page=2",
                     "/s/plainshare/folder/6?page=3"]   # 5 items, 2 per page
    blobs = [r for r in prov.requests if r["path"].startswith("/boxcloud/")]
    assert not any(r["path"].endswith(("/110", "/111", "/112")) for r in blobs)   # 2012, png, txt
    retry = [r for r in blobs if r["path"] == "/boxcloud/101"]
    assert [r["range"] for r in retry] == [None, "bytes=50-"]
    assert not any("/s/bmshare/folder/21" in r["path"] for r in prov.requests)     # OM not walked
    assert rep.status.dataset("acag").status == "ready"


def test_box_fallbacks_and_a_server_that_ignores_range(prov, h, archive, data_dir):
    _box_tree(prov)
    prov.marker, prov.page_count = "Box.prefetchedData", False   # older page format, no page count
    prov.ignore_range = True
    prov.drops["/boxcloud/200"] = (40, 1)
    man = _add_acag(_base_manifest(h, archive, prov.base), prov.base)
    events = []
    rep = fetch(["acag"], data_dir=data_dir, manifest=man, accept_terms=["acag-v5na05"],
                on_event=events.append, **h.fast())
    assert rep.ok and rep.items[0].files == 6
    assert any(e["ev"] == "range_ignored" and e["artifact"].endswith("2013001-2013014.nc") for e in events)
    assert (data_dir / "ACAG/C4/xNorthAmerica/BC_bm/BiWeekly/"
            "V5NA05.02.HybridBC-BC.FromBiomass.xNorthAmerica.2013001-2013014.nc").read_bytes() == prov.blobs["200"]


def test_box_share_that_moved(prov, h, archive, data_dir):
    _box_tree(prov)
    del prov.box[("bmshare", "22")]
    man = _add_acag(_base_manifest(h, archive, prov.base), prov.base)
    with pytest.raises(DataMissingError, match="shared folder is gone"):
        fetch(["acag"], data_dir=data_dir, manifest=man, accept_terms=["acag-v5na05"], **h.fast())


def test_parse_box_page_shapes():
    data = {"a": {"b": [{"items": [{"typedID": "f_9", "name": "x.nc", "itemSize": 12},
                                   {"typedID": "d_8", "name": "sub"},
                                   {"type": "web_link", "id": 1, "name": "link"}]}]}}
    items, pages = parse_box_page(f"<script>Box.postStreamData = {json.dumps(data)};</script>")
    assert [(i.type, i.id, i.name, i.size) for i in items] == [("file", "9", "x.nc", 12),
                                                              ("folder", "8", "sub", None)]
    assert pages is None
    from spacescans.data._providers import ProviderFormatError
    with pytest.raises(ProviderFormatError):
        parse_box_page("<html>Box changed everything</html>")


def test_real_manifest_provider_files_are_what_the_readers_expect():
    """No network: the FAQSD and TEMIS file lists come from the layout rules."""
    from spacescans.data import load_manifest, remote_files
    from spacescans.data.transport import Transport

    man = load_manifest()
    faqsd = list(remote_files(man, man.datasets["faqsd"], transport=Transport()))
    assert len(faqsd) == 14
    assert faqsd[0].url == ("https://ofmpub.epa.gov/rsig/rsigserver?data/FAQSD/outputs/"
                            "2013_ozone_daily_8hour_maximum.txt.gz")
    assert faqsd[0].target == "FAQSD/C4/2013_ozone_daily_8hour_maximum.txt"
    assert sum(1 for f in faqsd if f.reference_bytes) == 8
    temis = list(remote_files(man, man.datasets["temis_uv_raw"], transport=Transport()))
    assert len(temis) == 4 * 2555 == 10220                # 7 years incl. 2016-02-29, minus that gap
    assert temis[0].url == "https://d1qb6yzwaaq4he.cloudfront.net/uvradiation/v2.0/2013/01/uvddc20130101.hdf"
    assert temis[0].target == "TEMIS/C4/raw/uvddc/2013/uvddc20130101.hdf"
