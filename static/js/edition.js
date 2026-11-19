// Search and source grouping; the page works without this script.
(() => {
  const root = document.querySelector("[data-edition]");
  if (!root) return;
  const t = (k) => root.dataset["t" + k[0].toUpperCase() + k.slice(1)] || "";
  const fill = (s, vars) => s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");

  const $ = (s, el = root) => el.querySelector(s);
  const $$ = (s, el = root) => [...el.querySelectorAll(s)];
  const stories = $$(".story");

  // Group repeated sources: "The Hacker News ↗ 2 3" instead of three identical links.
  for (const el of stories) {
    const groups = new Map();
    for (const a of $$(".story-links a", el)) {
      const g = groups.get(a.dataset.source);
      if (!g) { groups.set(a.dataset.source, [a]); continue; }
      a.textContent = g.length + 1; a.className = "story-src-more"; a.title = a.dataset.source;
      g[g.length - 1].after(a); g.push(a);
    }
    const c = $(".story-count", el), n = +c.dataset.count;
    c.textContent = n === 1 ? t("sourceOne") : fill(t("sources"), { n });
  }

  // "#rce" matches a tag exactly; anything else matches the story's own text
  // (not the "N sources" label, which would make "rce" match everything).
  const index = stories.map((el) => ({
    tags: $$(".story-tags a", el).map((s) => s.textContent.slice(1).toLowerCase()),
    text: $$(".story-title, .story-summary, .story-why, .story-links, .story-tags", el)
      .map((s) => s.textContent).join(" ").toLowerCase(),
  }));

  $("[data-search]").addEventListener("input", (e) => {
    const raw = e.target.value.trim().toLowerCase();
    const tag = raw.startsWith("#") ? raw.slice(1) : null;
    let shown = 0;
    stories.forEach((el, i) => {
      const match = !raw || tag === "" || (tag !== null ? index[i].tags.includes(tag) : index[i].text.includes(raw));
      el.hidden = !match;
      if (match) shown++;
    });
    $("[data-results]").textContent = raw ? fill(t("results"), { n: shown }) : "";
    $("[data-no-results]").hidden = shown > 0;
  });

  // ¶ copies the story's permalink; the link still navigates if the clipboard is unavailable.
  root.addEventListener("click", async (e) => {
    const a = e.target.closest(".story-anchor");
    if (!a || !navigator.clipboard || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    try {
      await navigator.clipboard.writeText(a.href);
      a.dataset.state = "copied";
      setTimeout(() => delete a.dataset.state, 1600);
    } catch {
      location.href = a.href;
    }
  });

  $("[data-search-wrap]").hidden = false;
})();
