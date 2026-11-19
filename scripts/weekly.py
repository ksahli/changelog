#!/usr/bin/env python3
"""Build last week's roundup from the daily digests: Claude writes an editorial
and picks the top stories by ID; the stories themselves are copied from the
dailies, so every link was already validated there.

Usage:
    python scripts/weekly.py [--force] [--dry-run] [--week YYYY-Www]

Writes content/weekly/YYYY-Www.md, dated the Monday after the week. By default
the week is the last complete ISO week (Mon-Sun) before today (UTC).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import DIGEST_DIR, MODEL, ROOT, _obj, call_claude, log, read_front_matter  # noqa: E402

WEEKLY_DIR = ROOT / "content" / "weekly"
MIN_EDITIONS = 3

SYSTEM_PROMPT = """\
You are the editor of a weekly tech roundup for a software engineering team \
(developers, SRE/ops, security). You are given every story from last week's daily \
digests, each with an ID. Look back over the week as a whole.

- Pick the 8-12 stories that mattered most over the week, not just on the day: \
actively exploited or widespread vulnerabilities, breaking changes and deprecations, \
major releases, big outages, and shifts that change how teams build software.
- Group them into 2-5 themes. A theme title is a short headline (max ~10 words); \
its blurb is one or two sentences connecting the stories.
- Each story appears in at most one theme. Use ONLY the IDs you are given.
- The editorial is two short paragraphs (separated by a blank line) on what defined \
the week and what to keep an eye on. Plain, concrete, neutral; no hype, no greetings.
- The tldr is 3-5 one-sentence bullets a reader can act on.
"""

WEEKLY_SCHEMA = _obj(
    {
        "tldr": {"type": "array", "description": "3-5 one-sentence bullets.", "items": {"type": "string"}},
        "editorial": {"type": "string", "description": "Two short paragraphs separated by a blank line."},
        "themes": {
            "type": "array",
            "items": _obj(
                {
                    "title": {"type": "string"},
                    "blurb": {"type": "string"},
                    "story_ids": {"type": "array", "items": {"type": "string"}},
                }
            ),
        },
    }
)


def parse_week(value: str) -> date:
    """'2026-W47' -> the Monday of that ISO week."""
    year, week = value.upper().split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def week_name(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def load_stories(monday: date) -> tuple[dict[str, dict], int]:
    """Every story of the week keyed by the same ID the templates use as anchor:
    <date>-<category>-<1-based index in its section>."""
    stories, editions = {}, 0
    for offset in range(7):
        day = monday + timedelta(days=offset)
        path = DIGEST_DIR / f"{day}.md"
        if not path.exists():
            continue
        editions += 1
        for section in read_front_matter(path).get("extra", {}).get("sections", []):
            for index, item in enumerate(section.get("items", []), start=1):
                key = f"{day}-{section['category']}-{index}"
                stories[key] = {
                    **item,
                    "key": key,
                    "edition": day.isoformat(),
                    "category": section["category"],
                    "label": section.get("label", section["category"]),
                }
    return stories, editions


def ask_claude(stories: dict[str, dict], monday: date) -> dict:
    sunday = monday + timedelta(days=6)
    payload = [
        {k: s[k] for k in ("key", "edition", "category", "importance", "title", "summary", "tags") if k in s}
        for s in stories.values()
    ]
    for p in payload:
        p["id"] = p.pop("key")
    return call_claude(
        SYSTEM_PROMPT,
        WEEKLY_SCHEMA,
        f"The week runs from {monday:%A, %B %d} to {sunday:%A, %B %d, %Y}. "
        f"Here are its {len(payload)} stories as JSON:\n\n{json.dumps(payload, ensure_ascii=False)}",
    )


def validate(roundup: dict, stories: dict[str, dict]) -> dict:
    """Keep known, unused IDs only; replace them with the full stories."""
    used, themes = set(), []
    for theme in roundup.get("themes", []):
        items = []
        for story_id in theme.get("story_ids", []):
            if story_id not in stories:
                log(f"  ! dropped unknown story id: {story_id}")
            elif story_id not in used:
                used.add(story_id)
                items.append(stories[story_id])
        if items:
            themes.append({"title": theme["title"], "blurb": theme.get("blurb", ""), "items": items})
    if not themes:
        raise RuntimeError("Claude's roundup has no theme with a known story")
    return {"tldr": roundup.get("tldr", []), "editorial": roundup.get("editorial", "").strip(), "themes": themes}


def render_page(roundup: dict, monday: date, stats: dict) -> str:
    sunday = monday + timedelta(days=6)
    published = sunday + timedelta(days=1)
    tldr = roundup["tldr"]
    front = {
        "title": f"The week in review — {monday:%B} {monday.day} to {sunday:%B} {sunday.day}, {sunday.year}",
        "date": published.isoformat(),
        "description": tldr[0] if tldr else "The week in review",
        "extra": {
            "week": monday.isocalendar().week,
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "tldr": tldr,
            "themes": roundup["themes"],
            "stats": stats,
            "model": MODEL,
        },
    }
    return f"---\n{json.dumps(front, ensure_ascii=False, indent=1)}\n---\n\n{roundup['editorial']}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="overwrite the roundup if it exists")
    parser.add_argument("--dry-run", action="store_true", help="print the roundup instead of writing it")
    parser.add_argument("--week", type=parse_week, help="ISO week, e.g. 2026-W47 (default: last complete week)")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    monday: date = args.week or today - timedelta(days=today.weekday() + 7)
    name = week_name(monday)
    out_path = WEEKLY_DIR / f"{name}.md"
    if out_path.exists() and not (args.force or args.dry_run):
        log(f"{out_path.relative_to(ROOT)} already exists; use --force to regenerate")
        return 0

    stories, editions = load_stories(monday)
    log(f"{name}: {len(stories)} stories from {editions} editions")
    if editions < MIN_EDITIONS:
        log(f"Fewer than {MIN_EDITIONS} editions that week; skipping the roundup")
        return 0

    roundup = validate(ask_claude(stories, monday), stories)
    if args.dry_run:
        print(json.dumps(roundup, ensure_ascii=False, indent=2))
        return 0

    picked = sum(len(t["items"]) for t in roundup["themes"])
    stats = {"items": picked, "candidates": len(stories), "editions": editions}
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_page(roundup, monday, stats), encoding="utf-8")
    log(f"Wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
