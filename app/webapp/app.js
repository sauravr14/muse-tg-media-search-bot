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
  const api = async (path, ms) => {
    const sep = path.includes("?") ? "&" : "?";
    // hard timeout: a hung request must fail fast instead of leaving
    // skeleton cards on screen forever
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 20000);
    try {
      const r = await fetch(path + (camOn ? sep + "hide_cam=1" : ""),
                            {signal: ctl.signal});
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    } finally { clearTimeout(t); }
  };

  // theatre-print filter: hides HDCAM/CAM/HDTS/PreDVD etc. app-wide
  let camOn = false;
  try { camOn = localStorage.getItem("camFilter") === "1"; } catch (e) {}
  function renderCamToggle() {
    const b = document.getElementById("camToggle");
    if (!b) return;
    b.classList.toggle("on", camOn);
    b.innerHTML = camOn ? "✨ Clean only" : "🎭 All prints";
  }
  function setCam(on) {
    camOn = on;
    try { localStorage.setItem("camFilter", on ? "1" : "0"); } catch (e) {}
    renderCamToggle();
    route();
  }

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

  const SORTS = [["recent", "🆕 Recent"], ["az", "🔤 A–Z"],
                 ["rating", "⭐ Top rated"], ["year", "📅 Newest"]];
  let browseState = {kind: "", sort: "recent", page: 1, hasMore: false, loading: false};

  async function browse(kind, resetSort) {
    setTab(kind === "movie" ? "movies" : "series");
    if (resetSort || browseState.kind !== kind)
      browseState = {kind, sort: resetSort || browseState.sort, page: 1, hasMore: false, loading: false};
    const st = browseState;
    skeletons(6);
    try {
      const d = await api(`/api/browse?kind=${kind}&sort=${st.sort}&page=1&per=30`);
      st.page = 2; st.hasMore = d.has_more;
      renderBrowse(d);
    } catch (e) { view.innerHTML = `<div class="empty">Couldn't load.</div>`; }
  }

  function renderBrowse(d) {
    const st = browseState;
    const label = st.kind === "movie" ? "Movies" : "Series";
    if (!d.results.length && st.page === 2) {
      view.innerHTML = `<div class="empty">No ${label.toLowerCase()} yet.</div>`;
      return;
    }
    let h = `<div class="section-title">${label} <span class="count">${d.total}</span></div>`;
    h += `<div class="sortbar">` + SORTS.map(([k, l]) =>
      `<button data-sort="${k}" class="${st.sort === k ? "active" : ""}">${l}</button>`).join("") + `</div>`;
    h += `<div class="grid" id="bgrid">${d.results.map(card).join("")}</div>`;
    if (d.has_more) h += `<div class="more-wrap"><button class="btn ghost" id="more">Load more</button></div>`;
    view.innerHTML = h;
    bindCards();
    view.querySelectorAll("[data-sort]").forEach((b) =>
      b.addEventListener("click", () => {
        if (b.dataset.sort !== browseState.sort) browse(browseState.kind, b.dataset.sort);
      }));
    const more = document.getElementById("more");
    if (more) more.addEventListener("click", loadMore);
  }

  async function loadMore() {
    const st = browseState;
    if (st.loading || !st.hasMore) return;
    st.loading = true;
    const btn = document.getElementById("more");
    if (btn) btn.textContent = "Loading…";
    try {
      const d = await api(`/api/browse?kind=${st.kind}&sort=${st.sort}&page=${st.page}&per=30`);
      st.page += 1; st.hasMore = d.has_more;
      document.getElementById("bgrid").insertAdjacentHTML("beforeend", d.results.map(card).join(""));
      bindCards();
      if (btn) { if (st.hasMore) btn.textContent = "Load more"; else btn.remove(); }
    } catch (e) { if (btn) btn.textContent = "Retry"; }
    st.loading = false;
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
        if (b.disabled) return;
        b.disabled = true;
        const old = b.textContent; b.textContent = "Sending…";
        try {
          // ask the backend to deliver straight to the user's DM —
          // no fragile deep-link hop, works every tap
          const ctl = new AbortController();
          const t = setTimeout(() => ctl.abort(), 30000);
          const r = await fetch("/api/getfile", {
            method: "POST",
            headers: {"Content-Type": "application/json",
                      "x-telegram-init-data": tg ? tg.initData : ""},
            body: JSON.stringify({file_id: +b.dataset.file}),
            signal: ctl.signal,
          }).finally(() => clearTimeout(t));
          if (!r.ok) {
            const msg = await r.text();
            throw new Error(msg.includes("not_started")
              ? "not_started" : msg);
          }
          toast("📩 File sent to your chat!");
        } catch (e) {
          toast(e.message === "not_started"
            ? "Open the bot chat and press START first, then tap Get again."
            : "Couldn't send the file. Try again.");
        }
        b.disabled = false; b.textContent = old;
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
  renderCamToggle();
  document.getElementById("camToggle").addEventListener("click", () => setCam(!camOn));
  document.getElementById("camInfo").addEventListener("click", () => {
    document.getElementById("camModal").hidden = false;
  });
  document.getElementById("camModalClose").addEventListener("click", () => {
    document.getElementById("camModal").hidden = true;
  });
  document.getElementById("camModal").addEventListener("click", (e) => {
    if (e.target.id === "camModal") e.target.hidden = true;
  });
  window.addEventListener("hashchange", route);

  // deep link: #/title/123  or  ?startapp=title_123 (from Telegram web_app button)
  const startParam = tg && tg.initDataUnsafe ? (tg.initDataUnsafe.start_param || "") : "";
  const sp = startParam.match(/^title_(\d+)$/);
  if (sp) { if (tg && tg.BackButton) tg.BackButton.show(); titlePage(+sp[1]); }
  else if (location.hash.match(/^#\/title\/\d+/)) route(); else home();
})();
