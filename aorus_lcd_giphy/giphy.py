"""Minimal GIPHY API client (stdlib only).

The API key stays on the server: the browser only ever sees GIF ids, titles
and media URLs. Downloads are restricted to https://*.giphy.com, and a GIF is
always fetched by id through the API, never from a URL the browser supplies.
"""
import json
import re
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


class GiphyError(Exception):
    pass


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


def summarize(item):
    """The fields the page needs from one GIPHY result."""
    images = item.get("images") or {}
    _, thumb = _rendition(images, THUMB_RENDITIONS)
    if not thumb:
        return None
    _, send = _rendition(images, SEND_RENDITIONS)
    return {
        "id": item.get("id", ""),
        "title": item.get("title") or "GIF",
        "thumb": _media_url(thumb["url"]),
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

    def _redact(self, text):
        """Errors can carry the request URL, and with it the key."""
        return text.replace(self.api_key, "***")

    def _get(self, path, **params):
        query = urllib.parse.urlencode({"api_key": self.api_key, **params})
        try:
            with self.open(f"{API}/{path}?{query}", timeout=self.timeout) as r:
                data = json.load(r)
        except OSError as e:
            reason = str(getattr(e, "reason", None) or type(e).__name__)
            raise GiphyError(f"GIPHY request failed: {self._redact(reason)}") from None
        status = (data.get("meta") or {}).get("status", 200)
        if status != 200:
            raise GiphyError(f"GIPHY answered {status}: {(data.get('meta') or {}).get('msg', '')}")
        return data

    def _list(self, data):
        items = [s for s in (summarize(i) for i in data.get("data") or []) if s]
        page = data.get("pagination") or {}
        return {"items": items, "offset": page.get("offset", 0),
                "total": page.get("total_count", len(items))}

    def search(self, q, offset=0, limit=24):
        q = (q or "").strip()[:50]
        if not q:
            return self.trending(offset, limit)
        return self._list(self._get("search", q=q, offset=max(0, int(offset)), limit=limit,
                                    rating=self.rating, lang=self.lang))

    def trending(self, offset=0, limit=24):
        return self._list(self._get("trending", offset=max(0, int(offset)), limit=limit,
                                    rating=self.rating))

    def download(self, gif_id):
        """(filename, GIF bytes) of the rendition sent to the panel."""
        if not ID_RE.match(gif_id or ""):
            raise GiphyError("bad GIF id")
        item = self._get(gif_id).get("data") or {}
        _, image = _rendition(item.get("images") or {}, SEND_RENDITIONS)
        if not image:
            raise GiphyError("that GIF has no usable rendition")
        try:
            with self.open(_media_url(image["url"]), timeout=self.timeout * 3) as r:
                data = r.read(MAX_DOWNLOAD + 1)
        except OSError as e:
            reason = str(getattr(e, "reason", None) or type(e).__name__)
            raise GiphyError(f"download failed: {self._redact(reason)}") from None
        if len(data) > MAX_DOWNLOAD:
            raise GiphyError(f"GIF larger than {MAX_DOWNLOAD >> 20} MB")
        if not data.startswith(b"GIF8"):
            raise GiphyError("GIPHY did not return a GIF")
        return f"giphy-{gif_id}.gif", data
