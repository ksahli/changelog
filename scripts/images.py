"""Story pictures: find the publisher's image for an article and print it as a
black-and-white halftone that fits the newspaper look.

Nothing here raises on bad input from the network: callers get None and move on.
"""

from __future__ import annotations

import html
import io
import math
import re
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps

MAX_BYTES = 8 * 1024 * 1024
HTML_BYTES = 300 * 1024
MIN_WIDTH = 480
ASPECT_RANGE = (0.6, 2.5)
GAMMA = 0.62  # < 1 lightens midtones
MAX_DOT = 0.88  # share of a cell's diagonal the darkest dot may cover

IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)
META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
META_KEY = re.compile(r"(?:property|name)\s*=\s*[\"'](og:image(?::url|:secure_url)?|twitter:image(?::src)?)[\"']", re.I)
META_CONTENT = re.compile(r"content\s*=\s*[\"']([^\"']+)", re.I)


def feed_image(entry: dict) -> str | None:
    """The first image a feed entry carries: media:content, media:thumbnail,
    an image enclosure, then the first <img> in its HTML."""
    for media in entry.get("media_content", []) or []:
        kind = (media.get("type") or "") + (media.get("medium") or "")
        if media.get("url") and ("image" in kind or not kind):
            return media["url"]
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for link in entry.get("links", []) or []:
        if (link.get("type") or "").startswith("image/") and link.get("href"):
            return link["href"]
    bodies = [entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content", []) or []]
    for body in bodies:
        match = IMG_TAG.search(body or "")
        if match:
            return html.unescape(match.group(1))
    return None


def page_image(client: httpx.Client, url: str) -> str | None:
    """og:image / twitter:image from the article's <head>."""
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if "html" not in resp.headers.get("content-type", ""):
                return None
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= HTML_BYTES:
                    break
            text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError:
        return None
    for tag in META_TAG.findall(text):
        if META_KEY.search(tag):
            content = META_CONTENT.search(tag)
            if content:
                return urljoin(url, html.unescape(content.group(1).strip()))
    return None


def download(client: httpx.Client, url: str) -> tuple[Image.Image | None, str]:
    """(image, "") when usable, else (None, reason)."""
    if not url.startswith(("http://", "https://")):
        return None, "not an http(s) URL"
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            kind = resp.headers.get("content-type", "")
            if not kind.startswith("image/"):
                return None, f"content-type {kind or 'missing'}"
            if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
                return None, "too large"
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    return None, "too large"
    except httpx.HTTPError as exc:
        return None, f"download failed ({exc.__class__.__name__})"
    try:
        img = Image.open(io.BytesIO(bytes(data)))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decoder error means "not usable"
        return None, f"unreadable ({exc.__class__.__name__})"
    w, h = img.size
    if w < MIN_WIDTH:
        return None, f"too small ({w}px wide)"
    if not ASPECT_RANGE[0] <= w / h <= ASPECT_RANGE[1]:
        return None, f"odd shape ({w}x{h})"
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGBA", img.size, "white")
        flat.alpha_composite(img)
        img = flat
    return img.convert("RGB"), ""


def halftone(img: Image.Image, width: int, aspect: float = 3 / 2, cell: int = 6) -> Image.Image:
    """A 45° dot screen: black dots on white, sized by the darkness under them."""
    ss = 2  # supersample, then downscale for smooth dots
    height = round(width / aspect)
    W, H, c = width * ss, height * ss, cell * ss

    pad = 3 * c  # screen a margin too, so the rotated grid never samples the fill color at the edges
    gray = ImageOps.fit(img.convert("L"), (W, H), Image.Resampling.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1).filter(ImageFilter.UnsharpMask(radius=2, percent=80))
    # Lift the midtones: printed dots spread ("dot gain"), and photos for the web skew dark.
    gray = gray.point([round(255 * (v / 255) ** GAMMA) for v in range(256)])
    framed = Image.new("L", (W + 2 * pad, H + 2 * pad))
    framed.paste(gray.resize((W + 2 * pad, H + 2 * pad), Image.Resampling.BICUBIC))
    framed.paste(gray, (pad, pad))
    gray = framed

    rotated = gray.rotate(45, expand=True, fillcolor=255, resample=Image.Resampling.BICUBIC)
    cols, rows = math.ceil(rotated.width / c), math.ceil(rotated.height / c)
    means = rotated.resize((cols, rows), Image.Resampling.BOX).load()

    screen = Image.new("L", rotated.size, 255)
    draw = ImageDraw.Draw(screen)
    full = c / math.sqrt(2) * MAX_DOT  # MAX_DOT = 1 would let the darkest dots merge into solid black
    for j in range(rows):
        for i in range(cols):
            darkness = 1 - means[i, j] / 255
            r = full * math.sqrt(darkness)
            if r < 0.6:
                continue
            x, y = i * c + c / 2, j * c + c / 2
            draw.ellipse((x - r, y - r, x + r, y + r), fill=0)

    back = screen.rotate(-45, fillcolor=255, resample=Image.Resampling.BICUBIC)
    left, top = (back.width - W) // 2, (back.height - H) // 2  # drops the padding too
    return back.crop((left, top, left + W, top + H)).resize((width, height), Image.Resampling.LANCZOS)
