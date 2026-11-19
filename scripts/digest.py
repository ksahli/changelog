#!/usr/bin/env python3
"""Build today's tech digest: fetch RSS feeds -> ask Claude -> write a Zola page.

Usage:
    python scripts/digest.py [--force] [--dry-run] [--date YYYY-MM-DD]

Env:
    ANTHROPIC_API_KEY   required (except with --fetch-only)
    DIGEST_MODEL        optional, defaults to claude-opus-5-5
    DIGEST_EFFORT       optional, low|medium|high (default medium)
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "scripts" / "sources.toml"
DIGEST_DIR = ROOT / "content" / "digest"

MODEL = os.environ.get("DIGEST_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("DIGEST_EFFORT") or "medium"
WINDOW = timedelta(hours=26)
MAX_CANDIDATES = 300
SNIPPET_LEN = 400
HISTORY_DAYS = 3
USER_AGENT = "daily-tech-digest/1.0 (+https://github.com/)"

CATEGORY_LABELS = {
    "sec": "Security",
    "dev": "Dev & Languages",
    "ops": "Ops & Cloud",
    "tech": "Tech & AI",
}

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|source$)")


def log(*args: object) -> None:
    print(*args, file=sys.stderr)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:80]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_time(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


async def fetch_feed(client: httpx.AsyncClient, feed: dict, since: datetime) -> list[dict]:
    try:
        resp = await client.get(feed["url"])
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log(f"  ! {feed['name']}: {exc}")
        return []

    parsed = feedparser.parse(resp.content)
    items = []
    for entry in parsed.entries[: feed.get("max_items", 25)]:
        link = entry.get("link")
        title = strip_html(entry.get("title", ""))
        published = entry_time(entry)
        if not link or not title or (published and published < since):
            continue
        if published is None:
            # Undated entries are usually evergreen pages; skip them.
            continue
        snippet = strip_html(entry.get("summary", "") or entry.get("description", ""))
        items.append(
            {
                "title": title,
                "link": link,
                "source": feed["name"],
                "category": feed["category"],
                "published": published.isoformat(timespec="minutes"),
                "snippet": snippet[:SNIPPET_LEN],
            }
        )
    log(f"  - {feed['name']}: {len(items)} recent")
    return items


async def fetch_all(feeds: list[dict], since: datetime) -> list[dict]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        results = await asyncio.gather(*(fetch_feed(client, f, since) for f in feeds))
    return [item for batch in results for item in batch]


def dedupe(items: list[dict], seen_urls: set[str]) -> list[dict]:
    out, urls, titles = [], set(), set()
    for item in sorted(items, key=lambda i: i["published"], reverse=True):
        url, tkey = canonical_url(item["link"]), title_key(item["title"])
        if url in urls or url in seen_urls or tkey in titles:
            continue
        urls.add(url)
        titles.add(tkey)
        out.append(item)
    return out[:MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Previous digests (front matter is JSON, which is valid YAML for Zola)
# ---------------------------------------------------------------------------

def read_front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return json.loads(match.group(1)) if match else {}


def recent_urls(today: date) -> set[str]:
    urls = set()
    for days in range(1, HISTORY_DAYS + 1):
        path = DIGEST_DIR / f"{today - timedelta(days=days)}.md"
        if not path.exists():
            continue
        extra = read_front_matter(path).get("extra", {})
        for section in extra.get("sections", []):
            for item in section.get("items", []):
                urls.update(canonical_url(link["url"]) for link in item.get("links", []))
    return urls


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the editor of a daily tech briefing for a software engineering team \
(developers, SRE/ops, security). From the candidate news items you are given, \
select what is genuinely worth a busy engineer's attention today and summarize it.

Prioritize, roughly in this order:
1. Security issues that may require action: actively exploited vulnerabilities, \
critical CVEs in widely used software, supply-chain compromises, major breaches.
2. Breaking changes, deprecations, end-of-life notices, license changes.
3. Significant releases of languages, frameworks, runtimes, databases, cloud services, dev tools.
4. Major outages and notable postmortems.
5. Important industry and AI developments that affect how teams build software.

Rules:
- Skip marketing fluff, listicles, opinion pieces without news, minor point releases, \
and consumer gadget news unless it matters to developers.
- Merge items that cover the same story into one entry with multiple links.
- Use ONLY links that appear in the candidates. Never invent URLs or facts.
- Be concise, concrete and neutral. Mention versions, CVE IDs, affected products when known.
- "why_it_matters" must say what a team should do or watch for (one sentence).
- Aim for 10-25 items total. Empty categories may be omitted.
- Use these category keys: sec, dev, ops, tech.
"""

