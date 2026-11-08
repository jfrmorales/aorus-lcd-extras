"""GIPHY page: client parsing, download guards and the HTTP handler, with fakes
for GIPHY and for the aorus-lcd service (no network, no panel)."""
import io
import json
import random
import threading
import urllib.error
import urllib.request

import pytest

from aorus_lcd import render as R
from aorus_lcd_giphy import favorites as F
from aorus_lcd_giphy import giphy as G
from aorus_lcd_giphy import optimize as O
from aorus_lcd_giphy import server as S

def make_gif(n=30, size=(200, 200), ms=40):
    """A noisy animated GIF, like a photographic GIPHY result."""
    from PIL import Image
    rng = random.Random(1)
    frames = [Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))
              for _ in range(n)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=ms, loop=0)
    return out.getvalue()


GIF = make_gif(6, (40, 40))


def item(gif_id="abc123", host="media2.giphy.com"):
    url = f"https://{host}/media/{gif_id}/200.gif"
    return {"id": gif_id, "title": "A cat",
            "images": {"fixed_height_small": {"url": url, "width": "100", "height": "100"},
                       "fixed_height": {"url": url, "width": "200", "height": "200", "size": "4096"}}}


class Resp(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGiphyWeb:
    """Answers GIPHY API URLs and media URLs; records what was requested."""

    def __init__(self, media=GIF, gif=None):
        self.media, self.gif, self.urls = media, gif or item(), []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if url.startswith(G.API + "/search") or url.startswith(G.API + "/trending"):
            body = {"meta": {"status": 200}, "data": [self.gif, {"id": "x", "images": {}}],
                    "pagination": {"offset": 0, "total_count": 1}}
        elif url.startswith(G.API + "/"):
            body = {"meta": {"status": 200}, "data": self.gif}
        else:
            return Resp(self.media)
        return Resp(json.dumps(body).encode())


def test_search_keeps_the_key_server_side_and_drops_unusable_items():
    web = FakeGiphyWeb()
    out = G.Giphy("SECRET", opener=web).search("cat")
    assert [i["id"] for i in out["items"]] == ["abc123"]
    assert out["items"][0]["thumb"].startswith("https://media2.giphy.com/")
    assert "SECRET" in web.urls[0] and "SECRET" not in json.dumps(out)


def test_empty_query_is_trending():
    web = FakeGiphyWeb()
    G.Giphy("k", opener=web).search("  ")
    assert web.urls[0].startswith(G.API + "/trending")


def test_download_by_id():
    name, data = G.Giphy("k", opener=FakeGiphyWeb()).download("abc123")
    assert name == "giphy-abc123.gif" and data == GIF


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", "x" * 65, "id?api_key=1"])
def test_download_rejects_bad_ids(bad):
    with pytest.raises(G.GiphyError):
        G.Giphy("k", opener=FakeGiphyWeb()).download(bad)


@pytest.mark.parametrize("host", ["evil.example", "giphy.com.evil.example", "127.0.0.1"])
def test_download_only_from_giphy_hosts(host):
    with pytest.raises(G.GiphyError):
        G.Giphy("k", opener=FakeGiphyWeb(gif=item(host=host))).download("abc123")


def test_download_rejects_non_gif_and_oversize():
    with pytest.raises(G.GiphyError, match="GIF"):
        G.Giphy("k", opener=FakeGiphyWeb(media=b"<html>")).download("abc123")
    with pytest.raises(G.GiphyError, match="larger"):
        G.Giphy("k", opener=FakeGiphyWeb(media=b"GIF89a" + b"\0" * G.MAX_DOWNLOAD)).download("abc123")


def test_errors_do_not_leak_the_key():
    def failing(url, timeout=None):
        raise urllib.error.URLError(f"cannot reach {url}")
    with pytest.raises(G.GiphyError) as e:
        G.Giphy("SECRET", opener=failing).search("cat")
    assert "SECRET" not in str(e.value)


def test_requires_a_key():
    with pytest.raises(G.GiphyError):
        G.Giphy("")


UPSTREAM = (b'<!doctype html><html><head><title>AORUS LCD</title></head><body><header><div>AORUS</div>'
            b'<div class="grow"></div><span class="pill">x</span></header><main>panel</main></body></html>')


class FakeDaemon:
    """The aorus-lcd service: its page, /api/media for prepare, and a forward log."""

    def __init__(self):
        self.calls = []

    def page(self):
        return UPSTREAM

    def save_media(self, name, data):
        self.calls.append(("media", name, len(data)))
        return {"source": f"/var/lib/aorus-lcd/media/{name}", "frames": 2}

    def forward(self, method, path, body, headers):
        self.calls.append(("forward", method, path, body, dict(headers)))
        if path == "/api/preview":
            return 200, {"Content-Type": "image/gif", "X-Estimate-Seconds": "12.5"}, b"GIF89a"
        if path == "/api/content":
            return 409, {"Content-Type": "application/json"}, b'{"error": "an upload is already running"}'
        return 200, {"Content-Type": "application/json"}, b'{"panel": {"connected": true}}'


@pytest.fixture
def served(tmp_path):
    daemon = FakeDaemon()
    server = S.make_server(G.Giphy("k", opener=FakeGiphyWeb()), daemon, port=0,
                           favorites=F.Favorites(tmp_path / "favorites.json"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", daemon
    server.shutdown()
    server.server_close()


def call(url, body=None, headers=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


H = {"X-Aorus-Lcd": "1", "Content-Type": "application/json"}


def test_root_is_the_panel_ui_with_the_nav(served):
    base, _ = served
    code, body, _ = call(base + "/")
    assert code == 200 and b"<main>panel</main>" in body
    assert b'<div class="grow"></div><nav class="xnav"' in body
    assert b'<a href="/" aria-current=page>Panel</a><a href="/giphy">GIPHY</a>' in body


def test_nav_falls_back_to_the_top_if_the_markup_changes():
    out = S.panel_page(b"<html><head></head><body><p>new ui</p></body></html>")
    assert out.index(b'class="xnav"') < out.index(b"new ui")


def test_giphy_page_and_search(served):
    base, _ = served
    code, body, headers = call(base + "/giphy")
    assert code == 200 and b"Powered by" in body
    assert b'<a href="/">Panel</a><a href="/giphy" aria-current=page>GIPHY</a>' in body
    assert "https://*.giphy.com" in headers["Content-Security-Policy"]
    code, body, _ = call(base + "/giphy/api/search?q=cat")
    assert code == 200 and json.loads(body)["items"][0]["id"] == "abc123"


def test_api_is_forwarded_verbatim(served):
    base, daemon = served
    code, body, headers = call(base + "/api/preview", {"content": {"type": "gif"}}, H)
    assert code == 200 and body == b"GIF89a" and headers["X-Estimate-Seconds"] == "12.5"
    method, path, sent, fwd = daemon.calls[-1][1:]
    assert (method, path, json.loads(sent)) == ("POST", "/api/preview", {"content": {"type": "gif"}})
    assert fwd["X-Aorus-Lcd"] == "1"
    code, body, _ = call(base + "/api/content", {"content": {}}, H)
    assert code == 409 and b"already running" in body          # the service's errors pass through
    code, _, _ = call(base + "/api/status")
    assert code == 200 and daemon.calls[-1][1:3] == ("GET", "/api/status")


def test_media_upload_keeps_its_filename(served):
    base, daemon = served
    call(base + "/api/media", raw=b"GIF89a...", headers={"X-Aorus-Lcd": "1", "X-Filename": "cat.gif"})
    assert daemon.calls[-1][4]["X-Filename"] == "cat.gif" and daemon.calls[-1][3] == b"GIF89a..."


def test_post_needs_the_api_header(served):
    base, daemon = served
    for path in ("/api/content", "/giphy/api/prepare"):
        code, _, _ = call(base + path, {"content": {}}, {"Content-Type": "application/json"})
        assert code == 403
    assert not daemon.calls


def test_rejects_foreign_host_header(served):
    base, daemon = served
    for path in ("/", "/api/status"):
        code, _, _ = call(base + path, headers={"Host": "evil.example"})
        assert code == 403
    assert not daemon.calls


def test_prepare(served):
    base, daemon = served
    code, body, _ = call(base + "/giphy/api/prepare", {"id": "abc123", "fit": "cover", "quality": "fast"}, H)
    media = json.loads(body)
    assert code == 200 and media["frames"] == 6 and media["fit"] == "cover"
    assert daemon.calls[0][:2] == ("media", "giphy-abc123-cover-fast.gif")


@pytest.mark.parametrize("body", [{"id": "abc123", "fit": "stretch"}, {"id": "abc123", "quality": "ultra"}])
def test_prepare_rejects_bad_options(served, body):
    base, daemon = served
    code, _, _ = call(base + "/giphy/api/prepare", body, H)
    assert code == 400 and not daemon.calls


def test_downloads_are_cached():
    web = FakeGiphyWeb()
    downloads = S.Downloads(G.Giphy("k", opener=web), size=1)
    downloads.get("abc123")
    downloads.get("abc123")
    assert sum(1 for u in web.urls if "media" in u and "200.gif" in u) == 1


@pytest.mark.parametrize("quality", ["fast", "balanced", "original"])
def test_optimize_output_is_what_the_panel_decodes(quality):
    src = make_gif(30, (200, 200), ms=40)
    gif, n, used = O.optimize(src, "contain", quality)
    cap = O.QUALITIES[quality].max_frames or 30
    assert n == cap
    from PIL import Image
    with Image.open(io.BytesIO(gif)) as im:
        assert im.size == (320, 170) and im.n_frames == n + 1      # + the black frame the panel skips
        assert im.convert("RGB").getextrema() == ((0, 0), (0, 0), (0, 0))
        total = 0
        for i in range(1, n + 1):
            im.seek(i)
            total += im.info["duration"]
    assert total == 30 * 40                   # same length, fewer frames
    if O.QUALITIES[quality].colors:
        path = io.BytesIO(gif)
        frames, _ = R.gif_frames(path)
        for f in frames:
            assert len(f.getcolors(1 << 16)) <= O.QUALITIES[quality].colors + 1   # + black bars


def test_optimize_shrinks_the_upload():
    from aorus_lcd import protocol as P
    src = make_gif(30, (200, 200))

    def seconds(quality):
        frames, delay = R.gif_frames(io.BytesIO(O.optimize(src, "cover", quality)[0]))
        return P.gif_upload([R.to_rgb565(f) for f in frames], delay).estimate_seconds()
    assert seconds("fast") < seconds("balanced") < seconds("original") / 1.4


@pytest.mark.parametrize("size, fit", [((356, 200), "cover"), ((200, 200), "blur"), ((150, 300), "blur"),
                                       ((640, 340), "cover")])
def test_auto_fit_crops_wide_gifs_and_blurs_the_rest(size, fit):
    assert O.resolve_fit("auto", *size) == fit


def test_auto_fit_fills_the_screen():
    """A square GIF on a 320x170 screen: no black bars left anywhere."""
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (200, 200), (90, 110, 220)).save(out, "GIF")
    gif, _, used = O.optimize(out.getvalue(), "auto", "balanced")
    frame = R.gif_frames(io.BytesIO(gif))[0][1].convert("RGB")      # [0] is the skipped black frame
    assert used == "blur"
    for x in (0, 5, 319):
        assert frame.getpixel((x, 85)) != (0, 0, 0)


@pytest.mark.parametrize("quality", ["fast", "balanced"])
def test_long_gifs_keep_their_speed(quality):
    """10 s at 100 ms: 12 or 20 frames would need 500-830 ms each, more than the
    panel's 255 ms, so it would play several times too fast. Keep enough frames."""
    src = make_gif(100, (40, 40), ms=100)
    gif, n, _ = O.optimize(src, "contain", quality)
    _, delay = R.gif_frames(io.BytesIO(gif))
    assert n == 40 and delay == 250


def test_optimize_rejects_garbage():
    with pytest.raises(ValueError):
        O.optimize(b"GIF89a not really", "contain", "balanced")


# -- favourites -------------------------------------------------------------------------

def fav(gif_id="abc123", **kw):
    return {"id": gif_id, "title": "A cat", "thumb": f"https://media2.giphy.com/media/{gif_id}/100.gif",
            "width": 100, "height": 100, **kw}


def test_favorites_add_remove_and_persist(tmp_path):
    path = tmp_path / "state" / "favorites.json"
    favs = F.Favorites(path)
    favs.add(fav("a1"))
    favs.add(fav("b2"))
    favs.add(fav("a1", title="again"))                    # re-adding moves it to the front, once
    assert [i["id"] for i in F.Favorites(path).list()] == ["a1", "b2"]
    assert favs.list()[0]["title"] == "again"
    favs.remove("a1")
    assert [i["id"] for i in F.Favorites(path).list()] == ["b2"]


@pytest.mark.parametrize("item", [fav("../x"), fav(thumb="https://evil.example/x.gif"), fav(thumb="http://media.giphy.com/x.gif"),
                                  "abc", None])
def test_favorites_reject_bad_items(tmp_path, item):
    with pytest.raises(ValueError):
        F.Favorites(tmp_path / "f.json").add(item)


def test_favorites_clean_title_and_size(tmp_path):
    item = F.Favorites(tmp_path / "f.json").add(fav(title="  a\n" + "x" * 500, width="99999", height=-3))[0]
    assert item["title"].startswith("a x") and len(item["title"]) == F.MAX_TITLE
    assert (item["width"], item["height"]) == (4096, 0)


def test_favorites_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "MAX_FAVORITES", 2)
    favs = F.Favorites(tmp_path / "f.json")
    favs.add(fav("a1")); favs.add(fav("b2"))
    with pytest.raises(ValueError, match="limited"):
        favs.add(fav("c3"))


def test_unreadable_favorites_file_starts_over(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{not json")
    favs = F.Favorites(path)
    assert favs.list() == []
    favs.add(fav("a1"))
    assert [i["id"] for i in favs.list()] == ["a1"]


def test_favorites_api(served):
    base, daemon = served
    code, body, _ = call(base + "/giphy/api/favorites")
    assert code == 200 and json.loads(body) == {"items": []}
    code, body, _ = call(base + "/giphy/api/favorites", {"action": "add", "item": fav()}, H)
    assert code == 200 and json.loads(body)["items"][0]["id"] == "abc123"
    code, body, _ = call(base + "/giphy/api/favorites")
    assert [i["id"] for i in json.loads(body)["items"]] == ["abc123"]
    code, body, _ = call(base + "/giphy/api/favorites", {"action": "remove", "id": "abc123"}, H)
    assert code == 200 and json.loads(body) == {"items": []}
    code, _, _ = call(base + "/giphy/api/favorites", {"action": "explode"}, H)
    assert code == 400
    code, _, _ = call(base + "/giphy/api/favorites", {"action": "add", "item": fav(thumb="https://evil.example/x")}, H)
    assert code == 400
    assert not daemon.calls                                # favourites never touch the service


def test_favorites_need_the_api_header(served):
    base, _ = served
    code, _, _ = call(base + "/giphy/api/favorites", {"action": "add", "item": fav()},
                      {"Content-Type": "application/json"})
    assert code == 403


# -- upload-time estimates ----------------------------------------------------------------

def exact_seconds(gif, fit, quality):
    from aorus_lcd import protocol as P
    frames, delay = R.gif_frames(io.BytesIO(O.optimize(gif, fit, quality)[0]))
    return P.gif_upload([R.to_rgb565(f) for f in frames], delay).estimate_seconds()


def flat_gif(n=12, size=(200, 200)):
    """Simple shapes on flat colour: the kind of GIF that uploads fast."""
    from PIL import Image, ImageDraw
    frames = []
    for i in range(n):
        im = Image.new("RGB", size, (20, 20, 60))
        ImageDraw.Draw(im).ellipse([10 + 8 * i, 60, 70 + 8 * i, 120], fill=(250, 200, 40))
        frames.append(im)
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return out.getvalue()


@pytest.mark.parametrize("gif", [make_gif(30, (200, 200)), make_gif(60, (356, 200), ms=100), flat_gif()],
                         ids=["noisy-square", "noisy-wide-long", "flat"])
@pytest.mark.parametrize("quality", ["fast", "balanced", "original"])
def test_estimate_matches_the_real_upload(gif, quality):
    seconds, frames = O.estimate(gif, "auto", quality)
    exact = exact_seconds(gif, "auto", quality)
    assert abs(seconds - exact) <= max(2.0, 0.1 * exact)
    assert frames == O.optimize(gif, "auto", quality)[1]


def test_estimate_tells_light_from_heavy():
    assert O.estimate(flat_gif(), "auto", "balanced")[0] < 30 < O.estimate(make_gif(30, (200, 200)), "auto", "balanced")[0]


def test_estimate_api_caches(served):
    base, daemon = served
    code, body, _ = call(base + "/giphy/api/estimate?id=abc123&fit=cover&quality=fast")
    first = json.loads(body)
    assert code == 200 and first["id"] == "abc123" and first["frames"] == 6 and first["seconds"] > 0
    code, body, _ = call(base + "/giphy/api/estimate?id=abc123&fit=cover&quality=fast")
    assert json.loads(body) == first
    assert not daemon.calls                                 # estimates never touch the service


def test_estimate_and_prepare_share_one_download(tmp_path):
    web = FakeGiphyWeb()
    giphy = G.Giphy("k", opener=web)
    downloads = S.Downloads(giphy)
    S.Estimates(downloads).get("abc123")
    S.prepare(downloads, FakeDaemon(), {"id": "abc123"})
    assert sum(1 for u in web.urls if u.endswith("200.gif")) == 1


@pytest.mark.parametrize("query", ["id=abc123&fit=stretch", "id=abc123&quality=ultra"])
def test_estimate_rejects_bad_options(served, query):
    base, _ = served
    code, _, _ = call(base + "/giphy/api/estimate?" + query)
    assert code == 400


# -- GIPHY API budget: only searches may spend calls --------------------------------------

def api_calls(web):
    return [u for u in web.urls if u.startswith(G.API)]


def test_estimate_and_send_after_a_search_cost_no_api_call(tmp_path):
    web = FakeGiphyWeb()
    giphy = G.Giphy("k", opener=web)
    downloads = S.Downloads(giphy)
    giphy.search("cat")
    assert len(api_calls(web)) == 1
    S.Estimates(downloads).get("abc123")
    S.prepare(downloads, FakeDaemon(), {"id": "abc123", "quality": "fast"})
    assert len(api_calls(web)) == 1                         # media came from the CDN, by the search's URL


def test_searches_are_cached_for_a_while():
    web = FakeGiphyWeb()
    giphy = G.Giphy("k", opener=web)
    now = [1000.0]
    giphy.clock = lambda: now[0]
    giphy.search("cat"); giphy.search("cat"); giphy.search(""); giphy.search("")
    assert len(api_calls(web)) == 2                         # one search, one trending
    now[0] += G.SEARCH_TTL + 1
    giphy.search("cat")
    assert len(api_calls(web)) == 3
    assert giphy.usage() == {"calls": 3, "limit": G.BETA_LIMIT}
    now[0] += 3601
    assert giphy.usage()["calls"] == 0


def test_favorites_send_without_api_calls(served):
    base, _ = served
    thumb = "https://media2.giphy.com/media/v1.X/zz9/100.gif"
    call(base + "/giphy/api/favorites", {"action": "add", "item": {"id": "zz9", "thumb": thumb}}, H)
    items = json.loads(call(base + "/giphy/api/favorites")[1])["items"]
    assert items[0]["send"] == "https://media2.giphy.com/media/v1.X/zz9/200.gif"


def test_known_favorite_downloads_from_the_cdn(tmp_path):
    web = FakeGiphyWeb()
    giphy = G.Giphy("k", opener=web)
    giphy.remember("zz9", "https://media2.giphy.com/media/v1.X/zz9/200.gif")
    giphy.download("zz9")
    assert not api_calls(web)


def test_unknown_gif_costs_one_lookup_then_none():
    web = FakeGiphyWeb()
    giphy = G.Giphy("k", opener=web)
    giphy.download("abc123"); giphy.download("abc123")
    assert len(api_calls(web)) == 1


def test_rate_limit_is_explained(tmp_path):
    def limited(url, timeout=None):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
    giphy = G.Giphy("SECRET", opener=limited)
    with pytest.raises(G.RateLimited) as e:
        giphy.search("cat")
    assert "100 calls an hour" in str(e.value) and "SECRET" not in str(e.value)
    server = S.make_server(giphy, FakeDaemon(), port=0, favorites=F.Favorites(tmp_path / "f.json"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, body, _ = call(f"http://127.0.0.1:{server.server_address[1]}/giphy/api/search?q=dog")
        assert code == 429 and json.loads(body)["rate_limited"] is True
    finally:
        server.shutdown(); server.server_close()


def test_search_results_carry_a_validated_send_url():
    web = FakeGiphyWeb(gif=item(host="evil.example"))
    with pytest.raises(G.GiphyError):
        G.Giphy("k", opener=web).search("cat")
