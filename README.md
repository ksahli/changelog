# Daily Tech Digest

A static site that publishes a daily news digest for dev teams, covering security, dev, ops and tech. Every morning a GitHub Action:

1. pulls the last ~24h of entries from the RSS/Atom feeds in [`scripts/sources.toml`](scripts/sources.toml),
2. asks Claude, through the Anthropic API, to pick what matters and summarize it,
3. asks Claude to translate it into French,
4. commits the result as `content/digest/YYYY-MM-DD.md` and `YYYY-MM-DD.fr.md`,
5. builds the site with [Zola](https://www.getzola.org/) and [Tailwind](https://tailwindcss.com/), then deploys it to GitHub Pages.

The site has today's digest on the home page, an archive at `/digest/`, and an Atom feed at `/atom.xml`. The French edition mirrors it under `/fr/` (`/fr/digest/`, `/fr/atom.xml`), and every page links to its other-language version.

## Setup

1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions**: add the `ANTHROPIC_API_KEY` secret.
   Optional repository variables are `DIGEST_MODEL` (default `claude-opus-5-5`) and `DIGEST_EFFORT` (`low` / `medium` / `high`, default `medium`).
3. **Settings → Pages → Source**: choose **GitHub Actions**.
4. **Actions → Daily digest → Run workflow** produces the first digest. After that it runs on its own at 06:00 UTC. To change the time, edit the cron line in `.github/workflows/daily.yml`.

The workflow sets `base_url` from the Pages URL, so `config.toml` doesn't need editing.

## Local development

```sh
python -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# feeds only, no API call
.venv/bin/python scripts/digest.py --fetch-only

# print the digest without writing it
ANTHROPIC_API_KEY=... .venv/bin/python scripts/digest.py --dry-run

# write content/digest/<today>.md and <today>.fr.md (add --force to overwrite)
ANTHROPIC_API_KEY=... .venv/bin/python scripts/digest.py

# (re)write only the French page from an existing English one
ANTHROPIC_API_KEY=... .venv/bin/python scripts/digest.py --translate-only --date 2026-11-19
```

To preview the site, put the [Tailwind standalone CLI](https://github.com/tailwindlabs/tailwindcss/releases) in `bin/` (it is gitignored) and Zola 0.23+ on your `PATH`:

```sh
bin/tailwindcss -i styles/input.css -o static/css/site.css --watch &
zola serve
```

## How it works

- **Sources**: edit `scripts/sources.toml`. Each feed has a `name`, a `url`, a `category` (`sec`, `dev`, `ops` or `tech`) and an optional `max_items`. A feed that fails is logged and skipped.
- **Filtering**: entries older than 26h are dropped. Duplicates are removed by URL and by title. Links featured in the previous 3 digests are excluded.
- **Claude**: a single request with a JSON-schema structured output: TL;DR bullets plus categorized items, each with a summary, "why it matters", importance and links. Any link that wasn't in the input is dropped, so hallucinated URLs can't reach the page. Server-side refusal fallbacks are on, so a security-heavy day that trips a safety classifier is retried on Anthropic's recommended fallback model.
- **French**: a second request sends only the text fields (TL;DR, titles, summaries, "why it matters") and gets them back in French; they are merged into a copy of the English digest by position, so links, tags and importance are identical in both languages. If translation fails the English page is still published, and the next run (or `--translate-only`) fills in the French one. UI strings live in the `[translations]` tables of `config.toml`.
- **Content**: each digest is a Zola page. Its front matter is JSON (valid YAML), and the structured data lives under `extra`. The templates in `templates/` render it, and the reusable pieces are Tera components in `templates/components.html`.
- **Cost**: about 31k input and 13.5k output tokens per day (the French translation accounts for ~5k in / ~6k out), so the daily cost is a bit under twice that of the English digest alone. Set `DIGEST_MODEL=claude-sonnet-5-5` to cut that in half.
