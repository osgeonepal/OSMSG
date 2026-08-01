// OSMSG Leaderboard
// Single source for the API origin: overridable at deploy time via window.OSMSG_API_BASE (e.g. an
// env-generated config script), else the page's own origin. Everything (fetches, docs link) derives from it.
const API_BASE = window.OSMSG_API_BASE || window.location.origin;
const HEALTH_ENDPOINT = "/health";
const ALL_TIME_START = "2004-08-09T00:00:00Z";
const RANGE_HOURS = { "1h": 1, "24h": 24, "7d": 168, "30d": 720, "90d": 2160 };
const RANGE_LABELS = {
  "1h": "last hour",
  "24h": "last 24 hours",
  "7d": "last 7 days",
  "30d": "last 30 days",
  "90d": "last 90 days",
  all: "all-time",
  custom: "custom range",
};
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

const state = {
  hashtags: [],
  range: "24h",
  customStart: null,
  customEnd: null,
  rows: [],
  batch: [],
  batchIndex: -1,
  batchSize: 100,
  podium: [],
  summary: null,
  tagRows: [],
  hashtagTrends: [],
  total: 0,
  totalPages: 1,
  sort: { key: "map_changes", dir: "desc" },
  search: "",
  windowStart: null,
  windowEnd: null,
  lastFetched: null,
  lastError: null,
  health: null,
  loading: false,
  status: "loading",
  refreshTimer: null,
  agoTimer: null,
  clockTimer: null,
  inflight: null,
  page: 1,
  pageSize: 10,
  osmAvatars: new Map(),
  editorStats: null,
};
state.userEditors = new Map();

function fetchOsmAvatar(uid) {
  if (uid == null) return Promise.resolve(null);
  const key = String(uid);
  if (state.osmAvatars.has(key)) return state.osmAvatars.get(key);
  const p = fetch(
    `https://api.openstreetmap.org/api/0.6/user/${encodeURIComponent(key)}.json`,
    { headers: { Accept: "application/json" } }
  )
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => j?.user?.img?.href || null)
    .catch(() => null);
  state.osmAvatars.set(key, p);
  return p;
}

function applyAvatar(el, uid, fallbackText) {
  if (!el) return;
  fetchOsmAvatar(uid).then((url) => {
    if (!url) return;
    if (el.dataset.osmUid !== String(uid)) return;
    el.innerHTML = `<img src="${escapeHtml(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.parentNode.textContent=${JSON.stringify(fallbackText)}">`;
  });
}

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const fmt = new Intl.NumberFormat("en-US");
const fmtCompact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const compact = (n) => fmtCompact.format(n || 0);
// A number that reads human-friendly (2.3M) by default; clicking its tile flips it to the exact value.
const numHtml = (n) =>
  `<span class="num" data-full="${fmt.format(n || 0)}" data-compact="${compact(n)}">${compact(n)}</span>`;
const dtf = (opts) =>
  new Intl.DateTimeFormat(undefined, { ...opts, hour12: false, timeZone: TZ });
