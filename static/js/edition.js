// Personalized front page: stack, read/saved state, search, patch checklist.
// Everything lives in localStorage; the page works without this script.
(() => {
  const root = document.querySelector("[data-edition]");
  if (!root) return;
  const KEY = "changelog:v1";
  const t = (k) => root.dataset["t" + k[0].toUpperCase() + k.slice(1)] || "";
  const fill = (s, vars) => s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
  const st = { stack: [], read: [], saved: [], done: [], ...load() };
  const save = () => localStorage.setItem(KEY, JSON.stringify(st));
  const toggle = (list, v) => { const i = st[list].indexOf(v); i < 0 ? st[list].push(v) : st[list].splice(i, 1); save(); render(); };

  const $ = (s, el = root) => el.querySelector(s);
  const $$ = (s, el = root) => [...el.querySelectorAll(s)];
  const lead = $("#lead"), forYou = $("#for-you"), rest = $("#stories");
  const stories = $$(".story").sort((a, b) => a.dataset.order - b.dataset.order);
  const tagsOf = (el) => el.dataset.tags.split(" ").filter(Boolean);
  const mine = (el) => tagsOf(el).some((x) => st.stack.includes(x));
  const defaultLead = $(".story", lead);
  let query = "";

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

  // Stack chips: every tag in this edition, plus anything already followed.
  const allTags = [...new Set([...stories.flatMap(tagsOf), ...st.stack])].sort();
  const chipBox = $("[data-stack-chips]");
  for (const tag of allTags) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "stack-chip"; b.textContent = tag; b.dataset.tag = tag;
    b.addEventListener("click", () => toggle("stack", tag));
    chipBox.append(b);
  }

  root.addEventListener("click", (e) => {
    const el = e.target.closest(".story");
    if (e.target.closest("[data-save],[data-save-text]")) { e.preventDefault(); toggle("saved", el.dataset.key); }
    else if (e.target.closest("[data-read]")) { e.preventDefault(); toggle("read", el.dataset.key); }
    const chip = e.target.closest("[data-patch]");
    if (chip && !e.altKey) { e.preventDefault(); toggle("done", chip.dataset.patch); }
  });
  $("[data-search]").addEventListener("input", (e) => { query = e.target.value.trim().toLowerCase().replace(/^#/, ""); render(); });

  function pickLead() {
    const unread = (el) => !st.read.includes(el.dataset.key);
    return stories.find((el) => el.dataset.importance === "high" && mine(el) && unread(el)) || defaultLead || stories[0];
  }

  function render() {
    const leadEl = pickLead();
    let shown = 0;
    for (const el of stories) {
      const key = el.dataset.key, isMine = mine(el);
      const target = el === leadEl ? lead : isMine ? forYou : rest;
      target.append(el); // stories are iterated in order, so each container stays ordered
      if (el === leadEl) el.open = true;
      else if (target === forYou) el.open = true;
      const match = !query || el.textContent.toLowerCase().includes(query);
      el.hidden = !match; if (match) shown++;
      const read = st.read.includes(key), saved = st.saved.includes(key);
      el.classList.toggle("is-read", read);
      const star = $("[data-save]", el);
      star.hidden = false; star.textContent = saved ? "★" : "☆"; star.setAttribute("aria-pressed", saved); star.classList.toggle("is-on", saved);
      const sv = $("[data-save-text]", el); sv.hidden = false; sv.textContent = saved ? t("saved") : t("save");
      const rd = $("[data-read]", el); rd.hidden = false; rd.textContent = read ? "✓ " + t("read") : t("markRead");
      const matchTag = tagsOf(el).find((x) => st.stack.includes(x));
      $("[data-reason]", el).textContent = el === leadEl ? (isMine ? "· " + t("inStack") : "· " + t("topStory")) : target === forYou && matchTag ? "#" + matchTag : "";
      for (const tg of $$(".story-tags span", el)) tg.classList.toggle("is-mine", st.stack.includes(tg.textContent.slice(1)));
    }
    const nMine = stories.filter(mine).length, nHigh = stories.filter((el) => mine(el) && el.dataset.importance === "high").length;
    const sum = $("[data-summary]");
    if (sum) sum.textContent = st.stack.length ? fill(t("summary"), { mine: nMine, total: stories.length, high: nHigh }) : t("summaryEmpty");
    $("[data-for-you-wrap]").hidden = !forYou.querySelector(".story:not([hidden])");
    $("[data-stack-label]").textContent = st.stack.map((x) => "#" + x).join(" ");
    $("[data-results]").textContent = query ? fill(t("results"), { n: shown }) : "";
    $("[data-no-results]").hidden = shown > 0;
    for (const c of $$("[data-patch]")) {
      c.classList.toggle("is-done", st.done.includes(c.dataset.patch));
      c.classList.toggle("is-mine", tagsOf(c).some((x) => st.stack.includes(x)));
    }
    for (const b of $$(".stack-chip")) b.setAttribute("aria-pressed", st.stack.includes(b.dataset.tag));
  }

  $("[data-search-wrap]").hidden = false;
  $("[data-stack-panel]").hidden = false;
  root.classList.add("is-enhanced");
  render();
})();
