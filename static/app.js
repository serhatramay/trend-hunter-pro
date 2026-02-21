const state = {
  filter: "all",
  keywordFilter: "",
  scanning: false,
  savedSet: new Set(),
  keywords: [],
  discoverTimeframe: "1h",
  discoverPerPage: 25,
  discoverPage: 1,
  discoverTotalPages: 1,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(`/api/${path}`, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || data.message || `HTTP ${res.status}`);
  }
  return data;
}

function discoverQuery(force = false) {
  const params = new URLSearchParams();
  params.set("timeframe", state.discoverTimeframe);
  params.set("per_page", String(state.discoverPerPage));
  params.set("page", String(state.discoverPage));
  if (state.keywordFilter) params.set("keyword", state.keywordFilter);
  if (force) params.set("force", "1");
  return `discover?${params.toString()}`;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    t.style.display = "none";
  }, 2200);
}

function fmtDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" });
}

function updateStatusBadge(scanning) {
  const statusText = $("statusText");
  const dot = $("dot");
  if (scanning) {
    statusText.textContent = "Taranıyor";
    dot.style.background = "#e36414";
  } else {
    statusText.textContent = "Hazır";
    dot.style.background = "#2a9d8f";
  }
}

function renderKeywords(items) {
  const root = $("kwList");
  if (!items.length) {
    root.innerHTML = '<p class="muted">Henüz kelime yok.</p>';
    return;
  }

  root.innerHTML = items
    .map((k) => {
      const active = state.keywordFilter === k.keyword ? "active" : "";
      return `
        <div class="kw-chip ${active}" data-keyword="${escapeHtml(k.keyword)}">
          <span>${escapeHtml(k.keyword)}</span>
          <span>(${k.count})</span>
          <span class="x" data-remove="${escapeHtml(k.keyword)}">×</span>
        </div>
      `;
    })
    .join("");
}

function renderScans(items) {
  const root = $("scanHistory");
  if (!items.length) {
    root.innerHTML = '<p class="muted">Henüz tarama geçmişi yok.</p>';
    return;
  }

  root.innerHTML = items
    .map((s) => {
      const stat = s.success ? "OK" : "HATA";
      return `<div class="scan-item">${fmtDate(s.started_at)} | ${stat} | yeni: ${s.new_articles} | toplam: ${s.total_articles}</div>`;
    })
    .join("");
}

function renderNews(items, total) {
  const root = $("newsList");
  $("newsMeta").textContent = `${total} sonuç`;

  if (!items.length) {
    root.innerHTML = '<p class="muted">Bu filtrede sonuç yok.</p>';
    return;
  }

  root.innerHTML = items
    .map((n) => {
      const cardFlags = ["news-card"];
      if (n.is_new) cardFlags.push("new");
      if (n.trend_signal) cardFlags.push("signal");
      const saved = n.saved === 1 || state.savedSet.has(n.id);
      return `
      <article class="${cardFlags.join(" ")}" data-link="${escapeHtml(n.link)}">
        <h3 class="news-title">${escapeHtml(n.title)}</h3>
        <div class="news-meta-row">
          <span class="tag">${escapeHtml(n.keyword)}</span>
          <span>${escapeHtml(n.source || "Kaynak")}</span>
          <span>${fmtDate(n.published_at || n.discovered_at)}</span>
          <span class="score">Skor ${n.trend_score}</span>
        </div>
        <div class="news-actions">
          <button class="save-btn ${saved ? "saved" : ""}" data-id="${n.id}">
            ${saved ? "Kaydedildi" : "Kaydet"}
          </button>
          <a href="${n.link}" target="_blank" rel="noopener noreferrer">Aç</a>
        </div>
      </article>`;
    })
    .join("");
}

