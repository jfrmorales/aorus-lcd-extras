"""Favourite GIFs, kept on the server so every browser sees the same list.

Only what the page needs to show and re-send a GIF is stored: its GIPHY id,
title and thumbnail URL. The GIF itself is fetched again from GIPHY when it
is sent, like any other.
"""
import json
import os
import threading
import time
from pathlib import Path

from .giphy import ID_RE, GiphyError, _media_url, send_from_thumb

MAX_FAVORITES = 200
MAX_TITLE = 140


def clean(item):
    """A favourite as stored, from what the page sends. Raises ValueError."""
    if not isinstance(item, dict):
        raise ValueError("expected a GIF")
    gif_id = str(item.get("id", ""))
    if not ID_RE.match(gif_id):
        raise ValueError("bad GIF id")
    try:
        thumb = _media_url(str(item.get("thumb", "")))
    except GiphyError as e:
        raise ValueError(str(e)) from None
    try:
        send = _media_url(str(item.get("send") or "")) if item.get("send") else send_from_thumb(thumb)
    except GiphyError as e:
        raise ValueError(str(e)) from None
    title = " ".join(str(item.get("title") or "GIF").split())[:MAX_TITLE]
    size = {k: max(0, min(4096, int(item.get(k) or 0))) for k in ("width", "height")}
    return {"id": gif_id, "title": title, "thumb": thumb, "send": send, **size}


class Favorites:
    """Newest first. Writes are atomic: a crash leaves the old or the new list."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []                     # unreadable: start over rather than break the page
        return [i for i in data if isinstance(i, dict) and ID_RE.match(str(i.get("id", "")))] \
            if isinstance(data, list) else []

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return self._load()

    def add(self, item):
        fav = {**clean(item), "added": int(time.time())}
        with self.lock:
            items = [i for i in self._load() if i["id"] != fav["id"]]
            if len(items) >= MAX_FAVORITES:
                raise ValueError(f"favourites are limited to {MAX_FAVORITES}; remove some first")
            items.insert(0, fav)
            self._save(items)
            return items

    def remove(self, gif_id):
        with self.lock:
            items = [i for i in self._load() if i["id"] != str(gif_id)]
            self._save(items)
            return items
