/* ── Utilities ──────────────────────────────────────────────────────────────── */

function timeAgo(isoStr) {
  if (!isoStr) return "never";
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60)   return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function timeUntil(isoStr) {
  if (!isoStr) return "—";
  const diff = (new Date(isoStr).getTime() - Date.now()) / 1000;
  if (diff <= 0)   return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

function formatDate(isoStr) {
  if (!isoStr || isoStr === "pre-existing") return isoStr || "—";
  try {
    return new Date(isoStr).toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit"
    });
  } catch { return isoStr; }
}

/* ── Dashboard ──────────────────────────────────────────────────────────────── */

function initDashboard() {
  refreshStatus();
  loadManga();
  setInterval(refreshStatus, 15000);
}

async function refreshStatus() {
  try {
    const data = await fetch("/api/status").then(r => r.json());
    const dot  = document.getElementById("statusDot");
    const text = document.getElementById("statusText");
    const btn  = document.getElementById("scanNowBtn");

    if (data.scanning) {
      dot.className  = "status-dot scanning";
      text.textContent = "Scanning for new chapters...";
      if (btn) btn.disabled = true;
    } else {
      dot.className  = "status-dot idle";
      text.textContent = "Idle — ready to scan";
      if (btn) btn.disabled = false;
    }
    refreshScanLog(data.scanning);

    const lastEl = document.getElementById("lastScan");
    const nextEl = document.getElementById("nextScan");
    if (lastEl) lastEl.textContent = timeAgo(data.last_scan);
    if (nextEl) nextEl.textContent = data.next_scan ? `in ${timeUntil(data.next_scan)}` : "—";
  } catch { /* network error, ignore */ }
}

let _logPollTimer = null;

