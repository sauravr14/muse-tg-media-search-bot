/* Media Library Mini App — vanilla SPA. Talks only to our own backend. */
(function () {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready(); tg.expand();
    const p = tg.themeParams || {};
    if (p.bg_color) document.documentElement.style.setProperty("--bg", p.bg_color);
    if (p.text_color) document.documentElement.style.setProperty("--text", p.text_color);
    if (p.button_color) document.documentElement.style.setProperty("--accent", p.button_color);
    if (tg.BackButton) tg.BackButton.onClick(() => history.back());
  }
  const view = document.getElementById("view");
  const searchInput = document.getElementById("search");
  const clearBtn = document.getElementById("clear");
  const PLACEHOLDER = "🎬";

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmtSize = (n) => {
    n = n || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  };
  const poster = (t, cls) => t.poster
    ? `<img class="${cls || ""}" src="${esc(t.poster)}" loading="lazy" alt="">`
    : `<div class="poster-fallback ${cls || ""}">${PLACEHOLDER}</div>`;

  function toast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg; el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2200);
  }
  function skeletons(n) {
    view.innerHTML = `<div class="grid">${'<div class="skel" style="aspect-ratio:2/3"></div>'.repeat(n || 6)}</div>`;
  }
  const api = async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  };

  function card(t) {
    return `<div class="card" data-id="${t.id}">
      ${poster(t)}
      <div class="t">${esc(t.title)}<br><span class="y">${t.year || ""} ${t.kind === "series" ? "· TV" : ""}</span></div>
    </div>`;
  }
  function bindCards() {
    view.querySelectorAll(".card").forEach((c) =>
      c.addEventListener("click", () => location.hash = "#/title/" + c.dataset.id));
  }

  async function home() {
    setTab("home"); skeletons(6);
    try {
      const [movies, series] = await Promise.all([
        api("/api/recent?kind=movie&limit=6"), api("/api/recent?kind=series&limit=6")]);
      let h = "";
      if (movies.results.length) {
        h += `<div class="section-title">🆕 Recently added movies</div><div class="grid">` +
          movies.results.map(card).join("") + "</div>";
      }
      if (series.results.length) {
        h += `<div class="section-title">🆕 Recently added series</div><div class="grid">` +
          series.results.map(card).join("") + "</div>";
      }
      view.innerHTML = h || `<div class="empty">Nothing indexed yet.<br>Add a source channel with /addchannel.</div>`;
      bindCards();
    } catch (e) { view.innerHTML = `<div class="empty">Couldn't load. <button class="btn ghost" onclick="location.reload()">Retry</button></div>`; }
  }

  async function browse(kind) {
    setTab(kind === "movie" ? "movies" : "series"); skeletons(6);
    try {
      const d = await api(`/api/recent?kind=${kind}&limit=30`);
      view.innerHTML = d.results.length
        ? `<div class="grid">${d.results.map(card).join("")}</div>`
        : `<div class="empty">No ${kind === "movie" ? "movies" : "series"} yet.</div>`;
      bindCards();
    } catch (e) { view.innerHTML = `<div class="empty">Couldn't load.</div>`; }
  }

  let searchTimer = null;
  function onSearchInput() {
    const q = searchInput.value.trim();
    clearBtn.hidden = !q;
    clearTimeout(searchTimer);
    if (!q) { route(); return; }
    searchTimer = setTimeout(() => doSearch(q), 350);
  }
  async function doSearch(q) {
    setTab(""); skeletons(6);
    try {
      const d = await api("/api/search?q=" + encodeURIComponent(q));
      view.innerHTML = d.results.length
        ? `<div class="section-title">Results for “${esc(q)}”</div><div class="grid">${d.results.map(card).join("")}</div>`
        : `<div class="empty">No results for “${esc(q)}”.<br>Try another spelling.</div>`;
      bindCards();
    } catch (e) { view.innerHTML = `<div class="empty">Search failed. Try again.</div>`; }
  }

  async function titlePage(id) {
    skeletons(2);
    if (tg && tg.BackButton) tg.BackButton.show();
    try {
      const t = await api("/api/title/" + id);
      let h = `<button class="back" onclick="history.back()">‹ Back</button>`;
      if (t.backdrop) h += `<img class="detail-backdrop" src="${esc(t.backdrop)}" loading="lazy">`;
      h += `<div class="detail-head">${poster(t, "poster")}
        <div class="meta"><h2>${esc(t.title)}</h2>
        <div>${t.year || ""} ${t.kind === "series" ? "· TV Series" : "· Movie"}</div>
        ${t.rating ? `<div>⭐ ${t.rating.toFixed(1)}</div>` : ""}</div></div>`;
      if (t.genres && t.genres.length)
        h += `<div>${t.genres.map((g) => `<span class="pill">${esc(g)}</span>`).join("")}</div>`;
      if (t.overview) h += `<div class="overview">${esc(t.overview)}</div>`;
      if (t.kind === "movie") {
        h += `<div class="section-title">Available versions</div>` +
          (t.versions.length ? t.versions.map(verRow).join("")
            : `<div class="empty">No files yet.</div>`);
        view.innerHTML = h;
      } else {
        h += `<div class="section-title">Seasons</div><div class="season-tabs">` +
          t.seasons.map((s, i) => `<button data-s="${i}" class="${i === 0 ? "active" : ""}">S${s.season_no}</button>`).join("") +
          `</div><div id="eps"></div>`;
        view.innerHTML = h;
        const renderEps = (i) => {
          const s = t.seasons[i];
          let ph = "";
          if (s.packs && s.packs.length) {
            ph = `<div class="section-title">📦 Season packs</div>` + s.packs.map((v) =>
              `<div class="ver"><div><div class="q">📦 ${esc(v.pack_label || "Pack")} · ${esc(v.resolution || "Unknown")}${v.quality ? " · " + esc(v.quality) : ""}</div>
              <div class="s">${fmtSize(v.size)}</div></div>
              <button class="btn" data-file="${v.id}">Get</button></div>`).join("");
          }
          document.getElementById("eps").innerHTML = ph + (s.episodes.length ? s.episodes.map((e) =>
            `<div class="ep"><div class="ep-head" data-ep="${e.ep_no}">
               <span>E${String(e.ep_no).padStart(2, "0")}${e.title ? " — " + esc(e.title) : ""}</span><span>▸</span>
             </div><div class="ep-body" hidden>${e.versions.length ? e.versions.map(verRow).join("") : "No files."}</div>
             </div>`).join("") : `<div class="empty">No episodes yet.</div>`);
          document.querySelectorAll(".ep-head").forEach((el) =>
            el.addEventListener("click", () => {
              const b = el.nextElementSibling; b.hidden = !b.hidden;
            }));
          bindGet();
        };
        view.querySelectorAll(".season-tabs button").forEach((b) =>
          b.addEventListener("click", () => {
            view.querySelectorAll(".season-tabs button").forEach((x) => x.classList.remove("active"));
            b.classList.add("active"); renderEps(+b.dataset.s);
          }));
        renderEps(0);
      }
      bindGet();
    } catch (e) { view.innerHTML = `<div class="empty">Couldn't load this title.</div>`; }
  }

  function verRow(v) {
    return `<div class="ver"><div><div class="q">${esc(v.resolution || "Unknown")}${v.quality ? " · " + esc(v.quality) : ""}</div>
      <div class="s">${fmtSize(v.size)}${v.codec ? " · " + esc(v.codec) : ""}</div></div>
      <button class="btn" data-file="${v.id}">Get</button></div>`;
  }
  function bindGet() {
    view.querySelectorAll("[data-file]").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const d = await api("/api/deeplink/file/" + b.dataset.file);
          if (tg) {
            tg.openTelegramLink(d.url);
            // hand the user to the bot chat where the file lands
            setTimeout(() => { try { tg.close(); } catch (e) {} }, 1200);
          } else location.href = d.url;
        } catch (e) { toast("Couldn't prepare the file link."); b.disabled = false; }
      }));
  }

  function setTab(name) {
    document.querySelectorAll("#tabs button").forEach((b) =>
      b.classList.toggle("active", b.dataset.tab === name));
    if (tg && tg.BackButton && name) tg.BackButton.hide();
  }

  function route() {
    const h = location.hash;
    const m = h.match(/^#\/title\/(\d+)/);
    if (m) { titlePage(+m[1]); return; }
    const q = searchInput.value.trim();
    if (q) { doSearch(q); return; }
    const tab = document.querySelector('#tabs button.active');
    if (tab && tab.dataset.tab === "movies") browse("movie");
    else if (tab && tab.dataset.tab === "series") browse("series");
    else home();
  }

  document.querySelectorAll("#tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      searchInput.value = ""; clearBtn.hidden = true; location.hash = "";
      setTab(b.dataset.tab);
      if (b.dataset.tab === "movies") browse("movie");
      else if (b.dataset.tab === "series") browse("series");
      else home();
    }));
  searchInput.addEventListener("input", onSearchInput);
  clearBtn.addEventListener("click", () => {
    searchInput.value = ""; clearBtn.hidden = true; route();
  });
  window.addEventListener("hashchange", route);

  // deep link: #/title/123  or  ?startapp=title_123 (from Telegram web_app button)
  const startParam = tg && tg.initDataUnsafe ? (tg.initDataUnsafe.start_param || "") : "";
  const sp = startParam.match(/^title_(\d+)$/);
  if (sp) { if (tg && tg.BackButton) tg.BackButton.show(); titlePage(+sp[1]); }
  else if (location.hash.match(/^#\/title\/\d+/)) route(); else home();
})();
