"""Make a GIF cheap to upload before it reaches the panel.

Uploads move ~15 KB/s (256-byte writes paced by aorus-lcd, plus flash-erase
time), and the panel's RLE only shrinks runs of identical pixels. GIPHY GIFs
are usually photographic and dithered, so a raw 60-frame GIF costs 5-8
minutes. Three cheap levers, measured on real GIPHY results:

- fewer frames, spread over the whole GIF so it keeps its length;
- a median filter, which removes the dither noise that breaks runs;
- a small palette without dithering, which turns flat areas into runs.

"balanced" (20 frames, 32 colours) cut uploads 3-5x with little visible loss
on a 320x170 screen; "fast" (12 frames, 16 colours) roughly halves that again
but looks posterized; "original" sends the GIF untouched.
"""
import io
import math
from dataclasses import dataclass

from aorus_lcd import render as R

FITS = ("auto", "contain", "cover")
# "auto" crops (cover) when that loses at most this much of the picture; otherwise
# it shows the whole GIF over a blurred, darkened, enlarged copy of itself, so
# square or tall GIFs still fill the 320x170 screen with no bars and no stretching.
MAX_AUTO_CROP = 0.2
BLUR_RADIUS, BLUR_BRIGHTNESS = 10, 0.45
# What the panel does with the upload (LCD firmware 1.3, seen on the panel):
# - one delay for every frame, in ms, in one byte: 255 at most. GIF delays are
#   in 1/100 s, so 250 is the longest that survives the round trip;
# - it never shows the first frame, so a black frame goes first: RLE packs it
#   into a few bytes, and every real frame gets shown.
MAX_DELAY_MS = 250


@dataclass(frozen=True)
class Quality:
    max_frames: int        # 0 = keep all
    colors: int            # 0 = keep full colour
    median: int            # median filter size, 0 = none


QUALITIES = {
    "fast": Quality(12, 16, 5),
    "balanced": Quality(20, 32, 3),
    "original": Quality(0, 0, 0),
}


def crop_loss(width, height, box=(R.W, R.H)):
    """Fraction of the picture a cover fit would crop away."""
    src, dst = width / height, box[0] / box[1]
    return 1 - (dst / src if src >= dst else src / dst)


def resolve_fit(fit, width, height):
    """"auto" -> "cover" or "blur" for this GIF's shape."""
    if fit != "auto":
        return fit
    return "cover" if crop_loss(width, height) <= MAX_AUTO_CROP else "blur"


def _compose(frame, fit):
    """One RGBA source frame -> a 320x170 RGB frame."""
    from PIL import ImageEnhance, ImageFilter
    if fit == "blur":
        back = R.fit(frame.convert("RGB"), mode="cover").filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
        back = ImageEnhance.Brightness(back).enhance(BLUR_BRIGHTNESS)
        return R.place(back, R.fit(frame, mode="contain"))
    return R.place(R.canvas("#000000"), R.fit(frame, mode=fit))


def _frames(gif, fit):
    """320x170 RGB frames, each frame's duration in ms, and the fit used."""
    from PIL import Image
    frames, durations = [], []
    with Image.open(io.BytesIO(gif)) as im:
        fit = resolve_fit(fit, im.width, im.height)
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            frames.append(_compose(im.convert("RGBA"), fit))
            durations.append(im.info.get("duration", 100) or 100)
    return frames, durations, fit


def _spread(frames, durations, n):
    """At most n frames, evenly spread; each kept frame lasts until the next
    kept one, so the animation keeps its total length."""
    if not n or len(frames) <= n:
        return frames, durations
    picks = [round(i * len(frames) / n) for i in range(n)] + [len(frames)]
    return ([frames[a] for a in picks[:-1]],
            [sum(durations[a:b]) for a, b in zip(picks, picks[1:])])


def _simplify(frame, q):
    from PIL import Image, ImageFilter
    if q.median:
        frame = frame.filter(ImageFilter.MedianFilter(q.median))
    if q.colors:
        frame = frame.quantize(q.colors, method=Image.Quantize.MEDIANCUT,
                               dither=Image.Dither.NONE).convert("RGB")
    return frame


def optimize(gif, fit="auto", quality="balanced"):
    """GIF bytes -> (GIF bytes at exactly 320x170, frame count, fit used)."""
    if fit not in FITS:
        raise ValueError(f"fit must be one of {', '.join(FITS)}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(QUALITIES)}")
    q = QUALITIES[quality]
    try:
        frames, durations, used = _frames(gif, fit)
    except (OSError, SyntaxError) as e:          # Pillow's errors for broken images
        raise ValueError(f"cannot decode that GIF: {e}") from None
    total = sum(durations)
    if q.max_frames:
        # Never so few frames that each would need more than the panel's longest
        # delay: that would play the GIF faster than it is.
        frames, durations = _spread(frames, durations, max(q.max_frames, math.ceil(total / MAX_DELAY_MS)))
    delay = max(10, min(MAX_DELAY_MS, round(total / len(frames) / 10) * 10))
    frames = [R.canvas("#000000")] + [_simplify(f, q) for f in frames]
    out = io.BytesIO()
    # optimize=False: no cross-frame palette tricks; every frame is complete.
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=delay,
                   loop=0, optimize=False, disposal=1)
    return out.getvalue(), len(frames) - 1, used