const dtfFull = dtf({
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const dtfShort = dtf({
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const dtfDate = dtf({ year: "numeric", month: "short", day: "2-digit" });
const dtfClock = dtf({ hour: "2-digit", minute: "2-digit", second: "2-digit" });

const escapeHtml = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
const refreshIcons = (root) =>
  window.lucide?.createIcons?.(
    root
      ? {
        attrs: { "stroke-width": 2 },
        nameAttr: "data-lucide",
        icons: window.lucide.icons,
      }
      : { attrs: { "stroke-width": 2 } }
  );
const isoUTC = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");
const nowUTC = () => new Date();

function tzOffsetLabel() {
  const m = -new Date().getTimezoneOffset(),
    s = m >= 0 ? "+" : "−",
    a = Math.abs(m);
  return `UTC${s}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}
function rangeWindow(k) {
  const end = nowUTC();
  if (k === "all") return { start: new Date(ALL_TIME_START), end };
  if (k === "custom")
    return {
      start: state.customStart || new Date(end - 86400000),
      end: state.customEnd || end,
    };
  return { start: new Date(end - (RANGE_HOURS[k] || 24) * 3600000), end };
}
function ago(d) {
  if (!d) return "never";
  const s = Math.max(0, Math.round((Date.now() - d) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = (s / 60) | 0;
  if (m < 60) return `${m}m ago`;
  const h = (m / 60) | 0;
  if (h < 24) return `${h}h ago`;
  return `${(h / 24) | 0}d ago`;
}
function avatarColor(name) {
  const s = name || "?";
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return ["#2D5F3F", "#3A6E4A", "#1F4D2E", "#4A7C5C", "#1F5C3D"][h % 5];
}
function initials(name) {
  if (!name) return "?";
  const p = name
    .replace(/[_\-]+/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  return (
    p.length === 1 ? p[0].slice(0, 2) : p[0][0] + p.at(-1)[0]
  ).toUpperCase();
}
function sumTagKey(ts, k) {
  const n = ts[k];
  if (!n) return { c: 0, m: 0, l: 0 };
  let c = 0, m = 0, l = 0;
  for (const v in n) {
    c += n[v].c;
    m += n[v].m;
    l += n[v].len || 0;
  }
  return { c, m, l };
}

function transform(row) {
  const ts = row.tag_stats || {};
  const b = sumTagKey(ts, "building"),
    h = sumTagKey(ts, "highway");
  const lu = sumTagKey(ts, "landuse"),
    wt = sumTagKey(ts, "waterway");
  const nt = sumTagKey(ts, "natural"),
    am = sumTagKey(ts, "amenity");
  return {
    uid: row.uid,
    username: row.name || `#${row.uid}`,
    hashtags: row.hashtags || [],
    editors: row.editors || [],
    rank: row.rank,
    changesets: row.changesets,
    map_changes: row.map_changes,
    nodes_created: row.nodes_created,
    nodes_modified: row.nodes_modified,
    nodes_deleted: row.nodes_deleted,
    ways_created: row.ways_created,
    ways_modified: row.ways_modified,
    ways_deleted: row.ways_deleted,
    rels_created: row.rels_created,
    rels_modified: row.rels_modified,
    rels_deleted: row.rels_deleted,
    pois_created: row.poi_created,
    pois_modified: row.poi_modified,
    buildings_created: b.c,
    buildings_modified: b.m,
    highways_created: h.c,
    highways_modified: h.m,
    highways_len: h.l,
    landuse_created: lu.c,
    landuse_modified: lu.m,
    waterways_created: wt.c,
    waterways_modified: wt.m,
    waterways_len: wt.l,
    natural_created: nt.c,
    natural_modified: nt.m,
    amenities_created: am.c,
    amenities_modified: am.m,
    created: row.nodes_created + row.ways_created + row.rels_created,
    modified: row.nodes_modified + row.ways_modified + row.rels_modified,
    deleted: row.nodes_deleted + row.ways_deleted + row.rels_deleted,
    tag_stats: ts,
  };
}

const hashtagInput = $("#hashtag-input"),
  chipsEl = $("#chips");
function renderChips() {
  chipsEl.innerHTML = state.hashtags
    .map(
      (h, i) =>
        `<span class="chip">#${escapeHtml(h)}<button type="button" data-i="${i}" aria-label="Remove ${escapeHtml(h)}"><i data-lucide="x"></i></button></span>`
    )
    .join("");
  chipsEl.querySelectorAll("button").forEach(
    (b) =>
    (b.onclick = () => {
      state.hashtags.splice(+b.dataset.i, 1);
      renderChips();
      apply();
    })
  );
  refreshIcons();
}
function addHashtag(raw) {
  const h = raw.trim().replace(/^#/, "").toLowerCase();
  if (!h || state.hashtags.includes(h)) return false;
  state.hashtags.push(h);
  renderChips();
  return true;
}

// Recent searches persisted in localStorage so a returning user can reload a prior query in one click.
// Newest first, unique by hashtag set, capped at RECENT_MAX.
const RECENT_KEY = "osmsg.recent.v1",
  RECENT_MAX = 5;
// Unique by the hashtag set alone (not the range), so the same hashtags searched over different ranges
// collapse to one recent entry keeping the latest range.
const recentKey = (e) => e.hashtags.join(",");
function loadRecent() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    const arr = Array.isArray(v) ? v.filter((e) => e && Array.isArray(e.hashtags) && e.hashtags.length) : [];
    const seen = new Set();
    return arr.filter((e) => {
      const k = recentKey(e);
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  } catch (err) {
    console.info("Recent searches unavailable:", err.message);
    return [];
  }
}
function saveRecentSearch() {
  if (!state.hashtags.length) return;
  const entry = { hashtags: [...state.hashtags], range: state.range };
  if (state.range === "custom" && state.customStart && state.customEnd) {
    entry.start = isoUTC(state.customStart);
    entry.end = isoUTC(state.customEnd);
  }
  const list = [entry, ...loadRecent().filter((e) => recentKey(e) !== recentKey(entry))].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch (err) {
    console.info("Could not save recent search:", err.message);
  }
  renderRecentSearches();
}
function applyRecentSearch(entry) {
  state.hashtags = [...new Set(entry.hashtags.map((h) => String(h).replace(/^#/, "").toLowerCase()))];
  if (entry.range === "custom" && entry.start && entry.end) {
    state.customStart = new Date(entry.start);
    state.customEnd = new Date(entry.end);
  } else {
    state.customStart = state.customEnd = null;
  }
  setRangePreset(entry.range);
  renderChips();
  // Only load the search into the form; the user presses Extract to run it (no auto-fire).
  apply();
}
function renderRecentSearches() {
  const box = $("#recent-searches");
  if (!box) return;
  const list = loadRecent();
  if (!list.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML =
    `<span class="rs-label">Recent</span>` +
    list
      .map(
        (e, i) =>
          `<button type="button" class="rs-chip" data-i="${i}" title="Re-run this search">${escapeHtml(e.hashtags.map((h) => "#" + h).join(", "))}</button>`
      )
      .join("");
  box.querySelectorAll(".rs-chip").forEach((b) => (b.onclick = () => applyRecentSearch(list[+b.dataset.i])));
}

// A related-hashtag click adds that tag to the current query and re-runs, so the user can drill into the
// combined set without retyping.
function addRelatedHashtag(tag) {
  if (addHashtag(tag)) apply();
}
hashtagInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === ",") {
    e.preventDefault();
    if (hashtagInput.value.trim() && addHashtag(hashtagInput.value)) {
      hashtagInput.value = "";
      apply();
    } else hashtagInput.value = "";
  } else if (
    e.key === "Backspace" &&
    !hashtagInput.value &&
    state.hashtags.length
  ) {
    state.hashtags.pop();
    renderChips();
    apply();
  }
});
const customRangePanel = $("#custom-range"),
  crRangeInput = $("#cr-range"),
  crChipText = $("#cr-chip-text"),
  crClearBtn = $("#cr-clear");

const CR_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtCrDate = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCDate()} ${CR_MON[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};
// Reflect the calendar's current selection in the chip (single source; no duplicate range input).
function updateCrChip() {
  const sel = crPicker?.selectedDates || [];
  if (sel.length === 2) {
    crChipText.textContent = `${fmtCrDate(utcInputToDate(sel[0]))} → ${fmtCrDate(utcInputToDate(sel[1]))} UTC`;
    crClearBtn.hidden = false;
  } else {
    crChipText.textContent = "Pick a start and end date below";
    crClearBtn.hidden = true;
  }
}

const dateToUtcInput = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};
const utcInputToDate = (d) =>
  new Date(
    Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), 0)
  );

let crPicker = null;
function initCustomRangePicker() {
  if (crPicker || typeof window.flatpickr !== "function") return;
  crPicker = window.flatpickr(crRangeInput, {
    mode: "range",
    enableTime: true,
    time_24hr: true,
    dateFormat: "Y-m-d H:i",
    minuteIncrement: 5,
    allowInput: false,
    disableMobile: true,
    inline: true,
    showMonths: window.matchMedia?.("(min-width: 700px)").matches ? 2 : 1,
    onChange: (dates) => {
      if (dates.length !== 2) {
        updateCrChip();
        return;
      }
      const s = utcInputToDate(dates[0]),
        e = utcInputToDate(dates[1]);
      if (s >= e)
        return toast({ msg: "Start must be before end", icon: "alert-triangle", err: true });
      state.customStart = s;
      state.customEnd = e;
      state.range = "custom";
      updateCrChip();
      apply();
    },
  });
}
crClearBtn?.addEventListener("click", () => {
  crPicker?.clear();
  state.customStart = state.customEnd = null;
  updateCrChip();
});

function setRangePreset(k) {
  $$(".preset button").forEach((b) =>
    b.setAttribute("aria-pressed", b.dataset.range === k ? "true" : "false")
  );
  state.range = k;
  customRangePanel.classList.toggle("show", k === "custom");
  if (k === "custom") {
    initCustomRangePicker();
    if (!state.customStart || !state.customEnd) {
      const end = nowUTC(),
        start = new Date(end - 86400000);
      crPicker?.setDate([dateToUtcInput(start), dateToUtcInput(end)], false);
    } else {
      crPicker?.setDate(
        [dateToUtcInput(state.customStart), dateToUtcInput(state.customEnd)],
        false
      );
    }
    updateCrChip();
  }
}
$$(".preset button").forEach(
  (b) =>
  (b.onclick = () => {
    const k = b.dataset.range;
    if (k === "custom") {
      // Custom toggles the picker: open it if closed, close it (keeping the recorded range) if open.
      if (state.range === "custom" && customRangePanel.classList.contains("show")) {
        customRangePanel.classList.remove("show");
      } else {
        setRangePreset("custom");
      }
      return;
    }
    setRangePreset(k);
    state.customStart = state.customEnd = null;
    apply();
  })
);

// Server sort names differ from the table's column keys in one spot.
const SERVER_SORT = { username: "name", map_changes: "map_changes", created: "created", modified: "modified", deleted: "deleted", changesets: "changesets" };
const LEADERBOARD_TIMEOUT_MS = 130_000;

function endpoint(name, params) {
  const base = `/api/v2/hashtag/${encodeURIComponent(state.hashtags.join(","))}/${name}`;
  const u = new URL(base, API_BASE);
  params.forEach((v, k) => u.searchParams.set(k, v));
  return u;
}
async function apiGet(name, params, signal) {
  const res = await fetch(endpoint(name, params), { headers: { accept: "application/json" }, mode: "cors", signal });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText || ""}`.trim());
  return res.json();
}
// Resolve the window once per query so all sections and the window bar share one [start, end); relative
// ranges like 30d must not each recompute "now".
function freezeWindow() {
  const { start, end } = rangeWindow(state.range);
  state.windowStart = start;
  state.windowEnd = end;
}
function windowParams() {
  if (!state.windowStart || !state.windowEnd) freezeWindow();
  const p = new URLSearchParams();
  p.set("start", isoUTC(state.windowStart));
  p.set("end", isoUTC(state.windowEnd));
  return p;
}

// Chips, range and search boxes only stage the query; nothing loads until the user hits Search.
function apply() {
  freezeWindow();
  writeURL();
  renderWindowBar();
}

$("#query-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (hashtagInput.value.trim()) {
    addHashtag(hashtagInput.value);
    hashtagInput.value = "";
  }
  // The picker only records the custom range; once extracted, hide it (the window bar shows the range,
  // click it to reopen).
  customRangePanel.classList.remove("show");
  runQuery();
});

// Reference-counted busy state for the Extract spinner. The button is never disabled, so a new query can
// be submitted anytime; runQuery aborts the in-flight one and restarts.
let _busyCount = 0;
// Toggle a class only, keeping the button's child nodes in place: a click whose mousedown landed on a
// replaced child would be dropped by the browser mid-load.
function setBusy(busy) {
  _busyCount = Math.max(0, _busyCount + (busy ? 1 : -1));
  const btn = $("#search-btn");
  if (!btn) return;
  const on = _busyCount > 0;
  btn.classList.toggle("is-loading", on);
  const label = btn.querySelector(".btn-label");
  if (label && !on) label.textContent = "Extract";
}

// The current loading phase, shown in the Extract button label while its spinner is up. Once the primary
// sections release the button, the secondary sections carry their own spinners, so this becomes a no-op.
function setStatus(msg) {
  const btn = $("#search-btn");
  if (!btn || !btn.classList.contains("is-loading")) return;
  const label = btn.querySelector(".btn-label");
  if (label && msg) label.textContent = msg;
}

// One Search loads each section SEQUENTIALLY (summary -> leaderboard -> tags -> trending -> editors) so
// the small backend is never hit by several heavy queries at once; each renders as it arrives.
async function runQuery() {
  state.page = 1;
  state.search = "";
  const searchBox = $("#search");
  if (searchBox) searchBox.value = "";
  state.sort = { key: "map_changes", dir: "desc" };
  freezeWindow();
  writeURL();
  renderWindowBar();
  fetchHealth();
  if (!state.hashtags.length) {
    showEmptyPrompt();
    return;
  }
  saveRecentSearch();
  state.query?.abort?.();
  const ctrl = new AbortController();
  state.query = ctrl;
  // Clear the previous query's results so nothing stale lingers while the new one loads.
  state.rows = [];
  state.batch = [];
  state.batchIndex = -1;
  state.podium = [];
  state.summary = null;
  state.tagRows = [];
  state.hashtagTrends = [];
  state.editorStats = null;
  state.total = 0;
  setOverviewLoading();
  $("#podium")?.closest("section")?.style.setProperty("display", "");
  $("#podium").innerHTML = "";
  if (typeof setChartsLoading === "function") setChartsLoading();
  const base = windowParams();
  const alive = () => state.query === ctrl;
  const param = (extra) => {
    const p = new URLSearchParams(base);
    for (const k in extra) p.set(k, extra[k]);
    return p;
  };
  setBusy(true);
  // Clear the button spinner once the primary content (summary + leaderboard) is up; secondary sections
  // keep loading behind their own inline status.
  let _released = false;
  const releasePrimary = () => { if (!_released) { _released = true; setBusy(false); } };
  try {
    setStatus("Fetching summary…");
    const summary = await apiGet("summary", param({}), ctrl.signal);
    if (!alive()) return;
    state.summary = summary;
    renderOverviewTotals();
    renderOverviewDetails();

    setStatus("Fetching leaderboard…");
    await loadLeaderboardPage(true);
    releasePrimary();
    if (!alive()) return;

    setStatus("Fetching related hashtags…");
    const trending = await apiGet("hashtags", param({ limit: "50" }), ctrl.signal);
    if (!alive()) return;
    // Related hashtags = the OTHER tags on the same changesets; drop the exact searched tag(s).
    const searched = new Set(state.hashtags.map((h) => String(h).replace(/^#/, "").toLowerCase()));
    state.hashtagTrends = (trending || []).filter(
      (t) => !searched.has(String(t.hashtag).replace(/^#/, "").toLowerCase())
    );
    if (typeof renderHashtagPieChart === "function") renderHashtagPieChart();

    setStatus("Fetching editors…");
    await fetchEditorStats();
    if (!alive()) return;

    setStatus("Fetching tag breakdown…");
    const tags = await apiGet("tags", param({ limit: "200" }), ctrl.signal);
    if (!alive()) return;
    state.tagRows = tags;
    renderOverviewDetails();
  } catch (err) {
    if (err?.name !== "AbortError") console.warn("OSMSG query failed:", err);
  } finally {
    releasePrimary();
    if (alive()) {
      const label = $("#search-btn .btn-label");
      if (label) label.textContent = "Extract";
      if (typeof renderHashtagPieChart === "function") renderHashtagPieChart();
      if (typeof renderEditorBarChart === "function") renderEditorBarChart();
    }
  }
}

// Set the visible page from the already-loaded batch (client-side; no API call).
function sliceBatchToRows() {
  const offset = (state.page - 1) * state.pageSize - state.batchIndex * state.batchSize;
  state.rows = state.batch.slice(offset, offset + state.pageSize);
}

// The leaderboard is fetched in BATCHES of `batchSize` (one server query, sorted/searched server-side);
// the UI then pages 10/20/50 WITHIN a batch client-side. Only crossing a batch boundary, or a new
// sort/search (`forceFetch`), hits the API. `setPodium` seeds the top-3 from the first batch.
async function loadLeaderboardPage(setPodium = false, forceFetch = false) {
  if (!state.hashtags.length) return;
  const startRow = (state.page - 1) * state.pageSize;
  const batchIndex = Math.floor(startRow / state.batchSize);
  // Serve from the loaded batch when possible.
  if (!forceFetch && !setPodium && batchIndex === state.batchIndex && state.batch.length) {
    sliceBatchToRows();
    renderTable();
    renderPagination();
    return;
  }
  showLoading();
  setBusy(true);
  state.lbInflight?.abort();
  const ctrl = new AbortController();
  state.lbInflight = ctrl;
  const timeout = setTimeout(() => ctrl.abort(), LEADERBOARD_TIMEOUT_MS);
  const p = windowParams();
  p.set("page", String(batchIndex + 1));
  p.set("page_size", String(state.batchSize));
  p.set("sort", SERVER_SORT[state.sort.key] || "map_changes");
  p.set("order", state.sort.dir);
  if (state.search.trim()) p.set("q", state.search.trim());
  try {
    const env = await apiGet("leaderboard", p, ctrl.signal);
    state.batch = (env.items || []).map(transform);
    state.batchIndex = batchIndex;
    state.total = env.total || 0;
    state.totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
    sliceBatchToRows();
    if (setPodium) {
      state.podium = state.batch.slice(0, 3);
      renderPodium();
    }
    renderTable();
    renderPagination();
    state.lastFetched = new Date();
    state.lastError = null;
    updateLastUpdated();
  } catch (err) {
    if (err?.name === "AbortError" && state.lbInflight !== ctrl) return;
    console.warn("OSMSG leaderboard fetch failed:", err);
    state.lastError = err;
    showError(err);
  } finally {
    clearTimeout(timeout);
    if (state.lbInflight === ctrl) state.lbInflight = null;
    setBusy(false);
  }
}

function onSectionError(section, err, ctrl) {
  if (err?.name === "AbortError" && state.query !== ctrl) return;
  console.warn(`OSMSG ${section} fetch failed:`, err);
}

let toastTimer;
function toast({ msg, icon = "info", err = false } = {}) {
  const t = $("#toast");
  t.innerHTML = `<i data-lucide="${icon}"></i>${escapeHtml(msg)}`;
  t.classList.toggle("err", !!err);
  t.classList.add("show");
  refreshIcons();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
}



function aggregateTagStats(rows) {
  const agg = {};
  for (const r of rows) {
    const ts = r.tag_stats;
    for (const key in ts) {
      const vals = ts[key];
      const a = (agg[key] ||= { values: {}, totalC: 0, totalM: 0, totalL: 0 });
      for (const v in vals) {
        const c = vals[v].c, m = vals[v].m, l = vals[v].len || 0;
        const slot = (a.values[v] ||= { c: 0, m: 0, l: 0 });
        slot.c += c;
        slot.m += m;
        slot.l += l;
        a.totalC += c;
        a.totalM += m;
        a.totalL += l;
      }
    }
  }
  return agg;
}
function tagBreakdownHtml(agg, { maxKeys = 10 } = {}) {
  const keys = Object.entries(agg)
    .filter(([, v]) => v.totalC + v.totalM > 0)
    .sort((a, b) => b[1].totalC + b[1].totalM - (a[1].totalC + a[1].totalM));
  if (!keys.length) return { html: "", keyCount: 0, valueCount: 0 };
  const valueCount = keys.reduce((s, [, v]) => s + Object.keys(v.values).length, 0);
  const pct = (n, t) => (t ? (n / t) * 100 : 0);
  const segDiv = (cls, w) => (w > 0 ? `<div class="${cls}" style="width:${w}%"></div>` : "");
  const cntC = (n) => (n ? `<span class="c">+${fmt.format(n)}</span>` : "");
  const cntM = (n) => (n ? `<span class="m">~${fmt.format(n)}</span>` : "");

  let html =
    `<div class="tag-legend">Feature counts: <span class="c">+ created</span> <span class="m">~ modified</span>` +
    ` · <span class="tag-key-len">km</span> = length of ways drawn</div>` +
    `<div class="tag-breakdown-grid">` +
    keys
      .slice(0, maxKeys)
      .map(([key, d]) => {
        const t = d.totalC + d.totalM;
        return `<div class="tag-key-card">
      <div class="tag-key-head">
        <span class="tag-key-name">${escapeHtml(key)}</span>
        <span class="tag-key-totals">${kmBadge(d.totalL)}${cntC(d.totalC)}${cntM(d.totalM)}</span>
      </div>
      <div class="tag-key-bar" title="${d.totalC} created · ${d.totalM} modified">
        ${segDiv("seg-c", pct(d.totalC, t))}${segDiv("seg-m", pct(d.totalM, t))}
      </div>
    </div>`;
      })
      .join("") +
    `</div>`;
  if (keys.length > maxKeys)
    html += `<div class="tag-key-more" style="margin-top:10px;text-align:center">+ ${fmt.format(keys.length - maxKeys)} more key${keys.length - maxKeys === 1 ? "" : "s"} not shown</div>`;
  return { html, keyCount: keys.length, valueCount };
}

const OV_CELLS_TOTALS = [
  ["Created", "created", "plus-square", "ov-add", "Elements created (nodes + ways + relations)"],
  ["Modified", "modified", "edit-3", "ov-mod", "Elements modified"],
  ["Deleted", "deleted", "trash-2", "ov-del", "Elements deleted"],
  ["Mappers", "mappers", "users", "", "Distinct contributors"],
  ["Changesets", "changesets", "git-commit-horizontal", "", "Number of changesets"],
];
const OV_CELLS = [
  ["Nodes", "nodes", "circle-dot", "elem", "Point features. + created  ~ modified  − deleted"],
  ["Ways", "ways", "spline", "elem", "Lines and areas. + created  ~ modified  − deleted"],
  ["Relations", "rels", "share-2", "elem", "Grouped features. + created  ~ modified  − deleted"],
  ["Buildings", "buildings", "building-2", "split", "building=* . + created  ~ modified"],
  ["Highways", "highways", "route", "split", "highway=* . + created  ~ modified"],
  ["POIs", "pois", "map-pin", "split", "Points of interest. + created  ~ modified"],
  ["Landuse", "landuse", "layers", "split", "landuse=* . + created  ~ modified"],
  ["Waterways", "waterways", "waves", "split", "waterway=* . + created  ~ modified"],
  ["Natural", "natural", "trees", "split", "natural=* . + created  ~ modified"],
  ["Amenities", "amenities", "coffee", "split", "amenity=* . + created  ~ modified"],
];
// Only line features carry a meaningful length; areas (buildings, landuse) and points (POIs) do not.
const LINEAR_CELLS = new Set(["highways", "waterways"]);
const renderOvCell =
  (data) =>
    ([l, k, ic, mod, desc]) => {
      const tip = escapeHtml(`${desc} · click to toggle exact numbers`);
      if (mod === "split") {
        const c = data[k] || 0, m = data[k + "_mod"] || 0;
        const isZero = !c && !m;
        const metres = data[k + "_len"] || 0;
        // Length is meaningful only for line features (highway/waterway); areas/points get no pill.
        const showKm = LINEAR_CELLS.has(k) && metres >= 100;
        const kmC = `${compact(metres / 1000)} km`, kmF = `${fmt.format(Math.round(metres / 1000))} km`;
        const pill = showKm
          ? `<span class="ov-len-pill" data-compact="${kmC}" data-full="${kmF}" title="${escapeHtml(l)}, length of ways created (created features only); click for the full number">${kmC}</span>`
          : "";
        return `<div class="ov-cell ov-split${isZero ? " is-zero" : ""}" title="${tip}">
      <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
      <div class="val"><span class="c" title="created">+${numHtml(c)}</span><span class="m" title="modified">~${numHtml(m)}</span>${pill}</div>
    </div>`;
      }
      if (mod === "elem") {
        const c = data[k + "_c"] || 0, m = data[k + "_m"] || 0, d = data[k + "_d"] || 0;
        const isZero = !c && !m && !d;
        return `<div class="ov-cell ov-elem${isZero ? " is-zero" : ""}" title="${tip}">
      <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
      <div class="val"><span class="c" title="created">+${numHtml(c)}</span><span class="m" title="modified">~${numHtml(m)}</span><span class="d" title="deleted">−${numHtml(d)}</span></div>
    </div>`;
      }
      return `<div class="ov-cell${mod ? " " + mod : ""}${data[k] ? "" : " is-zero"}" title="${tip}">
    <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
    <div class="val">${numHtml(data[k] || 0)}</div>
  </div>`;
    };
const ovCellsHtml = (data) => OV_CELLS.map(renderOvCell(data)).join("");
const ovTotalsHtml = (data) => OV_CELLS_TOTALS.map(renderOvCell(data)).join("");
// The /summary totals mapped to the overview's element cells; created/modified/deleted fold node+way+rel.
function summaryToData(s) {
  return {
    created: (s.nodes_created || 0) + (s.ways_created || 0) + (s.rels_created || 0),
    modified: (s.nodes_modified || 0) + (s.ways_modified || 0) + (s.rels_modified || 0),
    deleted: (s.nodes_deleted || 0) + (s.ways_deleted || 0) + (s.rels_deleted || 0),
    mappers: s.users || 0,
    changesets: s.changesets || 0,
    nodes_c: s.nodes_created || 0, nodes_m: s.nodes_modified || 0, nodes_d: s.nodes_deleted || 0,
    ways_c: s.ways_created || 0, ways_m: s.ways_modified || 0, ways_d: s.ways_deleted || 0,
    rels_c: s.rels_created || 0, rels_m: s.rels_modified || 0, rels_d: s.rels_deleted || 0,
    pois: s.poi_created || 0, pois_mod: s.poi_modified || 0,
  };
}
// The /tags rows folded to the overview's tag cells + the key breakdown grid.
const TAG_CELL_KEYS = ["building", "highway", "landuse", "waterway", "natural", "amenity"];
const TAG_CELL_FIELD = { building: "buildings", highway: "highways", landuse: "landuse", waterway: "waterways", natural: "natural", amenity: "amenities" };
function tagRowsToData(rows) {
  const out = {};
  for (const k of TAG_CELL_KEYS) { out[TAG_CELL_FIELD[k]] = 0; out[TAG_CELL_FIELD[k] + "_mod"] = 0; }
  for (const r of rows) {
    const f = TAG_CELL_FIELD[r.tag_key];
    if (!f) continue;
    out[f] += r.creates || 0;
    out[f + "_mod"] += r.modifies || 0;
    out[f + "_len"] = (out[f + "_len"] || 0) + (r.length_m || 0);
  }
  return out;
}
function tagRowsToAgg(rows) {
  const agg = {};
  for (const r of rows) {
    const a = (agg[r.tag_key] ||= { values: {}, totalC: 0, totalM: 0, totalL: 0 });
    const slot = (a.values[r.tag_value] ||= { c: 0, m: 0, l: 0 });
    slot.c += r.creates || 0; slot.m += r.modifies || 0; slot.l += r.length_m || 0;
    a.totalC += r.creates || 0; a.totalM += r.modifies || 0; a.totalL += r.length_m || 0;
  }
  return agg;
}

function kmText(metres) {
  if (!metres || metres < 100) return "";
  const km = metres / 1000;
  return (km >= 100 ? fmt.format(Math.round(km)) : km.toFixed(1)) + " km";
}
function kmBadge(metres) {
  const t = kmText(metres);
  return t ? `<span class="tag-key-len" title="${fmt.format(Math.round(metres))} m of open ways">${t}</span>` : "";
}

function setOverviewLoading() {
  $("#overview")?.closest("section")?.style.setProperty("display", "");
  const skel = Array.from({ length: 5 }, () => `<div class="ov-cell"><div class="skeleton" style="height:12px;width:60px"></div><div class="skeleton" style="height:20px;width:90px;margin-top:8px"></div></div>`).join("");
  $("#ov-strip-totals").innerHTML = skel;
  setDetailsOpen(false);
  $("#ov-toggle-btn").disabled = true;
  $("#ov-breakdown-meta").textContent = "";
}

function showEmptyPrompt() {
  $("#overview")?.closest("section")?.style.setProperty("display", "none");
  $("#podium")?.closest("section")?.style.setProperty("display", "none");
  $("#ov-details").hidden = true;
  $("#podium").innerHTML = "";
  $("#lb-body").innerHTML = `<tr><td colspan="8"><div class="empty"><i data-lucide="arrow-down-to-line"></i><h3>Extract a hashtag</h3><p>Type one or more hashtags above and press Extract.</p></div></td></tr>`;
  $("#pagination").hidden = true;
  refreshIcons();
}

function renderOverviewTotals() {
  if (!state.summary) return;
  $("#ov-strip-totals").innerHTML = ovTotalsHtml(summaryToData(state.summary));
  refreshIcons($("#ov-strip-totals"));
}

function renderOverviewDetails() {
  const data = { ...(state.summary ? summaryToData(state.summary) : {}), ...tagRowsToData(state.tagRows) };
  $("#ov-strip").innerHTML = ovCellsHtml(data);
  const btn = $("#ov-toggle-btn"), meta = $("#ov-breakdown-meta");
  const agg = tagRowsToAgg(state.tagRows);
  const { html, keyCount, valueCount } = tagBreakdownHtml(agg);
  if (keyCount) {
    $("#ov-breakdown").innerHTML = html;
    meta.textContent = `${fmt.format(keyCount)} tag key${keyCount === 1 ? "" : "s"} · ${fmt.format(valueCount)} value${valueCount === 1 ? "" : "s"} available`;
  } else {
    $("#ov-breakdown").innerHTML = `<div class="tag-stats-empty">No detailed tag stats reported in this window.</div>`;
    meta.textContent = "";
  }
  btn.disabled = false;
  refreshIcons($("#ov-details"));
}

function renderPodium() {
  const top3 = state.podium.slice(0, 3);
  const el = $("#podium");

  if (!top3.length) {
    el.innerHTML = `<div class="empty" style="grid-column:1/-1"><i data-lucide="users"></i><h3>No contributors yet</h3><p>Try a different time range or hashtag.</p></div>`;
    return refreshIcons(el);
  }

  el.innerHTML = "";

  for (let i = 0; i < 3; i++) {
    const r = top3[i];
    const place = i + 1;
    const div = document.createElement("div");
    div.className = `pod pod-${place} fade-in`;

    if (!r) {
      div.style.opacity = "0.4";
      div.innerHTML = `<span class="pod-rank">${place}</span><span class="pod-avatar">·</span><span class="pod-name">·</span><span class="pod-score-wrap"><span class="pod-score">0</span></span>`;
      el.appendChild(div);
      continue;
    }

    const created = (r.nodes_created || 0) + (r.ways_created || 0) + (r.rels_created || 0);
    const modified = (r.nodes_modified || 0) + (r.ways_modified || 0) + (r.rels_modified || 0);
    const deleted = (r.nodes_deleted || 0) + (r.ways_deleted || 0) + (r.rels_deleted || 0);

    // Editor info is shown only in the user modal, not on the podium.
    div.innerHTML = `
      <span class="pod-rank">${place}</span>
      <span class="pod-avatar" data-osm-uid="${r.uid}" style="background:${avatarColor(r.username)}">${initials(r.username)}</span>
      <span class="pod-name" title="${escapeHtml(r.username)}">${escapeHtml(r.username)}</span>
      <span class="pod-score-wrap">
        <div class="pod-score-line">
          <span class="pod-score" title="${fmt.format(r.map_changes)} map changes">${compact(r.map_changes)}</span>
          <span class="pod-cs" title="${fmt.format(r.changesets || 0)} changesets">
            <i data-lucide="git-commit-horizontal"></i>${compact(r.changesets || 0)}
          </span>
        </div>
        <div class="pod-score-label">changes · changesets</div>
      </span>
      <div class="pod-mini" aria-label="Created, modified, deleted">
        <span class="c" title="${fmt.format(created)} created"><i data-lucide="plus"></i>${compact(created)}</span>
        <span class="m" title="${fmt.format(modified)} modified"><i data-lucide="pencil"></i>${compact(modified)}</span>
        <span class="d" title="${fmt.format(deleted)} deleted"><i data-lucide="minus"></i>${compact(deleted)}</span>
      </div>`;

    applyAvatar(div.querySelector(".pod-avatar"), r.uid, initials(r.username));
    div.addEventListener("click", () => openUserModal(r.username));
    div.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUserModal(r.username); }
    });
    el.appendChild(div);
  }
  refreshIcons(el);
}

function shortEditor(s) {
  if (!s) return "Unknown";
  const iD = s.match(/iD\s*([\d.]+)/i); if (iD) return "iD " + iD[1];
  const josm = s.match(/JOSM\/([\d.]+)/i); if (josm) return "JOSM " + josm[1];
  const rapid = s.match(/Rapid\s*([\d.]+)/i); if (rapid) return "Rapid " + rapid[1];
  if (/Vespucci/i.test(s)) return "Vespucci";
  if (/StreetComplete/i.test(s)) return "StreetComplete";
  if (/OsmAnd/i.test(s)) return "OsmAnd";
  return s.length > 22 ? s.slice(0, 20) + "…" : s;
}
function editorFamily(s) {
  if (!s) return null;
  if (/iD/i.test(s)) return "iD";
  if (/JOSM/i.test(s)) return "JOSM";
  if (/Rapid/i.test(s)) return "Rapid";
  if (/Vespucci/i.test(s)) return "Vespucci";
  if (/StreetComplete/i.test(s)) return "StreetComplete";
  return null;
}

// Group a user's editors by family for a compact profile line: "iD (2.41.2, 2.34.0), JOSM 1.5".
function groupEditorsText(editors) {
  const fams = new Map();
  for (const e of editors) {
    const fam = editorFamily(e) || String(e || "").split(/[/\s]/)[0] || "Unknown";
    const short = shortEditor(e);
    const ver = short.toLowerCase().startsWith(fam.toLowerCase()) ? short.slice(fam.length).trim() : short;
    if (!fams.has(fam)) fams.set(fam, []);
    if (ver && !fams.get(fam).includes(ver)) fams.get(fam).push(ver);
  }
  return [...fams.entries()]
    .map(([fam, vers]) =>
      vers.length === 0 ? fam : vers.length === 1 ? `${fam} ${vers[0]}` : `${fam} (${vers.join(", ")})`
    )
    .join(", ");
}
function editorColor(family) {
  const map = {
    iD: ["#E6F1FB", "#185FA5"],
    JOSM: ["#FAEEDA", "#854F0B"],
    Rapid: ["#EEEDFE", "#534AB7"],
    Vespucci: ["#EAF3DE", "#3B6D11"],
    StreetComplete: ["#FAECE7", "#993C1D"],
  };
  return map[family] || ["#F1EFE8", "#5F5E5A"];
}

const USER_TOTAL_CELLS = [
  ["Created", "created", "plus-square", "ov-add"],
  ["Modified", "modified", "edit-3", "ov-mod"],
  ["Deleted", "deleted", "trash-2", "ov-del"],
  ["Changesets", "changesets", "git-commit-horizontal", ""],
  ["Buildings", "buildings", "building-2", "split"],
  ["Highways", "highways", "route", "split"],
  ["POIs", "pois", "map-pin", "split"],
  ["Landuse", "landuse", "layers", "split"],
  ["Waterways", "waterways", "waves", "split"],
  ["Natural", "natural", "trees", "split"],
  ["Amenities", "amenities", "coffee", "split"],
];
const USER_ELEM_GROUPS = [
  ["Nodes", "circle-dot", "nodes_created", "nodes_modified", "nodes_deleted"],
  ["Ways", "spline", "ways_created", "ways_modified", "ways_deleted"],
  ["Relations", "share-2", "rels_created", "rels_modified", "rels_deleted"],
];
const elemCellsHtml = (r) =>
  USER_ELEM_GROUPS.map(([l, ic, ck, mk, dk]) => {
    const c = r[ck] || 0, m = r[mk] || 0, d = r[dk] || 0;
    const isZero = !c && !m && !d;
    return `<div class="ov-cell ov-elem${isZero ? " is-zero" : ""}">
    <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
    <div class="val">
      <span class="c" title="created">+${numHtml(c)}</span>
      <span class="m" title="modified">~${numHtml(m)}</span>
      <span class="d" title="deleted">−${numHtml(d)}</span>
    </div>
  </div>`;
  }).join("");

const SPLIT_KEY_MAP = {
  buildings: ["buildings_created", "buildings_modified"],
  highways: ["highways_created", "highways_modified"],
  pois: ["pois_created", "pois_modified"],
  landuse: ["landuse_created", "landuse_modified"],
  waterways: ["waterways_created", "waterways_modified"],
  natural: ["natural_created", "natural_modified"],
  amenities: ["amenities_created", "amenities_modified"],
};
const cellsHtml = (cells, r) =>
  cells
    .map(([l, k, ic, mod]) => {
      if (mod === "split") {
        const [ck, mk] = SPLIT_KEY_MAP[k];
        const c = r[ck] || 0, m = r[mk] || 0;
        const isZero = !c && !m;
        const metres = LINEAR_CELLS.has(k) ? (r[k + "_len"] || 0) : 0;
        const kmC = `${compact(metres / 1000)} km`, kmF = `${fmt.format(Math.round(metres / 1000))} km`;
        const kmPill = metres >= 100
          ? `<span class="ov-len-pill" data-compact="${kmC}" data-full="${kmF}" title="${kmF}, length of ways created (created features only); click for the full number">${kmC}</span>`
          : "";
        return `<div class="ov-cell ov-split${isZero ? " is-zero" : ""}">
      <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
      <div class="val"><span class="c">+${numHtml(c)}</span><span class="m">~${numHtml(m)}</span>${kmPill}</div>
    </div>`;
      }
      return `<div class="ov-cell${mod ? " " + mod : ""}${r[k] ? "" : " is-zero"}">
    <div class="lbl"><i data-lucide="${ic}"></i>${l}</div>
    <div class="val">${numHtml(r[k] || 0)}</div>
  </div>`;
    })
    .join("");

function openUserModal(username) {
  const r = state.rows.find((x) => x.username === username);
  if (!r) return;
  const modal = $("#user-modal");

  $("#user-modal-name").innerHTML = `
    <a href="https://www.openstreetmap.org/user/${encodeURIComponent(r.username)}"
       target="_blank" rel="noopener">${escapeHtml(r.username)}</a>`;


  const subEl = $("#user-modal-sub");
  subEl.textContent = `rank #${state.rows.findIndex((x) => x.username === username) + 1} · ${fmt.format(r.map_changes)} map changes · ${fmt.format(r.changesets)} changesets`;

  const av = $("#user-modal-avatar");
  av.style.background = avatarColor(r.username);
  av.textContent = initials(r.username);
  av.dataset.osmUid = String(r.uid);
  applyAvatar(av, r.uid, initials(r.username));

  const userHashtags = (r.hashtags || [])
    .filter(Boolean)
    .map((h) => "#" + String(h).replace(/^#/, ""));
  const hashtagLine = userHashtags.length
    ? `<div class="modal-hashtags">${userHashtags.map((h) => `<span class="mh-chip">${escapeHtml(h)}</span>`).join("")}</div>`
    : "";
  const modalEditors = (r.editors || []).filter(Boolean);
  const editorText = modalEditors.length ? groupEditorsText(modalEditors) : "Unknown";
  const editorLine = `<div class="modal-editor"><i data-lucide="pen-tool"></i> <span title="${escapeHtml(modalEditors.join(", "))}">${escapeHtml(editorText)}</span></div>`;

  const { html: tagHtml, keyCount, valueCount } = tagBreakdownHtml(aggregateTagStats([r]), { maxKeys: 24, maxVals: 8 });
  let html = `<div class="modal-meta">${hashtagLine}${editorLine}</div>`;
  html += `<div class="overview-strip">${cellsHtml(USER_TOTAL_CELLS, r)}</div>`;
  html += `<div class="overview-strip" style="margin-top:6px">${elemCellsHtml(r)}</div>`;

  if (keyCount) {
    html += `
      <div class="ov-toggle" style="margin-top:10px">
        <span class="ov-breakdown-meta"><i data-lucide="tags"></i> Detailed tag contributions · ${fmt.format(keyCount)} key${keyCount === 1 ? "" : "s"}</span>
        <button type="button" class="ov-toggle-btn" id="modal-tag-toggle" aria-expanded="false" aria-controls="modal-tag-details">
          <span id="modal-tag-label">Show details</span><span class="ov-caret" aria-hidden="true">▾</span>
        </button>
      </div>
      <div class="ov-breakdown" id="modal-tag-details" hidden style="margin-top:10px">${tagHtml}</div>`;
  } else {
    html += `<div class="tag-stats-empty" style="margin-top:14px">No detailed tag stats reported for this contributor in this window.</div>`;
  }

  $("#user-modal-body").innerHTML = html;
  const mtToggle = $("#modal-tag-toggle");
  if (mtToggle) {
    mtToggle.addEventListener("click", () => {
      const open = mtToggle.getAttribute("aria-expanded") !== "true";
      mtToggle.setAttribute("aria-expanded", open ? "true" : "false");
      $("#modal-tag-details").hidden = !open;
      $("#modal-tag-label").textContent = open ? "Hide details" : "Show details";
      const c = mtToggle.querySelector(".ov-caret");
      if (c) c.textContent = open ? "▴" : "▾";
    });
  }
  modal.hidden = false;
  modal.classList.add("open");
  document.body.style.overflow = "hidden";
  refreshIcons(modal);
  $("#user-modal-close").focus();
}

function closeUserModal() {
  const m = $("#user-modal");
  m.hidden = true;
  m.classList.remove("open");
  document.body.style.overflow = "";
}

function renderTable() {
  const tb = $("#lb-body");
  if (!state.rows.length) {
    tb.innerHTML = `<tr><td colspan="8"><div class="empty"><i data-lucide="search-x"></i><h3>Nothing to show</h3><p>${state.search ? "No contributor matches your search." : "No data for this time range and hashtag combination yet."}</p></div></td></tr>`;
    refreshIcons(tb);
    renderPagination();
    return;
  }
  $$("th.sortable").forEach((th) => {
    const k = th.dataset.sort, arrow = th.querySelector(".arrow");
    if (k === state.sort.key) {
      th.setAttribute("aria-sort", state.sort.dir === "asc" ? "ascending" : "descending");
      arrow.setAttribute("data-lucide", state.sort.dir === "asc" ? "arrow-up" : "arrow-down");
    } else {
      th.removeAttribute("aria-sort");
      arrow.setAttribute("data-lucide", "chevrons-up-down");
    }
  });
  tb.innerHTML = state.rows
    .map((r) => {
      const rank = r.rank;
      const t = Math.max(1, r.map_changes);
      const cP = (r.created / t) * 100, mP = (r.modified / t) * 100, dP = (r.deleted / t) * 100;
      return `<tr data-user="${escapeHtml(r.username)}" class="lb-row" tabindex="0" role="button" aria-label="View ${escapeHtml(r.username)} contributions">
      <td class="col-rank">${rank}</td>
      <td class="col-user"><div class="user-cell">
        <span class="avatar" style="background:${avatarColor(r.username)}">${initials(r.username)}</span>
        <a class="username" href="https://www.openstreetmap.org/user/${encodeURIComponent(r.username)}" target="_blank" rel="noopener" title="${escapeHtml(r.username)}" onclick="event.stopPropagation()">${escapeHtml(r.username)}</a><i data-lucide="external-link" class="ext-link"></i>
      </div></td>
      <td class="col-num primary">${fmt.format(r.map_changes)}</td>
      <td class="col-num col-c${r.created ? "" : " is-zero"}">${fmt.format(r.created)}</td>
      <td class="col-num col-m${r.modified ? "" : " is-zero"}">${fmt.format(r.modified)}</td>
      <td class="col-num col-d${r.deleted ? "" : " is-zero"}">${fmt.format(r.deleted)}</td>
      <td class="col-num col-cs">${fmt.format(r.changesets)}</td>
      <td class="col-spark"><div class="stack-bar" title="${r.created} created · ${r.modified} modified · ${r.deleted} deleted">
        <div class="seg-c" style="width:${cP}%"></div><div class="seg-m" style="width:${mP}%"></div><div class="seg-d" style="width:${dP}%"></div>
      </div></td>
    </tr>`;
    })
    .join("");
  refreshIcons(tb);
  tb.querySelectorAll(".lb-row").forEach((tr) => {
    tr.addEventListener("click", () => openUserModal(tr.dataset.user));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUserModal(tr.dataset.user); }
    });
  });
  renderPagination();
}

function renderPagination() {
  const wrap = $("#pagination"), info = $("#pg-info"), ctrls = $("#pg-controls");
  const total = state.total;
  if (!total) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const totalPages = state.totalPages, cur = state.page;
  const from = (cur - 1) * state.pageSize + 1, to = Math.min(total, cur * state.pageSize);
  info.innerHTML = `Showing <b>${fmt.format(from)}</b>–<b>${fmt.format(to)}</b> of <b>${fmt.format(total)}</b>`;
  const pages = [1];
  if (cur - 1 > 2) pages.push("…");
  for (let p = Math.max(2, cur - 1); p <= Math.min(totalPages - 1, cur + 1); p++) pages.push(p);
  if (cur + 1 < totalPages - 1) pages.push("…");
  if (totalPages > 1) pages.push(totalPages);
  const btn = (lab, p, { dis = false, active = false } = {}) =>
    `<button class="pg-btn${active ? " active" : ""}" data-page="${p}"${dis ? " disabled" : ""}${active ? ' aria-current="page"' : ""}>${lab}</button>`;
  ctrls.innerHTML =
    btn(`<i data-lucide="chevron-left"></i>`, cur - 1, { dis: cur <= 1 }) +
    pages.map((p) => p === "…" ? `<span class="pg-ellipsis">…</span>` : btn(String(p), p, { active: p === cur })).join("") +
    btn(`<i data-lucide="chevron-right"></i>`, cur + 1, { dis: cur >= totalPages });
  refreshIcons(ctrls);
  ctrls.querySelectorAll(".pg-btn").forEach(
    (b) => (b.onclick = () => {
      if (b.disabled) return;
      const p = parseInt(b.dataset.page, 10);
      if (!isFinite(p)) return;
      state.page = p;
      writeURL();
      loadLeaderboardPage();
      document.querySelector(".table-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
    })
  );
}

$("#pg-size").addEventListener("change", (e) => {
  state.pageSize = parseInt(e.target.value, 10) || 10;
  state.page = 1;
  writeURL();
  loadLeaderboardPage();
});
let searchTimer;
$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  state.page = 1;
  clearTimeout(searchTimer);
  // Search is server-side across all users -> refetch a fresh batch.
  searchTimer = setTimeout(() => loadLeaderboardPage(false, true), 350);
});
$$("th.sortable").forEach(
  (th) => (th.onclick = () => {
    if (!state.hashtags.length) return;
    const k = th.dataset.sort;
    if (state.sort.key === k)
      state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
    else { state.sort.key = k; state.sort.dir = k === "username" ? "asc" : "desc"; }
    state.page = 1;
    writeURL();
    // Sorting is server-side across all users -> refetch a fresh batch.
    loadLeaderboardPage(false, true);
  })
);

