"""aorus-lcd-giphy: a local page to search GIPHY and send a GIF to the AORUS LCD.

It does not talk to the panel itself: it hands the GIF to the aorus-lcd
service over its local API (/api/media, /api/preview, /api/content), so the
service keeps owning the bus, the upload pacing and its bus lock.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .giphy import RATINGS, Giphy, GiphyError
from .optimize import FITS, QUALITIES, optimize

API_HEADER = "X-Aorus-Lcd"
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

    def _post_json(self, path, data):
        return self._call(path, json.dumps(data).encode(), {"Content-Type": "application/json"})

    def status(self):
        return json.loads(self._call("/api/status")[0])

    def save_media(self, filename, data):
        return json.loads(self._call("/api/media", data, {"X-Filename": filename})[0])

    def preview(self, content):
        body, headers = self._post_json("/api/preview", {"content": content})
        return body, headers.get("Content-Type", "image/gif"), float(headers.get("X-Estimate-Seconds") or 0)

    def send(self, content):
        return json.loads(self._post_json("/api/content", {"content": content})[0])


def _page():
    return resources.files("aorus_lcd_giphy").joinpath("static/index.html").read_bytes()


def _content(data):
    """The prepared GIF is already 320x170, so the service's own fit is a no-op."""
    source = str(data.get("source", ""))
    if not source:
        raise ValueError("missing source")
    return {"type": "gif", "source": source, "fit": "contain", "background": "#000000"}


class Downloads:
    """The last few GIPHY downloads, so changing fit or quality does not refetch."""

    def __init__(self, giphy, size=8):
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


def prepare(downloads, daemon, data):
    gif_id, fit, quality = str(data.get("id", "")), data.get("fit", "auto"), data.get("quality", "balanced")
    if fit not in FITS or quality not in QUALITIES:
        raise ValueError(f"fit is one of {', '.join(FITS)}; quality one of {', '.join(QUALITIES)}")
    gif, frames, used = optimize(downloads.get(gif_id), fit, quality)
    media = daemon.save_media(f"giphy-{gif_id}-{used}-{quality}.gif", gif)
    return {**media, "frames": frames, "fit": used}


class Handler(BaseHTTPRequestHandler):
    server_version = "aorus-lcd-giphy"
    giphy = daemon = downloads = None

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
        except GiphyError as e:
            self._send(502, {"error": str(e)})
        except DaemonError as e:
            self._send(e.status if 400 <= e.status < 600 else 502, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": str(e)})

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, {"error": "forbidden host"})
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path in ("/", "/index.html"):
            return self._send(200, _page(), "text/html; charset=utf-8")
        if url.path == "/api/search":
            return self._guard(lambda: self._send(200, self.giphy.search(query.get("q", ""),
                                                                         int(query.get("offset", 0)))))
        if url.path == "/api/status":
            return self._guard(lambda: self._send(200, self.daemon.status()))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed_host() or self.headers.get(API_HEADER) != "1":
            return self._send(403, {"error": "forbidden"})

        def route():
            data = self._json()
            if self.path == "/api/prepare":
                return self._send(200, prepare(self.downloads, self.daemon, data))
            if self.path == "/api/preview":
                image, mime, seconds = self.daemon.preview(_content(data))
                return self._send(200, image, mime, {"X-Estimate-Seconds": f"{seconds:.1f}"})
            if self.path == "/api/send":
                return self._send(202, self.daemon.send(_content(data)))
            return self._send(404, {"error": "not found"})
        self._guard(route)


def make_server(giphy, daemon, bind="127.0.0.1", port=5091):
    handler = type("BoundHandler", (Handler,), {"giphy": giphy, "daemon": daemon,
                                                "downloads": Downloads(giphy)})
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
    log.info("GIPHY page: http://%s:%d/ (aorus-lcd service at %s)", args.bind, args.port, args.daemon)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
