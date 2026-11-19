#!/usr/bin/env python3
"""Render social preview cards (og:image) and favicons into static/.

For every digest and weekly page it draws a 1200x630 newspaper front:
masthead, rule, date line and the lead headline. Output is a build artifact
(gitignored); CI runs this before `zola build`.

Usage:
    python scripts/og.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import ROOT, read_front_matter  # noqa: E402

FONTS = ROOT / "assets" / "fonts"
OUT = ROOT / "static" / "og"
STATIC = ROOT / "static"

W, H = 1200, 630
MARGIN = 72
# Light palette from styles/input.css (:root tokens).
PAPER = "#efe6d2"
INK = "#1b1813"
MUTED = "#5e5547"
ACCENT = "#8b2a1e"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def blackletter(size: int) -> ImageFont.FreeTypeFont:
    return font("UnifrakturMaguntia-Book.ttf", size)


def spaced(text: str) -> str:
    """Cheap small caps: uppercase with letter spacing."""
    return " ".join(text.upper())


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_headline(draw: ImageDraw.ImageDraw, text: str, width: int, max_lines: int = 3):
    """Largest size (64 -> 40) that fits in max_lines; at 40, truncate with an ellipsis."""
    for size in range(64, 39, -4):
        fnt = font("OldStandard-Bold.ttf", size)
        lines = wrap(draw, text, fnt, width)
        if len(lines) <= max_lines:
            return fnt, lines
    lines = lines[:max_lines]
    while draw.textlength(lines[-1] + "…", font=fnt) > width:
        lines[-1] = lines[-1].rsplit(" ", 1)[0]
    lines[-1] += "…"
    return fnt, lines


def oxford_rule(draw: ImageDraw.ImageDraw, y: int) -> int:
    draw.rectangle((MARGIN, y, W - MARGIN, y + 3), fill=INK)
    draw.rectangle((MARGIN, y + 6, W - MARGIN, y + 6), fill=INK)
    return y + 7


def centered(draw: ImageDraw.ImageDraw, y: int, text: str, fnt, fill=INK) -> None:
    draw.text((W / 2, y), text, font=fnt, fill=fill, anchor="mt")


def card(dateline: str, headline: str, kicker: str = "") -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    centered(d, 34, "Changelog", blackletter(112))
    y = oxford_rule(d, 168)
    small = font("OldStandard-Regular.ttf", 20)
    centered(d, y + 14, spaced(dateline), small, MUTED)
    d.line((MARGIN, y + 52, W - MARGIN, y + 52), fill=INK, width=1)

    fnt, lines = fit_headline(d, headline, W - 2 * MARGIN)
    line_h = int(fnt.size * 1.12)
    kicker_h = 44 if kicker else 0
    block = kicker_h + line_h * len(lines)
    top, bottom = y + 52, H - 64
    ty = top + (bottom - top - block) // 2
    if kicker:
        centered(d, ty, spaced(kicker), font("OldStandard-Bold.ttf", 20), ACCENT)
        ty += kicker_h
    for line in lines:
        centered(d, ty, line, fnt)
        ty += line_h

    oxford_rule(d, H - 48)
    return img


def long_date(day: date) -> str:
    return f"{day:%A, %B} {day.day}, {day.year}"


def digest_card(extra: dict, day: date) -> Image.Image:
    sections = extra.get("sections", [])
    items = [i for s in sections for i in s["items"]]
    # Same lead as the template: the first high-importance story.
    lead = next((i for i in items if i.get("importance") == "high"), items[0] if items else None)
    n = len(items)
    dateline = f"{long_date(day)}  ·  {n} stories"
    if lead:
        return card(dateline, lead["title"], "High" if lead.get("importance") == "high" else "")
    return card(dateline, (extra.get("tldr") or ["What changed in the world since yesterday"])[0])


def weekly_card(extra: dict, stem: str) -> Image.Image:
    year, week = stem.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    sunday = monday + timedelta(days=6)
    dateline = f"The week in review  ·  {monday:%B} {monday.day} – {sunday:%B} {sunday.day}, {sunday.year}"
    themes = extra.get("themes", [])
    headline = themes[0]["title"] if themes else (extra.get("tldr") or ["The week in review"])[0]
    return card(dateline, headline, f"Week {int(week)}")


def favicon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), PAPER)
    d = ImageDraw.Draw(img)
    d.text((size / 2, size * 0.54), "C", font=blackletter(int(size * 0.92)), fill=INK, anchor="mm")
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted((ROOT / "content" / "digest").glob("????-??-??.md")):
        digest_card(read_front_matter(path).get("extra", {}), date.fromisoformat(path.stem)).save(
            OUT / f"{path.stem}.png", optimize=True)
        count += 1
    for path in sorted((ROOT / "content" / "weekly").glob("????-W??.md")):
        # Zola lowercases slugs: 2026-W47.md -> 2026-w47.
        weekly_card(read_front_matter(path).get("extra", {}), path.stem).save(
            OUT / f"{path.stem.lower()}.png", optimize=True)
        count += 1
    card("What changed in the world since yesterday", "A daily briefing on security, dev, ops and tech").save(
        OUT / "default.png", optimize=True)
    favicon(32).save(STATIC / "favicon-32.png", optimize=True)
    favicon(180).save(STATIC / "apple-touch-icon.png", optimize=True)
    print(f"Wrote {count} cards to {OUT.relative_to(ROOT)}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