// Overview tiles read compact (2.3M) by default; clicking a tile flips its numbers to the exact value.
$("#overview").addEventListener("click", (e) => {
  const pill = e.target.closest(".ov-len-pill");
  if (pill) {
    // Clicking the length pill toggles compact km <-> the full number, without flipping the tile's counts.
    const full = pill.classList.toggle("full");
    pill.textContent = full ? pill.dataset.full : pill.dataset.compact;
    return;
  }
  const cell = e.target.closest(".ov-cell");
  if (!cell) return;
  const raw = cell.classList.toggle("raw");
  cell.querySelectorAll(".num").forEach((s) => { s.textContent = raw ? s.dataset.full : s.dataset.compact; });
});

// Same compact <-> exact toggle inside the profile modal, so the tiles behave like the main page.
$("#user-modal-body").addEventListener("click", (e) => {
  const pill = e.target.closest(".ov-len-pill");
  if (pill) {
    const full = pill.classList.toggle("full");
    pill.textContent = full ? pill.dataset.full : pill.dataset.compact;
    return;
  }
  const cell = e.target.closest(".ov-cell");
  if (!cell) return;
  const raw = cell.classList.toggle("raw");
  cell.querySelectorAll(".num").forEach((s) => { s.textContent = raw ? s.dataset.full : s.dataset.compact; });
});

