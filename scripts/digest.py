#!/usr/bin/env python3
"""Build today's tech digest: fetch RSS feeds -> ask Claude -> write a Zola page.

Usage:
    python scripts/digest.py [--force] [--dry-run] [--translate-only] [--date YYYY-MM-DD]

Writes content/digest/<date>.md (English) and <date>.fr.md (French translation).

Env:
    ANTHROPIC_API_KEY   required (except with --fetch-only)
    DIGEST_MODEL        optional, defaults to claude-opus-5-5
    DIGEST_EFFORT       optional, low|medium|high (default medium)
"""

from __future__ import annotations

import argparse
import asyncio
import copy
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

CATEGORY_LABELS_FR = {
    "sec": "Sécurité",
    "dev": "Dev & langages",
    "ops": "Ops & cloud",
    "tech": "Tech & IA",
}

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

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
# French translation (text fields only, so links can't be altered)
# ---------------------------------------------------------------------------

TRANSLATE_PROMPT = """\
You translate a daily tech briefing for software engineers from English into French. \
Write natural, concise French as a French tech journalist would. Keep product and \
project names, CVE IDs, version numbers, code identifiers, quoted vulnerability \
nicknames and English tech terms that French engineers commonly use (e.g. "patch", \
"release", "cloud", "container") unchanged. Translate every string; keep the same \
number of entries in the same order and do not add or drop information.
"""

TEXT_FIELDS = ("title", "summary", "why_it_matters")

TRANSLATION_SCHEMA = _obj(
    {
        "tldr": {"type": "array", "items": {"type": "string"}},
        "items": {
            "type": "array",
            "items": _obj({field: {"type": "string"} for field in TEXT_FIELDS}),
        },
    }
)


def translate(digest: dict) -> dict:
    """Return a French copy of `digest`; only the text fields are sent to Claude."""
    items = [item for section in digest["sections"] for item in section["items"]]
    source = {
        "tldr": digest["tldr"],
        "items": [{field: item[field] for field in TEXT_FIELDS} for item in items],
    }
    result = call_claude(
        TRANSLATE_PROMPT,
        TRANSLATION_SCHEMA,
        "Translate this JSON into French:\n\n" + json.dumps(source, ensure_ascii=False),
    )
    if len(result["tldr"]) != len(source["tldr"]) or len(result["items"]) != len(items):
        raise RuntimeError("translation does not match the English digest's structure")

    french = copy.deepcopy(digest)
    french["tldr"] = result["tldr"]
    fr_items = iter(result["items"])
    for section in french["sections"]:
        section["label"] = CATEGORY_LABELS_FR[section["category"]]
        for item in section["items"]:
            item.update({field: value for field, value in next(fr_items).items() if field in TEXT_FIELDS})
    return french


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def page_path(today: date, lang: str = "en") -> Path:
    suffix = "" if lang == "en" else f".{lang}"
    return DIGEST_DIR / f"{today}{suffix}.md"


def render_page(digest: dict, today: date, stats: dict, model: str = MODEL, lang: str = "en") -> str:
    tldr = digest["tldr"]
    if lang == "fr":
        long_date = f"{FR_DAYS[today.weekday()]} {today.day} {FR_MONTHS[today.month - 1]} {today.year}"
        title, fallback = f"Revue tech — {long_date}", "Revue tech quotidienne"
    else:
        title, fallback = f"Tech digest — {today:%A, %B} {today.day}, {today.year}", "Daily tech digest"
    front = {
        "title": title,
        "date": today.isoformat(),
        "description": tldr[0] if tldr else fallback,
        "extra": {"tldr": tldr, "sections": digest["sections"], "stats": stats, "model": model},
    }
    body = "\n".join(f"- {line}" for line in tldr)
    return f"---\n{json.dumps(front, ensure_ascii=False, indent=1)}\n---\n\n{body}\n"


def write_french(digest: dict, today: date, stats: dict, model: str = MODEL) -> bool:
    """Write <date>.fr.md. A failed translation is logged, never fatal: English stays published."""
    log("Translating into French…")
    try:
        french = translate(digest)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! French translation failed: {exc}")
        return False
    path = page_path(today, "fr")
    path.write_text(render_page(french, today, stats, model, lang="fr"), encoding="utf-8")
    log(f"Wrote {path.relative_to(ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="overwrite today's digest if it exists")
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of writing it")
    parser.add_argument("--fetch-only", action="store_true", help="only fetch feeds and print candidates")
    parser.add_argument("--translate-only", action="store_true", help="(re)write the French page from the existing English one")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()

    today: date = args.date
    out_path = page_path(today)
    fr_path = page_path(today, "fr")
    resume = out_path.exists() and not fr_path.exists() and not (args.force or args.dry_run or args.fetch_only)
    if args.translate_only or resume:
        if not out_path.exists():
            log(f"{out_path.relative_to(ROOT)} does not exist; nothing to translate")
            return 1
        extra = read_front_matter(out_path)["extra"]
        return 0 if write_french(extra, today, extra.get("stats", {}), extra.get("model", MODEL)) else 1
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
        print(json.dumps({"en": digest, "fr": translate(digest)}, ensure_ascii=False, indent=2))
        return 0

    n_items = sum(len(s["items"]) for s in digest["sections"])
    stats = {"items": n_items, "candidates": len(candidates), "sources": len(feeds)}
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_page(digest, today, stats), encoding="utf-8")
    log(f"Wrote {out_path.relative_to(ROOT)}")
    write_french(digest, today, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
