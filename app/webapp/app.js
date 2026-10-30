/* Core TG Mini App — vanilla SPA, no build step. Talks only to our own backend. */
(function () {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const metaBase = document.querySelector('meta[name="api-base"]');
  const API_BASE = ((metaBase && metaBase.content) || window.API_BASE || "").replace(/\/$/, "");
  if (tg) {
    tg.ready(); tg.expand();
    try { tg.setHeaderColor("#07070b"); tg.setBackgroundColor("#07070b"); } catch (e) {}
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
    if (tg.BackButton) tg.BackButton.onClick(() => history.back());
  }
  const $ = (id) => document.getElementById(id);
  const view = $("view"), searchInput = $("search"), clearBtn = $("clear"),
        recentBox = $("recent-searches"), loadbar = $("loadbar");

  // ---------- helpers ----------
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmtSize = (n) => {
    n = n || 0;
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(0) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  const haptic = (kind) => { try { tg && tg.HapticFeedback && (kind === "ok" || kind === "err"
    ? tg.HapticFeedback.notificationOccurred(kind === "ok" ? "success" : "error")
    : tg.HapticFeedback.impactOccurred("light")); } catch (e) {} };
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg; el.classList.add("show");
    clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove("show"), 2400);
  }
  let busy = 0;
  function progress(on) {
    busy += on ? 1 : -1;
    if (busy > 0) { loadbar.classList.add("on"); }
    else { busy = 0; loadbar.style.width = "100%"; setTimeout(() => { loadbar.classList.remove("on"); loadbar.style.width = ""; }, 250); }
  }
  // images fade in when loaded; broken ones degrade to the clapper placeholder
  document.addEventListener("load", (e) => { if (e.target.tagName === "IMG") e.target.classList.add("in"); }, true);
  document.addEventListener("error", (e) => {
    const im = e.target;
    if (im.tagName === "IMG" && im.closest(".poster-box")) {
      im.parentElement.insertAdjacentHTML("beforeend", FALLBACK);
      im.remove();
    }
  }, true);

  // ---------- state ----------
  let camOn = store.get("camFilter2", null);
  if (camOn === null) { try { camOn = localStorage.getItem("camFilter") === "1"; } catch (e) { camOn = false; } }
  camOn = !!camOn;
  const api = async (path, opt) => {
    opt = opt || {};
    const sep = path.includes("?") ? "&" : "?";
    const ctl = new AbortController();
    if (opt.signal) opt.signal.addEventListener("abort", () => ctl.abort());
    const timer = setTimeout(() => ctl.abort(), opt.ms || 15000);
    try {
      const r = await fetch(API_BASE + path + (camOn ? sep + "hide_cam=1" : ""), {
        signal: ctl.signal,
        // Sent on every catalog call: when force-subscribe is on, the
        // backend uses this to check channel membership. When it is off,
        // the backend ignores the header and stays anonymous.
        headers: { "x-telegram-init-data": tg ? tg.initData : "" },
      });
      if (r.status === 403) {
        let d = null; try { d = await r.json(); } catch (e) {}
        if (d && d.fsub_required) { const e = new Error("fsub_required"); e.fsub = d.channels || []; throw e; }
        throw new Error("HTTP 403");
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } finally { clearTimeout(timer); }
  };
  // Force-subscribe join screen: shown when the backend says this user
  // must join channels first (only possible when force-subscribe is on).
  function showFsub(channels) {
    const links = (channels || []).map((ch) => {
      const url = ch.invite_link || (ch.username ? "https://t.me/" + ch.username : "");
      const name = esc(ch.title || ch.chat_id);
      return url ? `<a class="btn ghost" href="${url}" target="_blank" rel="noopener">📢 Join ${name}</a>`
                 : `<div class="empty" style="padding:8px">${name}</div>`;
    }).join("");
    view.innerHTML = `<div class="section-title">🔒 Join to continue</div>
      <div class="pad" style="line-height:1.6">Join the channel(s) below, then reopen the app or search again.</div>
      <div class="pad" style="display:flex;flex-direction:column;gap:8px;margin-top:12px">${links}</div>`;
  }

  // ---------- components ----------
  const FALLBACK = `<div class="poster-fallback">🎬</div>`;
  function card(t) {
    const rating = t.rating ? `<span class="badge">★ ${t.rating.toFixed(1)}</span>` : "";
    const tv = t.kind === "series" ? `<span class="badge tv">TV</span>` : "";
    return `<div class="card" data-id="${t.id}">
      <div class="poster-box">${t.poster ? "" : FALLBACK}${rating}${tv}
        ${t.poster ? `<img src="${esc(t.poster)}" alt="" loading="lazy" decoding="async">` : ""}</div>
      <div class="t">${esc(t.title)}<span class="y">${t.year || ""}</span></div></div>`;
  }
  const skeletons = (n) => { view.innerHTML = `<div class="grid">${'<div class="skel"></div>'.repeat(n || 6)}</div>`; };
  const homeSkeleton = () => {
    view.innerHTML = `<div class="skel wide"></div><div class="section-title">&nbsp;</div>
      <div class="strip">${'<div class="card"><div class="skel"></div></div>'.repeat(5)}</div>`;
  };
  function bindCards(root) {
    (root || view).querySelectorAll(".card[data-id]").forEach((c) =>
      c.addEventListener("click", () => { haptic(); location.hash = "#/title/" + c.dataset.id; }));
  }

  // ---------- HOME (stale-while-revalidate) ----------
  let heroTimer = null;
  function heroHTML(list) {
    if (!list || !list.length) return "";
    const slides = list.map((t, i) => `<div class="slide" data-id="${t.id}">
      <img src="${esc(t.backdrop)}" alt="" ${i === 0 ? 'fetchpriority="high"' : 'loading="lazy"'} decoding="async">
      <div class="slide-info"><h2>${esc(t.title)}</h2>
        <div class="slide-meta">${t.year || ""} · ${t.kind === "series" ? "TV Series" : "Movie"}${t.rating ? " · ★ " + t.rating.toFixed(1) : ""}${t.genres && t.genres.length ? " · " + esc(t.genres.slice(0, 2).join(", ")) : ""}</div>
        <div class="slide-ov">${esc(t.overview)}</div>
        <div class="hero-btns"><button class="btn">▶ Watch options</button></div></div></div>`).join("");
    return `<div class="hero"><div class="hero-track">${slides}</div>
      <div class="dots">${list.map((_, i) => `<i class="${i === 0 ? "on" : ""}"></i>`).join("")}</div></div>`;
  }
  function startHero() {
    clearInterval(heroTimer);
    const track = view.querySelector(".hero-track");
    if (!track) return;
    const slides = track.children, dots = view.querySelectorAll(".dots i");
    if (slides.length < 2) return;
    let i = 0, paused = false;
    track.addEventListener("scroll", () => {
      const k = Math.round(track.scrollLeft / track.clientWidth);
      if (k !== i) { i = k; dots.forEach((d, j) => d.classList.toggle("on", j === i)); }
    }, { passive: true });
    ["touchstart", "pointerdown"].forEach((ev) => track.addEventListener(ev, () => { paused = true; }, { passive: true }));
    heroTimer = setInterval(() => {
      if (paused || document.hidden) { paused = false; return; }
      i = (i + 1) % slides.length;
      track.scrollTo({ left: i * track.clientWidth, behavior: "smooth" });
    }, 5500);
  }
  function renderHome(d) {
    let h = heroHTML(d.hero);
    (d.rows || []).forEach((r) => {
      h += `<div class="section-title">${esc(r.title)}</div><div class="strip">${r.results.map(card).join("")}</div>`;
    });
    view.innerHTML = h || `<div class="empty"><span class="big">🎞️</span>Nothing here yet.<br>Titles appear once their posters are ready.</div>`;
    bindCards();
    view.querySelectorAll(".slide").forEach((s) =>
      s.addEventListener("click", () => { haptic(); location.hash = "#/title/" + s.dataset.id; }));
    startHero();
  }
  async function home() {
    setTab("home");
    const ck = "home:v2:" + (camOn ? 1 : 0);
    const cached = store.get(ck, null);
    if (cached) renderHome(cached); else homeSkeleton();   // paint instantly from the last visit
    progress(true);
    try {
      const d = await api("/api/home");
      store.set(ck, d);
      if (currentRoute() === "home") renderHome(d);
    } catch (e) {
      if (e.fsub) { showFsub(e.fsub); }
      else if (!cached) view.innerHTML = `<div class="empty"><span class="big">📡</span>Couldn't load.<br><br><button class="btn" id="retry">Retry</button></div>`,
        ($("retry") || {}).onclick = home;
    } finally { progress(false); }
  }

  // ---------- BROWSE ----------
  const SORTS = [["recent", "🆕 Recent"], ["az", "🔤 A–Z"], ["rating", "⭐ Top rated"], ["year", "📅 Newest"]];
  let bs = { kind: "", sort: "recent", page: 1, hasMore: false, loading: false };
  async function browse(kind, sort) {
    setTab(kind === "movie" ? "movies" : "series");
    if (bs.kind !== kind) bs = { kind, sort: "recent", page: 1, hasMore: false, loading: false };
    if (sort) bs.sort = sort;
    skeletons(9); progress(true);
    try {
      const d = await api(`/api/browse?kind=${kind}&sort=${bs.sort}&page=1&per=30`);
      bs.page = 2; bs.hasMore = d.has_more;
      renderBrowse(d);
    } catch (e) { if (e.fsub) showFsub(e.fsub); else view.innerHTML = `<div class="empty"><span class="big">📡</span>Couldn't load.</div>`; }
    finally { progress(false); }
  }
  function renderBrowse(d) {
    const label = bs.kind === "movie" ? "Movies" : "Series";
    if (!d.results.length) { view.innerHTML = `<div class="empty"><span class="big">🎞️</span>No ${label.toLowerCase()} yet.</div>`; return; }
    view.innerHTML = `<div class="section-title">${label} <span class="count">${d.total}</span></div>
      <div class="sortbar">${SORTS.map(([k, l]) => `<button data-sort="${k}" class="${bs.sort === k ? "active" : ""}">${l}</button>`).join("")}</div>
      <div class="grid" id="bgrid">${d.results.map(card).join("")}</div>
      ${d.has_more ? `<div class="more-wrap"><button class="btn ghost" id="more">Load more</button></div>` : ""}`;
    bindCards();
    view.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.sort !== bs.sort) { haptic(); browse(bs.kind, b.dataset.sort); }
    }));
    const more = $("more"); if (more) more.addEventListener("click", loadMore);
    // infinite scroll: load the next page a bit before the user hits the end
    if (d.has_more && "IntersectionObserver" in window && more) {
      const io = new IntersectionObserver((en) => { if (en[0].isIntersecting) loadMore(); }, { rootMargin: "600px" });
      io.observe(more);
    }
  }
  async function loadMore() {
    if (bs.loading || !bs.hasMore) return;
    bs.loading = true;
    const btn = $("more"); if (btn) btn.textContent = "Loading…";
    try {
      const d = await api(`/api/browse?kind=${bs.kind}&sort=${bs.sort}&page=${bs.page}&per=30`);
      bs.page += 1; bs.hasMore = d.has_more;
      const g = $("bgrid");
      if (g) { const tmp = document.createElement("div"); tmp.innerHTML = d.results.map(card).join("");
        bindCards(tmp); while (tmp.firstChild) g.appendChild(tmp.firstChild); }
      if (btn) { if (bs.hasMore) btn.textContent = "Load more"; else btn.parentElement.remove(); }
    } catch (e) { if (btn) btn.textContent = "Retry"; }
    bs.loading = false;
  }

  // ---------- SEARCH ----------
  const searchCache = new Map();
  let searchCtl = null, searchTimer = null;
  const getRecents = () => store.get("recents", []);
  function pushRecent(q) {
    const r = [q, ...getRecents().filter((x) => x.toLowerCase() !== q.toLowerCase())].slice(0, 8);
    store.set("recents", r);
  }
  function showRecents() {
    const r = getRecents();
    recentBox.hidden = !(r.length && !searchInput.value.trim());
    recentBox.innerHTML = r.map((q) => `<span class="chip" data-q="${esc(q)}">🕘 ${esc(q)}</span>`).join("");
    recentBox.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
      searchInput.value = c.dataset.q; onSearchInput(); haptic();
    }));
  }
  function onSearchInput() {
    const q = searchInput.value.trim();
    clearBtn.hidden = !q;
    clearTimeout(searchTimer);
    recentBox.hidden = true;
    if (!q) { route(); showRecents(); return; }
    searchTimer = setTimeout(() => doSearch(q), 180);
  }
  async function doSearch(q) {
    setTab("");
    const key = q.toLowerCase() + "|" + camOn;
    if (searchCtl) searchCtl.abort();            // newest keystroke wins
    searchCtl = new AbortController();
    const myCtl = searchCtl;
    if (searchCache.has(key)) renderSearch(q, searchCache.get(key)); else skeletons(6);
    progress(true);
    try {
      const d = await api("/api/search?q=" + encodeURIComponent(q), { signal: myCtl.signal, ms: 10000 });
      searchCache.set(key, d);
      if (searchInput.value.trim() === q) { renderSearch(q, d); if (d.results.length) pushRecent(q); }
    } catch (e) {
      if (myCtl.signal.aborted && searchCtl !== myCtl) return;
      if (e.fsub) { showFsub(e.fsub); }
      else view.innerHTML = `<div class="empty"><span class="big">📡</span>Search failed. Try again.</div>`;
    } finally { progress(false); }
  }
  function renderSearch(q, d) {
    view.innerHTML = d.results.length
      ? `<div class="section-title">Results for “${esc(q)}”</div><div class="grid">${d.results.map(card).join("")}</div>`
      : `<div class="empty"><span class="big">🔍</span>No results for “${esc(q)}”.<br>Try another spelling.</div>`;
    bindCards();
  }
  // animated typing placeholder
  (function typing() {
    const words = ["Avengers", "Breaking Bad", "Interstellar", "Money Heist", "Dune", "The Boys", "Oppenheimer", "Stranger Things"];
    let w = 0, c = 0, dir = 1, wait = 0;
    setInterval(() => {
      if (document.activeElement === searchInput || searchInput.value) return;
      if (wait > 0) { wait--; return; }
      c += dir;
      const cur = words[w];
      searchInput.placeholder = "Search " + cur.slice(0, c) + (Date.now() % 800 < 400 ? "▍" : " ");
      if (dir === 1 && c >= cur.length) { dir = -1; wait = 14; }
      else if (dir === -1 && c <= 0) { dir = 1; w = (w + 1) % words.length; wait = 4; }
    }, 90);
    searchInput.addEventListener("focus", () => { searchInput.placeholder = "Search movies, series…"; showRecents(); });
    searchInput.addEventListener("blur", () => setTimeout(() => { recentBox.hidden = true; }, 150));
  })();

  // ---------- DETAIL ----------
  async function titlePage(id) {
    clearInterval(heroTimer);
    view.innerHTML = `<div class="skel wide" style="height:230px"></div><div class="grid" style="grid-template-columns:1fr 2fr"><div class="skel"></div><div></div></div>`;
    if (tg && tg.BackButton) tg.BackButton.show();
    window.scrollTo(0, 0); progress(true);
    try {
      const t = await api("/api/title/" + id, {ms: 30000});
      let h = `<div class="detail-top"><button class="back" onclick="history.back()">‹ Back</button>
        ${t.backdrop ? `<img class="bd" src="${esc(t.backdrop)}" alt="" decoding="async">` : ""}</div>
        <div class="detail-head"><div class="poster-box">${t.poster ? "" : FALLBACK}
          ${t.poster ? `<img src="${esc(t.poster)}" alt="" decoding="async">` : ""}</div>
        <div class="meta"><h2>${esc(t.title)}</h2><div class="meta-line">
          <span>${t.year || ""}</span><span>${t.kind === "series" ? "TV Series" : "Movie"}</span>
          ${t.rating ? `<span class="rate">★ ${t.rating.toFixed(1)}</span>` : ""}</div></div></div>`;
      if (t.genres && t.genres.length) h += `<div class="pills">${t.genres.map((g) => `<span class="pill">${esc(g)}</span>`).join("")}</div>`;
      if (t.overview) h += `<div class="overview">${esc(t.overview)}</div>`;
      if (t.kind === "movie") {
        h += `<div class="section-title">Available versions</div>` +
          (t.versions.length ? t.versions.map((v) => verRow(v)).join("") : `<div class="empty">No files yet.</div>`);
        view.innerHTML = h;
      } else {
        h += `<div class="section-title">Seasons</div><div class="season-tabs">` +
          t.seasons.map((s, i) => `<button data-s="${i}" class="${i === 0 ? "active" : ""}">S${s.season_no}</button>`).join("") +
          `</div><div id="eps"></div>`;
        view.innerHTML = h;
        const renderEps = (i) => {
          const s = t.seasons[i]; if (!s) return;
          let ph = "";
          if (s.packs && s.packs.length) {
            ph = `<div class="section-title">📦 Season packs</div>` + s.packs.map((v) => verRow(v, "📦 " + (v.pack_label || "Pack"))).join("");
          }
          $("eps").innerHTML = ph + (s.episodes.length ? s.episodes.map((e) =>
            `<div class="ep"><div class="ep-head"><span>E${String(e.ep_no).padStart(2, "0")}${e.title ? " — " + esc(e.title) : ""}</span><span>▸</span></div>
             <div class="ep-body" hidden>${e.versions.length ? e.versions.map((v) => verRow(v)).join("") : `<div class="empty" style="padding:14px">No files.</div>`}</div></div>`).join("")
            : `<div class="empty">No episodes yet.</div>`);
          view.querySelectorAll(".ep-head").forEach((el) => el.addEventListener("click", () => {
            const b = el.nextElementSibling; b.hidden = !b.hidden; el.lastElementChild.textContent = b.hidden ? "▸" : "▾"; haptic();
          }));
          bindGet();
          bindPlay();
        };
        view.querySelectorAll(".season-tabs button").forEach((b) => b.addEventListener("click", () => {
          view.querySelectorAll(".season-tabs button").forEach((x) => x.classList.remove("active"));
          b.classList.add("active"); renderEps(+b.dataset.s); haptic();
        }));
        renderEps(0);
      }
      bindGet();
      bindPlay();
    } catch (e) { if (e.fsub) showFsub(e.fsub); else view.innerHTML = `<div class="empty"><span class="big">⚠️</span>Couldn't load this title.</div>`; }
    finally { progress(false); }
  }
  function verRow(v, label) {
    const res = v.resolution || "";
    return `<div class="ver"><div><div class="q">${label ? esc(label) : ""}${res ? `<span class="res r${esc(res)}">${esc(res.replace("2160p", "4K"))}</span>` : ""}
      ${v.quality ? `<span>${esc(v.quality)}</span>` : ""}${!res && !v.quality && !label ? "Unknown" : ""}</div>
      <div class="s">${fmtSize(v.size)}${v.codec ? " · " + esc(v.codec) : ""}</div></div>
      <div class="ver-btns"><button class="btn ghost" data-play="${v.id}">▶ Play</button><button class="btn" data-file="${v.id}">Get</button></div></div>`;
  }
  function bindGet() {
    view.querySelectorAll("[data-file]").forEach((b) => b.addEventListener("click", async () => {
      if (b.disabled) return;
      b.disabled = true; haptic();
      const old = b.textContent; b.textContent = "Sending…";
      try {
        const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 45000);
        const r = await fetch(API_BASE + "/api/getfile", {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-telegram-init-data": tg ? tg.initData : "" },
          body: JSON.stringify({ file_id: +b.dataset.file }), signal: ctl.signal,
        }).finally(() => clearTimeout(t));
        if (!r.ok) { const msg = await r.text(); throw new Error(msg.includes("fsub_required") ? "fsub_required" : (msg.includes("not_started") ? "not_started" : msg)); }
        haptic("ok"); toast("📩 File sent to your chat!");
        b.textContent = "✓ Sent"; setTimeout(() => { b.textContent = old; b.disabled = false; }, 2500);
        return;
      } catch (e) {
        haptic("err");
        toast(e.message === "not_started" ? "Open the bot chat and press START first, then tap Get again."
          : (e.message === "fsub_required" ? "Join the required channels first, then tap Get again."
          : "Couldn't send the file. Try again."));
      }
      b.disabled = false; b.textContent = old;
    }));
  }

  function bindPlay() {
    view.querySelectorAll("[data-play]").forEach((b) => b.addEventListener("click", async () => {
      if (b.disabled) return;
      b.disabled = true; haptic();
      const old = b.textContent; b.textContent = "Loading…";
      try {
        const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 45000);
        const r = await fetch(API_BASE + "/api/stream_token", {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-telegram-init-data": tg ? tg.initData : "" },
          body: JSON.stringify({ file_id: +b.dataset.play }), signal: ctl.signal,
        }).finally(() => clearTimeout(t));
        if (!r.ok) { const msg = await r.text(); throw new Error(msg.includes("fsub_required") ? "fsub_required" : msg); }
        const d = await r.json();
        openPlayer(d.url, d.name || "Video");
      } catch (e) {
        haptic("err");
        toast(e.message === "fsub_required" ? "Join the required channels first, then try playing again."
          : "Couldn't start playback. Try again.");
      }
      b.disabled = false; b.textContent = old;
    }));
  }

  // ---------- popup player (same page, blurred album behind) ----------
  const playModal = $("playModal"), playVideo = $("playVideo");
  function openPlayer(url, name) {
    $("playTitle").textContent = name;
    const abs = new URL(url, location.href).href;
    const hostPath = abs.replace(/^https?:\/\//, "");
    $("playVlc").href = "intent://" + hostPath + "#Intent;scheme=https;package=org.videolan.vlc;type=video/*;end";
    $("playMx").href = "intent://" + hostPath + "#Intent;scheme=https;package=com.mxtech.videoplayer.ad;S.title=" + encodeURIComponent(name) + ";end";
    $("playCopy").onclick = async () => {
      try { await navigator.clipboard.writeText(abs); toast("Stream link copied"); }
      catch (e) { toast("Copy failed — long-press the player link"); }
      haptic("ok");
    };
    playVideo.src = url;
    playModal.hidden = false;
    playVideo.play().catch(() => {});
  }
  function closePlayer() {
    playModal.hidden = true;
    exitFsMode();
    playVideo.pause();
    playVideo.removeAttribute("src");
    playVideo.load();
  }
  // Fullscreen: the native controls' button is greyed out in Telegram's
  // webview (it doesn't support the Fullscreen API), so this button tries
  // real fullscreen first and falls back to an edge-to-edge popup.
  const playFullBtn = $("playFull");
  function exitFsMode() {
    playModal.classList.remove("fs");
    if (tg && tg.exitFullscreen) { try { tg.exitFullscreen(); } catch (e) {} }
  }
  async function toggleFs() {
    if (document.fullscreenElement) { try { await document.exitFullscreen(); } catch (e) {} return; }
    if (playModal.classList.contains("fs")) { exitFsMode(); return; }
    const req = playVideo.requestFullscreen || playVideo.webkitEnterFullscreen;
    if (req) {
      try { await req.call(playVideo); return; }
      catch (e) { /* webview blocks it: fall through to CSS mode */ }
    }
    playModal.classList.add("fs");
    if (tg && tg.expand) tg.expand();
    // Hide Telegram's own header bar too: closest a Mini App gets to
    // YouTube-style fullscreen (the OS status bar stays — only native
    // apps can hide that).
    if (tg && tg.requestFullscreen) { try { tg.requestFullscreen(); } catch (e) {} }
  }
  playFullBtn.addEventListener("click", toggleFs);
  $("playFsClose").addEventListener("click", exitFsMode);
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) exitFsMode(); });
  $("playClose").addEventListener("click", closePlayer);
  playModal.addEventListener("click", (e) => { if (e.target.id === "playModal") closePlayer(); });
  function setTab(name) {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    if (tg && tg.BackButton && name) tg.BackButton.hide();
  }
  function currentRoute() {
    if (/^#\/title\/\d+/.test(location.hash)) return "title";
    const a = document.querySelector("#tabs button.active");
    return a ? a.dataset.tab : "search";
  }
  function route() {
    const m = location.hash.match(/^#\/title\/(\d+)/);
    if (m) { titlePage(+m[1]); return; }
    clearInterval(heroTimer);
    const q = searchInput.value.trim();
    if (q) { doSearch(q); return; }
    const tab = document.querySelector("#tabs button.active");
    if (tab && tab.dataset.tab === "movies") browse("movie");
    else if (tab && tab.dataset.tab === "series") browse("series");
    else home();
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    haptic(); searchInput.value = ""; clearBtn.hidden = true; recentBox.hidden = true;
    const wasTitle = /^#\/title\//.test(location.hash);
    setTab(b.dataset.tab);
    if (wasTitle) { location.hash = ""; return; }   // hashchange -> route()
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (b.dataset.tab === "movies") browse("movie");
    else if (b.dataset.tab === "series") browse("series");
    else home();
  }));
  searchInput.addEventListener("input", onSearchInput);
  clearBtn.addEventListener("click", () => { searchInput.value = ""; clearBtn.hidden = true; route(); });

  function renderCamToggle() {
    const b = $("camToggle");
    b.classList.toggle("on", camOn);
    b.innerHTML = camOn ? "✨ Clean only" : "🎭 All prints";
  }
  $("camToggle").addEventListener("click", () => {
    camOn = !camOn; store.set("camFilter2", camOn); renderCamToggle(); haptic(); route();
  });
  renderCamToggle();
  $("camInfo").addEventListener("click", () => { $("camModal").hidden = false; });
  $("camModalClose").addEventListener("click", () => { $("camModal").hidden = true; });
  $("camModal").addEventListener("click", (e) => { if (e.target.id === "camModal") e.target.hidden = true; });
  window.addEventListener("hashchange", route);

  // ---------- splash ----------
  const splash = $("splash");
  const t0 = Date.now(), seen = (() => { try { return sessionStorage.getItem("splash") === "1"; } catch (e) { return false; } })();
  function hideSplash() {
    const wait = seen ? 0 : Math.max(0, 1500 - (Date.now() - t0));   // full intro once per session
    setTimeout(() => { splash.classList.add("hide"); setTimeout(() => splash.remove(), 600); }, wait);
    try { sessionStorage.setItem("splash", "1"); } catch (e) {}
  }

  // ---------- boot ----------
  // Decode a base64url search query carried in a start_param (q_...) or
  // in the #/search/... hash fallback. UTF-8 safe: atob gives latin-1
  // bytes, which are percent-decoded back to the original string.
  function decodeSearchParam(enc) {
    try {
      const b64 = enc.replace(/-/g, "+").replace(/_/g, "/");
      const padded = b64 + "=".repeat((4 - b64.length % 4) % 4);
      return decodeURIComponent(Array.from(atob(padded),
        (c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2)).join(""));
    } catch (e) { return ""; }
  }
  function openSearch(q) {
    if (!q) return false;
    searchInput.value = q; clearBtn.hidden = false; route();
    return true;
  }
  const startParam = tg && tg.initDataUnsafe ? (tg.initDataUnsafe.start_param || "") : "";
  const sp = startParam.match(/^title_(\d+)$/);
  const sq = startParam.match(/^q_([A-Za-z0-9_-]+)$/);
  const hashSearch = location.hash.match(/^#\/search\/([A-Za-z0-9_-]+)/);
  if (sp) { if (tg && tg.BackButton) tg.BackButton.show(); titlePage(+sp[1]); }
  else if (sq && openSearch(decodeSearchParam(sq[1]))) { /* searched */ }
  else if (hashSearch && openSearch(decodeSearchParam(hashSearch[1]))) { /* searched */ }
  else if (/^#\/title\/\d+/.test(location.hash)) route();
  else home();
  window.addEventListener("load", hideSplash);
  setTimeout(hideSplash, 3500);   // never trap the user behind the splash
})();
