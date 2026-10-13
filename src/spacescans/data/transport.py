"""A small HTTP(S) client on ``http.client``, standard library only.

Why not ``urllib.request``: it has one timeout for connect and reads alike,
follows redirects before we can read a share link's ``Location``, and hides
the cookie a SharePoint share hands out. Here:

* the connect timeout (default 30 s) and the idle-read timeout (default
  300 s) are separate, and there is no overall cap: a 38 GB file may take
  hours, which is fine as long as bytes keep arriving;
* redirects are followed by hand (or not, ``follow_redirects=False``);
* cookies are kept per host, for the life of the Transport only;
* ``https_proxy`` / system proxy settings are honoured via CONNECT tunnels,
  except for loopback hosts;
* certificates are checked against the OS trust store when the optional
  ``truststore`` package is installed (macOS Keychain, so institutional TLS
  proxies work), otherwise against OpenSSL's defaults (``SSL_CERT_FILE``).
"""
from __future__ import annotations

import base64
import http.client
import ssl
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from spacescans.data.errors import NetworkError

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
REDIRECTS = (301, 302, 303, 307, 308)


def default_ssl_context() -> ssl.SSLContext:
    try:
        import truststore  # optional; uses the OS certificate store
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def _user_agent() -> str:
    from spacescans import __version__
    return f"spacescans/{__version__} (+https://github.com/IU-Ultraman/spacescans)"


@dataclass
class Response:
    status: int
    url: str
    _resp: http.client.HTTPResponse
    _conn: http.client.HTTPConnection

    def header(self, name: str, default: str | None = None) -> str | None:
        return self._resp.getheader(name, default)

    @property
    def headers(self) -> http.client.HTTPMessage:
        return self._resp.headers

    def read(self, n: int | None = -1) -> bytes:
        # Before Python 3.12, http.client's read(-1) ignores Content-Length and waits
        # for EOF, which never comes on a keep-alive connection; read() does not.
        if n is None or n < 0:
            return self._resp.read()
        return self._resp.read(n)

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Transport:
    def __init__(self, *, connect_timeout: float = 30.0, read_timeout: float = 300.0,
                 max_redirects: int = 10, ssl_context: ssl.SSLContext | None = None,
                 proxies: Mapping[str, str] | None = None, user_agent: str | None = None) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_redirects = max_redirects
        self._ssl = ssl_context
        self._proxies = proxies
        self.user_agent = user_agent or _user_agent()
        self.cookies: dict[str, dict[str, str]] = {}

    # -- public --------------------------------------------------------
    def open(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
             follow_redirects: bool = True) -> Response:
        """Send a request; return the (final) response with its body unread."""
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        hdrs.update(headers or {})
        for _ in range(self.max_redirects + 1):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise NetworkError(f"unsupported URL: {url!r}")
            conn = self._connect(parts)
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            req_headers = dict(hdrs)
            jar = self.cookies.get(parts.hostname.lower())
            if jar:
                req_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
            try:
                conn.request(method, target, headers=req_headers)
                resp = conn.getresponse()
            except BaseException:
                conn.close()
                raise
            self._store_cookies(parts.hostname.lower(), resp)
            location = resp.getheader("Location")
            if follow_redirects and resp.status in REDIRECTS and location:
                try:
                    resp.read(65536)
                finally:
                    resp.close()
                    conn.close()
                if resp.status == 303:
                    method = "GET"
                url = urllib.parse.urljoin(url, location)
                continue
            return Response(status=resp.status, url=url, _resp=resp, _conn=conn)
        raise NetworkError(f"too many redirects fetching {url}")

    # -- internals -----------------------------------------------------
    def _proxy_for(self, parts: urllib.parse.SplitResult) -> urllib.parse.SplitResult | None:
        host = (parts.hostname or "").lower()
        if host in _LOOPBACK:
            return None
        proxies = self._proxies if self._proxies is not None else urllib.request.getproxies()
        proxy = proxies.get(parts.scheme)
        if not proxy:
            return None
        if self._proxies is None and urllib.request.proxy_bypass(host):
            return None
        if "://" not in proxy:
            proxy = "http://" + proxy
        return urllib.parse.urlsplit(proxy)

    def _connect(self, parts: urllib.parse.SplitResult) -> http.client.HTTPConnection:
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        proxy = self._proxy_for(parts)
        if parts.scheme == "https":
            ctx = self._ssl or default_ssl_context()
            if proxy is not None:
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    proxy.hostname, proxy.port or 8080, timeout=self.connect_timeout, context=ctx)
            else:
                conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout, context=ctx)
        else:
            if proxy is not None:
                conn = http.client.HTTPConnection(proxy.hostname, proxy.port or 8080,
                                                  timeout=self.connect_timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
        if proxy is not None:
            tunnel_headers = {}
            if proxy.username:
                cred = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
                tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
            conn.set_tunnel(host, port, headers=tunnel_headers)
        try:
            conn.connect()
        except BaseException:
            conn.close()
            raise
        if conn.sock is not None:
            conn.sock.settimeout(self.read_timeout)
        return conn

    def _store_cookies(self, host: str, resp: http.client.HTTPResponse) -> None:
        for raw in resp.headers.get_all("Set-Cookie") or ():
            pair = raw.split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            if name:
                self.cookies.setdefault(host, {})[name] = value.strip()


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    """``bytes START-END/TOTAL`` -> (start, end, total or None)."""
    if not value:
        return None
    try:
        unit, rest = value.strip().split(" ", 1)
        if unit.lower() != "bytes":
            return None
        span, total = rest.split("/", 1)
        start, end = span.split("-", 1)
        return int(start), int(end), (None if total.strip() == "*" else int(total))
    except ValueError:
        return None
