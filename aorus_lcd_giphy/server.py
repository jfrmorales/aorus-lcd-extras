"""aorus-lcd-giphy: one address for the AORUS LCD: aorus-lcd's panel UI plus a GIPHY page.

  /            aorus-lcd's own web UI, proxied, with a nav bar added
  /giphy       search GIPHY and send a GIF to the panel
  /api/...     forwarded verbatim to the aorus-lcd service
  /giphy/api/  search, prepare (download + optimize) a GIF, and favourites

It does not talk to the panel itself: everything goes through the aorus-lcd
service's local API, so the service keeps owning the bus, the upload pacing
and its bus lock.

Same protection model as aorus-lcd's own UI: loopback only, the Host header
must be the loopback address (DNS rebinding), and every POST carries an
`X-Aorus-Lcd: 1` header that another origin cannot add without a CORS
preflight this server never approves.
"""
import argparse
import collections
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .favorites import Favorites
from .giphy import RATINGS, Giphy, GiphyError, RateLimited, send_from_thumb
from .optimize import FITS, QUALITIES, estimate, optimize

API_HEADER = "X-Aorus-Lcd"
FORWARD_REQUEST = ("Content-Type", "X-Filename", API_HEADER)
FORWARD_RESPONSE = ("Content-Type", "X-Estimate-Seconds")
MAX_FORWARD = 33 << 20                     # aorus-lcd's own upload limit is 32 MB
NAV_STYLE = (b"<style>.xnav{display:flex;gap:4px;background:#1a1a1f;border:1px solid #26262d;"
             b"border-radius:11px;padding:3px}.xnav a{color:#8b8b96;text-decoration:none;padding:6px 14px;"
             b"border-radius:8px;font-size:13px}.xnav a[aria-current]{background:#131316;color:#ececf1;"
             b"box-shadow:0 0 0 1px #26262d}</style>")
log = logging.getLogger("aorus-lcd-giphy")


class DaemonError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Daemon:
    """Client for the aorus-lcd service's local API."""

    def __init__(self, url="http://127.0.0.1:5090", opener=None, timeout=30):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.open = opener or urllib.request.urlopen

    def _call(self, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, headers={API_HEADER: "1", **(headers or {})},
                                     method="POST" if body is not None else "GET")
        try:
            with self.open(req, timeout=self.timeout) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error", str(e))
            except ValueError:
                message = str(e)
            raise DaemonError(e.code, message) from None
        except OSError as e:
            raise DaemonError(503, f"aorus-lcd service not reachable at {self.url} ({e})") from None

    def save_media(self, filename, data):
        return json.loads(self._call("/api/media", data, {"X-Filename": filename})[0])

    def page(self):
        req = urllib.request.Request(self.url + "/")
        try:
            with self.open(req, timeout=self.timeout) as r:
                return r.read()
        except OSError as e:
            raise DaemonError(503, f"aorus-lcd service not reachable at {self.url} ({e})") from None

    def forward(self, method, path, body, headers):
        """(status, headers, body) of the service's answer, errors included."""
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with self.open(req, timeout=self.timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
        except OSError as e:
            body = json.dumps({"error": f"aorus-lcd service not reachable at {self.url} ({e})"}).encode()
            return 503, {"Content-Type": "application/json"}, body


def nav(current):
    links = [("/", "Panel"), ("/giphy", "GIPHY")]
    return ('<nav class="xnav" aria-label="Sections">' + "".join(
        f'<a href="{href}"{" aria-current=page" if href == current else ""}>{label}</a>'
        for href, label in links) + "</nav>").encode()


def panel_page(html):
    """aorus-lcd's page with the nav bar in its header (or on top, if its markup changed)."""
    html = html.replace(b"</head>", NAV_STYLE + b"</head>", 1)
    marker = b'<div class="grow"></div>'
    if marker in html:
        return html.replace(marker, marker + nav("/"), 1)
    return html.replace(b"<body>", b'<body><div style="padding:8px 24px">' + nav("/") + b"</div>", 1)


def giphy_page():
    html = resources.files("aorus_lcd_giphy").joinpath("static/index.html").read_bytes()
    return html.replace(b"</head>", NAV_STYLE + b"</head>", 1).replace(b"<!--nav-->", nav("/giphy"), 1)


class Downloads:
    """Recent GIPHY downloads (~700 KB each), so estimating a page of results,
    changing fit or quality, and sending one never fetch the same GIF twice."""

    def __init__(self, giphy, size=48):
        self.giphy, self.size = giphy, size
        self.items = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, gif_id):
        with self.lock:
            if gif_id in self.items:
                self.items.move_to_end(gif_id)
                return self.items[gif_id]
        _, data = self.giphy.download(gif_id)
        with self.lock:
            self.items[gif_id] = data
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return data


class Estimates:
    """Upload-time estimates for search results, cached by (id, fit, quality).
    A few at a time: each one decodes a GIF and runs frames through Pillow."""

    def __init__(self, downloads, size=2000, workers=3):
        self.downloads, self.size = downloads, size
        self.items = collections.OrderedDict()
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(workers)

    def get(self, gif_id, fit="auto", quality="balanced"):
        if fit not in FITS or quality not in QUALITIES:
            raise ValueError(f"fit is one of {', '.join(FITS)}; quality one of {', '.join(QUALITIES)}")
        key = (gif_id, fit, quality)
        with self.lock:
            if key in self.items:
                return self.items[key]
        with self.slots:
            seconds, frames = estimate(self.downloads.get(gif_id), fit, quality)
        result = {"id": gif_id, "seconds": round(seconds, 1), "frames": frames}
        with self.lock:
            self.items[key] = result
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return result


