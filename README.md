# Daily Tech Digest

A static site that publishes a daily news digest for dev teams, covering security, dev, ops and tech. Every morning a GitHub Action:

1. pulls the last ~24h of entries from the RSS/Atom feeds in [`scripts/sources.toml`](scripts/sources.toml),
2. asks Claude, through the Anthropic API, to pick what matters and summarize it,
3. commits the result as `content/digest/YYYY-MM-DD.md`,
4. on Mondays, also writes a roundup of the previous week as `content/weekly/YYYY-Www.md`,
5. renders social preview images, builds the site with [Zola](https://www.getzola.org/) and [Tailwind](https://tailwindcss.com/), then deploys it to GitHub Pages.

The site has today's digest on the home page, an archive at `/digest/`, weekly roundups at `/weekly/`, tag and section pages at `/tags/` and `/categories/`, and Atom feeds at `/atom.xml`, `/tags/<tag>/atom.xml` and `/categories/<cat>/atom.xml`. `/en/...` paths redirect to the same page at the root.

## Setup

1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions**: add the `ANTHROPIC_API_KEY` secret.
   Optional repository variables are `DIGEST_MODEL` (default `claude-opus-5-5`) and `DIGEST_EFFORT` (`low` / `medium` / `high`, default `medium`).
3. **Settings → Pages → Source**: choose **GitHub Actions**.
4. **Actions → Daily digest → Run workflow** produces the first digest (tick **weekly** to also build last week's roundup). After that it runs on its own at 06:00 UTC. To change the time, edit the cron line in `.github/workflows/daily.yml`.

The workflow sets `base_url` from the Pages URL, so `config.toml` doesn't need editing.

## Local development

```sh
python -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# feeds only, no API call
.venv/bin/python scripts/digest.py --fetch-only

# print the digest without writing it
ANTHROPIC_API_KEY=... .venv/bin/python scripts/digest.py --dry-run

# write content/digest/<today>.md (add --force to overwrite)
ANTHROPIC_API_KEY=... .venv/bin/python scripts/digest.py

# re-render existing digests' front matter (tags, taxonomies), no API call
.venv/bin/python scripts/digest.py --refresh-front-matter

# last week's roundup (or --week 2026-W47; --dry-run prints it)
ANTHROPIC_API_KEY=... .venv/bin/python scripts/weekly.py

# social preview cards and favicons into static/ (gitignored)
.venv/bin/python scripts/og.py
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
- **Tags and sections**: digest.py normalizes tags (lowercase, hyphenated) and gives Claude the last two weeks' tags as a preferred vocabulary. Each digest page lists the union of its stories' tags and categories as Zola taxonomies; the tag and section pages show the matching stories with links back to their editions.
- **Weekly roundup**: `scripts/weekly.py` sends last week's stories (IDs, titles, summaries, no links) to Claude, which writes an editorial and picks about 10 stories grouped into themes. Unknown IDs are dropped and the picked stories are copied from the dailies, so their links were already validated. Weeks with fewer than 3 editions are skipped.
- **Sharing**: every story has a `¶` permalink to its anchor on its edition page. `scripts/og.py` (Pillow, fonts in `assets/fonts`) draws a 1200×630 card per edition and per week for `og:image`.
- **Pictures**: the lead and one story per other section (the first of its top three stories that has one) get the publisher's own image: the one in the feed entry, or else the article's `og:image`. Tiny, odd-shaped or unreadable images are skipped. `scripts/images.py` turns each into a crisp black-and-white photo (cropped to 3:2, contrast stretched, lightly sharpened), saved as a grayscale JPEG in `static/img/digest/<date>/` (committed, about 100–140 KB for the lead and 15–60 KB for the others) and credited to the article's site under the picture. The images belong to their publishers. Set `DIGEST_IMAGES=0` to turn pictures off, add `images = false` to a feed in `sources.toml` to skip one publisher, run `digest.py --images-only --date YYYY-MM-DD` to add pictures to an existing digest, and add `--redo` to rebuild the ones it already has.
- **Drift**: each photo is toned in the page's paper color and pans and zooms in its frame over an 18-second cycle (a CSS animation, out of step from picture to picture). Readers who prefer reduced motion get a still photo.
- **UI text**: strings live in the `[translations]` table of `config.toml`. Each digest page gets an `/en/...` alias, so that path redirects to the root.
- **Content**: each digest is a Zola page. Its front matter is JSON (valid YAML), and the structured data lives under `extra`. The templates in `templates/` render it, and the reusable pieces are Tera components in `templates/components.html`.
- **Cost**: about 26k input and 7.5k output tokens per day, roughly $0.25/day on Opus 5.5. Set `DIGEST_MODEL=claude-sonnet-5-5` to cut that in half.