function _fmtTime() {
  const now = new Date();
  return now.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

async function refreshScanLog(scanning) {
  const panel   = document.getElementById("scanLogPanel");
  const body    = document.getElementById("scanLogBody");
  const title   = document.getElementById("scanLogTitle");
  const spinner = document.getElementById("scanSpinner");
  if (!panel || !body) return;

  try {
    const data = await fetch("/api/scan-log").then(r => r.json());
    const hasContent = data.log && data.log.length > 0;

    if (hasContent || data.scanning) {
      panel.classList.remove("hidden");
    }

    if (hasContent) {
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      body.innerHTML = data.log.map(entry => {
        const msg   = typeof entry === "string" ? entry : entry.msg;
        const level = typeof entry === "string" ? "info" : (entry.level || "info");
        const time  = entry.time || "";
        return `<div class="log-line level-${level}"><span class="log-time">${time}</span><span class="log-text">${escHtml(msg)}</span></div>`;
      }).join("");
      if (atBottom) body.scrollTop = body.scrollHeight;
    }

    if (data.scanning) {
      if (title) title.textContent = "Scan in progress...";
      if (spinner) spinner.style.display = "";
      if (!_logPollTimer) _logPollTimer = setInterval(() => refreshScanLog(true), 2000);
    } else {
      if (title) title.textContent = hasContent ? "Last scan complete" : "Scan log";
      if (spinner) spinner.style.display = "none";
      if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
    }
  } catch { /* ignore */ }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function loadManga() {
  const grid = document.getElementById("mangaGrid");
  const countBadge = document.getElementById("mangaCount");
  try {
    const manga = await fetch("/api/manga").then(r => r.json());
    countBadge.textContent = `${manga.length} series`;

    if (!manga.length) {
      grid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">&#128366;</div>
          <p>No manga configured yet.</p>
          <a href="/config" class="btn btn-primary" style="margin-top:8px">Go to Config</a>
        </div>`;
      return;
    }

    grid.innerHTML = manga.map(m => buildMangaCard(m)).join("");
  } catch (e) {
    grid.innerHTML = `<div class="empty-state"><p>Failed to load library: ${e.message}</p></div>`;
  }
}

function buildMangaCard(m) {
  // Use the cached MangaDex CDN URL if available (works even when NAS is offline),
  // otherwise fall back to the proxied NAS cover endpoint.
  const coverUrl = m.cover_url || `/api/cover/${encodeURIComponent(m.name)}`;
  const latestChap = m.latest_chapter != null ? `Ch. ${m.latest_chapter}` : "—";

  // Most recently downloaded chapter (chapters already sorted desc by number)
  const recentChap = m.chapters.length ? m.chapters[0] : null;
  let recentStr;
  if (recentChap && recentChap.downloaded_at) {
    recentStr = `Ch. ${recentChap.number} &mdash; ${formatDate(recentChap.downloaded_at)}`;
  } else if (recentChap) {
    recentStr = `Ch. ${recentChap.number}`;
  } else {
    recentStr = "—";
  }

  const chapRows = m.chapters.slice(0, 200).map(c => `
    <div class="chapter-row">
      <span class="chapter-num">Ch. ${c.number}</span>
      <span class="chapter-date">${c.downloaded_at ? formatDate(c.downloaded_at) : "—"}</span>
    </div>`).join("");

  const srcClass = m.source === 'MangaDex' ? 'mangadex' : m.source === 'Web Scraper' ? 'scraper' : 'third';
  const cardId   = `card-${m.id.replace(/[^a-z0-9]/gi, "_")}`;

  return `
    <div class="manga-card" id="${cardId}">
      <div class="manga-card-header">
        <img class="manga-cover"
             src="${coverUrl}"
             alt="cover"
             onerror="this.outerHTML='<div class=\\'manga-cover-placeholder\\'>&#128366;</div>'" />
        <div class="manga-meta">
          <div class="manga-title" title="${escHtml(m.name)}">${escHtml(m.name)}</div>
          <div class="manga-source-badge ${srcClass}">${m.source}</div>
          <div class="manga-stats">
            <div class="stat-row">
              <span>Chapters on NAS</span>
              <span class="stat-value ${m.total_chapters > 0 ? 'stat-positive' : ''}">${m.total_chapters}</span>
            </div>
            <div class="stat-row">
              <span>Latest chapter</span>
              <span class="stat-value">${latestChap}</span>
            </div>
            <div class="stat-row" style="flex-direction:column;gap:2px">
              <span>Last downloaded</span>
              <span class="stat-value" style="font-size:11px">${recentStr}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="manga-card-footer">
        <div class="card-footer-actions">
          <button class="chapters-toggle" onclick="toggleChapters('${cardId}')">
            <span>Chapter history (${m.chapters.length})</span>
            <span class="toggle-arrow" id="arrow-${cardId}">&#9660;</span>
          </button>
          <button class="btn-download-now" id="dl-${cardId}"
                  onclick="downloadSingle('${m.id}','${m.source}','${cardId}')"
                  title="Download new chapters for this manga now">
            &#8595; Download Now
          </button>
        </div>
        <div class="chapters-list" id="chaplist-${cardId}">
          ${chapRows || '<div class="chapter-row"><span class="chapter-date" style="color:var(--text-muted)">No history recorded — chapters may exist on NAS from before tracking started.</span></div>'}
        </div>
      </div>
    </div>`;
}

function toggleChapters(cardId) {
  const list  = document.getElementById(`chaplist-${cardId}`);
  const arrow = document.getElementById(`arrow-${cardId}`);
  list.classList.toggle("open");
  arrow.classList.toggle("open");
}

async function triggerScan() {
  const btn = document.getElementById("scanNowBtn");
  if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/scan", { method: "POST" }).then(r => r.json());
    if (!r.success) alert(r.message);
    else {
      refreshStatus();
      // Poll until scan finishes then reload manga list
      const poll = setInterval(async () => {
        const s = await fetch("/api/status").then(r => r.json());
        if (!s.scanning) {
          clearInterval(poll);
          loadManga();
          refreshStatus();
        }
      }, 3000);
    }
  } catch (e) { alert("Could not contact server: " + e.message); }
}

async function triggerKomgaScan() {
  const btn    = document.getElementById("komgaScanBtn");
  const status = document.getElementById("komgaScanStatus");
  if (btn) { btn.disabled = true; btn.textContent = "Scanning..."; }
  if (status) status.textContent = "";
  try {
    const r = await fetch("/api/komga/scan", { method: "POST" }).then(r => r.json());
    if (status) {
      status.textContent = r.success ? "✓ Scan triggered successfully!" : "✗ " + r.message;
      status.style.color = r.success ? "var(--success)" : "var(--accent)";
    }
  } catch(e) {
    if (status) { status.textContent = "✗ Request failed"; status.style.color = "var(--accent)"; }
  }
  if (btn) { btn.disabled = false; btn.textContent = "↻ Scan Komga Now"; }
  setTimeout(() => { if (status) status.textContent = ""; }, 5000);
}

async function downloadSingle(mangaId, source, cardId) {
  const btn = document.getElementById(`dl-${cardId}`);
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Downloading..."; }

  try {
    const r = await fetch("/api/scan/single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: mangaId, source }),
    }).then(r => r.json());

    if (!r.success) {
      alert(r.message);
      if (btn) { btn.disabled = false; btn.innerHTML = "&#8595; Download Now"; }
      return;
    }

    // Show scan log and poll until done
    const panel = document.getElementById("scanLogPanel");
    if (panel) panel.classList.remove("hidden");
    refreshStatus();

    const poll = setInterval(async () => {
      const s = await fetch("/api/status").then(r => r.json());
      refreshScanLog(s.scanning);
      if (!s.scanning) {
        clearInterval(poll);
        loadManga();
        if (btn) { btn.disabled = false; btn.innerHTML = "&#8595; Download Now"; }
      }
    }, 2000);
  } catch (e) {
    alert("Error: " + e.message);
    if (btn) { btn.disabled = false; btn.innerHTML = "&#8595; Download Now"; }
  }
}

/* ── Cleanup blocked manga ───────────────────────────────────────────────────── */

async function triggerCleanup() {
  const btn = document.getElementById("cleanupBtn");
  const modal = document.getElementById("cleanupModal");
  const modalTitle = document.getElementById("cleanupModalTitle");
  const modalBody  = document.getElementById("cleanupModalBody");

  if (!confirm(
    "This will check every MangaDex manga in your library.\n\n" +
    "Any manga whose chapters are all blocked or hosted externally (e.g. Viz-licensed) " +
    "will be removed from your library AND deleted from your NAS.\n\n" +
    "This may take a minute. Continue?"
  )) return;

  if (btn) { btn.disabled = true; btn.textContent = "⏳ Checking…"; }

  try {
    const r = await fetch("/api/cleanup/blocked", { method: "POST" }).then(r => r.json());

    if (r.removed.length === 0) {
      modalTitle.textContent = "✅ Nothing to clean up";
      modalBody.innerHTML = `<p style="color:var(--text-muted)">All ${r.kept_count} manga in your library have downloadable chapters on MangaDex.</p>`;
    } else {
      modalTitle.textContent = `🧹 Removed ${r.removed.length} blocked manga`;
      const rows = r.removed.map(m => `
        <div style="padding:8px 0;border-bottom:1px solid var(--border,#333)">
          <strong>${escHtml(m.name)}</strong>
          <span style="font-size:11px;color:var(--text-muted);margin-left:8px">${m.nas_deleted ? "NAS folder deleted" : "NAS folder not found"}</span>
          <div style="font-size:12px;color:#e07070;margin-top:3px">${escHtml(m.reason)}</div>
        </div>`).join("");
      const errHtml = r.errors.length
        ? `<div style="margin-top:12px;font-size:12px;color:#e07070"><strong>Errors:</strong><br>${r.errors.map(e => escHtml(e)).join("<br>")}</div>`
        : "";
      modalBody.innerHTML = rows + errHtml +
        `<p style="margin-top:12px;font-size:13px;color:var(--text-muted)">${r.kept_count} manga remain in your library.</p>`;
    }

    modal.style.display = "flex";
  } catch(e) {
    alert("Cleanup failed: " + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🧹 Clean Up Blocked"; }
  }
}

/* ── MangaDex Search ─────────────────────────────────────────────────────────── */

let _searchTimeout = null;

function searchMangaDex() {
  const q = document.getElementById("mangaSearchInput")?.value.trim();
  if (!q || q.length < 2) return;

  const resultsEl = document.getElementById("searchResults");
  resultsEl.classList.remove("hidden");
  resultsEl.innerHTML = '<div class="search-loading">Searching MangaDex...</div>';

  fetch(`/api/search/mangadex?q=${encodeURIComponent(q)}`)
    .then(r => r.json())
    .then(results => {
      if (!Array.isArray(results)) {
        resultsEl.innerHTML = '<div class="search-loading">Search error — please try again.</div>';
        return;
      }
      if (!results.length) {
        resultsEl.innerHTML = '<div class="search-loading">No results found.</div>';
        return;
      }
      resultsEl.innerHTML = results.map(r => `
        <div class="search-result-item" id="sr-${r.id}">
          <img class="search-result-cover"
               src="${r.cover_url || ''}"
               onerror="this.style.display='none'"
               alt="" />
          <div class="search-result-info">
            <div class="search-result-title">${escHtml(r.title)}</div>
            <div class="search-result-desc">${escHtml(r.description)}</div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="addFromSearch('${r.id}','${escHtml(r.title).replace(/'/g,"\\'")}')">
            + Add
          </button>
        </div>`).join("");
    })
    .catch(() => {
      resultsEl.innerHTML = '<div class="search-loading">Search failed — is the server running?</div>';
    });
}

function addFromSearch(mangaId, title) {
  const tbody = document.getElementById("mangaTableBody");
  const list  = tbody._list || [];

  if (list.some(m => m.id === mangaId)) {
    showMsg("Already in your list.", false);
    return;
  }

  list.push({ id: mangaId, name: title });
  renderMangaTable(list);

  // Mark the button as added in the search results panel
  const btn = document.querySelector(`#sr-${mangaId} button`);
  if (btn) { btn.textContent = "✓ Added"; btn.disabled = true; btn.style.color = "var(--success)"; }

  showMsg(`Added: ${title}`, true);
  saveConfig(true);
}

/* ── Config page ────────────────────────────────────────────────────────────── */

let _currentConfig = {};

async function initConfig() {
  _currentConfig = await fetch("/api/config").then(r => r.json());
  populateForm(_currentConfig);
}

function populateForm(cfg) {
  setValue("nas_path",              cfg.nas_path ?? "");
  setValue("check_interval_hours",  cfg.check_interval_hours ?? 6);
  setValue("language",              cfg.language ?? "en");
  setValue("image_quality",         cfg.image_quality ?? "data");
  setValue("page_delay_seconds",    cfg.page_delay_seconds ?? 0.5);
  setValue("chapter_delay_seconds", cfg.chapter_delay_seconds ?? 2);
  setValue("max_chapters_per_run",  cfg.max_chapters_per_run ?? 0);
  setValue("web_port",              cfg.web_port ?? 8080);
  // Integrations
  setValue("discord_webhook_url",   cfg.discord_webhook_url ?? "");
  setValue("komga_url",             cfg.komga_url ?? "");
  setValue("komga_username",        cfg.komga_username ?? "");
  setValue("komga_password",        cfg.komga_password ?? "");
  setValue("komga_library_id",      cfg.komga_library_id ?? "");

  renderMangaTable(cfg.manga ?? []);
  renderSitesTable(cfg.third_party_sites ?? []);
}

function setValue(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function renderMangaTable(list) {
  const tbody = document.getElementById("mangaTableBody");
  if (!tbody) return;
  tbody.innerHTML = list.map((m, i) => `
    <tr id="manga-row-${i}">
      <td style="font-size:11px;color:var(--text-muted)">${m.id}</td>
      <td><strong>${escHtml(m.name ?? "")}</strong></td>
      <td><button class="btn btn-danger" onclick="removeMangaRow(${i})">Remove</button></td>
    </tr>`).join("");
  tbody._list = list.slice();
  const lbl = document.getElementById("mangaCountLabel");
  if (lbl) lbl.textContent = list.length;
}

function addMangaRow() {
  const rawId  = document.getElementById("newMangaId").value.trim();
  const name   = document.getElementById("newMangaName").value.trim();

  // Extract UUID from a full URL if pasted
  const uuidMatch = rawId.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  const id = uuidMatch ? uuidMatch[0] : rawId;

  if (!id) { alert("Please enter a MangaDex UUID or URL."); return; }

  const tbody = document.getElementById("mangaTableBody");
  const list  = tbody._list || [];
  if (list.some(m => m.id === id)) { alert("This manga is already in your list."); return; }

  list.push({ id, name: name || "" });
  renderMangaTable(list);
  document.getElementById("newMangaId").value   = "";
  document.getElementById("newMangaName").value = "";
  saveConfig(true);
}

function removeMangaRow(idx) {
  const tbody = document.getElementById("mangaTableBody");
  const list  = tbody._list || [];
  list.splice(idx, 1);
  renderMangaTable(list);
  saveConfig(true);
}

async function saveConfig(silent = false) {
  const tbody      = document.getElementById("mangaTableBody");
  const sitesTbody = document.getElementById("sitesTableBody");

  const newCfg = {
    ..._currentConfig,
    nas_path:              document.getElementById("nas_path").value.trim(),
    check_interval_hours:  parseFloat(document.getElementById("check_interval_hours").value),
    language:              document.getElementById("language").value.trim(),
    image_quality:         document.getElementById("image_quality").value,
    page_delay_seconds:    parseFloat(document.getElementById("page_delay_seconds").value),
    chapter_delay_seconds: parseFloat(document.getElementById("chapter_delay_seconds").value),
    max_chapters_per_run:  parseInt(document.getElementById("max_chapters_per_run").value, 10),
    web_port:              parseInt(document.getElementById("web_port").value, 10),
    discord_webhook_url:   (document.getElementById("discord_webhook_url")?.value ?? "").trim(),
    komga_url:             (document.getElementById("komga_url")?.value ?? "").trim(),
    komga_username:        (document.getElementById("komga_username")?.value ?? "").trim(),
    komga_password:        (document.getElementById("komga_password")?.value ?? "").trim(),
    komga_library_id:      (document.getElementById("komga_library_id")?.value ?? "").trim(),
    manga: (tbody._list || []),
    third_party_sites: (sitesTbody._list || []),
    scrapers: {}
  };

  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(newCfg),
    }).then(r => r.json());

    if (r.success) {
      _currentConfig = newCfg;
      if (!silent) showMsg("Settings saved successfully.", true);
    } else {
      showMsg(`Error: ${r.message}`, false);
    }
  } catch (e) {
    showMsg("Failed to save: " + e.message, false);
  }
}

/* ── Third-party sites table ─────────────────────────────────────────────── */

function renderSitesTable(list) {
  const tbody = document.getElementById("sitesTableBody");
  if (!tbody) return;
  tbody.innerHTML = list.map((s, i) => `
    <tr id="site-row-${i}">
      <td>${s.name ?? ""}</td>
      <td style="word-break:break-all;font-size:11px">${s.base_url ?? ""}</td>
      <td><code>${s.chapter_url_template ?? "{base_url}/c{num}"}</code></td>
      <td>${s.nas_folder ?? s.name ?? ""}</td>
      <td>
        <label class="toggle" style="margin:0">
          <input type="checkbox" ${s.enabled ? "checked" : ""}
                 onchange="toggleSite(${i}, this.checked)" />
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td><button class="btn btn-danger" onclick="removeSiteRow(${i})">Remove</button></td>
    </tr>`).join("");
  tbody._list = list.slice();
}

function toggleSite(idx, enabled) {
  const tbody = document.getElementById("sitesTableBody");
  if (!tbody._list) return;
  tbody._list[idx].enabled = enabled;
}

function addSiteRow() {
  const name     = document.getElementById("newSiteName").value.trim();
  const base_url = document.getElementById("newSiteUrl").value.trim();
  const template = document.getElementById("newSiteTemplate").value.trim() || "{base_url}/c{num}";
  const folder   = document.getElementById("newSiteFolder").value.trim() || name;

  if (!name || !base_url) { alert("Name and Base URL are required."); return; }

  const tbody = document.getElementById("sitesTableBody");
  const list  = tbody._list || [];
  if (list.some(s => s.base_url === base_url)) {
    alert("This site is already in your list."); return;
  }

  list.push({ name, base_url, chapter_url_template: template, nas_folder: folder, enabled: true });
  renderSitesTable(list);

  document.getElementById("newSiteName").value = "";
  document.getElementById("newSiteUrl").value  = "";
  document.getElementById("newSiteFolder").value = "";
  saveConfig(true);
}

function removeSiteRow(idx) {
  const tbody = document.getElementById("sitesTableBody");
  const list  = tbody._list || [];
  list.splice(idx, 1);
  renderSitesTable(list);
  saveConfig(true);
}

function showMsg(text, success) {
  const el = document.getElementById("saveMsg");
  el.textContent = text;
  el.className = `save-message ${success ? "success" : "error"}`;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 4000);
}