def _obj(properties: dict, **extra) -> dict:
    """Structured-output object schema: every field required, no extras."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
        **extra,
    }


DIGEST_SCHEMA = _obj(
    {
        "tldr": {
            "type": "array",
            "description": "3-5 one-sentence bullets: the most important things today.",
            "items": {"type": "string"},
        },
        "sections": {
            "type": "array",
            "items": _obj(
                {
                    "category": {"type": "string", "enum": list(CATEGORY_LABELS)},
                    "items": {
                        "type": "array",
                        "items": _obj(
                            {
                                "title": {"type": "string"},
                                "summary": {"type": "string", "description": "2-3 sentences."},
                                "why_it_matters": {"type": "string"},
                                "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                                "links": {
                                    "type": "array",
                                    "items": _obj({"url": {"type": "string"}, "source": {"type": "string"}}),
                                },
                                "tags": {"type": "array", "items": {"type": "string"}},
                            }
                        ),
                    },
                }
            ),
        },
    }
)


def call_claude(system: str, schema: dict, content: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    # Security news can trip safety classifiers; "default" fallbacks retry a
    # declined request on Anthropic's recommended model for that category.
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": content}],
    )
    log(f"Claude ({response.model}) usage: in={response.usage.input_tokens} out={response.usage.output_tokens}")
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens; output would be truncated")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def ask_claude(candidates: list[dict], today: date) -> dict:
    payload = json.dumps(candidates, ensure_ascii=False)
    return call_claude(
        SYSTEM_PROMPT,
        DIGEST_SCHEMA,
        f"Today is {today:%A, %B %d, %Y}. "
        f"Here are {len(candidates)} candidate items from the last 24 hours as JSON:\n\n"
        f"{payload}",
    )


def validate(digest: dict, candidates: list[dict]) -> dict:
    """Drop hallucinated links and items left without any valid link."""
    known = {canonical_url(c["link"]): c["link"] for c in candidates}
    order = list(CATEGORY_LABELS)
    sections = []
    for section in digest.get("sections", []):
        if section.get("category") not in CATEGORY_LABELS:
            continue
        items = []
        for item in section.get("items", []):
            links = []
            for link in item.get("links", []):
                original = known.get(canonical_url(link.get("url", "")))
                if original:
                    links.append({"url": original, "source": link.get("source", "")})
                else:
                    log(f"  ! dropped unknown link: {link.get('url')}")
            if links:
                item["links"] = links
                item.setdefault("tags", [])
                items.append(item)
        if items:
            rank = {"high": 0, "medium": 1, "low": 2}
            items.sort(key=lambda i: rank.get(i.get("importance"), 3))
            sections.append({
                "category": section["category"],
                "label": CATEGORY_LABELS[section["category"]],
                "items": items,
            })
    sections.sort(key=lambda s: order.index(s["category"]))
    return {"tldr": digest.get("tldr", []), "sections": sections}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_page(digest: dict, today: date, stats: dict) -> str:
    tldr = digest["tldr"]
    front = {
        "title": f"Tech digest — {today:%A, %B} {today.day}, {today.year}",
        "date": today.isoformat(),
        "description": tldr[0] if tldr else "Daily tech digest",
        "extra": {"tldr": tldr, "sections": digest["sections"], "stats": stats, "model": MODEL},
        # The site is served at the root; /en/... redirects there.
        "aliases": [f"/en/digest/{today}/"],
    }
    body = "\n".join(f"- {line}" for line in tldr)
    return f"---\n{json.dumps(front, ensure_ascii=False, indent=1)}\n---\n\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="overwrite today's digest if it exists")
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of writing it")
    parser.add_argument("--fetch-only", action="store_true", help="only fetch feeds and print candidates")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()

    today: date = args.date
    out_path = DIGEST_DIR / f"{today}.md"
    if out_path.exists() and not (args.force or args.dry_run or args.fetch_only):
        log(f"{out_path.relative_to(ROOT)} already exists; use --force to regenerate")
        return 0

    feeds = tomllib.loads(SOURCES.read_text(encoding="utf-8"))["feed"]
    log(f"Fetching {len(feeds)} feeds…")
    since = datetime.now(timezone.utc) - WINDOW
    items = asyncio.run(fetch_all(feeds, since))
    candidates = dedupe(items, recent_urls(today))
    log(f"{len(items)} recent entries -> {len(candidates)} candidates after dedupe")

    if args.fetch_only:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0
    if not candidates:
        log("No candidates; nothing to do")
        return 1

    digest = validate(ask_claude(candidates, today), candidates)
    if args.dry_run:
        print(json.dumps(digest, ensure_ascii=False, indent=2))
        return 0

    n_items = sum(len(s["items"]) for s in digest["sections"])
    stats = {"items": n_items, "candidates": len(candidates), "sources": len(feeds)}
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_page(digest, today, stats), encoding="utf-8")
    log(f"Wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