const EXPORT_COLS = [
  "rank", "uid", "username", "map_changes", "created", "modified", "deleted", "changesets",
  "nodes_created", "nodes_modified", "nodes_deleted",
  "ways_created", "ways_modified", "ways_deleted",
  "rels_created", "rels_modified", "rels_deleted",
  "pois_created", "pois_modified",
  "buildings_created", "buildings_modified",
  "highways_created", "highways_modified", "highways_km",
  "landuse_created", "landuse_modified",
  "waterways_created", "waterways_modified", "waterways_km",
  "natural_created", "natural_modified",
  "amenities_created", "amenities_modified",
];

function buildCsv(rows) {
  const km = (metres) => Math.round((metres || 0) / 100) / 10;
  const sorted = rows.slice().sort((a, b) => b.map_changes - a.map_changes);
  const lines = [EXPORT_COLS.join(",")];
  sorted.forEach((r, i) => {
    const row = { ...r, rank: i + 1, highways_km: km(r.highways_len), waterways_km: km(r.waterways_len) };
    lines.push(EXPORT_COLS.map((c) => {
      const v = row[c];
      const s = v === undefined || v === null ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(","));
  });
  return lines.join("\n");
}

// The leaderboard is server-paginated (100/page max), so export pages through every contributor rather
// than only the rows currently on screen, reporting progress as each page lands.
async function fetchAllLeaderboardRows(onProgress, signal) {
  const SIZE = 100;
  const params = (page) => {
    const p = windowParams();
    p.set("page", String(page));
    p.set("page_size", String(SIZE));
    p.set("sort", SERVER_SORT[state.sort.key] || "map_changes");
    p.set("order", state.sort.dir);
    if (state.search.trim()) p.set("q", state.search.trim());
    return p;
  };
  const first = await apiGet("leaderboard", params(1), signal);
  const totalPages = Math.max(1, Math.ceil((first.total || 0) / SIZE));
  let rows = (first.items || []).map(transform);
  onProgress(1, totalPages);
  for (let page = 2; page <= totalPages; page++) {
    const env = await apiGet("leaderboard", params(page), signal);
    rows = rows.concat((env.items || []).map(transform));
    onProgress(page, totalPages);
  }
  return rows;
}

function setExportProgress(done, total) {
  const box = $("#export-progress");
  if (!box) return;
  box.hidden = false;
  const pct = total ? Math.round((done / total) * 100) : 0;
  $("#export-progress-fill").style.width = `${pct}%`;
  $("#export-progress-count").textContent = `${done} / ${total} pages`;
}

let _exporting = false;
$("#export-btn").addEventListener("click", async () => {
  if (_exporting) return;
  if (!state.hashtags.length || !state.total)
    return toast({ msg: "Nothing to export", icon: "alert-triangle", err: true });
  _exporting = true;
  const btn = $("#export-btn");
  btn.disabled = true;
  const ctrl = new AbortController();
  setExportProgress(0, Math.max(1, Math.ceil(state.total / 100)));
  try {
    const rows = await fetchAllLeaderboardRows(setExportProgress, ctrl.signal);
    const tag = state.hashtags.length ? state.hashtags.join("-") : "all";
    const blob = new Blob([buildCsv(rows)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `osmsg-leaderboard-${tag}-${state.range}-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    toast({ msg: `CSV downloaded (${rows.length} contributors)`, icon: "download" });
  } catch (err) {
    toast({ msg: `Export failed: ${err.message}`, icon: "alert-triangle", err: true });
  } finally {
    _exporting = false;
    btn.disabled = false;
    $("#export-progress").hidden = true;
  }
});

function showLoading() {
  $("#lb-body").innerHTML = Array.from(
    { length: 6 },
    () => `<tr>
    <td class="col-rank"><div class="skeleton" style="height:14px;width:24px"></div></td>
    <td><div class="user-cell"><div class="skeleton" style="width:28px;height:28px;border-radius:50%"></div><div class="skeleton" style="height:12px;width:120px"></div></div></td>
    <td><div class="skeleton" style="height:12px;width:50px;margin-left:auto"></div></td>
    <td><div class="skeleton" style="height:12px;width:40px;margin-left:auto"></div></td>
    <td><div class="skeleton" style="height:12px;width:40px;margin-left:auto"></div></td>
    <td><div class="skeleton" style="height:12px;width:30px;margin-left:auto"></div></td>
    <td class="col-cs"><div class="skeleton" style="height:12px;width:40px;margin-left:auto"></div></td>
    <td class="col-spark"><div class="skeleton" style="height:8px;width:120px"></div></td>
  </tr>`
  ).join("");
  $("#pagination").hidden = true;
}
function showError(err) {
  const tb = $("#lb-body");
  const msg = err?.message || "Network error";
  const isAbort = err?.name === "AbortError";
  tb.innerHTML = `<tr><td colspan="8"><div class="errbox">
    <i data-lucide="cloud-off"></i>
    <h3>${isAbort ? "Request timed out" : "Couldn't reach the OSMSG API"}</h3>
    <p style="margin-top:8px"><code style="font-family:var(--mono);font-size:12px;background:#F4F0E6;padding:2px 6px;border-radius:4px;color:#3A4744">${escapeHtml(msg)}</code></p>
    <p style="margin-top:14px;color:#717D78">If this is a CORS error and you're hosting this page off the API origin, the API needs to allow your origin. Hit Search to try again.</p>
    <p style="margin-top:18px"><a href="${API_BASE}/docs/swagger" target="_blank" rel="noopener">Open the API docs <i data-lucide="external-link" class="ico-sm" style="vertical-align:-2px"></i></a></p>
  </div></td></tr>`;
  $("#pagination").hidden = true;
  refreshIcons(tb);
}

function updateLastUpdated() {
  const txt = $("#last-updated-text");
  const chip = $("#last-updated");
  const h = state.health;
  if (h?.last_ts) {
    const t = dtfClock.format(h.last_ts);
    txt.innerHTML = `Server ${ago(h.last_ts)} \u00b7 <time datetime="${h.last_ts.toISOString()}">${t}</time>`;
    const lines = [
      `OSM diff timestamp (last_ts): ${h.last_ts.toISOString()}`,
      h.updated_at ? `Server processed at: ${h.updated_at.toISOString()}` : null,
      h.last_seq != null ? `Sequence: ${h.last_seq}` : null,
      state.lastFetched ? `Browser last refresh: ${dtfClock.format(state.lastFetched)}` : null,
    ].filter(Boolean);
    if (chip) chip.title = lines.join("\n");
  } else if (state.lastFetched) {
    const t = dtfClock.format(state.lastFetched);
    txt.innerHTML = `Updated ${ago(state.lastFetched)} \u00b7 <time datetime="${state.lastFetched.toISOString()}">${t}</time>`;
    if (chip) chip.title = "";
  } else {
    txt.textContent = "never";
    if (chip) chip.title = "";
  }
}

async function fetchHealth() {
  try {
    const res = await fetch(new URL(HEALTH_ENDPOINT, API_BASE), {
      headers: { accept: "application/json" },
      mode: "cors",
    });
    if (!res.ok) return;
    const j = await res.json();
    state.health = {
      status: j.status ?? null,
      last_seq: j.last_seq ?? null,
      last_ts: j.last_ts ? new Date(j.last_ts) : null,
      updated_at: j.updated_at ? new Date(j.updated_at) : null,
    };
    updateLastUpdated();
  } catch (err) {
    console.warn("OSMSG health fetch failed:", err);
  }
}

function renderEditorStats() {
  // charts.js is a separate deferred script; guard so an early call (during boot) can't throw.
  if (typeof renderEditorBarChart === "function") renderEditorBarChart();
}

async function fetchEditorStats() {
  if (!state.hashtags.length) { state.editorStats = null; renderEditorStats(); return; }
  try {
    const editors = await apiGet("editors", windowParams(), state.query?.signal);
    const all = (editors || [])
      .slice()
      .sort((a, b) => (b.map_changes || 0) - (a.map_changes || 0))
      .map((e) => ({
        editor: e.editor || "Unknown",
        changes: e.map_changes || 0,
        users: e.users || 0,
        changesets: e.changesets || 0,
      }));
    state.editorStats = { totalEditors: all.length, all, top5: all.slice(0, 5) };
    renderEditorStats();
  } catch (err) {
    console.warn("Editor stats fetch failed:", err);
  }
}

function renderWindowBar() {
  const { start, end } =
    state.windowStart && state.windowEnd
      ? { start: state.windowStart, end: state.windowEnd }
      : rangeWindow(state.range);
  const useDate = state.range === "all" || end - start > 60 * 86400 * 1000;
  const f = useDate ? dtfDate : dtfShort;
  $("#wb-window-text").textContent = `${f.format(start)} → ${f.format(end)}`;
  $("#wb-window").classList.add("clickable");
  $("#wb-window").title = `Click to edit this time range\nUTC: ${start.toISOString()} → ${end.toISOString()}`;
  $("#wb-localtime").textContent = dtfClock.format(new Date());
  $("#wb-tzname").textContent = `${TZ} · ${tzOffsetLabel()}`;
}

function readURL() {
  const p = new URLSearchParams(location.search);
  const r = p.get("range");
  if (r && (RANGE_HOURS[r] || r === "all" || r === "custom")) {
    state.range = r;
    setRangePreset(r);
  }
  if (r === "custom") {
    const s = p.get("start"), e = p.get("end");
    if (s && e) {
      const sd = new Date(s), ed = new Date(e);
      if (!isNaN(sd) && !isNaN(ed)) {
        state.customStart = sd;
        state.customEnd = ed;
      }
    }
  }
  const tags = p.getAll("hashtag").concat(p.getAll("hashtags"));
  if (tags.length)
    state.hashtags = [...new Set(tags.map((t) => t.replace(/^#/, "").toLowerCase()))];
  const ps = parseInt(p.get("size") || "", 10);
  if ([10, 25, 50, 100].includes(ps)) {
    state.pageSize = ps;
    $("#pg-size").value = String(ps);
  }
}
function writeURL() {
  const p = new URLSearchParams();
  p.set("range", state.range);
  if (state.range === "custom" && state.customStart && state.customEnd) {
    p.set("start", isoUTC(state.customStart));
    p.set("end", isoUTC(state.customEnd));
  }
  state.hashtags.forEach((h) => p.append("hashtag", h));
  if (state.pageSize !== 25) p.set("size", String(state.pageSize));
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

if ("serviceWorker" in navigator && location.protocol !== "file:") {
  navigator.serviceWorker
    .register("sw.js", { scope: "./" })
    .catch((err) => console.info("Service worker not registered:", err.message));
}

function setDetailsOpen(open) {
  const btn = $("#ov-toggle-btn");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  $("#ov-details").hidden = !open;
  $("#ov-toggle-label").textContent = open ? "Hide details" : "Show details";
  const caret = btn.querySelector(".ov-caret");
  if (caret) caret.textContent = open ? "▴" : "▾";
}
$("#ov-toggle-btn").addEventListener("click", () => {
  const btn = $("#ov-toggle-btn");
  if (btn.disabled) return;
  setDetailsOpen(btn.getAttribute("aria-expanded") !== "true");
});

// The "Updated" pill doubles as a manual refresh: re-run the active query, or just re-check health
// when nothing is loaded yet.
function refreshStats() {
  if (state.hashtags.length) runQuery();
  else fetchHealth();
}
$("#last-updated")?.addEventListener("click", refreshStats);
$("#last-updated")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    refreshStats();
  }
});

// Clicking the window pill converts the active window (any preset) into an editable custom range and
// opens the same date picker the Custom button uses.
$("#wb-window")?.addEventListener("click", () => {
  const { start, end } =
    state.windowStart && state.windowEnd
      ? { start: state.windowStart, end: state.windowEnd }
      : rangeWindow(state.range);
  state.customStart = start;
  state.customEnd = end;
  setRangePreset("custom");
  customRangePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

const userModal = $("#user-modal");
$("#user-modal-close").addEventListener("click", closeUserModal);
userModal.addEventListener("click", (e) => {
  if (e.target === userModal) closeUserModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && userModal.classList.contains("open")) closeUserModal();
});

// Methodology modal: footer link + shareable #methodology deep link.
const mthModal = $("#methodology-modal");
function openMethodology() {
  if (!mthModal) return;
  mthModal.hidden = false;
  mthModal.classList.add("open");
  document.body.style.overflow = "hidden";
  refreshIcons(mthModal);
  $("#methodology-close")?.focus();
  if (location.hash !== "#methodology") history.replaceState(null, "", "#methodology");
}
function closeMethodology() {
  if (!mthModal) return;
  mthModal.hidden = true;
  mthModal.classList.remove("open");
  document.body.style.overflow = "";
  if (location.hash === "#methodology") history.replaceState(null, "", location.pathname + location.search);
}
$("#methodology-link")?.addEventListener("click", (e) => { e.preventDefault(); openMethodology(); });
$("#methodology-close")?.addEventListener("click", closeMethodology);
mthModal?.addEventListener("click", (e) => { if (e.target === mthModal) closeMethodology(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && mthModal?.classList.contains("open")) closeMethodology(); });
window.addEventListener("hashchange", () => { if (location.hash === "#methodology") openMethodology(); });
if (location.hash === "#methodology") openMethodology();

function boot() {
  const swaggerURL = `${API_BASE}/docs/swagger`;
  const apiLink = $("#api-link");
  if (apiLink) {
    apiLink.href = swaggerURL;
    const host = $("#api-host");
    if (host) host.textContent = new URL(API_BASE).host;
  }
  const apiDocsLink = $("#api-docs-link");
  if (apiDocsLink) apiDocsLink.href = swaggerURL;
  readURL();
  renderChips();
  renderRecentSearches();
  renderWindowBar();
  refreshIcons();
  fetchHealth();
  // Submit-driven only: a page load never runs a query. The URL pre-fills the hashtags and range;
  // the user presses Extract to trigger it, so a shared or bookmarked link cannot flood the API.
  showEmptyPrompt();
  state.agoTimer = setInterval(updateLastUpdated, 5000);
  state.clockTimer = setInterval(() => {
    $("#wb-localtime").textContent = dtfClock.format(new Date());
  }, 1000);
  if (state.range === "custom") {
    const tryInit = () => {
      if (typeof window.flatpickr === "function") setRangePreset("custom");
      else setTimeout(tryInit, 50);
    };
    tryInit();
  }
}
if (document.readyState !== "loading") boot();
else window.addEventListener("DOMContentLoaded", boot);
