"""Minimal GIPHY API client (stdlib only).

The API key stays on the server: the browser only ever sees GIF ids, titles
and media URLs. Downloads are restricted to https://*.giphy.com and go by id:
the URL comes from GIPHY's own answers (a search, a favourite saved from one),
never from the browser.

API calls are scarce: a beta key allows 100 an hour. Only searches use them.
Media downloads come from GIPHY's CDN, which does not count, using the URL
each search result already carries; searches are cached for a while.
"""
import collections
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.giphy.com/v1/gifs"
ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
# Smallest renditions that still cover the 320x170 panel without upscaling much.
# fixed_height is 200 px tall; downsized keeps the original size under ~2 MB.
SEND_RENDITIONS = ("fixed_height", "downsized", "original")
THUMB_RENDITIONS = ("fixed_height_small", "fixed_height", "preview_gif")
MAX_DOWNLOAD = 8 << 20
RATINGS = ("g", "pg", "pg-13", "r")
SEARCH_TTL = 15 * 60                  # seconds a search or trending page is reused
BETA_LIMIT = 100                      # API calls an hour on a GIPHY beta key


class GiphyError(Exception):
    pass


class RateLimited(GiphyError):
    pass


RATE_LIMITED = (f"GIPHY's limit for this key is used up ({BETA_LIMIT} calls an hour on a beta key). "
                "Searches already made, favorites and sending still work; new searches will "
                "work again within the hour, or ask GIPHY for a production key.")


def _media_url(url):
    """Only https URLs on GIPHY's own media hosts."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "giphy.com" or host.endswith(".giphy.com")):
        raise GiphyError(f"refusing a non-GIPHY media URL: {host or url!r}")
    return url


def _rendition(images, names):
    for name in names:
        image = images.get(name) or {}
        if image.get("url"):
            return name, image
    return None, None


def send_from_thumb(thumb):
    """fixed_height_small (.../100.gif) and fixed_height (.../200.gif) share a path."""
    return thumb[:-len("100.gif")] + "200.gif" if thumb and thumb.endswith("/100.gif") else None


def summarize(item):
    """The fields the page needs from one GIPHY result."""
    images = item.get("images") or {}
    _, thumb = _rendition(images, THUMB_RENDITIONS)
    if not thumb or not ID_RE.match(str(item.get("id", ""))):
        return None
    _, send = _rendition(images, SEND_RENDITIONS)
    return {
        "id": item["id"],
        "title": item.get("title") or "GIF",
        "thumb": _media_url(thumb["url"]),
        "send": _media_url(send["url"]) if send else None,
        "width": int(thumb.get("width") or 0),
        "height": int(thumb.get("height") or 0),
        "size": int((send or {}).get("size") or 0),
    }


class Giphy:
    def __init__(self, api_key, rating="pg", lang="es", opener=None, timeout=10):
        if not api_key:
            raise GiphyError("no GIPHY API key (set GIPHY_API_KEY or GIPHY_API_KEY_FILE)")
        if rating not in RATINGS:
            raise GiphyError(f"rating must be one of {', '.join(RATINGS)}")
        self.api_key, self.rating, self.lang, self.timeout = api_key, rating, lang, timeout
        self.open = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.media = collections.OrderedDict()       # GIF id -> URL of the rendition to send
        self.pages = collections.OrderedDict()       # (endpoint, params) -> (time, result)
        self.calls = collections.deque()             # times of recent API calls
        self.clock = time.monotonic

    def _redact(self, text):
        """Errors can carry the request URL, and with it the key."""
        return text.replace(self.api_key, "***")

    def usage(self):
        """API calls in the last hour, as counted by this process."""
        with self.lock:
            cutoff = self.clock() - 3600
            while self.calls and self.calls[0] < cutoff:
                self.calls.popleft()
            return {"calls": len(self.calls), "limit": BETA_LIMIT}

    def _get(self, path, **params):
        query = urllib.parse.urlencode({"api_key": self.api_key, **params})
        with self.lock:
            self.calls.append(self.clock())
        try:
            with self.open(f"{API}/{path}?{query}", timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(RATE_LIMITED) from None
            raise GiphyError(f"GIPHY answered {e.code}") from None
        except OSError as e:
            reason = str(getattr(e, "reason", None) or type(e).__name__)
            raise GiphyError(f"GIPHY request failed: {self._redact(reason)}") from None
        status = (data.get("meta") or {}).get("status", 200)
        if status == 429:
            raise RateLimited(RATE_LIMITED)
        if status != 200:
            raise GiphyError(f"GIPHY answered {status}: {(data.get('meta') or {}).get('msg', '')}")
        return data

    def remember(self, gif_id, url):
        """Where to download a GIF from, so sending it costs no API call."""
        if not url or not ID_RE.match(str(gif_id)):
            return
        try:
            url = _media_url(url)
        except GiphyError:
            return
        with self.lock:
            self.media[gif_id] = url
            self.media.move_to_end(gif_id)
            while len(self.media) > 5000:
                self.media.popitem(last=False)

    def _list(self, data):
        items = [s for s in (summarize(i) for i in data.get("data") or []) if s]
        for item in items:
            self.remember(item["id"], item["send"])
        page = data.get("pagination") or {}
        return {"items": items, "offset": page.get("offset", 0),
                "total": page.get("total_count", len(items))}

    def _page(self, path, **params):
        """A search or trending page, from the cache when it is recent enough."""
        key = (path, tuple(sorted(params.items())))
        with self.lock:
            hit = self.pages.get(key)
            if hit and self.clock() - hit[0] < SEARCH_TTL:
                return hit[1]
        result = self._list(self._get(path, **params))
        with self.lock:
            self.pages[key] = (self.clock(), result)
            while len(self.pages) > 300:
                self.pages.popitem(last=False)
        return result

    def search(self, q, offset=0, limit=24):
        q = (q or "").strip()[:50]
        if not q:
            return self.trending(offset, limit)
        return self._page("search", q=q, offset=max(0, int(offset)), limit=limit,
                          rating=self.rating, lang=self.lang)

    def trending(self, offset=0, limit=24):
        return self._page("trending", offset=max(0, int(offset)), limit=limit, rating=self.rating)

    def download(self, gif_id):
        """(filename, GIF bytes) of the rendition sent to the panel. Free when the
        GIF came from a search or a favourite; otherwise one API call finds it."""
        if not ID_RE.match(gif_id or ""):
            raise GiphyError("bad GIF id")
        with self.lock:
            url = self.media.get(gif_id)
        if not url:
            item = self._get(gif_id).get("data") or {}
            _, image = _rendition(item.get("images") or {}, SEND_RENDITIONS)
            if not image:
                raise GiphyError("that GIF has no usable rendition")
            url = _media_url(image["url"])
            self.remember(gif_id, url)
        try:
            with self.open(_media_url(url), timeout=self.timeout * 3) as r:
                data = r.read(MAX_DOWNLOAD + 1)
        except OSError as e:
            reason = str(getattr(e, "reason", None) or type(e).__name__)
            raise GiphyError(f"download failed: {self._redact(reason)}") from None
        if len(data) > MAX_DOWNLOAD:
            raise GiphyError(f"GIF larger than {MAX_DOWNLOAD >> 20} MB")
        if not data.startswith(b"GIF8"):
            raise GiphyError("GIPHY did not return a GIF")
        return f"giphy-{gif_id}.gif", data