function renderDiscoverList(rootId, list) {
  const root = $(rootId);
  const items = list || [];
  if (!items.length) {
    root.innerHTML = '<p class="muted">Bu aralıkta keşif sorgusu bulunamadı.</p>';
    return;
  }
  root.innerHTML = items
    .map((it) => {
      const fv = it.formatted_value || it.value || 0;
      const breakout = String(fv).toLowerCase() === "breakout" || String(fv).toLowerCase() === "hızlı artış";
      return `
      <div class="related-row">
        <div class="related-q">${escapeHtml(it.query)} <span class="muted">(${escapeHtml((it.from_keywords || []).slice(0,2).join(", "))})</span></div>
        <div class="related-right">
          <span class="trend-chip ${breakout ? "up" : ""}">${escapeHtml(String(fv))}</span>
          <button class="btn btn-primary related-add-btn" data-q="${escapeHtml(it.query)}">+ Ekle</button>
        </div>
      </div>`;
    })
    .join("");
}

function renderDiscover(payload) {
  const src = payload?.source_keywords || [];
  $("discoverMeta").textContent = state.keywordFilter
    ? `Kaynak keyword: ${state.keywordFilter}`
    : `Kaynak keyword: ${src.length}`;
  const rising = payload?.rising || {};
  const top = payload?.top || {};
  state.discoverPage = Number(rising.page || top.page || 1);
  state.discoverTotalPages = Number(rising.total_pages || top.total_pages || 1);
  $("discoverRisingMeta").textContent = `${rising.total || 0} sonuç`;
  $("discoverTopMeta").textContent = `${top.total || 0} sonuç`;
  $("discoverPageInfo").textContent = `Sayfa ${state.discoverPage} / ${state.discoverTotalPages}`;
  renderDiscoverList("discoverRisingList", rising.items || []);
  renderDiscoverList("discoverTopList", top.items || []);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function loadAll() {
  try {
    const [status, keywords, news, scans] = await Promise.all([
      api("status"),
      api("keywords"),
      api(`news?filter=${state.filter}&keyword=${encodeURIComponent(state.keywordFilter)}&limit=140`),
      api("scans"),
    ]);
    const discover = await api(discoverQuery(false));

    $("sTotal").textContent = status.total_news;
    $("sNew").textContent = status.new_count;
    $("sSaved").textContent = status.saved_count;
    $("sScan").textContent = status.scan_count;
    $("sKw").textContent = status.keyword_count;

    $("autoToggle").checked = !!status.auto_scan;
    $("intervalSelect").value = String(status.interval_minutes || 10);
    $("lastScanInfo").textContent = `Son tarama: ${status.last_scan_time ? fmtDate(status.last_scan_time) : "-"}`;

    updateStatusBadge(status.is_scanning || state.scanning);

    state.savedSet = new Set((news.news || []).filter((n) => n.saved === 1).map((n) => n.id));
    state.keywords = (keywords.keywords || []).map((k) => k.keyword);

    renderKeywords(keywords.keywords || []);
    renderNews(news.news || [], news.total || 0);
    renderScans(scans.scans || []);
    renderDiscover(discover);
  } catch (err) {
    toast(err.message || "Veri çekilemedi");
  }
}

async function addKeywordValue(keyword, clearInput = true) {
  if (!keyword) return;
  try {
    await api("keywords", {
      method: "POST",
      body: JSON.stringify({ keyword }),
    });
    if (clearInput) {
      const inp = $("kwInput");
      if (inp) inp.value = "";
    }
    toast("Kelime eklendi");
    await loadAll();
  } catch (err) {
    toast(err.message || "Kelime eklenemedi");
  }
}

async function addKeyword() {
  const inp = $("kwInput");
  const keyword = inp.value.trim();
  await addKeywordValue(keyword, true);
}

async function deleteKeyword(keyword) {
  try {
    await api(`keywords/${encodeURIComponent(keyword)}`, { method: "DELETE" });
    if (state.keywordFilter === keyword) state.keywordFilter = "";
    toast("Kelime silindi");
    await loadAll();
  } catch (err) {
    toast(err.message || "Kelime silinemedi");
  }
}

async function triggerScan() {
  if (state.scanning) return;
  state.scanning = true;
  updateStatusBadge(true);
  try {
    const r = await api("scan", { method: "POST" });
    toast(`${r.newArticles} yeni haber bulundu`);
    await loadAll();
  } catch (err) {
    toast(err.message || "Tarama başarısız");
  } finally {
    state.scanning = false;
    updateStatusBadge(false);
  }
}

async function toggleSave(id) {
  try {
    const r = await api(`save/${id}`, { method: "POST" });
    if (r.saved) state.savedSet.add(Number(id));
    else state.savedSet.delete(Number(id));
    await loadAll();
  } catch (err) {
    toast(err.message || "Kaydetme işlemi başarısız");
  }
}

async function updateSettings(payload) {
  try {
    await api("settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadAll();
  } catch (err) {
    toast(err.message || "Ayar güncellenemedi");
  }
}

async function markSeen() {
  try {
    await api("mark-seen", { method: "POST" });
    await loadAll();
    toast("Yeni etiketleri temizlendi");
  } catch (err) {
    toast(err.message || "İşlem başarısız");
  }
}

function bindEvents() {
  $("kwAddBtn").addEventListener("click", addKeyword);
  $("kwInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addKeyword();
  });

  $("scanBtn").addEventListener("click", triggerScan);
  $("markSeenBtn").addEventListener("click", markSeen);

  $("autoToggle").addEventListener("change", (e) => {
    updateSettings({ auto_scan: e.target.checked });
  });

  $("intervalSelect").addEventListener("change", (e) => {
    updateSettings({ interval_minutes: Number(e.target.value) });
  });

  $("refreshDiscoverBtn").addEventListener("click", async () => {
    try {
      const discover = await api(discoverQuery(true));
      renderDiscover(discover);
      toast("Keşif verisi güncellendi");
    } catch (err) {
      toast(err.message || "Keşif verisi alınamadı");
    }
  });

  document.querySelectorAll("[data-discover-timeframe]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("[data-discover-timeframe]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.discoverTimeframe = btn.dataset.discoverTimeframe || "1h";
      state.discoverPage = 1;
      const discover = await api(discoverQuery(false));
      renderDiscover(discover);
    });
  });

  document.querySelectorAll("[data-discover-per-page]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("[data-discover-per-page]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.discoverPerPage = Number(btn.dataset.discoverPerPage || 25);
      state.discoverPage = 1;
      const discover = await api(discoverQuery(false));
      renderDiscover(discover);
    });
  });

  document.querySelectorAll(".news-panel .fbtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".news-panel .fbtn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filter = btn.dataset.filter || "all";
      loadAll();
    });
  });

  $("kwList").addEventListener("click", (e) => {
    const rem = e.target.closest("[data-remove]");
    if (rem) {
      deleteKeyword(rem.dataset.remove);
      return;
    }

    const chip = e.target.closest("[data-keyword]");
    if (!chip) return;
    const kw = chip.dataset.keyword;
    state.keywordFilter = state.keywordFilter === kw ? "" : kw;
    state.discoverPage = 1;
    loadAll();
  });

  $("discoverRisingList").addEventListener("click", (e) => {
    const addBtn = e.target.closest(".related-add-btn");
    if (!addBtn) return;
    const q = (addBtn.dataset.q || "").trim();
    if (!q) return;
    addKeywordValue(q, false);
  });
  $("discoverTopList").addEventListener("click", (e) => {
    const addBtn = e.target.closest(".related-add-btn");
    if (!addBtn) return;
    const q = (addBtn.dataset.q || "").trim();
    if (!q) return;
    addKeywordValue(q, false);
  });

  $("discoverPrevBtn").addEventListener("click", async () => {
    if (state.discoverPage <= 1) return;
    state.discoverPage -= 1;
    const discover = await api(discoverQuery(false));
    renderDiscover(discover);
  });
  $("discoverNextBtn").addEventListener("click", async () => {
    if (state.discoverPage >= state.discoverTotalPages) return;
    state.discoverPage += 1;
    const discover = await api(discoverQuery(false));
    renderDiscover(discover);
  });

  $("newsList").addEventListener("click", (e) => {
    const btn = e.target.closest(".save-btn");
    if (btn) {
      toggleSave(btn.dataset.id);
      return;
    }

    const directLink = e.target.closest("a");
    if (directLink) return;

    const card = e.target.closest(".news-card");
    if (!card || !card.dataset.link) return;
    window.open(card.dataset.link, "_blank", "noopener,noreferrer");
  });
}

bindEvents();
loadAll();
setInterval(loadAll, 15000);