def prepare(downloads, daemon, data):
    gif_id, fit, quality = str(data.get("id", "")), data.get("fit", "auto"), data.get("quality", "balanced")
    if fit not in FITS or quality not in QUALITIES:
        raise ValueError(f"fit is one of {', '.join(FITS)}; quality one of {', '.join(QUALITIES)}")
    gif, frames, used = optimize(downloads.get(gif_id), fit, quality)
    media = daemon.save_media(f"giphy-{gif_id}-{used}-{quality}.gif", gif)
    return {**media, "frames": frames, "fit": used}


class Handler(BaseHTTPRequestHandler):
    server_version = "aorus-lcd-giphy"
    giphy = daemon = downloads = favorites = estimates = None

    def log_message(self, *args):
        pass

    def _allowed_host(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _send(self, code, body=b"", mime="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data: https://*.giphy.com; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 << 10:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _guard(self, fn):
        try:
            return fn()
        except RateLimited as e:
            self._send(429, {"error": str(e), "rate_limited": True})
        except GiphyError as e:
            self._send(502, {"error": str(e)})
        except DaemonError as e:
            self._send(e.status if 400 <= e.status < 600 else 502, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": str(e)})

    def _known(self, favorites):
        """Favourites can be sent without an API call: tell the client where they are."""
        for item in favorites:
            self.giphy.remember(item["id"], item.get("send") or send_from_thumb(item.get("thumb")))
        return favorites

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_FORWARD:
            return self._send(413, {"error": "upload too large"})
        body = self.rfile.read(length) if method == "POST" else None
        headers = {k: self.headers[k] for k in FORWARD_REQUEST if self.headers.get(k) is not None}
        code, rh, data = self.daemon.forward(method, self.path, body, headers)
        extra = {k: rh.get(k) for k in FORWARD_RESPONSE[1:] if rh.get(k)}
        self._send(code, data, rh.get("Content-Type") or "application/octet-stream", extra)

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, {"error": "forbidden host"})
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path in ("/", "/index.html"):
            return self._guard(lambda: self._send(200, panel_page(self.daemon.page()), "text/html; charset=utf-8"))
        if url.path in ("/giphy", "/giphy/"):
            return self._send(200, giphy_page(), "text/html; charset=utf-8")
        if url.path == "/giphy/api/search":
            return self._guard(lambda: self._send(200, self.giphy.search(query.get("q", ""),
                                                                         int(query.get("offset", 0)))))
        if url.path == "/giphy/api/favorites":
            return self._send(200, {"items": self._known(self.favorites.list())})
        if url.path == "/giphy/api/usage":
            return self._send(200, self.giphy.usage())
        if url.path == "/giphy/api/estimate":
            return self._guard(lambda: self._send(200, self.estimates.get(
                query.get("id", ""), query.get("fit", "auto"), query.get("quality", "balanced"))))
        if url.path.startswith("/api/"):
            return self._forward("GET")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed_host() or self.headers.get(API_HEADER) != "1":
            return self._send(403, {"error": "forbidden"})
        if self.path.startswith("/api/"):
            return self._forward("POST")

        def route():
            data = self._json()
            if self.path == "/giphy/api/prepare":
                return self._send(200, prepare(self.downloads, self.daemon, data))
            if self.path == "/giphy/api/favorites":
                action = data.get("action")
                if action == "add":
                    return self._send(200, {"items": self._known(self.favorites.add(data.get("item")))})
                if action == "remove":
                    return self._send(200, {"items": self.favorites.remove(data.get("id", ""))})
                raise ValueError("action must be add or remove")
            return self._send(404, {"error": "not found"})
        self._guard(route)


def state_dir():
    return Path(os.environ.get("STATE_DIRECTORY") or
                Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "aorus-lcd-giphy")


def make_server(giphy, daemon, bind="127.0.0.1", port=5091, favorites=None):
    favorites = favorites or Favorites(state_dir() / "favorites.json")
    downloads = Downloads(giphy)
    handler = type("BoundHandler", (Handler,), {"giphy": giphy, "daemon": daemon, "downloads": downloads,
                                                "favorites": favorites, "estimates": Estimates(downloads)})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


def _api_key():
    key = os.environ.get("GIPHY_API_KEY", "").strip()
    path = os.environ.get("GIPHY_API_KEY_FILE")
    if not key and path:
        with open(path) as f:
            key = f.read().strip()
    return key


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aorus-lcd-giphy", description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="127.0.0.1", help="keep it on loopback (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5091)
    ap.add_argument("--daemon", default=os.environ.get("AORUS_LCD_URL", "http://127.0.0.1:5090"),
                    help="aorus-lcd service URL (default http://127.0.0.1:5090)")
    ap.add_argument("--rating", default=os.environ.get("GIPHY_RATING", "pg"), choices=RATINGS)
    ap.add_argument("--lang", default=os.environ.get("GIPHY_LANG", "es"), help="search language")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        giphy = Giphy(_api_key(), args.rating, args.lang)
    except GiphyError as e:
        raise SystemExit(str(e))
    server = make_server(giphy, Daemon(args.daemon), args.bind, args.port)
    log.info("AORUS LCD: http://%s:%d/ (panel) and /giphy (aorus-lcd service at %s)",
             args.bind, args.port, args.daemon)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
