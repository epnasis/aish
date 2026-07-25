/* aish web client: one WebSocket, dumb rendering.
 *
 * The server owns all state; on every (re)connect it sends hello + a full
 * transcript replay and this client just clears the DOM and re-renders.
 * Approval cards are keyed by request id so a later approval_resolved (live
 * or replayed) collapses them. Assistant answers render as markdown; command
 * output renders ANSI SGR colors. All text lands via textContent /
 * createTextNode — model output never reaches innerHTML. The one exception
 * is highlightFences(): hljs.highlight() escapes the source it's given
 * before wrapping tokens in spans, so it is the model's raw string, escaped
 * by hljs and never by us, that lands in innerHTML — see the comment there.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");

// ---- notifications -------------------------------------------------------
// Best-effort: fires only while the page is alive but unfocused (background
// tab, other app in front). True lock-screen push would need Web Push +
// VAPID server-side. On iOS this requires the installed (home-screen) app.
let swRegistration = null;
if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .register("sw.js")
    .then((registration) => { swRegistration = registration; })
    .catch(() => {});
}

let askedNotify = false;
let replaying = false;

function maybeRequestNotifyPermission() {
  // Called from a user gesture (task submit) — required on iOS.
  if (!("Notification" in window) || Notification.permission !== "default" || askedNotify) {
    return;
  }
  askedNotify = true;
  Notification.requestPermission().catch(() => {});
}

function notify(title, body) {
  if (replaying || document.hasFocus()) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const options = {
    body: (body || "").slice(0, 140),
    tag: "aish", // coalesce: the newest state is the only one that matters
    icon: "icon-192.png",
    badge: "icon-192.png",
  };
  if (swRegistration) {
    swRegistration.showNotification(title, options).catch(() => {});
  } else {
    try { new Notification(title, options); } catch { /* unsupported */ }
  }
}

// ---- base path (subpath-mounted deploys) --------------------------------
// The app is normally served at "/", but a reverse proxy may mount it under a
// prefix (e.g. https://host/preview/ for a branch preview). Static assets and
// the manifest are already relative; these are the endpoints that were rooted
// at "/". Derive the mount point from the document's directory so ws + fetches
// stay same-origin under whatever prefix served index.html. Always "/"-bounded.
const BASE = location.pathname.replace(/[^/]*$/, "");

// ---- token (optional auth) ----------------------------------------------
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) localStorage.setItem("aish-token", urlToken);
const token = localStorage.getItem("aish-token");

// The shareable URL carries a PUBLIC session id WITHOUT the .jsonl storage
// extension, so the link doesn't leak the file-based store — a move to a DB
// would only change these two functions (or push the mapping server-side).
// The wire protocol still uses the store name.
const publicSession = (name) => (name || "").replace(/\.jsonl$/, "");
const storeSession = (id) => (!id || id.endsWith(".jsonl") ? id : `${id}.jsonl`);

// ---- offline mirror (#165) ----------------------------------------------
// A local copy of the conversation archive, so the installed app opens and
// reads its history on a plane, on a foreign SIM, or with the server simply
// off. Three things make this cheap rather than a second implementation:
//
//   * rendering is already a pure function of an event array — onHello() +
//     onReplay() take exactly what the server sends, so a CACHED event array
//     replays through the identical code path with no offline-specific
//     renderer to drift;
//   * the server reconstructs any session's event stream straight off disk
//     (GET /offline/session), so syncing costs no session slot and no Agent;
//   * search ranking is deterministic tiers, not a model, so it ports to JS
//     and behaves the same offline as on.
//
// It doubles as a speed feature: the last session paints from IndexedDB before
// the socket is even open, and a sync that changes nothing costs one small
// request (ETag → 304) instead of re-downloading anything.
//
// Deliberately NOT offered offline: sending. Queuing prompts for later would
// dispatch an agent that runs shell commands at a moment nobody is watching,
// with the approval gate answered by a person who has moved on. Read-only is
// not a limitation here, it is the correct scope.

const OFFLINE_DB = "aish-offline";
const OFFLINE_DB_VERSION = 1;
// Aggressive mirror: everything that fits, newest first. Bulk command output is
// already capped server-side (OFFLINE_OUTPUT_CAP), which is what makes a whole
// archive fit in the space a handful of raw transcripts would take.
const OFFLINE_MAX_BYTES = 150 * 1024 * 1024;
const OFFLINE_MAX_SESSIONS = 200;
const OFFLINE_SEARCH_CHARS = 200000; // per-session searchable text kept for offline search

let offlineDbPromise = null;

// An IndexedDB open can hang FOREVER with no error and no `blocked` event: if a
// deleteDatabase is pending (another tab of this app tapped "Clear offline
// copies" while this one held a connection), a fresh open just queues behind it
// indefinitely. Unguarded, every offline read would await a promise that never
// settles — the first paint silently never happens and the app looks like it
// simply lost the feature. Time it out instead and degrade to online-only; the
// promise is not cached on failure, so the next attempt retries cleanly.
const OFFLINE_OPEN_TIMEOUT_MS = 8000;

function offlineOpen() {
  if (offlineDbPromise) return offlineDbPromise;
  offlineDbPromise = new Promise((resolve, reject) => {
    if (!self.indexedDB) { reject(new Error("no indexedDB")); return; }
    const timer = setTimeout(() => reject(new Error("indexedDB open timed out")),
                             OFFLINE_OPEN_TIMEOUT_MS);
    const settle = (fn) => (value) => { clearTimeout(timer); fn(value); };
    resolve = settle(resolve);
    reject = settle(reject);
    const request = indexedDB.open(OFFLINE_DB, OFFLINE_DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      // Metadata and bodies are separate stores on purpose: the session list
      // and the search index read ONLY metadata, so opening the sessions sheet
      // never deserializes megabytes of transcripts.
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "name" });
      if (!db.objectStoreNames.contains("events")) db.createObjectStore("events", { keyPath: "name" });
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv", { keyPath: "k" });
    };
    request.onsuccess = () => {
      const db = request.result;
      // The other half of the deadlock: if ANOTHER tab clears offline data,
      // step aside instead of blocking its delete forever. Dropping the cached
      // promise means the next read reopens on the rebuilt database.
      db.onversionchange = () => { db.close(); offlineDbPromise = null; };
      resolve(db);
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("indexedDB blocked"));
  }).catch((err) => {
    offlineDbPromise = null; // a private-mode / quota refusal may succeed later
    throw err;
  });
  return offlineDbPromise;
}

function idbRun(store, mode, fn) {
  return offlineOpen().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode);
        const result = fn(tx.objectStore(store));
        tx.oncomplete = () => resolve(result && result.__req ? result.__req.result : result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      })
  );
}

const idbGet = (store, key) => idbRun(store, "readonly", (s) => ({ __req: s.get(key) }));
const idbAll = (store) => idbRun(store, "readonly", (s) => ({ __req: s.getAll() }));
const idbPut = (store, value) => idbRun(store, "readwrite", (s) => { s.put(value); });
const idbDel = (store, key) => idbRun(store, "readwrite", (s) => { s.delete(key); });

// Every offline read is best-effort: a browser in private mode, a denied quota
// or a corrupt database must degrade to "online only", never to a broken app.
const offlineSafe = (promise, fallback = null) => promise.catch(() => fallback);

// ---- offline: what a cached session looks like ---------------------------
// meta:   { name, title, snippet, ts, origin, pinned, openedAt, syncedAt,
//           total, sig, bytes, text }
// events: { name, events: [...] }   ← replayed verbatim through onReplay()

function offlineSearchText(events) {
  // Only what a person would search FOR: their own words and aish's answers.
  // Command output is noise in a search index (and the bulk of the bytes).
  const parts = [];
  for (const event of events || []) {
    if (event.type === "user" && event.text) parts.push(event.text);
    else if (event.type === "done" && event.result) parts.push(event.result);
    else if (event.type === "history") {
      for (const m of event.messages || []) if (m.content) parts.push(m.content);
    }
  }
  return parts.join("\n").slice(0, OFFLINE_SEARCH_CHARS).toLowerCase();
}

async function offlineSave(name, payload, events) {
  const text = offlineSearchText(events);
  const meta = {
    name,
    title: payload.title || "",
    snippet: payload.snippet || "",
    ts: payload.ts || Date.now() / 1000,
    origin: payload.origin || "user",
    pinned: Boolean(payload.pinned),
    openedAt: payload.openedAt || 0,
    syncedAt: Date.now(),
    total: payload.total || events.length,
    sig: payload.sig || "",
    // Approximate, and that is fine — it decides eviction order, not correctness.
    bytes: JSON.stringify(events).length + text.length,
    text,
  };
  await idbPut("events", { name, events });
  await idbPut("meta", meta);
  return meta;
}

async function offlineLoad(name) {
  const [meta, body] = await Promise.all([
    offlineSafe(idbGet("meta", name)),
    offlineSafe(idbGet("events", name)),
  ]);
  if (!meta || !body) return null;
  return { meta, events: body.events || [] };
}

async function offlineList() {
  return (await offlineSafe(idbAll("meta"), [])) || [];
}

// [OFFLINE-EVICT-START]
// Which cached sessions to drop when over budget, lowest value first.
// Priority, highest to lowest: explicitly pinned ("Available offline" — the
// user's promise to themselves that a reference chat is always there), then the
// session on screen, then most-recently-opened-on-this-device, then
// most-recently-active anywhere (which is what makes a chat started on the
// laptop readable on the phone without opening it first).
function offlineEvictionOrder(metas, current) {
  const value = (m) => Math.max(m.openedAt || 0, (m.ts || 0) * 1000);
  return metas
    .filter((m) => !m.pinned && m.name !== current)
    .sort((a, b) => value(a) - value(b)); // oldest-value first = evict first
}

function offlineOverBudget(metas, maxBytes, maxSessions) {
  const bytes = metas.reduce((sum, m) => sum + (m.bytes || 0), 0);
  return bytes > maxBytes || metas.length > maxSessions;
}

// Returns the names to delete, in order, to get back under budget. Pinned
// sessions are counted against the budget but never returned — a mirror that
// silently dropped what you pinned would be worse than one that ran over.
function offlinePlanEviction(metas, current, maxBytes, maxSessions) {
  const doomed = offlineEvictionOrder(metas, current);
  const keep = new Map(metas.map((m) => [m.name, m]));
  const evict = [];
  for (const meta of doomed) {
    if (!offlineOverBudget([...keep.values()], maxBytes, maxSessions)) break;
    keep.delete(meta.name);
    evict.push(meta.name);
  }
  return evict;
}
// [OFFLINE-EVICT-END]

async function offlineEnforceBudget(current) {
  const metas = await offlineList();
  const evict = offlinePlanEviction(metas, current, OFFLINE_MAX_BYTES, OFFLINE_MAX_SESSIONS);
  for (const name of evict) {
    await offlineSafe(idbDel("events", name));
    await offlineSafe(idbDel("meta", name));
  }
  return evict.length;
}

// [OFFLINE-SEARCH-START]
// Offline port of SessionLog.rank (session.py) — same tiers, same order, so a
// search offline ranks like the same search online. Tiers 5..2 are exact ports;
// tier 1's fuzzy match approximates difflib's SequenceMatcher with an
// LCS ratio, which agrees with it for the typo-shaped cases it exists to catch.
const OFFLINE_FUZZY_THRESHOLD = 0.55;  // whole query vs whole title
const OFFLINE_FUZZY_WORD_CUTOFF = 0.75; // single query word vs single session word
const OFFLINE_PUNCT_RE = /^[.,;:!?()[\]{}<>'"`]+|[.,;:!?()[\]{}<>'"`]+$/g;

function lcsRatio(a, b) {
  if (!a.length || !b.length) return 0;
  // Rolling single row: query words and titles are short, and this runs per
  // candidate per keystroke.
  let prev = new Array(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i += 1) {
    const row = new Array(b.length + 1).fill(0);
    for (let j = 1; j <= b.length; j += 1) {
      row[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], row[j - 1]);
    }
    prev = row;
  }
  return (2 * prev[b.length]) / (a.length + b.length);
}

function offlineRank(metas, query) {
  const queryCf = query.split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  const words = queryCf ? queryCf.split(" ") : [];
  if (!words.length) return metas.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
  const scored = [];
  for (const meta of metas) {
    const titleCf = (meta.title || "").toLowerCase();
    const contentCf = meta.text || "";
    let score;
    if (titleCf === queryCf) score = 5;
    else if (titleCf.includes(queryCf)) score = 4;
    else if (contentCf.includes(queryCf)) score = 3;
    else if (words.every((w) => contentCf.includes(w))) score = 2;
    else {
      const vocab = new Set(
        contentCf.split(/\s+/).map((w) => w.replace(OFFLINE_PUNCT_RE, "")).filter(Boolean)
      );
      const everyWordClose = words.every((w) => {
        for (const candidate of vocab) {
          if (lcsRatio(w, candidate) >= OFFLINE_FUZZY_WORD_CUTOFF) return true;
        }
        return false;
      });
      if (everyWordClose || lcsRatio(queryCf, titleCf) >= OFFLINE_FUZZY_THRESHOLD) score = 1;
      else continue;
    }
    scored.push({ score, meta });
  }
  // Newest-first within a tier, matching the server (whose input is already
  // recency-ordered and whose sort is stable).
  scored.sort((a, b) => b.score - a.score || (b.meta.ts || 0) - (a.meta.ts || 0));
  return scored.map((s) => s.meta);
}
// [OFFLINE-SEARCH-END]

// ---- offline: connectivity ----------------------------------------------
// "Offline" here means OUR server is unreachable, which is not the same as
// navigator.onLine (a hotel wifi with no route home is "online"). The socket is
// the ground truth; navigator.onLine only ever accelerates the conclusion.
let offlineMode = false;

function setOfflineMode(value) {
  if (offlineMode === value) return;
  offlineMode = value;
  document.body.classList.toggle("offline", value);
  const bar = $("offlinebar");
  if (bar) bar.hidden = !value;
  // One bar, not two: the offline bar says everything "reconnecting…" did and
  // adds what still works, and it carries the same tap-to-retry. Retries keep
  // running underneath either way.
  if (value) $("connbar").hidden = true;
  if (!value) offlineSyncSoon(0);
}

// ---- offline: background sync -------------------------------------------
// One pass = fetch the catalogue, then fetch each session that changed, newest
// first, one at a time. It is resumable by construction rather than by
// bookkeeping: each session commits atomically, and the next pass recomputes
// what is missing from the same timestamp comparison — so a sync interrupted by
// a tunnel picks up exactly where it stopped, with no partial state to repair.

const OFFLINE_SYNC_IDLE_MS = 5 * 60 * 1000; // periodic catch-up while connected
const OFFLINE_SYNC_GAP_MS = 40;             // breathing room between sessions

let offlineSyncing = false;
let offlineSyncTimer = null;
let offlinePersistAsked = false;
const offlineMeta = new Map(); // name -> cached meta, for badges and lookups

function offlineUrl(path, params) {
  const url = new URL(BASE + path, location.href);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  }
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

// Opening a chat is the strongest signal that it matters to this device — it
// outranks recency in the eviction order (offlineEvictionOrder).
async function offlineTouch(name) {
  const meta = await offlineSafe(idbGet("meta", name));
  if (!meta) return;
  meta.openedAt = Date.now();
  await offlineSafe(idbPut("meta", meta));
  offlineMeta.set(name, meta);
}

async function offlineRefreshMetaMap() {
  offlineMeta.clear();
  for (const meta of await offlineList()) offlineMeta.set(meta.name, meta);
}

async function offlineFetchSession(name, local) {
  // since/sig ask for a delta; the ETag asks for nothing at all. Both are
  // values the server minted — the client stores them opaquely and echoes them,
  // so there is no protocol detail to get wrong on this side.
  const url = offlineUrl("offline/session", {
    session: name,
    since: local?.total || "",
    sig: local?.sig || "",
  });
  const headers = local?.etag ? { "If-None-Match": local.etag } : {};
  const response = await fetch(url, { headers, cache: "no-store" });
  if (response.status === 304) return "unchanged";
  if (!response.ok) throw new Error(`offline sync ${response.status}`);
  const payload = await response.json();
  const previous = payload.base > 0 ? (await offlineLoad(name))?.events || [] : [];
  // base > 0 means the server verified our prefix, so appending is safe;
  // base === 0 means it did not, and the full stream replaces what we had.
  const events =
    payload.base > 0 ? previous.slice(0, payload.base).concat(payload.events) : payload.events;
  const meta = await offlineSave(name, {
    ...payload,
    pinned: local?.pinned,
    openedAt: local?.openedAt,
  }, events);
  meta.etag = response.headers.get("etag") || "";
  await idbPut("meta", meta);
  offlineMeta.set(name, meta);
  return "updated";
}

async function offlineSyncOnce() {
  if (offlineSyncing || offlineMode) return;
  offlineSyncing = true;
  try {
    const response = await fetch(offlineUrl("offline/index"), { cache: "no-store" });
    if (!response.ok) return; // 403 (bad token) or a server mid-restart: try later
    const index = await response.json();
    await offlineRefreshMetaMap();
    const server = new Map(index.sessions.map((s) => [s.name, s]));

    // A session deleted on the server is dropped locally too — a mirror that
    // resurrects deleted chats is a surprise, not a feature. Pinned ones are
    // the deliberate exception: pinning is the user saying "keep this for me".
    for (const [name, meta] of offlineMeta) {
      if (!server.has(name) && !meta.pinned) {
        await offlineSafe(idbDel("events", name));
        await offlineSafe(idbDel("meta", name));
        offlineMeta.delete(name);
      }
    }

    // The catalogue is newest-first, so a sync cut short by a dying connection
    // has still fetched the sessions most likely to be wanted.
    for (const info of index.sessions) {
      if (offlineMode) break;
      const local = offlineMeta.get(info.name);
      // Second-resolution mtimes: only refetch on a strictly newer stamp, or
      // the current session would be refetched on every single pass.
      if (local && local.ts >= info.ts && local.total) continue;
      try {
        await offlineFetchSession(info.name, local);
      } catch {
        return; // network died mid-pass; the next pass resumes from here
      }
      await new Promise((resolve) => setTimeout(resolve, OFFLINE_SYNC_GAP_MS));
    }
    await offlineEnforceBudget(currentSession);
    await offlineRefreshMetaMap();
    if (!offlinePersistAsked && navigator.storage?.persist) {
      // Ask ONCE, after there is something worth keeping: persistent storage
      // exempts the mirror from the browser's eviction-under-pressure sweep.
      offlinePersistAsked = true;
      navigator.storage.persist().catch(() => {});
    }
  } catch { /* offline or blocked — the next trigger retries */ }
  finally {
    offlineSyncing = false;
  }
}

function offlineSyncSoon(delay = 1500) {
  clearTimeout(offlineSyncTimer);
  offlineSyncTimer = setTimeout(() => {
    offlineSyncOnce().finally(() => offlineSyncSoon(OFFLINE_SYNC_IDLE_MS));
  }, delay);
}

// ---- offline: reading a cached session ----------------------------------
let offlineViewing = false; // the transcript on screen came from the mirror
let serverPainted = false;  // an authoritative replay has landed — don't overpaint it

// The URL always names the viewed session (shareable, and it identifies the
// log for debugging), alongside the token and any #console hash. replaceState
// so it doesn't spam browser history.
//
// It is also what connect() reads to decide where to reconnect, and the URL
// WINS over the last-session key — so a session opened from the mirror must
// update it too. Without that, reading an old chat offline and then regaining
// signal would yank you back to whatever chat the last hello had pinned here.
function deepLinkSession(name) {
  const url = new URL(location.href);
  const pub = publicSession(name);
  if (url.searchParams.get("session") === pub) return;
  url.searchParams.set("session", pub);
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

async function openCachedSession(name) {
  const cached = await offlineLoad(name);
  if (!cached) {
    showToast("that chat isn't available offline");
    return false;
  }
  offlineViewing = true;
  currentSession = name;
  setTitle(cached.meta.title || "aish");
  try { localStorage.setItem("aish-session", name); } catch { /* private mode */ }
  deepLinkSession(name); // so the reconnect lands on the chat being read
  onReplay({ events: cached.events, truncated: false });
  offlineTouch(name);
  return true;
}

// Switching chats: the socket when there is one, the mirror when there isn't.
function resumeSession(name) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    send({ type: "resume", path: name });
    return;
  }
  openCachedSession(name);
}

// First paint. Runs before connect() so the last chat is on screen while the
// socket is still opening — the same code path that made this cheap offline is
// what makes it fast online. A server hello/replay overwrites it moments later.
async function offlineFirstPaint() {
  try {
    const urlSession = new URLSearchParams(location.search).get("session");
    const remembered = storeSession(urlSession) || localStorage.getItem("aish-session");
    let cached = remembered ? await offlineLoad(remembered) : null;
    if (!cached) {
      // The remembered chat can legitimately be missing from the mirror: an
      // EMPTY new chat is never mirrored (the server's own listing skips it),
      // and that is exactly what you leave behind by opening the app and not
      // typing. Landing on a blank screen with no chat to swipe from would
      // make the whole offline archive unreachable, so fall back to the newest
      // chat there IS.
      const metas = await offlineList();
      const newest = metas.sort((a, b) => (b.ts || 0) - (a.ts || 0))[0];
      if (newest) cached = await offlineLoad(newest.name);
    }
    // The socket won the race — the authoritative transcript is already up.
    if (!cached || serverPainted) return;
    currentSession = cached.meta.name;
    setTitle(cached.meta.title || "aish");
    // Anchor the pager (and any reconnect) to what is actually on screen.
    deepLinkSession(cached.meta.name);
    onReplay({ events: cached.events, truncated: false });
  } catch { /* no mirror yet — the socket will fill the page in */ }
}

// ---- offline: pinning ("Available offline") -----------------------------
// [OFFLINE-PIN-STATE-START]
// The pin lives in IndexedDB, and that is the ONLY place anything may read it
// from. Reading it off the in-memory `offlineMeta` mirror instead was the "my
// pin got reset" bug: that map is empty for a moment after every reload (it is
// filled by an async refresh), so a pinned chat's menu showed "Off" — and
// tapping the item to "pin" it read the real state and flipped it, silently
// UNPINNING the chat the user was trying to protect. A label that can lie about
// a toggle's state is worse than a slow one, so this is always a real read.
async function offlineIsPinned(name) {
  if (!name) return false;
  const meta = await offlineSafe(idbGet("meta", name));
  return Boolean(meta && meta.pinned);
}
// [OFFLINE-PIN-STATE-END]

// The menu is already open when this resolves, so the label fills in a beat
// later rather than being wrong immediately. "…" while reading, "—" if the
// store can't be reached — never a confident wrong answer.
async function refreshOfflinePinLabel() {
  const label = $("offline-state");
  if (!label) return;
  label.textContent = "…";
  const name = currentSession;
  let pinned;
  try {
    pinned = await offlineIsPinned(name);
  } catch {
    label.textContent = "—";
    return;
  }
  // The menu may have closed, or moved to another chat, while we were reading.
  if ($("session-menu").hidden || name !== currentSession) return;
  label.textContent = pinned ? "On" : "Off";
}

async function toggleOfflinePin() {
  if (!currentSession) { showToast("no chat to pin yet"); return; }
  const meta = (await offlineSafe(idbGet("meta", currentSession))) || null;
  if (!meta) {
    // Not mirrored yet (a brand-new chat, or a sync that hasn't reached it):
    // fetch it now so "available offline" is true the moment it is promised.
    try {
      await offlineFetchSession(currentSession, null);
    } catch {
      showToast("can't save this chat offline right now");
      return;
    }
  }
  const current = (await offlineSafe(idbGet("meta", currentSession))) || null;
  if (!current) { showToast("offline storage unavailable"); return; }
  current.pinned = !current.pinned;
  await idbPut("meta", current);
  offlineMeta.set(currentSession, current);
  refreshOfflinePinLabel(); // keep the label honest if the menu is still open
  showToast(current.pinned ? "kept available offline" : "no longer kept offline");
}

async function offlineClearAll() {
  try {
    const db = await offlineOpen();
    db.close();
    offlineDbPromise = null;
    await new Promise((resolve) => {
      const request = indexedDB.deleteDatabase(OFFLINE_DB);
      request.onsuccess = request.onerror = request.onblocked = () => resolve();
    });
  } catch { /* nothing to clear */ }
  offlineMeta.clear();
  navigator.serviceWorker?.controller?.postMessage({ type: "CLEAR_CACHES" });
  showToast("offline copies cleared");
  offlineSyncSoon(2000);
}

// ---- websocket lifecycle -------------------------------------------------
let ws = null;
let backoff = 1000;
let reconnectTimer = null;
// A transient drop usually reconnects in well under a second, so we don't flash
// the red dot + "reconnecting" bar the instant the socket closes — we arm a
// short grace timer and only surface the warning if the outage outlives it. A
// successful onopen clears it, so a quick blip is never shown to the user (#129).
let connWarnTimer = null;
const CONN_WARN_DELAY = 2000;

function connect() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  // Name the session this device was on: after a server restart the active
  // session is a fresh empty chat, and without this every reconnect (and
  // every rev-mismatch reload) would silently move the user there.
  // A shared/deep link names the session (?session=<public id>); it wins on
  // load, else resume the last session this device was on. Map the public id
  // back to the store name for the server.
  const urlSession = new URLSearchParams(location.search).get("session");
  const lastSession = urlSession || localStorage.getItem("aish-session");
  if (lastSession) params.set("session", storeSession(lastSession));
  const query = params.size ? `?${params}` : "";
  ws = new WebSocket(`${proto}//${location.host}${BASE}ws${query}`);
  ws.onopen = () => {
    backoff = 1000;
    clearTimeout(connWarnTimer); // reconnected within the grace window — no warning
    connWarnTimer = null;
    $("connbar").hidden = true;
    connOk = true;
    updateDot();
    setOfflineMode(false);
    offlineSyncSoon(); // catch the mirror up on whatever happened while away
    checkAppVersion(); // server restarts are when the UI code changes
  };
  ws.onmessage = (raw) => handle(JSON.parse(raw.data));
  ws.onclose = (event) => {
    if (event.code === 4000) {
      connOk = false;
      updateDot();
      showToast("another device connected — this tab is detached");
      return; // deliberate replacement: do not fight over the session
    }
    if (event.code === 4403) {
      // In-app entry: iOS home-screen apps launch without query params and
      // have storage isolated from Safari, so the URL trick can't help there.
      connOk = false;
      updateDot();
      if (token) showToast("that token was rejected — check for typos");
      $("token-gate").hidden = false;
      $("token-input").focus();
      return;
    }
    // Transient drop: defer the red dot + "reconnecting" bar past a grace window
    // so a sub-second blip stays invisible (#129). Arm once — don't reset it on
    // each failed retry, or a sustained outage would never surface.
    // …and once the offline bar has taken over, don't re-arm: every failed
    // retry would otherwise flash "reconnecting…" back on for an instant.
    if (connWarnTimer === null && $("connbar").hidden && !offlineMode) {
      connWarnTimer = setTimeout(() => {
        connWarnTimer = null;
        connOk = false;
        updateDot();
        $("connbar").hidden = false;
        // The socket has been down long enough to call it: switch the UI to
        // read-from-the-mirror mode. Retries continue underneath, and the first
        // successful onopen clears it.
        setOfflineMode(true);
      }, CONN_WARN_DELAY);
    }
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  };
}

// Manual nudge (#18): standalone PWA mode has no browser chrome to refresh
// from, so a stalled connection otherwise strands the user until they
// force-quit. A socket can also look OPEN to the browser long after the
// underlying connection actually died (e.g. the phone slept through a
// network change) — drop whatever we have unconditionally and retry now
// instead of waiting out the backoff ladder.
function reconnect() {
  clearTimeout(reconnectTimer);
  backoff = 1000;
  connOk = false;
  updateDot();
  if (ws) {
    ws.onclose = null; // this close is deliberate; the connect() below replaces it
    ws.close();
  }
  connect();
}

// New chat over a socket that already knows it's dead (CLOSING/CLOSED/
// CONNECTING) would otherwise just toast "not connected" and go nowhere —
// reconnect first and ask for the fresh chat once the new socket is up. A
// "zombie" socket (readyState still OPEN, dead underneath) isn't detectable
// here; the manual Reconnect control covers that case (#18).
function requestNewChat() {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    reconnect();
    ws.addEventListener("open", () => send({ type: "new" }), { once: true });
    return;
  }
  send({ type: "new" });
}

let appVersion = null;

async function checkAppVersion() {
  // A long-lived tab/PWA keeps running old JS across server upgrades and
  // silently speaks an outdated protocol. Compare the served app.js
  // fingerprint on every (re)connect; reload when it changed — the replay
  // mechanism restores the full view afterwards.
  try {
    const response = await fetch("app.js", { method: "HEAD", cache: "no-store" });
    const tag = response.headers.get("etag") || response.headers.get("last-modified");
    if (!tag) return;
    if (appVersion === null) {
      appVersion = tag;
    } else if (tag !== appVersion) {
      showToast("aish-web updated — reloading");
      setTimeout(() => reloadThrottled("asset"), 1000);
    }
  } catch { /* offline blip; next reconnect checks again */ }
}

function send(message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    // Offline is read-only by design (see the offline mirror notes above), so
    // say which it is: a blip worth retrying, or a state the user should stop
    // fighting. Composer drafts are already preserved either way.
    showToast(offlineMode ? "offline — you can read past chats, not send" : "not connected");
    return false;
  }
  ws.send(JSON.stringify(message));
  return true;
}

document.addEventListener("visibilitychange", () => {
  // Phone unlock: reconnect immediately instead of waiting out the backoff.
  if (!document.hidden && (!ws || ws.readyState === WebSocket.CLOSED)) connect();
});

// ---- event dispatch ------------------------------------------------------
let answerEl = null; // the assistant block tokens append to
let answerText = "";
let sawAnswer = false; // any tokens streamed since the task started —
// echo lines close the answer block, so this (not answerText) decides
// whether done.result still needs rendering
// Streaming renders incrementally: blocks above the last committed blank
// line are stable DOM that is never rebuilt (so an embedded image decodes
// once, not once per token), and renders are coalesced to one per frame.
let answerStableLen = 0; // chars of answerText already in stable DOM
let answerStableNodes = 0; // answerEl children that are stable
let answerRenderQueued = false;
const cards = new Map(); // approval id -> card element

function handle(event) {
  switch (event.type) {
    case "hello": onHello(event); break;
    case "replay": serverPainted = true; onReplay(event); break;
    case "user":
      closeAnswer();
      finishTrace(); // close any trace from a prior turn before the new one
      removeQueueChip(event.text); // a queued message that just started running
      retireQuickReplies();
      // The rerun button belongs to the last ANSWER: attachAnswerTools retires
      // it when the next answer lands. Don't retire it here on the user turn —
      // user-only turns (/cd, a bare !command) produce no answer and would
      // otherwise strip the last answer's rerun for good (survives replay too).

      sawAnswer = false;
      answerFilling = false;
      userCmdBlock = null; // a new turn supersedes any dangling ! command block
      taskErrored = false; // a new turn clears the prior error's red dot
      turnStart = replaying ? 0 : Date.now(); // timing readout on the answer
      setBusy(true);
      if (!sessionTitled) setTitle(event.text.split("\n")[0]);
      rememberPrompt(stripAttachmentNotes(event.text));
      lastUserPrompt = stripAttachmentNotes(event.text); // for error Retry
      // A user-direct `!` command is already fully shown by the terminal block
      // that follows — its prompt line carries the command. The extra blue
      // user-input bubble duplicated that and, on a restored session, rendered
      // the raw (multi-line) command as if typed into chat (#154). So skip the
      // bubble for `!` commands; the turn-boundary handling above still runs.
      turnAnchorEl = event.text.startsWith("!") ? null : addUserMsg(event.text);
      // Your own message always comes into view, even if you were scrolled up.
      if (!replaying) scrollToEnd(true);
      break;
    case "queued":
      addQueueChip(event.text);
      break;
    case "cwd_queued": addCwdChip(event.path); break;
    case "cwd_dequeued": removeCwdChip(); break;
    case "token": onToken(event.text); break;
    case "echo":
      // The activity trace already shows a run_command's approval + result and
      // its own Stop/Stopping state, so drop the approver's redundant
      // confirmation and the "stop requested" line while a trace is open.
      if (currentTrace && /^[✓✕] (auto-approved|session-allowed|always-allowed|blocked|stop requested)/.test(event.text)) break;
      closeAnswer();
      addAnsiMsg("echo", event.text);
      break;
    case "stream": traceStream(event.text); break;
    case "command_start": onCommandStart(event); break;
    case "command_end": onCommandEnd(event); break;
    case "step": traceStep(event); break;
    case "workspace": addWorkspaceNote(event.change, event.path); break;
    case "error":
      closeAnswer();
      finishTrace(true); // #48: a mid-turn error must close the live trace, not leave it stuck "Working…"
      addErrorMsg(event.text);
      // A live error means the current task failed → red dot. A REPLAYED error
      // (a past interrupted turn on a freshly-loaded session) must not: the
      // connection is fine, so keep the dot green and just show Retry.
      if (!replaying) taskErrored = true;
      setBusy(false);
      setStatus(null);
      notify("aish — task failed", event.text);
      break;
    case "stopped": onStopped(); break;
    case "status": onStatus(event); break;
    case "approval_request": onApprovalRequest(event); break;
    case "approval_resolved": onApprovalResolved(event); break;
    case "done":
      onDone(event);
      // A finished turn is exactly what the mirror is missing; pull it now
      // rather than at the next idle tick, so closing the laptop right after
      // an answer still leaves that answer readable on the phone.
      if (!replaying) offlineSyncSoon(2000);
      break;
    case "history": onHistory(event.messages); break;
    case "session_list": renderSessions(event); break;
    case "model_list": renderModels(event); break;
    case "model_changed": onModelChanged(event); break;
    case "cwd_changed": renderWorkspace(event); break;
    case "job_list": $("ws-jobs").textContent = event.text || "—"; break;
    case "file_list": onFileList(event); break;
    case "session_state": onSessionState(event); break;
    case "session_deleted": showToast("session deleted"); break;
    case "session_renamed": onSessionRenamed(event); break;
    case "role": onRole(event); break;
    case "console_started": onConsoleStarted(event); break;
    case "console_out": onConsoleOut(event.data); break;
    case "console_exit": onConsoleExit(event.code); break;
    case "console_shared": showToast("shared to context"); break;
    case "console_error": showToast(event.text); break;
  }
}

function onSessionRenamed(event) {
  // The header follows only when the renamed chat is the one on screen; the
  // drawer refreshes via the session_list the server sends right after.
  if (event.name === currentSession) setTitle(event.title);
  const page = pagerSessions.find((s) => s.name === event.name);
  if (page) page.title = event.title; // keep the swipe pager label in sync
}

function onSessionState(event) {
  const label = event.title
    ? `“${event.title.slice(0, 40)}”`
    : event.session.replace(/^session-|\.jsonl$/g, "").replace(/-\d{6}$/, "");
  showToast(`${label}: task finished — tap ‹ Sessions to switch back`);
  notify("aish — background task finished", event.title || event.session);
  attentionSessions.add(event.session);
  refreshBadge();
  if (!$("sessions-sheet").hidden) {
    send({ type: "sessions", query: $("sessions-search").value });
  }
}

let sessionTitled = false;

function setTitle(text) {
  sessionTitled = Boolean(text);
  $("session-title").textContent = text || "New chat";
  updateBackLabel();
}

// Collapse "‹ Sessions" to just "‹" when the title is long enough to be
// truncated — freeing header room at any width, on top of the ≤430px viewport
// rule (#83). "Long" is content- not viewport-dependent, so it's measured:
// clear the class (label shown), read whether the title overflows, re-add it if
// so. Synchronous, so the transient label-shown state never paints.
function updateBackLabel() {
  const nav = $("nav-row");
  const title = $("session-title");
  nav.classList.remove("crowded");
  if (title.scrollWidth > title.clientWidth + 1) nav.classList.add("crowded");
}
let backLabelResizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(backLabelResizeTimer);
  backLabelResizeTimer = setTimeout(updateBackLabel, 100);
});

// The ?v= the server stamped into our own <script> tag — ground truth for
// which code revision this page actually runs (unlike any value learned at
// runtime, it can't be polluted by a stale-from-HTTP-cache load).
const PAGE_REV = (() => {
  const script = document.querySelector('script[src*="app.js"]');
  try { return new URL(script.src).searchParams.get("v"); } catch { return null; }
})();

// [OFFLINE-RELOAD-START]
// A rev mismatch means this page runs older code than the server serves, and
// the fix is a reload. With a service worker in front of index.html that is one
// bad cache entry away from a loop: reload → SW serves the same stale HTML →
// same stale rev → reload. Two independent guards, because a loop here bricks
// the app on the device it happens on.
//
//  1. Purge the cached shell first, so the reload is guaranteed to re-fetch.
//  2. Refuse to reload more than RELOAD_MAX times in RELOAD_WINDOW_MS. Running
//     one revision behind is a papercut; an app that reboots forever is not
//     usable at all, so when the guard trips we stay put and say so.
const RELOAD_KEY = "aish-reloads";
const RELOAD_WINDOW_MS = 60000;
const RELOAD_MAX = 3;

function reloadThrottled(reason) {
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(RELOAD_KEY) || "[]"); } catch { history = []; }
  const now = Date.now();
  history = history.filter((t) => now - t < RELOAD_WINDOW_MS);
  if (history.length >= RELOAD_MAX) {
    showToast("update loop detected — staying on this version");
    return false;
  }
  history.push(now);
  try { sessionStorage.setItem(RELOAD_KEY, JSON.stringify(history)); } catch { /* private mode */ }
  const worker = navigator.serviceWorker?.controller;
  if (!worker) { location.reload(); return true; }
  // Give the purge a moment to land, but never hang on it — a reload that
  // reuses the cache is still better than no reload.
  let done = false;
  const go = () => { if (!done) { done = true; location.reload(); } };
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "SHELL_PURGED") go();
  }, { once: true });
  worker.postMessage({ type: "PURGE_SHELL", reason });
  setTimeout(go, 1500);
  return true;
}
// [OFFLINE-RELOAD-END]

function onHello(event) {
  // Server code changed since this page was built (or the page predates rev
  // stamping entirely) — reload; the replay mechanism restores the view.
  if (event.rev && event.rev !== PAGE_REV) { reloadThrottled("rev"); return; }
  offlineViewing = false; // a live hello supersedes anything read from the mirror
  // The interactive console is GLOBAL (#148 follow-up): it floats above whatever
  // chat is shown and is untouched by a session switch. A hello also means a
  // (re)connect. `#console` is a deep-link that survives a reload / server
  // restart (incl. the rev-reload above), so restore the overlay from it; on a
  // plain reconnect it is already open and we just re-attach (tmux redraws).
  if (consoleOpen) send({ type: "console_open" });
  else if (location.hash === "#console") openConsole();
  $("model-name").textContent = event.model;
  setTitle(event.title);
  pagerSessions = event.pager || [];
  cmdHistory = event.cmd_history || []; // personal command palette (#104)
  currentSession = event.session;
  offlineTouch(event.session); // MRU input to the mirror's eviction order
  currentLogPath = event.log_path || ""; // /session + "Copy log path" (#146)
  localStorage.setItem("aish-session", event.session); // reconnects return here
  deepLinkSession(event.session);
  renderWorkspace(event);
  taskErrored = false; // fresh connected view — clear any stale red
  setBusy(event.busy);
  if (!event.busy) setStatus(null);
  // A fresh view starts with no known role: the server sends a `role` event
  // only when ANOTHER tab is already driving this session (#102). Until then,
  // hide the indicator — this tab is the presumed driver.
  setRolePill(false);
  updateEmptyHint();
}

// Multi-connection (#102): a subtle top-bar pill shows when ANOTHER tab/device
// is the current driver of the session THIS tab is viewing. It clears the
// instant this tab acts (the server sends a fresh `role` with you:true). No
// disabled composer, no "take control" button — acting IS how you take over.
function onRole(event) {
  if (event.session && event.session !== currentSession) return; // not our view
  setRolePill(!!event.controller && !event.you);
}

function setRolePill(active) {
  const pill = $("role-pill");
  if (pill) pill.hidden = !active;
}

function onReplay(event) {
  stopSpeaking(); // the active button is about to be detached with the DOM
  if (swipeInFrom) {
    // This replay is the landing half of a committed swipe: enter from the
    // side the old transcript left toward, completing the pager illusion.
    const from = swipeInFrom;
    swipeInFrom = 0;
    messagesEl.style.transition = "none";
    messagesEl.style.transform = `translateX(${from * messagesEl.clientWidth}px)`;
    requestAnimationFrame(() => {
      messagesEl.style.transition = "transform 0.18s ease-out";
      messagesEl.style.transform = "";
    });
  } else {
    messagesEl.style.transition = "none";
    messagesEl.style.transform = "";
  }
  messagesEl.replaceChildren();
  removeCwdChip(); // a session switch drops any stale cwd card; _show re-emits if pending (#92)
  cards.clear();
  pendingCards = 0;
  answerEl = null;
  answerText = "";
  answerStableLen = 0;
  answerStableNodes = 0;
  sawAnswer = false;
  renderedAnswers = 0; // fork ordinals restart with the rebuilt transcript
  if (event.truncated) addMsg("notice", "… earlier events trimmed …");
  replaying = true; // replayed history must not re-fire notifications
  try {
    for (const item of event.events) handle(item);
  } finally {
    replaying = false;
  }
  scrollToEnd(true);
  snapViewportSoon(); // session switches race keyboard dismissal with this rebuild (#8)
  setTimeout(() => reportViewport("after-replay"), 1200);
  // Every replay marks a fresh view (new chat, resume, reconnect) — on
  // desktop, land the cursor in the composer ready to type.
  if (FINE_POINTER && $("backdrop").hidden) input.focus();
}

let answerFilling = false; // once the answer streams, the page stays put

function onToken(text) {
  sawAnswer = true;
  if (!answerEl) {
    answerEl = addMsg("answer md", "");
    answerText = "";
    answerStableLen = 0;
    answerStableNodes = 0;
    // Content is streaming, but it may be mid-work narration before another
    // tool call — NOT necessarily the final answer. So the live trace stays
    // OPEN and keeps showing steps (and stays expandable); only finishTrace,
    // when the turn actually ends, collapses it to "Worked for Xs". Meanwhile
    // hold the page still so the text fills in from the top instead of the
    // view chasing the streaming bottom.
    answerFilling = true;
    // The live "Thinking…" step is the last row on the timeline when the reply
    // starts streaming; relabel it so it reads as the answer landing, not more
    // thinking, and mark it so finishTrace finalizes it in place ("Answered")
    // instead of dropping the live thinking row.
    if (currentTrace && currentTrace.thinkingRow) {
      currentTrace.thinkingRow.titleEl.textContent = "Answering…";
      currentTrace.thinkingRow.isAnswer = true;
    }
    requestAnimationFrame(() => anchorAnswer(true));
  }
  answerText += text;
  if (!answerRenderQueued) {
    answerRenderQueued = true;
    requestAnimationFrame(renderAnswerFrame);
  }
}

function renderAnswerFrame() {
  answerRenderQueued = false;
  if (!answerEl) return; // answer already closed (and flushed) this frame
  renderAnswerNow();
  if (!answerFilling) anchorAnswer(); // once filling, the page holds still
}

// The element that marks the START of the current turn's response — the
// collapsed "Worked for Xs" trace, or (no trace) the user's own bubble.
let turnAnchorEl = null;

// Once the answer is streaming, keep that anchor pinned to the TOP of the
// viewport and let the rest of the answer flow in below the fold — so you
// read from the beginning (incl. how long it took), instead of the view
// jumping to the bottom and making you scroll back up.
function anchorAnswer(force) {
  const anchor = turnAnchorEl && turnAnchorEl.isConnected ? turnAnchorEl : null;
  if (!anchor) { scrollToEnd(force); return; }
  // getBoundingClientRect, not offsetTop: robust regardless of offsetParent —
  // put the anchor's top a hair below the container's top.
  const delta = anchor.getBoundingClientRect().top - messagesEl.getBoundingClientRect().top;
  const target = Math.max(0, Math.min(
    messagesEl.scrollTop + delta - 6,
    messagesEl.scrollHeight - messagesEl.clientHeight
  ));
  // Scroll DOWN to bring the anchor to the top; never past it (don't chase the
  // streaming answer's bottom — the reader starts from the top and scrolls).
  if (force || messagesEl.scrollTop < target) messagesEl.scrollTop = target;
  updateScrollButton();
  updateEmptyHint();
}

function renderAnswerNow() {
  const boundary = stableBoundary(answerText);
  while (answerEl.childNodes.length > answerStableNodes) answerEl.lastChild.remove();
  if (boundary > answerStableLen) {
    answerEl.appendChild(renderMarkdown(answerText.slice(answerStableLen, boundary)));
    answerStableLen = boundary;
    answerStableNodes = answerEl.childNodes.length;
  }
  answerEl.appendChild(renderMarkdown(answerText.slice(answerStableLen)));
}

// A fence opens on a run of 3+ backticks or tildes. Per CommonMark, the
// closing line must reuse the SAME character, be at least as long, and carry
// no trailing info string — so a shorter/different marker nested inside (e.g.
// the model demonstrating markdown syntax with its own example fence) can't
// masquerade as the outer close and desync everything parsed after it (#80).
// Shared by stableBoundary and renderMarkdown so the two fence-trackers can't
// drift apart and disagree on where a block actually ends.
// The info string allows hyphens (valid per CommonMark, and needed for the
// aish-issue feedback block, #110) — not just \w, which stops at a hyphen.
const FENCE_RE = /^(`{3,}|~{3,})([\w-]*)\s*$/;
function fenceOpen(line) {
  const m = line.match(FENCE_RE);
  return m ? { ch: m[1][0], len: m[1].length, lang: m[2] } : null;
}
function fenceCloses(line, fence) {
  const m = line.match(FENCE_RE);
  return Boolean(m) && m[2] === "" && m[1][0] === fence.ch && m[1].length >= fence.len;
}

// Offset where the stable prefix ends: just past the last blank line that is
// outside a code fence and already followed by another line (a trailing
// blank may still grow into a paragraph continuation). Blocks never span a
// blank line except fenced code, so splitting here renders identically to a
// full parse.
function stableBoundary(text) {
  const lines = text.split("\n");
  let boundary = 0;
  let pos = 0;
  let fence = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (fence) {
      if (fenceCloses(line, fence)) fence = null;
    } else {
      const open = fenceOpen(line);
      if (open) fence = open;
      else if (line.trim() === "" && i < lines.length - 1) {
        boundary = pos + line.length + 1;
      }
    }
    pos += line.length + 1;
  }
  return boundary;
}

function closeAnswer() {
  // A finished answer (streaming ends, or something else interrupts the
  // block) gets its copy/read-aloud row; mid-stream re-renders would clobber it.
  if (answerEl) {
    renderAnswerNow(); // flush any tokens still waiting on the next frame
    highlightFences(answerEl); // the answer is settled now — safe to tokenize fences once
    if (answerText.trim()) attachAnswerTools(answerEl, answerText, lastUserPrompt);
  }
  answerEl = null;
  answerText = "";
}

function onDone(event) {
  answerTiming = turnStart ? (Date.now() - turnStart) / 1000 : 0;
  if (!sawAnswer && event.result) {
    const el = addMsg("answer md", "");
    el.replaceChildren(renderMarkdown(event.result));
    highlightFences(el);
    attachAnswerTools(el, event.result, lastUserPrompt);
  }
  closeAnswer();
  maybeSpeakReply(); // voice-in → voice-out: auto-read a reply to a dictated message (#97)
  finishTrace();
  if (event.sources && event.sources.length) addSources(event.sources);
  setBusy(false);
  setStatus(null);
  // Settle the view on the response start (the collapsed trace is now smaller);
  // never on the bottom of a long answer.
  if (!replaying) requestAnimationFrame(() => anchorAnswer(true));
  notify("aish — answer ready", event.result);
}

// The server had nothing running for this session when Stop was pressed (#48).
// The foreground may be wedged showing "working" (e.g. a terminal event that
// never landed) — reconcile it to idle quietly: collapse any live trace and
// clear busy WITHOUT the red "task failed" box, red dot, or notification a
// real `error` carries. Stop thus always succeeds instead of dead-ending.
function onStopped() {
  closeAnswer();
  finishTrace();
  setBusy(false);
  setStatus(null);
}

function addSources(sources) {
  const details = document.createElement("details");
  details.className = "sources";
  const summary = document.createElement("summary");
  summary.textContent = `Sources (${sources.length})`;
  details.appendChild(summary);
  for (const source of sources) {
    const row = document.createElement("a");
    row.className = "source-row";
    row.href = source.url;
    row.target = "_blank";
    row.rel = "noopener noreferrer";
    const name = document.createElement("span");
    name.className = "source-name";
    let host = source.url;
    try { host = new URL(source.url).hostname; } catch { /* keep full url */ }
    name.textContent = source.title || host;
    const url = document.createElement("span");
    url.className = "source-url";
    url.textContent = source.url;
    row.append(name, url);
    details.appendChild(row);
  }
  messagesEl.appendChild(details);
  scrollToEnd();
}

function onStatus(event) {
  if (event.state === "idle") { setStatus(null); return; }
  let text = `${event.label || "working"}…`;
  if (event.tokens) text += ` · ↓ ${event.tokens >= 1000 ? (event.tokens / 1000).toFixed(1) + "k" : event.tokens} tokens`;
  setStatus(text);
}

let clientBusy = false;
let pendingCards = 0;
let statusText = "";

function setStatus(text) {
  statusText = text || "";
  refreshStatusline();
}

// Header status dot (#61): red when the socket is down or the last turn
// errored (can't reach/use the model), green + glow while working, green +
// static when connected and idle.
let connOk = false;
let taskErrored = false;
function updateDot() {
  const dot = document.querySelector(".model-dot");
  if (!dot) return;
  const bad = !connOk || taskErrored;
  dot.classList.toggle("bad", bad);
  dot.classList.toggle("working", !bad && clientBusy);
}

function setBusy(busy) {
  clientBusy = busy;
  updateDot();
  refreshStatusline();
}

function refreshStatusline() {
  // Visible whenever the session is working — including parked on an
  // approval card — so Stop is always reachable while something runs. A live
  // activity trace has its own header Stop + status, so suppress the bottom
  // bar then to avoid a duplicate "thinking…" line below the timeline (#10).
  const traceLive = currentTrace && currentTrace.el.classList.contains("live");
  const visible = (clientBusy || Boolean(statusText)) && !traceLive;
  $("statusline").hidden = !visible;
  $("status-text").textContent =
    statusText || (pendingCards > 0 ? "waiting for approval" : "working…");
  $("stop-btn").hidden = !clientBusy;
  // While an approval is pending the model is blocked — no progress is
  // streaming — so let the sticky live-trace yield (CSS) rather than pinning at
  // the top where the approval card would scroll underneath it.
  document.body.classList.toggle("awaiting-approval", pendingCards > 0);
}

$("stop-btn").onclick = () => send({ type: "stop" });

// ---- activity trace ------------------------------------------------------
// One collapsible group per task, built from structured `step` events. Live
// while the task runs (spinner, streaming output into the running step),
// collapsed to a one-line summary when it finishes. Replays deterministically
// because the steps are recorded events like everything else.
let currentTrace = null;

const TRACE_ICONS = {
  thinking: (c) => `<path d="M9 4.5A4 4 0 0 0 5.5 10 3.5 3.5 0 0 0 6 16.5 3.5 3.5 0 0 0 12 18a3.5 3.5 0 0 0 6-1.5A3.5 3.5 0 0 0 18.5 10 4 4 0 0 0 15 4.5a3 3 0 0 0-6 0z" fill="none" stroke="${c}" stroke-width="1.5"/><path d="M12 5v13" stroke="${c}" stroke-width="1.5"/>`,
  knowledge: (c) => `<path d="M12 3.5 14 8.6l5.5.4-4.2 3.6 1.3 5.4L12 15.4 7.4 18l1.3-5.4L4.5 9l5.5-.4z" fill="${c}" stroke="${c}" stroke-width="1" stroke-linejoin="round"/>`,
  web: (c) => `<circle cx="11" cy="11" r="6.5" fill="none" stroke="${c}" stroke-width="1.7"/><path d="M16 16l4 4" stroke="${c}" stroke-width="1.7" stroke-linecap="round"/>`,
  command: (c) => `<path d="M4 17.5V6.5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" fill="none" stroke="${c}" stroke-width="1.6"/><path d="M7.5 9l3 3-3 3M13 15h4" stroke="${c}" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  denied: (c) => `<path d="M4 17.5V6.5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" fill="none" stroke="${c}" stroke-width="1.6"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke="${c}" stroke-width="1.6" stroke-linecap="round"/>`,
  doc: (c) => `<path d="M7 3.5h6.5L18 8v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7 3.5z" fill="none" stroke="${c}" stroke-width="1.6"/><path d="M13 3.5V8h4.5" fill="none" stroke="${c}" stroke-width="1.6"/>`,
  write: (c) => `<path d="M12 19.5h8" stroke="${c}" stroke-width="1.8" stroke-linecap="round"/><path d="M15.5 5.2a1.7 1.7 0 0 1 2.4 2.4l-8.3 8.3-3.2.8.8-3.2z" fill="none" stroke="${c}" stroke-width="1.7" stroke-linejoin="round"/>`,
  check: (c) => `<path d="M5 12.5l4 4 10-10.5" fill="none" stroke="${c}" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>`,
  chat: (c) => `<path d="M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H10l-4 3.5V15.5H6.5a2 2 0 0 1-2-2z" fill="none" stroke="${c}" stroke-width="1.6" stroke-linejoin="round"/>`,
  folder: (c) => `<path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="${c}" stroke-width="1.6"/>`,
  dot: (c) => `<circle cx="12" cy="12" r="3.5" fill="${c}"/>`,
};

function traceSvg(name, color) {
  const build = TRACE_ICONS[name] || TRACE_ICONS.dot;
  return `<svg viewBox="0 0 24 24" width="15" height="15">${build(color)}</svg>`;
}

const SPINNER = '<span class="spin"></span>';

// tool name → (friendly title, icon key, accent css var)
const TOOL_META = {
  run_command: ["run_command", "command", "--green"],
  web_search: ["Searched the web", "web", "--blue"],
  read_url: ["Read a page", "web", "--blue"],
  recall: ["Recalled from memory", "knowledge", "--yellow"],
  read_docs: ["Read docs", "doc", "--dim"],
  read_file: ["read_file", "doc", "--dim"],
  read_skill: ["Read a skill", "knowledge", "--green"],
  write_file: ["write_file", "write", "--green"],
  edit_file: ["edit_file", "write", "--green"],
  remember: ["Saved to memory", "knowledge", "--yellow"],
  forget_memory: ["Forgot a memory", "knowledge", "--yellow"],
};

function ensureTrace() {
  if (currentTrace) return currentTrace;
  const el = document.createElement("div");
  el.className = "trace live open"; // always expanded while the turn runs
  const head = document.createElement("button");
  head.type = "button";
  head.className = "trace-head";
  head.innerHTML =
    `<span class="trace-status">${SPINNER}</span>` +
    `<span class="trace-headtext"><span class="trace-title">Working…</span>` +
    `<span class="trace-sub"></span></span>` +
    `<span class="trace-tokens"></span>` +
    `<button type="button" class="trace-stop"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/></svg>Stop</button>` +
    `<svg class="trace-chev" viewBox="0 0 24 24"><path d="M6 9.5l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const body = document.createElement("div");
  body.className = "trace-body";
  // Steps live in an inner content div so the timeline rail spans the FULL
  // (scrollable) content, not just the visible slice.
  body.innerHTML = '<div class="trace-inner"><div class="trace-rail"></div></div>';
  el.append(head, body);
  const t = {
    el, head, body, inner: body.querySelector(".trace-inner"),
    started: 0, secs: 0, tokensIn: 0, tokensOut: 0,
    pending: null, thinkingRow: null, startedAt: Date.now(), timer: null,
    autoCollapsed: false, // collapsed by an approval card, to be restored after
  };
  // The head toggles expand freely — even while the turn runs (#65). A manual
  // toggle is the user's choice, so clear any pending auto-restore so the
  // approval-resolved handler won't fight them by re-expanding.
  head.onclick = (e) => {
    if (e.target.closest(".trace-stop")) return;
    el.classList.toggle("open");
    t.autoCollapsed = false;
  };
  head.querySelector(".trace-stop").onclick = (e) => {
    e.stopPropagation();
    send({ type: "stop" });
    markStopping(currentTrace); // immediate "Stopping…" feedback in the header
  };
  messagesEl.appendChild(el);
  turnAnchorEl = el; // the "Worked for Xs" box is the response-start anchor
  currentTrace = t;
  body.addEventListener("scroll", () => updateScrollHints(body));
  currentTrace.timer = setInterval(() => updateTraceHead(currentTrace), 1000);
  refreshStatusline(); // the trace header owns Stop now; hide the bottom bar
  scrollToEnd();
  return currentTrace;
}

// The Stop button was pressed: reflect it in the header until `done` lands.
function markStopping(t) {
  if (!t) return;
  t.el.classList.add("stopping");
  const title = t.el.querySelector(".trace-title");
  if (title) title.textContent = "Stopping…";
  const btn = t.el.querySelector(".trace-stop");
  if (btn) btn.disabled = true;
}

// Fade the top/bottom edge of a scroll box when there's more content there.
function updateScrollHints(box) {
  box.classList.toggle("more-above", box.scrollTop > 4);
  box.classList.toggle("more-below", box.scrollTop + box.clientHeight < box.scrollHeight - 4);
}

// Keep the newest step visible inside the height-capped live steps pane.
function pinTrace(t) {
  if (t.el.classList.contains("live")) {
    requestAnimationFrame(() => {
      t.body.scrollTop = t.body.scrollHeight;
      updateScrollHints(t.body);
    });
  }
}

// A live "how long has THIS step run" timer on the active row.
function startStepTimer(t, ref) {
  const timer = document.createElement("span");
  timer.className = "step-timer";
  timer.textContent = "0s";
  ref.titleEl.appendChild(timer);
  t.activeStartedAt = Date.now();
}
function clearStepTimer(t, ref) {
  const el = ref.titleEl.querySelector(".step-timer");
  if (el) el.remove();
  t.activeStartedAt = null;
}

function traceRow(t, iconHtml, title, sub) {
  const row = document.createElement("div");
  row.className = "step";
  const badge = document.createElement("span");
  badge.className = "step-badge";
  badge.innerHTML = iconHtml;
  const main = document.createElement("div");
  main.className = "step-main";
  const titleEl = document.createElement("span");
  titleEl.className = "step-title";
  titleEl.append(title); // string or node
  main.appendChild(titleEl);
  if (sub) {
    const subEl = document.createElement("span");
    subEl.className = "step-sub";
    subEl.textContent = sub;
    main.appendChild(subEl);
  }
  row.append(badge, main);
  t.inner.appendChild(row);
  scrollToEnd();
  pinTrace(t);
  return { row, badge, main, titleEl };
}

function traceStep(step) {
  const t = ensureTrace();
  if (step.kind === "thinking_start") {
    // A live, highlighted "Thinking…" row — the active step on the timeline.
    if (!t.thinkingRow) {
      t.started += 1;
      const ref = traceRow(t, '<span class="spin spin-purple"></span>', "Thinking…", "");
      ref.row.classList.add("running", "active-step");
      startStepTimer(t, ref);
      t.thinkingRow = ref;
    }
    updateTraceHead(t);
    return;
  }
  if (step.kind === "thinking_cancel") {
    // A plain answer needs no thinking row — but if the answer already streamed
    // into it (relabeled "Answering…"), keep it as a finalized "Answered" step
    // instead of dropping it.
    // A text-only turn never emits a "thinking" step (that only fires when the
    // turn has tool calls), so this is the only place its token usage lands —
    // without it the "↑N ↓M tokens" header goes missing on plain answers (#84).
    if (step.tokens) { t.tokensIn += step.tokens[0] || 0; t.tokensOut += step.tokens[1] || 0; }
    if (t.thinkingRow) {
      if (t.thinkingRow.isAnswer) finalizeAnswerRow(t, t.thinkingRow, step.secs);
      else { t.thinkingRow.row.remove(); t.started -= 1; }
      t.thinkingRow = null;
    }
    updateTraceHead(t);
    return;
  }
  if (step.kind === "thinking") {
    t.secs += step.secs || 0;
    if (step.tokens) { t.tokensIn += step.tokens[0] || 0; t.tokensOut += step.tokens[1] || 0; }
    if (t.thinkingRow) { // finalize the live row in place
      const ref = t.thinkingRow;
      clearStepTimer(t, ref);
      ref.row.classList.remove("running", "active-step");
      ref.badge.innerHTML = traceSvg("thinking", "var(--purple)");
      ref.titleEl.textContent = `Thought for ${fmtSecs(step.secs)}`;
      t.thinkingRow = null;
    } else {
      t.started += 1;
      traceRow(t, traceSvg("thinking", "var(--purple)"), `Thought for ${fmtSecs(step.secs)}`, "");
    }
    updateTraceHead(t);
    return;
  }
  if (step.kind === "knowledge") {
    t.started += 1;
    const items = step.items || [];
    const nSkill = items.filter((i) => i.kind === "skill").length;
    const nMem = items.length - nSkill;
    const parts = [];
    if (nSkill) parts.push(`${nSkill} skill${nSkill === 1 ? "" : "s"}`);
    if (nMem) parts.push(`${nMem} ${nMem === 1 ? "memory" : "memories"}`);
    const { main } = traceRow(
      t, traceSvg("knowledge", "var(--yellow)"), "Recalled knowledge",
      `${parts.join(" · ") || items.length} from past work`
    );
    if (items.length) {
      const chips = document.createElement("div");
      chips.className = "know-chips";
      for (const it of items) {
        const isSkill = it.kind === "skill";
        const chip = document.createElement("span");
        chip.className = "know-chip " + (isSkill ? "skill" : "mem");
        const tag = document.createElement("span");
        tag.className = "know-tag";
        tag.textContent = isSkill ? "SKILL" : "MEM";
        chip.append(tag, document.createTextNode(it.label || ""));
        chips.appendChild(chip);
      }
      main.appendChild(chips);
    }
    updateTraceHead(t);
    return;
  }
  if (step.kind === "injected") {
    // A message the user typed mid-task (issue #95): injected as steering into
    // the running task, not deferred as a separate follow-up. Render a distinct
    // note in the trace and retire its queued chip (a no-op on cold replay,
    // where no chip exists). Emitted identically live and by reconstruct_events.
    t.started += 1;
    traceRow(t, traceSvg("chat", "var(--blue)"), "You added", step.text || "");
    removeQueueChip(step.text);
    updateTraceHead(t);
    return;
  }
  if (step.kind === "tool_start") { toolStart(t, step); return; }
  if (step.kind === "tool") { toolFinish(t, step); return; }
}

// A "SKILL" pill on read_skill rows, mirroring the knowledge-preload chips so
// recalling a specific skill reads consistently with them (vs. memory).
function knowledgeTag(ref, name) {
  if (name !== "read_skill") return;
  const tag = document.createElement("span");
  tag.className = "step-tag know";
  tag.textContent = "SKILL";
  ref.titleEl.appendChild(tag);
}

function toolStart(t, step) {
  t.started += 1;
  const [title, iconKey] = TOOL_META[step.name] || [step.name, "dot", "--dim"];
  const ref = traceRow(t, SPINNER, title, step.name === "run_command" ? "" : step.summary);
  knowledgeTag(ref, step.name);
  ref.row.classList.add("running", "active-step");
  startStepTimer(t, ref);
  // The command + output + exit for run_command are drawn by the terminal
  // block that command_start builds once the command is approved and runs;
  // while the approval card is up the row is just the spinner.
  t.pending = { ...ref, name: step.name };
}

function toolFinish(t, step) {
  t.secs += step.secs || 0;
  const meta = TOOL_META[step.name] || [step.name, "dot", "--dim"];
  const denied = step.decision === "denied" || step.decision === "blocked" || step.decision === "rejected";
  // Approve + comment holds the action for adjustment (#81): it did not run,
  // so it renders in the same "not executed" style as a denial, but amber and
  // labelled "Held" — it is a pause, not a failure.
  const held = step.decision === "held";
  const notRun = denied || held;
  let ref = t.pending && t.pending.name === step.name ? t.pending : null;
  if (!ref) {
    // No matching start (e.g. replay ordering): synthesize a completed row.
    t.started += 1;
    ref = traceRow(t, "", meta[0], step.name === "run_command" ? "" : step.summary);
    knowledgeTag(ref, step.name);
    if (step.name === "run_command" && step.command) {
      const cmd = document.createElement("div");
      cmd.className = "step-cmd mono";
      cmd.textContent = step.command;
      ref.main.appendChild(cmd);
    }
  }
  clearStepTimer(t, ref);
  t.pending = null;
  ref.row.classList.remove("running", "active-step");
  // finalize badge icon
  const iconName = held ? "command" : denied ? "denied" : step.name === "run_command" ? "command"
    : !step.ok ? "denied" : meta[1];
  const color = held ? "var(--orange)" : denied || !step.ok ? "var(--red)" : `var(${meta[2]})`;
  ref.badge.innerHTML = traceSvg(iconName, color);
  // status tag on the title
  const tag = document.createElement("span");
  tag.className = "step-tag " + (held ? "held" : denied || !step.ok ? "bad" : "ok");
  tag.textContent = held ? "Held — adjust"
    : denied ? (step.decision === "blocked" ? "Blocked" : "Denied")
    : !step.ok ? "Error"
    : step.name === "run_command" ? `${ref.manual ? "Approved" : "Auto-approved"} · ${fmtSecs(step.secs)}`
    : fmtSecs(step.secs);
  ref.titleEl.appendChild(tag);
  // The user's approval note, shown back on the step (#3), clamped when long.
  if (step.comment) ref.main.appendChild(clampNote(step.comment));
  if (notRun) {
    // A denied/blocked/held command never runs, so no terminal block is built
    // — show the command struck-through here with the reason it was skipped.
    if (step.name === "run_command" && step.command && !ref.row.querySelector(".step-cmd")) {
      const cmd = document.createElement("div");
      cmd.className = "step-cmd mono struck";
      cmd.textContent = step.command;
      ref.main.appendChild(cmd);
    }
    // A denied write/edit never reached disk — the diff shown on the approval
    // card was NOT applied. Say so plainly, mirroring the struck command above
    // so the timeline doesn't read the change as written (#67).
    if (step.name === "write_file" || step.name === "edit_file") {
      const skipped = document.createElement("span");
      skipped.className = "step-sub";
      skipped.textContent = "Change not applied";
      ref.main.appendChild(skipped);
    }
    // Why it was skipped/blocked (denial comment, gate reason) — #5, #12.
    if (step.output) {
      const why = document.createElement("span");
      why.className = "step-sub";
      why.textContent = step.output;
      ref.main.appendChild(why);
    }
  }
  // The diff of a file edit, shown inline in the timeline (#55): what was
  // written for an applied edit, or (dimmed, under "Change not applied") what
  // was proposed for a denied/held one. Reuses the approval card's renderer so
  // the styling matches, and works identically live and on cold replay since
  // the step carries the same diff the card computed.
  if ((step.name === "write_file" || step.name === "edit_file") && step.diff) {
    const d = renderDiff(step.diff);
    d.classList.add("step-diff");
    if (notRun) d.classList.add("not-applied");
    ref.main.appendChild(d);
  }
  // An executed run_command's output lives in the terminal block that
  // command_start/command_end drew and finalized — live AND on cold replay,
  // where reconstruct_events replays the same framing events. So there is
  // nothing to render here, and no framing-less fallback path to diverge.
  // error detail for a failed non-run_command tool (#18)
  if (!step.ok && step.error && step.name !== "run_command") {
    const errWrap = document.createElement("div");
    errWrap.className = "step-output";
    ref.main.appendChild(errWrap);
    renderErrorBox(errWrap, step.error);
  }
  updateTraceHead(t);
}

const WRAP_SVG = '<svg viewBox="0 0 24 24"><path d="M4 6.5h16M4 12h12a3.25 3.25 0 0 1 0 6.5h-2.5m0 0 2.2-2.2m-2.2 2.2 2.2 2.2M4 18.5h5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// The collapsible command-output box: header (label · wrap · copy), a scrolling
// body, and a "Show full output · N lines" expander when it's tall.
function outBox(errorMode) {
  const box = document.createElement("div");
  box.className = "out-box" + (errorMode ? " error" : "");
  box.innerHTML =
    `<div class="out-head"><span class="out-label">${errorMode ? "error" : "output"}</span>` +
    `<div class="out-actions"><button type="button" class="out-wrap" title="Wrap lines">${WRAP_SVG}</button></div></div>` +
    `<div class="out-scroll"><div class="out-body mono"></div><div class="out-fade"></div></div>` +
    `<button type="button" class="out-expand" hidden></button>`;
  const body = box.querySelector(".out-body");
  box.querySelector(".out-wrap").onclick = () => box.classList.toggle("wrap-on");
  box.querySelector(".out-actions").prepend(copyChip(() => body.textContent, "copy output"));
  const scroll = box.querySelector(".out-scroll");
  box.querySelector(".out-expand").onclick = () => {
    box.classList.toggle("expanded"); labelExpand(box);
    requestAnimationFrame(() => updateScrollHints(scroll));
  };
  scroll.addEventListener("scroll", () => updateScrollHints(scroll));
  return box;
}

function outLines(box) {
  return (box.querySelector(".out-body").textContent.match(/\n/g) || []).length + 1;
}
function labelExpand(box) {
  box.querySelector(".out-expand").textContent =
    box.classList.contains("expanded") ? "Collapse output" : `Show full output · ${outLines(box)} lines`;
}
function finalizeOutBox(box, label) {
  if (label) box.querySelector(".out-label").textContent = label;
  if (outLines(box) > 6) { box.classList.add("collapsible"); box.querySelector(".out-expand").hidden = false; labelExpand(box); }
  requestAnimationFrame(() => updateScrollHints(box.querySelector(".out-scroll")));
}

// Peel a trailing "[exit code: N]" into the header label.
// A run_command approval note on the finished step: clamp long / multi-line
// text to a few lines with a Show more/less toggle, mirroring the output box's
// graceful handling instead of ellipsizing to a single line.
function clampNote(text) {
  const wrap = document.createElement("div");
  wrap.className = "step-note-wrap";
  const note = document.createElement("span");
  note.className = "step-sub step-note";
  note.textContent = `“${text}”`;
  wrap.appendChild(note);
  const more = document.createElement("button");
  more.type = "button";
  more.className = "note-more";
  more.textContent = "Show more";
  more.hidden = true;
  more.onclick = () => {
    const expanded = wrap.classList.toggle("expanded");
    more.textContent = expanded ? "Show less" : "Show more";
  };
  wrap.appendChild(more);
  requestAnimationFrame(() => {
    if (note.scrollHeight - note.clientHeight > 4) more.hidden = false;
  });
  return wrap;
}

function renderErrorBox(container, text) {
  container.replaceChildren();
  const box = outBox(true);
  box.querySelector(".out-body").appendChild(ansiFragment(text));
  container.appendChild(box);
  finalizeOutBox(box, "error");
}

// ---- terminal block (run_command) ---------------------------------------
// A single black terminal panel per executed command: a pinned prompt line
// (dir$ command), a rule, the ANSI output (capped with a "Show all" expander
// once tall), a rule, and a pinned exit-code line. command_start builds it,
// stream events fill the output live, command_end sets the exit label. The
// framing events are recorded, so a session replay reconstructs it identically.

const TERM_OUTPUT_CAP_VH = 40;

// Live DOM cap (issue #109): a command with tens of thousands of output lines
// would append one node + reflow per line, freezing the tab. Keep only the last
// N lines in the live terminal block; the full (truncated) result still arrives
// on the tool step. Each line is wrapped in its own `.tol` span so lines are
// cheap to count and trim regardless of the ANSI nodes inside them.
const TERM_LIVE_LINE_CAP = 800;

// The prompt-line directory, Starship [directory]-style: keep the last
// DIR_SEGMENTS path segments, prefixed with "…/" when anything was truncated
// (repo root is not special — truncate_to_repo=false). Home is shown as ~.
const DIR_SEGMENTS = 4;
function promptDir(cwd) {
  let p = (cwd || "").replace(/\/+$/, "");
  if (!p) return "/";
  if (homeDir && p === homeDir) return "~";
  if (homeDir && p.startsWith(homeDir + "/")) p = "~" + p.slice(homeDir.length);
  if (p === "/" || p === "~") return p;
  const home = p.startsWith("~");
  const segs = p.split("/").filter(Boolean);
  if (segs.length <= DIR_SEGMENTS) return home ? segs.join("/") : "/" + segs.join("/");
  return "…/" + segs.slice(-DIR_SEGMENTS).join("/");
}

function termRule(cls) {
  const r = document.createElement("div");
  r.className = "term-rule " + cls;
  return r;
}

// A wrap toggle for a terminal zone: highlights while that zone is wrapped.
// `on` is the zone's default wrap state (command wraps by default, output
// doesn't), so the button's lit state always matches what the eye sees.
// Toggling wrap reflows the output height, so anchor `anchorSel`'s top to the
// same viewport position afterward — the content you were looking at stays put
// instead of jumping — and re-measure the cap for the new line count.
function termWrapBtn(block, anchorSel, toggle, on) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "term-tool term-wrap" + (on ? " on" : "");
  b.title = "Wrap lines";
  b.innerHTML = WRAP_SVG;
  b.onclick = () => {
    const anchor = block.querySelector(anchorSel);
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    b.classList.toggle("on");
    toggle();
    recomputeTermCap(block);
    if (anchor) messagesEl.scrollTop += anchor.getBoundingClientRect().top - before;
  };
  return b;
}

// The global top-bar wrap toggle overrides every command block: force each
// block's output wrap (and its button's lit state) to the global value, then
// re-cap for the new line count. New blocks seed from the same global value at
// build time; a per-block toggle diverges until the next global change.
function syncTermWrap(on) {
  for (const block of document.querySelectorAll(".term-block")) {
    block.classList.toggle("term-owrap", on);
    const btn = block.querySelector(".term-out-wrap .term-wrap");
    if (btn) btn.classList.toggle("on", on);
    recomputeTermCap(block);
  }
}

// The global top-bar wrap toggle also overrides every diff card: force each
// diff's soft-wrap and its button's lit state to the global value. New diffs
// seed from the same global value at build time; a per-card toggle diverges
// until the next global change (same contract as syncTermWrap).
function syncDiffWrap(on) {
  for (const diff of document.querySelectorAll(".diff")) {
    diff.classList.toggle("diff-soft", on);
    const btn = diff.parentElement && diff.parentElement.querySelector(".diff-wrap-btn");
    if (btn) btn.classList.toggle("on", on);
  }
}

// (Re)decide whether the output needs the "Show all" cap for its current
// height — used at command_end and after a wrap toggle changes the line count.
function recomputeTermCap(block) {
  if (block.classList.contains("expanded")) return;
  const out = block.querySelector(".term-out");
  const hasOutput = out.textContent.trim() !== "";
  const cap = (window.innerHeight * TERM_OUTPUT_CAP_VH) / 100;
  block.classList.toggle("capped", hasOutput && out.scrollHeight > cap + 12);
}

function buildTermBlock(cwd, command) {
  const block = document.createElement("div");
  block.className = "term-block running";

  // The prompt line scrolls horizontally in nowrap mode; its tools live on the
  // non-scrolling wrapper so they stay pinned top-right instead of sliding away.
  const promptWrap = document.createElement("div");
  promptWrap.className = "term-prompt-wrap";
  const prompt = document.createElement("div");
  prompt.className = "term-prompt mono";
  const dir = document.createElement("span");
  dir.className = "term-dir";
  dir.textContent = promptDir(cwd);
  dir.title = cwd || "";
  const dollar = document.createElement("span");
  dollar.className = "term-dollar";
  dollar.textContent = "$";
  const cmd = document.createElement("span");
  cmd.className = "term-cmd";
  cmd.textContent = command || "";
  prompt.append(dir, dollar, cmd);
  // Command tools: copy grabs the COMMAND only (no dir/$ prompt); wrap toggles
  // the prompt line between wrapping and single-line horizontal scroll.
  const cmdTools = document.createElement("div");
  cmdTools.className = "term-tools";
  cmdTools.append(
    termWrapBtn(block, ".term-prompt", () => block.classList.toggle("term-cnowrap"), true),
    copyChip(() => cmd.textContent, "copy command"),
  );
  promptWrap.append(prompt, cmdTools);

  const outWrap = document.createElement("div");
  outWrap.className = "term-out-wrap";
  const out = document.createElement("div");
  out.className = "term-out mono";
  const fade = document.createElement("div");
  fade.className = "term-fade";
  // Output tools: copy grabs the OUTPUT only (the two rules reinforce that the
  // prompt line and exit code aren't part of it); wrap soft-wraps the output.
  // Seed the wrap state from the global top-bar wrap preference, but the
  // per-block toggle then owns it independently (the term block ignores the
  // global body.wrap so this button can always override it).
  const outWrapped = document.body.classList.contains("wrap");
  if (outWrapped) block.classList.add("term-owrap");
  const outTools = document.createElement("div");
  outTools.className = "term-tools";
  outTools.append(
    termWrapBtn(block, ".term-out", () => block.classList.toggle("term-owrap"), outWrapped),
    copyChip(() => out.textContent, "copy output"),
  );
  outWrap.append(outTools, out, fade);

  const showall = document.createElement("button");
  showall.type = "button";
  showall.className = "term-showall";
  showall.textContent = "Show all output";
  showall.onclick = () => {
    const on = block.classList.toggle("expanded");
    showall.textContent = on ? "Show less" : "Show all output";
  };

  const exit = document.createElement("div");
  exit.className = "term-exit mono";
  const label = document.createElement("span");
  label.className = "term-exit-label";
  label.innerHTML = SPINNER + '<span class="term-exit-cap">running</span>';
  exit.appendChild(label);

  block.append(promptWrap, termRule("term-rule-top"), outWrap, showall,
    termRule("term-rule-bot"), exit);
  return block;
}

// A user-typed ! command runs directly (not model work), so its terminal block
// renders inline in the transcript rather than inside the activity trace. This
// holds that standalone block while its output streams (#51 follow-up).
let userCmdBlock = null;

function onCommandStart(event) {
  const block = buildTermBlock(event.cwd, event.command);
  if (event.user) {
    // Direct user command: stand it in the main chat, no trace wrapper, and
    // expanded by default — the user ran it and wants to see the whole outcome
    // to decide what's next, not a capped "Show all output" preview.
    block.classList.add("expanded");
    messagesEl.appendChild(block);
    userCmdBlock = block;
    scrollToEnd();
    return;
  }
  const t = ensureTrace();
  const pending = t.pending && t.pending.name === "run_command" ? t.pending : null;
  if (pending) {
    pending.main.appendChild(block);
    pending.term = block;
  } else {
    // No matching run_command row (unusual replay ordering): synthesize one.
    const ref = traceRow(t, traceSvg("command", "var(--green)"), "run_command", "");
    ref.main.appendChild(block);
    t.pending = { ...ref, name: "run_command", term: block };
  }
  scrollToEnd();
  pinTrace(t);
}

function onCommandEnd(event) {
  const block = userCmdBlock || (currentTrace && currentTrace.pending && currentTrace.pending.term);
  if (block) finalizeTermBlock(block, event);
  userCmdBlock = null;
}

function finalizeTermBlock(block, event) {
  block.classList.remove("running");
  const out = block.querySelector(".term-out");
  const hasOutput = out.textContent.trim() !== "";
  if (!hasOutput) block.classList.add("no-output"); // collapse the middle zone

  // A dim uppercase caption + a colored value, so the status line never reads
  // like part of the command (the old bare "exit 0" did). ok/bad color the
  // value only.
  const label = block.querySelector(".term-exit-label");
  let cls, cap, val;
  if (event.status === "detached") {
    cls = "detached"; cap = "job"; val = event.job ? `pid ${event.job}` : "detached";
  } else if (event.status === "interrupted") {
    cls = "bad"; cap = "status"; val = "interrupted";
  } else if (typeof event.exit_code === "number") {
    cls = event.exit_code === 0 ? "ok" : "bad"; cap = "exit code"; val = String(event.exit_code);
  } else {
    cls = "bad"; cap = "status"; val = "error"; // e.g. the command never started
  }
  label.className = "term-exit-label";
  label.replaceChildren();
  const capEl = document.createElement("span");
  capEl.className = "term-exit-cap";
  capEl.textContent = cap;
  const valEl = document.createElement("span");
  valEl.className = "term-exit-val " + cls;
  valEl.textContent = val;
  label.append(capEl, valEl);

  // Cap tall output with a "Show all" expander instead of an inner scroll
  // region — expanding flows into the page's own scroll (iOS-safe). Measured
  // synchronously here, not in a rAF: command_end is processed just before the
  // turn's "done" collapses the trace (display:none), which would zero out
  // scrollHeight and defeat the check.
  recomputeTermCap(block);
}

function traceStream(text) {
  // While a run_command is live, its output streams into the terminal block
  // command_start built — the standalone user-command block if one is live,
  // else the model's trace block; otherwise (no active block) it renders inline.
  const term = userCmdBlock || (currentTrace && currentTrace.pending && currentTrace.pending.term);
  if (term) {
    appendTermLines(term.querySelector(".term-out"), text);
    term.classList.add("has-output");
    // Coalesce scroll+pin to one per frame (issue #109): N stream events in a
    // single tick otherwise trigger N layout reflows.
    scheduleStreamRender(currentTrace);
    return;
  }
  addStreamLine(text);
}

// Append output to a terminal block, one `.tol` span per line, and enforce the
// live DOM cap. The backend coalesces lines, so `text` may carry several joined
// by '\n' (cold replay hands the whole truncated output as one chunk) — split
// so trimming stays line-accurate either way.
function appendTermLines(body, text) {
  for (const line of text.split("\n")) {
    if (body.childNodes.length) body.appendChild(document.createTextNode("\n"));
    const tol = document.createElement("span");
    tol.className = "tol";
    tol.appendChild(ansiFragment(line));
    body.appendChild(tol);
  }
  capTermLines(body);
}

function capTermLines(body) {
  const lines = body.querySelectorAll(".tol");
  const excess = lines.length - TERM_LIVE_LINE_CAP;
  if (excess <= 0) return;
  for (let i = 0; i < excess; i++) {
    const tol = lines[i];
    const nl = tol.nextSibling; // the "\n" text node that separated it
    tol.remove();
    if (nl && nl.nodeType === 3 && nl.textContent === "\n") nl.remove();
  }
  // One muted marker so the user knows output was dropped (the full truncated
  // result is on the tool step below). Prepended once, then kept at the top.
  if (!body.querySelector(".term-trim-note")) {
    const note = document.createElement("span");
    note.className = "term-trim-note a-dim";
    note.textContent = "… earlier output trimmed (see full result below) …";
    body.insertBefore(document.createTextNode("\n"), body.firstChild);
    body.insertBefore(note, body.firstChild);
  }
}

// One scroll + pin per animation frame no matter how many stream events land in
// the frame; slow output still autoscrolls (a lone event schedules its own rAF).
let streamRenderTrace = null;
let streamRenderScheduled = false;
function scheduleStreamRender(trace) {
  if (trace) streamRenderTrace = trace;
  if (streamRenderScheduled) return;
  streamRenderScheduled = true;
  requestAnimationFrame(() => {
    streamRenderScheduled = false;
    const trace = streamRenderTrace;
    streamRenderTrace = null;
    scrollToEnd();
    if (trace) pinTrace(trace);
  });
}

function mmss(sec) {
  const m = Math.floor(sec / 60);
  return `${m}:${String(sec % 60).padStart(2, "0")}`;
}

function updateTraceHead(t) {
  const title = t.el.querySelector(".trace-title");
  const sub = t.el.querySelector(".trace-sub");
  const tokens = t.el.querySelector(".trace-tokens");
  const live = t.el.classList.contains("live");
  if (live) {
    title.textContent = "Working…";
    const elapsed = Math.floor((Date.now() - t.startedAt) / 1000);
    sub.textContent = `step ${t.started} · ${mmss(elapsed)}`;
    if (t.activeStartedAt) {
      const st = t.body.querySelector(".step.active-step .step-timer");
      if (st) st.textContent = `${Math.floor((Date.now() - t.activeStartedAt) / 1000)}s`;
    }
  } else {
    title.textContent = `Worked for ${fmtSecs(t.secs)}`;
    sub.textContent = `${t.started} step${t.started === 1 ? "" : "s"}`;
  }
  const parts = [];
  if (t.tokensIn) parts.push("↑" + fmtTokens(t.tokensIn));
  if (t.tokensOut) parts.push("↓" + fmtTokens(t.tokensOut));
  tokens.textContent = parts.join(" ");
}

// The answer streamed into this (formerly "Thinking…") row — finalize it as a
// permanent "Answered" step instead of dropping the live row, so the last step
// on the timeline reflects that the reply landed.
function finalizeAnswerRow(t, ref, secs) {
  clearStepTimer(t, ref);
  ref.row.classList.remove("running", "active-step");
  ref.badge.innerHTML = traceSvg("check", "var(--green)");
  ref.titleEl.textContent =
    typeof secs === "number" ? `Answered in ${fmtSecs(secs)}` : "Answered";
}

function finishTrace(errored) {
  if (!currentTrace) return;
  const t = currentTrace;
  if (t.timer) { clearInterval(t.timer); t.timer = null; }
  if (t.thinkingRow) {
    if (t.thinkingRow.isAnswer) finalizeAnswerRow(t, t.thinkingRow);
    else t.thinkingRow.row.remove();
    t.thinkingRow = null;
  }
  t.pending = null;
  t.activeStartedAt = null;
  currentTrace = null;
  // Finalize any step still spinning — a tool cut off by a server restart
  // mid-run (the "co to czarna dziura?" deploy bug) leaves a running row with
  // no finish event; a closed trace must never keep a perpetual spinner.
  t.body.querySelectorAll(".step.running").forEach((row) => {
    row.classList.remove("running", "active-step");
    const badge = row.querySelector(".step-badge");
    if (badge) badge.innerHTML = traceSvg("denied", "var(--dim)");
    const timer = row.querySelector(".step-timer");
    if (timer) timer.remove();
    const main = row.querySelector(".step-main");
    if (main && !main.querySelector(".step-interrupted")) {
      const note = document.createElement("span");
      note.className = "step-sub step-interrupted";
      note.textContent = "interrupted";
      main.appendChild(note);
    }
  });
  // A pure-answer turn leaves no steps — drop the empty trace box entirely,
  // unless it still carries token usage (#84): that's the only place those
  // counts are shown, so an empty-but-billed turn must keep its header.
  if (!t.body.querySelector(".step") && !t.tokensIn && !t.tokensOut) {
    t.el.remove();
    refreshStatusline();
    return;
  }
  t.el.classList.remove("live", "stopping");
  t.el.classList.remove("open"); // collapse to the summary; tap to expand
  t.el.querySelector(".trace-status").innerHTML = errored
    ? traceSvg("denied", "var(--red)")
    : traceSvg("check", "var(--green)");
  updateTraceHead(t);
  refreshStatusline();
}

function fmtSecs(s) {
  if (s == null) return "";
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}
function fmtTokens(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}

// ---- message rendering ---------------------------------------------------
function addMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollToEnd();
  return el;
}

// The prompt that started the current turn, so an error (or a finished answer)
// can offer to re-run it.
let lastUserPrompt = "";
const RERUN_SVG =
  '<svg viewBox="0 0 24 24"><path d="M5 6.5v3.6h3.6M19 17.5v-3.6h-3.6M18.4 9.2A6.5 6.5 0 0 0 6.5 8M5.6 14.8A6.5 6.5 0 0 0 17.5 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// Regenerate the last answer from scratch (#60): `retry` re-runs the prompt AND
// discards the previous attempt from the model's context, the log, and the
// transcript, so the rerun is not anchored to the old (likely wrong) answer. The
// server cancels a still-running or wedged turn first, so this is safe in every
// state: idle, busy, or stuck after a disruption. The rolled-back transcript
// comes back as a fresh `replay`, so the discarded answer disappears in place.
function rerunPrompt(prompt) {
  if (!prompt) return;
  if (clientBusy) { showToast("can't rerun while working"); return; }
  send({ type: "retry", text: prompt });
}

// An error message with a Retry button (resends the last prompt, Gemini-style).
function addErrorMsg(text) {
  const wrap = document.createElement("div");
  wrap.className = "msg error error-wrap";
  const body = document.createElement("div");
  body.textContent = text;
  wrap.appendChild(body);
  if (lastUserPrompt) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "retry-btn";
    retry.innerHTML = RERUN_SVG + "Retry";
    retry.onclick = () => rerunPrompt(lastUserPrompt);
    wrap.appendChild(retry);
  }
  messagesEl.appendChild(wrap);
  scrollToEnd();
  return wrap;
}

// A regenerate control lives only on the MOST RECENT answer (re-runs the last
// prompt as a fresh turn). Branching an arbitrary earlier message is a separate
// feature; this one supersedes itself so only the latest answer carries it.
let lastRegenBtn = null;
function retireRegen() {
  if (lastRegenBtn) { lastRegenBtn.remove(); lastRegenBtn = null; }
}

// Your own prompt bubble: tap-to-recall plus a copy chip underneath (issue
// #39). Copy hands back the prompt minus attachment notes — same text the
// recall paths reuse.
// A compact audit marker for a user-driven workspace change (issue #94):
// "Working directory → <path>" on a /cd, "Trusted <path>" on a dir trust. Same
// row whether emitted live (on_state) or replayed (reconstruct_events). The
// folder glyph is static markup; the path is user data, so it goes in via
// textContent.
function addWorkspaceNote(change, path) {
  const label = change === "cwd" ? "Working directory" : "Trusted directory";
  // During a task, show it INLINE in the activity trace at the moment it
  // happened — exactly like the "You added" steering note — so it sits on the
  // timeline where it occurred, not as a detached row after the answer (#94/#95).
  if (currentTrace) {
    currentTrace.started += 1;
    traceRow(currentTrace, traceSvg("folder", "var(--blue)"), label, abbreviatePath(path));
    updateTraceHead(currentTrace);
    scrollToEnd();
    return;
  }
  // Idle (no live trace): a standalone note, still styled like "You added".
  const el = document.createElement("div");
  el.className = "msg workspace-note";
  const ico = document.createElement("span");
  ico.className = "wsnote-ico";
  ico.innerHTML = FOLDER_SVG;
  const lab = document.createElement("span");
  lab.className = "wsnote-label";
  lab.textContent = label;
  const val = document.createElement("span");
  val.className = "wsnote-path mono";
  val.textContent = abbreviatePath(path);
  el.append(ico, lab, val);
  messagesEl.appendChild(el);
  scrollToEnd();
  return el;
}

function addUserMsg(text) {
  const el = addMsg("user", text);
  const tools = document.createElement("div");
  tools.className = "user-tools";
  const getText = () => stripAttachmentNotes(el.textContent);
  tools.append(reuseChip(getText), copyChip(getText, "copy prompt"));
  messagesEl.appendChild(tools);
  return el;
}

function addAnsiMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.appendChild(ansiFragment(text));
  messagesEl.appendChild(el);
  scrollToEnd();
  return el;
}

// Consecutive stream lines share one block so the output scrolls sideways as a
// whole; any other message ending up last (echo, answer, card) starts a new one.
// The block sits in a wrapper so its copy chip stays put while the pre-formatted
// content scrolls sideways underneath.
function addStreamLine(text) {
  const last = messagesEl.lastElementChild;
  if (last && last.classList.contains("stream-wrap")) {
    const body = last.querySelector(".stream");
    body.appendChild(document.createTextNode("\n"));
    body.appendChild(ansiFragment(text));
    scrollToEnd();
    return body;
  }
  const wrap = document.createElement("div");
  wrap.className = "stream-wrap";
  const body = document.createElement("div");
  body.className = "msg stream";
  body.appendChild(ansiFragment(text));
  wrap.append(copyChip(() => body.textContent, "copy output"), body);
  messagesEl.appendChild(wrap);
  scrollToEnd();
  return body;
}

function nearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;
}

function scrollToEnd(force) {
  if (force || nearBottom()) messagesEl.scrollTop = messagesEl.scrollHeight;
  updateScrollButton();
  updateEmptyHint(); // every content-adding path funnels through here
}

// Empty-state welcome hero (#123): shown only while the transcript is empty.
function updateEmptyHint() {
  // A workspace-note (a UI /cd or dir-trust marker) is system metadata, not a
  // real turn — a fresh chat is still "empty" for onboarding after one, so the
  // welcome hero stays until the user actually sends a message (#135).
  const empty = [...messagesEl.children].every((c) =>
    c.classList.contains("workspace-note")
  );
  $("welcome").hidden = !empty; // brand hero on a fresh/empty chat (#123)
}

// iOS Safari settles keyboard-driven layout changes a beat after the gesture;
// the second pass lands the view at the true bottom once heights are final.
function scrollToEndSettled() {
  scrollToEnd(true);
  setTimeout(() => scrollToEnd(true), 120);
}

// Keyboard show/hide resizes the viewport; if the user was reading the tail,
// keep them pinned to it instead of leaving the bottom hidden. iOS standalone
// additionally pans the layout viewport up to reveal the focused input and
// often forgets to pan back on dismissal, leaving a blank band at the bottom
// (#8) — once the visual viewport is full-height again (keyboard gone), snap
// the window home. Never snap while the keyboard is up: the pan is what keeps
// the composer visible above it.
// Whether an editable element has focus. iOS resizes innerHeight while the
// keyboard animates open, so height comparisons alone briefly misread the
// keyboard as closed — but focus always lands before the viewport events, so
// this is the race-free "hands off the viewport" signal.
function editingNow() {
  const el = document.activeElement;
  return Boolean(el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable));
}

// Keyboard-free viewport height, the baseline for keyboard detection.
// innerHeight cannot be the reference: on current iOS it tracks the *visual*
// viewport, so with the keyboard settled vv.height === innerHeight (device
// telemetry: both 543 with the pan at 351) and any vv-vs-innerHeight
// comparison reads the keyboard as closed. Refreshed whenever no editable is
// focused and no pan is active, which also tracks rotation and browser-chrome
// changes.
let vvFullHeight = window.visualViewport ? visualViewport.height : 0;

function snapViewportHome() {
  if (!window.visualViewport || editingNow()) return;
  const keyboardClosed = visualViewport.height >= vvFullHeight - 1;
  if (keyboardClosed && (scrollY || visualViewport.offsetTop)) window.scrollTo(0, 0);
}

// iOS standalone ignores interactive-widget=resizes-content, so the keyboard
// resize is done by hand: while the keyboard is up, pin the fixed body to the
// visual viewport's exact box. The composer then sits flush on the
// keyboard/accessory bar and the top bar stays on-screen, instead of iOS
// panning a full-height layout up past the header with a dead gap below
// (#24). The kb-open class also drops the home-indicator padding — the
// keyboard covers that inset, and keeping it was most of the visible black
// strip.
function syncKeyboardInset() {
  if (!window.visualViewport) return;
  if (!editingNow() && !visualViewport.offsetTop) vvFullHeight = visualViewport.height;
  const kbOpen = editingNow() && visualViewport.height < vvFullHeight - 60;
  document.body.classList.toggle("kb-open", kbOpen);
  if (kbOpen) {
    // offsetTop dips negative mid-animation; clamping keeps the header from
    // being pinned above the very edge it must stay under.
    document.body.style.top = `${Math.max(visualViewport.offsetTop, 0)}px`;
    document.body.style.height = `${visualViewport.height}px`;
  } else {
    document.body.style.top = "";
    document.body.style.height = "";
  }
}

// The pan sometimes settles without any visualViewport event — seen when the
// keyboard dismissal is a side effect of hiding its input (closing a sheet)
// while the transcript is being replaced (#8). After such moments, retry the
// snap across the dismissal animation window; each attempt is a no-op unless
// the keyboard is gone and an offset is left over.
function snapViewportSoon() {
  for (const ms of [50, 150, 350, 700]) setTimeout(snapViewportHome, ms);
}

// Temporary #8 diagnostics: the band only reproduces on-device where there is
// no console, so ship the viewport numbers to the server log instead.
function reportViewport(label) {
  const vv = window.visualViewport;
  const text =
    `${label} vv.h=${vv ? vv.height.toFixed(1) : "n/a"} vv.top=${vv ? vv.offsetTop.toFixed(1) : "n/a"}` +
    ` innerH=${innerHeight} scrollY=${scrollY} docH=${document.documentElement.getBoundingClientRect().height.toFixed(1)}` +
    ` screen=${screen.width}x${screen.height} composerBot=${$("composer").getBoundingClientRect().bottom.toFixed(1)}` +
    ` msgTop=${messagesEl.getBoundingClientRect().top.toFixed(1)} msgBot=${messagesEl.getBoundingClientRect().bottom.toFixed(1)}` +
    ` botEl=${(document.elementFromPoint(innerWidth / 2, innerHeight - 4) || {}).id || "none"}` +
    ` bodyTop=${document.body.style.top || "-"} bodyH=${document.body.style.height || "-"}` +
    ` kbOpen=${document.body.classList.contains("kb-open")}` +
    ` active=${(document.activeElement || {}).id || "none"}` +
    ` standalone=${matchMedia("(display-mode: standalone)").matches} rev=${PAGE_REV}`;
  // Pure telemetry: drop it silently when there is no socket. Routing it
  // through send() would toast "not connected" at the user on every offline
  // replay, for a message they never asked to send (#165).
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { send({ type: "client_debug", text }); } catch { /* socket died mid-send */ }
  }
}

let lastVvReport = 0;

if (window.visualViewport) {
  const onViewportChange = () => {
    syncKeyboardInset();
    snapViewportHome();
    scrollToEnd();
    if (Date.now() - lastVvReport > 400) {
      lastVvReport = Date.now();
      reportViewport("vv-change"); // #8/#24 diagnostics at the moment it matters
    }
  };
  visualViewport.addEventListener("resize", onViewportChange);
  visualViewport.addEventListener("scroll", onViewportChange);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) snapViewportSoon(); // app-switcher restore can land in the short-window state
});

// The top arrow only appears while the user is actively scrolling up (and is
// far enough from the top for the jump to be worth a button); any downward
// movement — including streaming content auto-scrolling to the tail — hides it.
let lastScrollTop = 0;
let scrollingToTop = false; // the button's own smooth scroll must not re-show it
let scrollFadeTimer = null; // hides the visible arrow a beat after scrolling stops
// How long a jump arrow lingers after scrolling stops before it fades. Long
// enough to comfortably reach up and tap it, not so long it overstays (#134).
const SCROLL_FADE_MS = 2500;

function fadeScrollArrows() {
  $("scroll-down").hidden = true;
  $("scroll-top").hidden = true;
}

function updateScrollButton() {
  // Hide both arrows while the composer is focused or expanded tall (#115):
  // they sit right above the input and would overlap/obstruct it. They come
  // back once the composer is blurred AND back to its small inline state.
  if (document.activeElement === input || $("composer").classList.contains("tall")) {
    if (scrollFadeTimer) clearTimeout(scrollFadeTimer);
    $("scroll-down").hidden = true;
    $("scroll-top").hidden = true;
    return;
  }
  const top = messagesEl.scrollTop;
  const fromBottom = messagesEl.scrollHeight - top - messagesEl.clientHeight;
  const vh = messagesEl.clientHeight;
  // Jump-to-latest is directional like the top arrow: it appears while you
  // scroll DOWN and are still at least one viewport from the bottom, and hides
  // the moment you scroll UP or come within a viewport of the bottom — so it
  // fades out when you scroll up to read instead of lingering (#119, #127).
  if (top > lastScrollTop && fromBottom > vh) {
    $("scroll-down").hidden = false;
  } else if (top < lastScrollTop || fromBottom < vh) {
    $("scroll-down").hidden = true;
  }
  // Jump-to-top: appears while scrolling UP and at least one viewport from top.
  if (top < vh || top > lastScrollTop) scrollingToTop = false;
  if (top < lastScrollTop && top > vh && !scrollingToTop) {
    $("scroll-top").hidden = false;
  } else if (top > lastScrollTop || top < vh) {
    $("scroll-top").hidden = true;
  }
  lastScrollTop = top;
  // Auto-fade: once scrolling stops the arrow no longer reflects an active
  // gesture, so hide it after a short beat (#127). Every scroll event re-runs
  // this and rearms the timer, so an arrow stays put while you keep scrolling.
  if (scrollFadeTimer) clearTimeout(scrollFadeTimer);
  if (!$("scroll-down").hidden || !$("scroll-top").hidden) {
    scrollFadeTimer = setTimeout(fadeScrollArrows, SCROLL_FADE_MS);
  }
}

messagesEl.addEventListener("scroll", updateScrollButton, { passive: true });
// The composer's focus/blur listeners (arrows hide the instant it takes focus,
// #115) are attached in attachInputListeners together with keydown/input, since
// terminal mode swaps the #input node for a fresh one (#156) and every listener
// must move with it.

$("scroll-down").onclick = () => {
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
};

$("scroll-top").onclick = () => {
  $("scroll-top").hidden = true;
  scrollingToTop = true;
  messagesEl.scrollTo({ top: 0, behavior: "smooth" });
};

function onHistory(history) {
  renderedAnswers = 0; // fork ordinals restart with the rebuilt transcript
  let prevPrompt = "";
  for (const message of history) {
    const content = (message.content || "").trim();
    if (!content) continue;
    if (message.role === "user") {
      retireQuickReplies();
      prevPrompt = stripAttachmentNotes(content);
      addUserMsg(content);
    }
    else if (message.role === "assistant") {
      const el = addMsg("answer md", "");
      el.replaceChildren(renderMarkdown(content));
      highlightFences(el);
      attachAnswerTools(el, content, prevPrompt);
    } else {
      const lines = content.split("\n");
      const shown = lines.slice(0, 4).join("\n");
      addMsg("echo", lines.length > 4 ? `${shown}\n… (${lines.length - 4} more lines)` : shown);
    }
  }
  scrollToEnd(true);
}

// ---- ANSI SGR rendering --------------------------------------------------
function ansiFragment(text) {
  // man/groff render bold as `X\bX` and underline as `_\bX` (backspace
  // overstrike). Convert to real bold/underline — SGR the parser below already
  // understands — so they show as emphasis AND copy cleanly; left as-is the \b
  // renders as a tofu box between every character and poisons the copied text.
  text = text
    .replace(/_\x08([^\n\x08])/g, "\x1b[4m$1\x1b[24m")   // underline: _\bX
    .replace(/([^\n\x08])\x08\1/g, "\x1b[1m$1\x1b[22m")  // bold: X\bX
    .replace(/.?\x08/g, "");                             // strip any leftover overstrike
  // OSC sequences (titles, hyperlinks) carry no visible text formatting.
  text = text.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "");
  const frag = document.createDocumentFragment();
  const classes = new Set();
  const re = /\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r/g;
  let last = 0;
  let match;
  const push = (chunk) => {
    if (!chunk) return;
    if (classes.size) {
      const span = document.createElement("span");
      span.className = [...classes].join(" ");
      span.textContent = chunk;
      frag.appendChild(span);
    } else {
      frag.appendChild(document.createTextNode(chunk));
    }
  };
  while ((match = re.exec(text))) {
    push(text.slice(last, match.index));
    last = re.lastIndex;
    if (match[1] !== undefined) applySgr(match[1], classes);
  }
  push(text.slice(last));
  return frag;
}

function applySgr(params, classes) {
  const dropColor = (prefix) => {
    for (const cls of [...classes]) if (cls.startsWith(prefix)) classes.delete(cls);
  };
  const codes = params === "" ? [0] : params.split(";").map(Number);
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i];
    if (code === 0) classes.clear();
    else if (code === 1) classes.add("a-b");
    else if (code === 2) classes.add("a-dim");
    else if (code === 3) classes.add("a-i");
    else if (code === 4) classes.add("a-u");
    else if (code === 22) { classes.delete("a-b"); classes.delete("a-dim"); }
    else if (code === 23) classes.delete("a-i");
    else if (code === 24) classes.delete("a-u");
    else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
      dropColor("a-fg");
      classes.add(`a-fg${code}`);
    } else if (code === 39) dropColor("a-fg");
    else if (code === 38 || code === 48) {
      // 256/truecolor: skip params, render unstyled rather than wrong.
      if (codes[i + 1] === 5) i += 2;
      else if (codes[i + 1] === 2) i += 4;
      if (code === 38) dropColor("a-fg");
    }
  }
}

// The global console (#148) renders through a real terminal emulator —
// vendored xterm.js (static/vendor/xterm.js, a plain global like highlight.js).
// Full cursor-addressing/erase/scroll support is what makes an interactive
// shell (zsh line-editor redraw, autosuggestions), gcloud auth, and simple TUIs
// render correctly — the old hand-rolled line model could not (#148 follow-up).
// The controller lives further down; see openConsole().

// ---- syntax highlighting (fenced code blocks) -----------------------------
// hljs is vendor/highlight.min.js (static/vendor, no CDN) — a plain global,
// loaded before this script. renderMarkdown() only stamps a fence's language
// onto data-lang; highlighting itself runs here, called just at the points
// an answer is fully settled (closeAnswer, onDone's unstreamed branch,
// onHistory's replay), never per streamed token — re-tokenizing a growing
// fence on every frame would be wasted work and would flicker mid-stream.
function highlightFences(container) {
  if (!window.hljs) return; // vendor script missing/blocked — fences stay plain
  for (const code of container.querySelectorAll("pre > code[data-lang]")) {
    if (code.classList.contains("hljs")) continue; // idempotent if called twice
    const lang = code.dataset.lang;
    if (!hljs.getLanguage(lang)) continue; // unknown language name — no auto-detect fallback
    // hljs.highlight() HTML-escapes the source before wrapping tokens in
    // spans, so this is the one sink allowed to use innerHTML: the text it
    // reads is what we already put in the DOM via textContent (never model
    // markup), and hljs's own escaping is what reaches the DOM, not the
    // model's raw string.
    code.innerHTML = hljs.highlight(code.textContent, { language: lang }).value;
    code.classList.add("hljs");
  }
}

// Syntax-highlight a proposed shell command (bash grammar) in an approval card
// so operators (&&, |, ||), flags, strings, and binaries stand out (#90). Same
// vendored hljs and the same innerHTML-safety as highlightFences: hljs escapes
// the source it's given, and the source is text we set via textContent, so
// code.textContent still returns the exact command for approve/copy.
function highlightCommand(code) {
  if (!window.hljs || !hljs.getLanguage("bash")) return; // vendor missing — stays plain
  code.innerHTML = hljs.highlight(code.textContent, { language: "bash" }).value;
  code.classList.add("hljs");
}

// ---- markdown rendering --------------------------------------------------
function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  // Normalize CRLF/lone-CR up front (#80): a trailing \r riding along on every
  // split line is otherwise harmless noise almost everywhere, but it can land
  // inside an inline match's captured text (e.g. a quick-reply payload) since
  // most of the inline regexes below don't exclude it explicitly.
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  let paragraph = [];

  const flush = () => {
    if (!paragraph.length) return;
    const p = document.createElement("p");
    p.appendChild(inlineMd(paragraph.join("\n")));
    frag.appendChild(p);
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    const fence = fenceOpen(line);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !fenceCloses(lines[i], fence)) body.push(lines[i++]);
      i++; // closing fence (or EOF while streaming)
      // A ```aish-issue block (#110) is a feedback draft, not code: render it as
      // a review card with Create/Edit controls instead of a raw code block.
      if (fence.lang === "aish-issue") {
        frag.appendChild(issueDraftCard(body.join("\n")));
        continue;
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (fence.lang) code.dataset.lang = fence.lang;
      code.textContent = body.join("\n");
      pre.appendChild(code);
      const holder = document.createElement("div");
      holder.className = "copywrap";
      const wrapBtn = document.createElement("button");
      wrapBtn.type = "button";
      wrapBtn.className = "code-wrap";
      wrapBtn.title = "Wrap lines";
      wrapBtn.innerHTML = WRAP_SVG;
      wrapBtn.onclick = () => holder.classList.toggle("wrap-on");
      holder.append(wrapBtn, copyChip(() => code.textContent, "copy code"), pre);
      frag.appendChild(holder);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const h = document.createElement("h" + Math.min(heading[1].length + 1, 6));
      h.className = "md-h";
      h.appendChild(inlineMd(heading[2]));
      frag.appendChild(h);
      i++;
      continue;
    }
    if (/^(\s*)([-*+]|\d+[.)])\s+/.test(line)) {
      flush();
      const ordered = /^\s*\d/.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (i < lines.length) {
        const item = lines[i].match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
        if (!item) break;
        const li = document.createElement("li");
        li.appendChild(inlineMd(item[1]));
        list.appendChild(li);
        i++;
      }
      frag.appendChild(list);
      continue;
    }
    if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      frag.appendChild(mdTable(lines, i));
      i += 2;
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) i++;
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flush();
      frag.appendChild(document.createElement("hr"));
      i++;
      continue;
    }
    if (/^>\s?/.test(line)) {
      flush();
      const quote = document.createElement("blockquote");
      const body = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      quote.appendChild(renderMarkdown(body.join("\n")));
      frag.appendChild(quote);
      continue;
    }
    if (line.trim() === "") {
      flush();
      i++;
      continue;
    }
    paragraph.push(line);
    i++;
  }
  flush();
  return frag;
}

function mdTable(lines, start) {
  const splitRow = (row) =>
    row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
  const wrap = document.createElement("div");
  wrap.className = "md-table";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const cell of splitRow(lines[start])) {
    const th = document.createElement("th");
    th.appendChild(inlineMd(cell));
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  const sourceRows = [lines[start], lines[start + 1]];
  for (let row = start + 2; row < lines.length && /^\|.*\|\s*$/.test(lines[row]); row++) {
    sourceRows.push(lines[row]);
    const tr = document.createElement("tr");
    for (const cell of splitRow(lines[row])) {
      const td = document.createElement("td");
      td.appendChild(inlineMd(cell));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  // Copy hands back the markdown source, so the table pastes as a table
  // anywhere markdown is understood — the chip lives outside the scroll box.
  const source = sourceRows.join("\n");
  const holder = document.createElement("div");
  holder.className = "copywrap";
  holder.append(copyChip(() => source, "copy table"), wrap);
  return holder;
}

// Link/chip/image labels and the quick-reply payload exclude "\n" (#80): a
// paragraph merges several source lines with no blank between them (e.g.
// consecutive quick-reply lines with no separator), and without the
// exclusion an unmatched "[" earlier in that blob could greedily consume
// across a line break into a LATER line's real chip syntax instead of
// leaving it to match on its own.
const INLINE_RE = new RegExp(
  "(`[^`]+`)" +
  "|(\\*\\*[^*]+\\*\\*|__[^_]+__)" +
  "|(\\*[^*\\s][^*]*\\*)" +
  "|(~~[^~]+~~)" +
  "|\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)" +
  "|\\[([^\\]\\n]+)\\]\\(aish-reply:\\/\\/([^)\\n]*)\\)" +
  "|!\\[([^\\]\\n]*)\\]\\(([^)\\s]+)\\)"
);

// Images (#9): ![alt](https://…) embeds a web image; ![alt](/abs/path.png)
// is rewritten to the token-gated /file endpoint, which only serves image
// files inside the active session's roots. Any other scheme stays as the
// literal text. Tap opens the full-size image in a new tab.
function inlineImage(alt, target) {
  let src;
  if (/^https?:\/\//.test(target)) {
    src = target;
  } else if (target.startsWith("/")) {
    const params = new URLSearchParams({ path: target });
    if (token) params.set("token", token);
    src = `/file?${params}`;
  } else {
    return document.createTextNode(`![${alt}](${target})`);
  }
  const link = document.createElement("a");
  link.href = src;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "img-link";
  const img = document.createElement("img");
  img.className = "md-img";
  img.loading = "lazy";
  img.alt = alt || target;
  // A missing file (deleted since, or another session's roots) renders as a
  // small broken-image note instead of the browser's default glyph.
  img.onerror = () => {
    link.textContent = `🖼 ${alt || target} (unavailable)`;
    link.classList.add("img-broken");
  };
  img.src = src;
  link.appendChild(img);
  return link;
}

// Quick replies (#17): [Label](aish-reply://answer text) links render as
// tap chips; tapping feeds the answer into the composer (like tapping an
// old prompt bubble) so the user can edit or just hit send — the sent text
// then shows as a normal user message. The scheme is intercepted here — it
// never navigates and needs no JSON output or schema support from the
// model, so small local models can use it too.
function quickReplyChip(label, payload) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "quick-reply";
  // Trim stray whitespace/\r (#80): INLINE_RE's label group isn't whitespace-
  // aware, so a chip built from a source line with trailing \r (or the model
  // just leaving a space before "]") would otherwise carry it into the button.
  label = (label || "").trim();
  btn.textContent = label;
  let reply = (payload || "").trim();
  try { reply = decodeURIComponent(reply) || reply; } catch { /* keep raw */ }
  if (!reply) reply = label;
  // #116: a tapped chip submits immediately for a fast one-tap reply — UNLESS
  // its payload is meant to be completed by the user first (it ends with ':' or
  // trailing whitespace, e.g. the feedback "Edit" chip "…change the draft: "),
  // in which case it just seeds the composer and waits for you to finish typing.
  const seedOnly = /[\s:]$/.test(payload || "");
  btn.onclick = () => {
    if (input.value.trim() && input.value.trim() !== reply) {
      showToast("clear the input first to use a quick reply");
      return;
    }
    input.value = reply;
    input.setSelectionRange(reply.length, reply.length);
    resizeInput();
    if (seedOnly) {
      input.focus(); // let the user complete the sentence before sending
    } else {
      submitInput(); // one-tap send
    }
  };
  return btn;
}

// Chips are one-shot: once ANY user reply goes out — a fed chip or a typed
// answer — every chip still on screen disappears. Paragraphs that held
// nothing but chips collapse with them.
function retireQuickReplies() {
  for (const btn of messagesEl.querySelectorAll(".quick-reply:not(.spent)")) {
    btn.classList.add("spent");
    const p = btn.parentElement;
    const emptied = p?.tagName === "P" && [...p.childNodes].every((node) =>
      node.nodeType === Node.TEXT_NODE
        ? !node.textContent.trim()
        : node.classList?.contains("spent"));
    if (emptied) p.classList.add("spent");
  }
}

// Issue-draft card (#110): a ```aish-issue block is the finished feedback issue
// — the ONE thing the user reviews and the EXACT text the backend files. Parse
// it (mirror of server.py parse_issue_block: line 1 `title:`, optional `---`
// separator, the rest is the body verbatim) and render a review card: bold
// title, the body as markdown, then Create / Edit controls. "Create the issue"
// sends {type:create_issue} — the backend files the stashed draft as the user's
// own action (no model, no approval gate, repo pinned). "Edit" is an ordinary
// quick reply that seeds the composer so the user tells the model what to change.
function issueDraftCard(inner) {
  const lines = inner.replace(/\r\n?/g, "\n").split("\n");
  const first = (lines[0] || "").trim();
  const title = /^title:/i.test(first) ? first.slice(first.indexOf(":") + 1).trim() : first;
  let rest = lines.slice(1);
  if (rest.length && rest[0].trim() === "---") rest = rest.slice(1); // optional separator
  const body = rest.join("\n").trim();

  const card = document.createElement("div");
  card.className = "issue-draft";
  const head = document.createElement("div");
  head.className = "issue-draft-title";
  head.textContent = title;
  card.appendChild(head);
  const bodyEl = document.createElement("div");
  bodyEl.className = "issue-draft-body md";
  bodyEl.appendChild(renderMarkdown(body));
  card.appendChild(bodyEl);

  const controls = document.createElement("div");
  controls.className = "issue-draft-controls";
  const createBtn = document.createElement("button");
  createBtn.type = "button";
  createBtn.className = "issue-draft-create";
  createBtn.textContent = "Create the issue";
  createBtn.onclick = () => {
    if (createBtn.disabled) return;
    if (send({ type: "create_issue" })) {
      controls.classList.add("spent"); // one-shot: a filed draft can't refire
    }
  };
  controls.appendChild(createBtn);
  // Edit reuses the quick-reply chip so it behaves exactly like today's edit
  // chip — it seeds the composer, the user says what to change, the model
  // re-drafts a fresh aish-issue block.
  controls.appendChild(quickReplyChip("Edit", "I'd like to change the draft: "));
  card.appendChild(controls);
  return card;
}

// Rich embeds (#50): whitelisted YouTube / Google Maps links become inline
// cards in the WEB transcript only — the CLI keeps plain markdown links.
// Security: only strictly-matched ids/queries ever reach an iframe src, and
// the value is decoded then re-encoded with encodeURIComponent, so raw
// model/page text is never interpolated into a frame URL. Frames are
// sandboxed, given no referrer, and share no origin with aish.
const YOUTUBE_RE =
  /^https?:\/\/(?:www\.)?(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|shorts\/)([a-zA-Z0-9_-]{11})|youtu\.be\/([a-zA-Z0-9_-]{11}))(?:[#&?/]|$)/;
// The path segment after /maps varies by link type (bare, /search/, /dir/,
// /place/…) — (?:\/[^?#\s]*)? absorbs any of it so the query string (the part
// that actually gets parsed below) is still reached.
const MAPS_RE =
  /^https?:\/\/(?:maps\.google\.com\/maps|(?:www\.)?google\.[a-z.]+\/maps)(?:\/[^?#\s]*)?\?([^#\s]+)/;

const YT_PLAY_SVG =
  '<svg viewBox="0 0 68 48" aria-hidden="true"><path class="yt-btn" d="M66.52 7.74a8 8 0 0 0-5.63-5.66C55.94 1 34 1 34 1S12.06 1 7.11 2.08A8 8 0 0 0 1.48 7.74 83.7 83.7 0 0 0 .5 24a83.7 83.7 0 0 0 .98 16.26 8 8 0 0 0 5.63 5.66C12.06 47 34 47 34 47s21.94 0 26.89-1.08a8 8 0 0 0 5.63-5.66A83.7 83.7 0 0 0 67.5 24a83.7 83.7 0 0 0-.98-16.26z"/><path class="yt-arrow" d="M27 34l18-10-18-10z"/></svg>';

// Returns an embed element for a whitelisted link, or null so the caller
// falls back to a normal <a>. `label` is used as accessible text/alt.
function embedForLink(label, url) {
  const yt = url.match(YOUTUBE_RE);
  if (yt) return youtubeEmbed(yt[1] || yt[2], label);
  const maps = url.match(MAPS_RE);
  if (maps) {
    const params = new URLSearchParams(maps[1]);
    const saddr = params.get("saddr");
    const daddr = params.get("daddr");
    if (saddr && daddr) {
      return mapsDirectionsEmbed(saddr, daddr, label);
    }
    // "q" is the classic ?q= link param; "query" is what the standard
    // /maps/search/?api=1&query=... share links use instead.
    const q = params.get("q") || params.get("query");
    if (q) {
      return mapsEmbed(encodeURIComponent(q), label);
    }
    // No renderable query (e.g. only @lat,lng / view params) — plain link.
    return null;
  }
  return null;
}

function youtubeEmbed(id, label) {
  const card = document.createElement("div");
  card.className = "embed embed-youtube";
  card.setAttribute("role", "button");
  card.tabIndex = 0;
  card.setAttribute("aria-label", `Play video: ${label}`);

  const img = document.createElement("img");
  img.className = "embed-thumb";
  img.loading = "lazy";
  img.alt = label;
  img.src = `https://img.youtube.com/vi/${id}/hqdefault.jpg`;
  card.appendChild(img);

  const play = document.createElement("div");
  play.className = "embed-play";
  play.innerHTML = YT_PLAY_SVG;
  card.appendChild(play);

  const activate = () => {
    const frame = document.createElement("iframe");
    frame.className = "embed-frame";
    frame.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1`;
    frame.title = label;
    frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
    frame.allowFullscreen = true;
    // Origin only, never the path: YouTube authorizes embedding by referrer,
    // so no-referrer trips "error 153". strict-origin sends just the scheme+
    // host (e.g. https://aish.example) — enough to authorize, while the aish
    // path/session in the URL is withheld.
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    // The player only bootstraps with allow-same-origin (it reads its own
    // youtube-nocookie.com storage). That is safe here BECAUSE the frame is
    // cross-origin to aish: allow-same-origin grants it YouTube's origin, not
    // aish's, so it still can't touch aish's DOM/cookies. The "allow-scripts +
    // allow-same-origin lets a frame drop its own sandbox" escape only matters
    // when the framed content is same-origin AS THE PARENT — it isn't here.
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
    card.replaceChildren(frame);
    card.classList.add("embed-active");
    card.removeAttribute("role");
    card.removeAttribute("tabindex");
  };
  card.addEventListener("click", activate);
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  });
  return card;
}

function mapsEmbed(query, label) {
  const card = document.createElement("div");
  card.className = "embed embed-maps";
  const frame = document.createElement("iframe");
  frame.className = "embed-frame";
  frame.src = `https://maps.google.com/maps?q=${query}&output=embed`;
  frame.title = label;
  frame.loading = "lazy";
  frame.referrerPolicy = "no-referrer";
  frame.allowFullscreen = true;
  // Same sandbox level as the YouTube embed above: allow-same-origin is safe
  // here BECAUSE maps.google.com is cross-origin to aish, so it grants Maps
  // its own origin (needed to bootstrap its "View larger map"/Directions UI)
  // without any ability to reach aish's origin. allow-popups-to-escape-sandbox
  // keeps the tab those buttons open from inheriting this sandbox; allow-forms
  // lets Maps' own search/route boxes submit.
  frame.setAttribute(
    "sandbox",
    "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms"
  );
  card.appendChild(frame);
  return card;
}

function mapsDirectionsEmbed(saddr, daddr, label) {
  const card = document.createElement("div");
  card.className = "embed embed-maps";
  const frame = document.createElement("iframe");
  frame.className = "embed-frame";
  frame.src = `https://maps.google.com/maps?saddr=${encodeURIComponent(saddr)}&daddr=${encodeURIComponent(daddr)}&output=embed`;
  frame.title = label;
  frame.loading = "lazy";
  frame.referrerPolicy = "no-referrer";
  frame.allowFullscreen = true;
  // Same sandbox as mapsEmbed above — see its comment for the allow-same-origin
  // rationale (cross-origin to aish) and why each flag is needed.
  frame.setAttribute(
    "sandbox",
    "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms"
  );
  card.appendChild(frame);
  return card;
}

function inlineMd(text) {
  const frag = document.createDocumentFragment();
  // [no-chips] (#46) is the model's opt-out from the quick-reply safety net —
  // a directive, not content, so it never renders (code blocks skip inlineMd
  // and keep it literal). Stripping here covers streaming, replay, and reload.
  let rest = text.replace(/\[no-chips\]/gi, "");
  while (rest) {
    const match = rest.match(INLINE_RE);
    if (!match) {
      frag.appendChild(document.createTextNode(rest));
      break;
    }
    if (match.index > 0) {
      frag.appendChild(document.createTextNode(rest.slice(0, match.index)));
    }
    if (match[1]) {
      const code = document.createElement("code");
      code.textContent = match[1].slice(1, -1);
      frag.appendChild(code);
    } else if (match[2]) {
      const strong = document.createElement("strong");
      strong.appendChild(inlineMd(match[2].slice(2, -2)));
      frag.appendChild(strong);
    } else if (match[3]) {
      const em = document.createElement("em");
      em.appendChild(inlineMd(match[3].slice(1, -1)));
      frag.appendChild(em);
    } else if (match[4]) {
      const del = document.createElement("del");
      del.appendChild(inlineMd(match[4].slice(2, -2)));
      frag.appendChild(del);
    } else if (match[7] !== undefined) {
      frag.appendChild(quickReplyChip(match[7], match[8]));
    } else if (match[10] !== undefined) {
      frag.appendChild(inlineImage(match[9], match[10]));
    } else {
      const embed = embedForLink(match[5], match[6]);
      if (embed) {
        frag.appendChild(embed);
      } else {
        const link = document.createElement("a");
        link.href = match[6];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.appendChild(inlineMd(match[5]));
        frag.appendChild(link);
      }
    }
    rest = rest.slice(match.index + match[0].length);
  }
  return frag;
}

// ---- read aloud (Web Speech API) -----------------------------------------
// Native speechSynthesis: offline, no audio-generation API, and iOS allows
// it because speak() runs inside the button's tap gesture. Answers are
// spoken as a queue of paragraph-sized chunks (the API can't seek, so
// chunking is what makes prev/next skip possible — and it sidesteps
// Chrome's stall on long utterances). One player is active at a time; its
// speaker button expands into prev / pause / next / speed / stop controls.
const TTS_OK = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;

const TTS_RATES = [0.8, 1, 1.2, 1.4, 1.6, 1.8, 2];
const TTS_RATE_KEY = "aish-tts-rate"; // device-local, like the wrap toggle
let ttsRate = parseFloat(localStorage.getItem(TTS_RATE_KEY));
if (!TTS_RATES.includes(ttsRate)) ttsRate = 1;

const player = {
  box: null,      // the active answer's .tts container
  chunks: [],
  index: 0,
  lang: "en-US",
  paused: false,
  seq: 0,         // bumped on every cancel/skip so stale onend callbacks no-op
  utterance: null, // held so WebKit can't GC it mid-speech (kills onend)
};

// Listening to an answer is passive, so the phone's idle timer sleeps the
// screen mid-playback (#96). Hold a Screen Wake Lock while TTS is speaking.
let wakeLock = null;

async function acquireWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch (err) {
    console.warn(`wake lock request failed: ${err.name}`);
  }
}

function releaseWakeLock() {
  const held = wakeLock;
  wakeLock = null; // clear first so a re-acquire can't race the async release
  if (held) held.release().catch(() => {});
}

// iOS drops the wake lock whenever the page is hidden (screen off, app
// switch). Re-take it on return if we're still mid-answer.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && player.box && !player.paused) {
    acquireWakeLock();
  }
});

function svgIcon(cls, build) {
  const NS = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };
  const svg = make("svg", { viewBox: "0 0 24 24", class: cls });
  build(make, svg);
  return svg;
}

function speakerIcon() {
  return svgIcon("i-speak", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", {
      d: "M11.5 5.5 7.4 9H4.8a.8.8 0 0 0-.8.8v4.4a.8.8 0 0 0 .8.8h2.6l4.1 3.5z",
    }));
    g.appendChild(make("path", { d: "M15 9.3a4 4 0 0 1 0 5.4" }));
    g.appendChild(make("path", { d: "M17.6 6.8a7.6 7.6 0 0 1 0 10.4" }));
    svg.appendChild(g);
  });
}

function pencilIcon() {
  return svgIcon("i-pencil", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", { d: "M4.5 19.5h3.6L19.4 8.2a2 2 0 0 0-2.9-2.9L5.2 16.6z" }));
    g.appendChild(make("path", { d: "M13.8 7l3.2 3.2" }));
    svg.appendChild(g);
  });
}

function pauseIcon() {
  return svgIcon("i-pause", (make, svg) => {
    svg.appendChild(make("rect", { x: "7", y: "6", width: "3.4", height: "12", rx: "1.4", fill: "currentColor" }));
    svg.appendChild(make("rect", { x: "13.6", y: "6", width: "3.4", height: "12", rx: "1.4", fill: "currentColor" }));
  });
}

function playIcon() {
  return svgIcon("i-play", (make, svg) => {
    svg.appendChild(make("path", {
      d: "M8.6 6.3v11.4a.7.7 0 0 0 1.07.6l8.9-5.7a.7.7 0 0 0 0-1.2l-8.9-5.7a.7.7 0 0 0-1.07.6z",
      fill: "currentColor",
    }));
  });
}

function skipSvg(forward) {
  return svgIcon(forward ? "" : "", (make, svg) => {
    if (forward) {
      svg.appendChild(make("path", {
        d: "M6.5 7.4v9.2a.7.7 0 0 0 1.08.59l7.2-4.6a.7.7 0 0 0 0-1.18l-7.2-4.6A.7.7 0 0 0 6.5 7.4z",
        fill: "currentColor",
      }));
      svg.appendChild(make("rect", { x: "16.2", y: "6.6", width: "1.9", height: "10.8", rx: ".95", fill: "currentColor" }));
    } else {
      svg.appendChild(make("path", {
        d: "M17.5 7.4v9.2a.7.7 0 0 1-1.08.59l-7.2-4.6a.7.7 0 0 1 0-1.18l7.2-4.6a.7.7 0 0 1 1.08.59z",
        fill: "currentColor",
      }));
      svg.appendChild(make("rect", { x: "5.9", y: "6.6", width: "1.9", height: "10.8", rx: ".95", fill: "currentColor" }));
    }
  });
}

function xIcon() {
  return svgIcon("", (make, svg) => {
    svg.appendChild(make("path", { d: "M7.5 7.5l9 9M16.5 7.5l-9 9", fill: "none",
      stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" }));
  });
}

// ---- copy to clipboard ---------------------------------------------------
function copyIcon() {
  return svgIcon("i-copy", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("rect", { x: "8.6", y: "8.6", width: "10.6", height: "10.6", rx: "2.4" }));
    g.appendChild(make("path", { d: "M5.4 15.4h-.6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h7.4a2 2 0 0 1 2 2v.6" }));
    svg.appendChild(g);
  });
}

function checkIcon() {
  return svgIcon("i-check", (make, svg) => {
    svg.appendChild(make("path", { d: "M5 12.8l4.4 4.4 9.4-10", fill: "none",
      stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round",
      "stroke-linejoin": "round" }));
  });
}

async function copyText(text) {
  // navigator.clipboard exists only in secure contexts; aish-web is often
  // plain http on the LAN, so fall back to the execCommand-on-a-textarea
  // trick (readonly keeps the iOS keyboard from flashing open).
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* permission hiccup — try the fallback */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus({ preventScroll: true });
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

function copyChip(getText, label) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip";
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.append(copyIcon(), checkIcon());
  btn.onclick = async () => {
    if (!(await copyText(getText()))) {
      showToast("copy failed — select the text manually");
      return;
    }
    btn.classList.add("ok");
    setTimeout(() => btn.classList.remove("ok"), 1300);
  };
  return btn;
}

// ---- export to PDF (issue #64) -------------------------------------------
// Conversion is server-side but fully LOCAL (see export.py) — the markdown is
// posted to /export/answer and comes back as a PDF blob the browser saves.
function pdfIcon() {
  return svgIcon("i-pdf", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", { d: "M12 4v9.5" }));
    g.appendChild(make("path", { d: "M8.4 10.2 12 13.8l3.6-3.6" }));
    g.appendChild(make("path", { d: "M5.5 16.5v1.5a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-1.5" }));
    svg.appendChild(g);
  });
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

// Pull the server-derived (ASCII-transliterated) download name out of the
// Content-Disposition header, so the file is titled from the prompt.
function dispositionName(response, fallback) {
  const cd = response.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i);
  try { return (m && decodeURIComponent(m[1])) || fallback; } catch { return fallback; }
}

async function exportAnswerPdf(markdown, title, btn) {
  // The prompt that led to the answer titles the document AND (via the server's
  // safe_pdf_filename) the download name; fall back to a generic title.
  const query = new URLSearchParams({ title: (title || "").trim() || "aish answer" });
  if (token) query.set("token", token);
  if (btn) btn.disabled = true;
  showToast("Exporting to PDF…", true);
  try {
    const response = await fetch(`${BASE}export/answer?${query}`, {
      method: "POST",
      headers: { "Content-Type": "text/markdown" },
      body: markdown,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showToast(`export failed: ${body.error || response.status}`);
      return;
    }
    saveBlob(await response.blob(), dispositionName(response, "aish-answer.pdf"));
    showToast("Exported");
  } catch {
    showToast("export failed — is the server reachable?");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Whole-session export (final answers only). Rendering a long session takes a
// few seconds, so this fetches the blob (rather than a fire-and-forget anchor)
// to show a sticky "Exporting…" toast that resolves to done/failed — otherwise
// pressing the shortcut or the menu item gives no sign anything happened.
let sessionExporting = false;
async function exportSessionPdf() {
  if (!currentSession || sessionExporting) return;
  sessionExporting = true;
  showToast("Exporting session to PDF…", true);
  const query = new URLSearchParams({ session: currentSession });
  if (token) query.set("token", token);
  try {
    const response = await fetch(`${BASE}export/session?${query}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showToast(`export failed: ${body.error || response.status}`);
      return;
    }
    saveBlob(await response.blob(), dispositionName(response, "aish-session.pdf"));
    showToast("Session exported");
  } catch {
    showToast("export failed — is the server reachable?");
  } finally {
    sessionExporting = false;
  }
}

function exportChip(getText, getTitle) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip";
  btn.title = "export answer to PDF";
  btn.setAttribute("aria-label", "export answer to PDF");
  btn.appendChild(pdfIcon());
  btn.onclick = () => exportAnswerPdf(getText(), getTitle ? getTitle() : "", btn);
  return btn;
}

function forkIcon() {
  return svgIcon("i-fork", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("circle", { cx: "7", cy: "5.5", r: "1.8" }));
    g.appendChild(make("circle", { cx: "7", cy: "18.5", r: "1.8" }));
    g.appendChild(make("circle", { cx: "17", cy: "9.5", r: "1.8" }));
    g.appendChild(make("path", { d: "M7 7.3v9.4" }));
    g.appendChild(make("path", { d: "M7 11.5h5a3 3 0 0 0 3-3v-.3" }));
    svg.appendChild(g);
  });
}

// Fork from a specific answer: branch the conversation up to and including this
// answer into a new session (issue #47, from-here). `ordinal` is 1-based.
function forkChip(ordinal) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip";
  btn.title = "fork the conversation from here into a new session";
  btn.setAttribute("aria-label", "fork from here");
  btn.appendChild(forkIcon());
  btn.onclick = () => {
    if (clientBusy) { showToast("can't fork while working"); return; }
    send({ type: "fork", after: ordinal });
  };
  return btn;
}

// Footer row under a finished answer: copy-as-markdown chip, plus the
// read-aloud player where speech synthesis exists.
let turnStart = 0;
let answerTiming = 0;

// Each rendered final answer gets an ordinal so its Fork button can branch the
// conversation up to and including that answer. Reset whenever the transcript
// is rebuilt (replay/history), so it stays aligned with the log's answer order.
let renderedAnswers = 0;

function attachAnswerTools(el, source, prompt) {
  const ordinal = ++renderedAnswers;
  const tools = document.createElement("div");
  tools.className = "msg-tools";
  // Order (#96): TTS first, then export/fork, Retry, and Copy last — same tight
  // spacing as before, just reordered so the two most-used actions (TTS, Copy)
  // bracket the row instead of sitting next to Retry.
  if (TTS_OK) tools.appendChild(buildTtsBox(el));
  tools.appendChild(exportChip(() => source, () => prompt || ""));
  tools.appendChild(forkChip(ordinal));
  // Regenerate: only the newest answer keeps it, so retire the previous one.
  // Gate on the per-answer `prompt` (populated by both the live and the
  // history-replay paths) — the global lastUserPrompt is unset during a cold
  // reload's onHistory rebuild, which used to drop the button after reconnect.
  retireRegen();
  if (prompt) {
    const regen = document.createElement("button");
    regen.type = "button";
    regen.className = "regen-chip";
    regen.title = "regenerate";
    regen.setAttribute("aria-label", "regenerate answer");
    regen.innerHTML = RERUN_SVG;
    regen.onclick = () => rerunPrompt(prompt);
    tools.appendChild(regen);
    lastRegenBtn = regen;
  }
  tools.appendChild(copyChip(() => source, "copy answer"));
  if (answerTiming) {
    const timing = document.createElement("span");
    timing.className = "answer-timing";
    timing.textContent = fmtSecs(answerTiming);
    tools.appendChild(timing);
    answerTiming = 0; // one readout per answer
  }
  el.appendChild(tools);
}

function buildTtsBox(el) {
  const box = document.createElement("div");
  box.className = "tts";
  const mkBtn = (cls, label, ...icons) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = cls;
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.append(...icons);
    return btn;
  };
  const prev = mkBtn("t-skip t-prev", "previous paragraph", skipSvg(false));
  const main = mkBtn("t-main", "read aloud", speakerIcon(), pauseIcon(), playIcon());
  const next = mkBtn("t-skip t-next", "next paragraph", skipSvg(true));
  const rate = mkBtn("t-rate", "reading speed");
  rate.textContent = rateLabel();
  const stop = mkBtn("t-stop", "stop reading", xIcon());
  prev.onclick = () => skipChunk(-1);
  next.onclick = () => skipChunk(1);
  rate.onclick = cycleRate;
  stop.onclick = stopSpeaking;
  main.onclick = () => {
    if (player.box === box) togglePause();
    else startPlayback(box, el);
  };
  box.append(prev, main, next, rate, stop);
  return box;
}

function speakableText(el) {
  // Read what's on screen, minus code blocks (hearing code character by
  // character is noise) and the player controls. Block elements become line
  // breaks — textContent alone would run "…end.Next" together and slur.
  const parts = [];
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) { parts.push(node.nodeValue); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === "PRE" || node.classList.contains("msg-tools")) return;
    for (const child of node.childNodes) walk(child);
    if (/^(P|LI|H[1-6]|TR|BLOCKQUOTE)$/.test(node.tagName)) parts.push("\n");
  };
  walk(el);
  return parts.join("").replace(/[^\S\n]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
}

function chunkParagraphs(text) {
  // One chunk per paragraph. Runs of short blocks (list items, one-line
  // headings) group into a single chunk so skip jumps feel like paragraphs,
  // not individual bullets; real paragraphs always stand alone.
  const chunks = [];
  let run = "";
  const flushRun = () => { if (run) { chunks.push(run); run = ""; } };
  for (const block of text.split("\n")) {
    if (block.length < 60) {
      if (run.length + block.length > 250) flushRun();
      run = run ? `${run}\n${block}` : block;
    } else {
      flushRun();
      chunks.push(block);
    }
  }
  flushRun();
  return chunks;
}

function speechLang(text) {
  // Without an explicit lang the engine uses the device's default voice —
  // a Polish phone reads English text with Polish phonemes. Cheap
  // bilingual vote: Polish reliably shows diacritics/stopwords; tie or
  // neither defaults to English.
  const sample = text.slice(0, 600).toLowerCase();
  let polish = (sample.match(/[ąćęłńśźż]/g) || []).length;
  polish += 2 * ((sample.match(/(^|\s)(się|jest|nie|czy|oraz|przez|tego|można|żeby|które)(?=\s|[.,;:!?)]|$)/g) || []).length);
  const english = (sample.match(/(^|\s)(the|and|is|of|to|that|with|this|for|are)(?=\s|[.,;:!?)]|$)/g) || []).length;
  return polish > english ? "pl-PL" : "en-US";
}

function rateLabel() {
  return `${ttsRate}×`;
}

function stopSpeaking() {
  if (!TTS_OK) return;
  player.seq += 1; // orphan any in-flight onend so it can't chain
  speechSynthesis.cancel();
  if (player.box) player.box.classList.remove("active", "paused");
  player.box = null;
  player.utterance = null;
  player.paused = false;
  releaseWakeLock();
  syncTtsDock();
}

function startPlayback(box, el) {
  stopSpeaking();
  const text = speakableText(el);
  if (!text) return;
  player.box = box;
  player.chunks = chunkParagraphs(text);
  box.classList.add("active");
  box.querySelector(".t-rate").textContent = rateLabel();
  acquireWakeLock();
  speakChunk(0);
  syncTtsDock();
}

function speakChunk(index) {
  player.seq += 1;
  const seq = player.seq;
  speechSynthesis.cancel();
  player.index = index;
  player.paused = false;
  player.box.classList.remove("paused");
  const utterance = new SpeechSynthesisUtterance(player.chunks[index]);
  // Detect language PER paragraph, not once for the whole reply: aish often
  // mixes languages (a Polish answer citing an English-created issue), so each
  // chunk is spoken in its own voice instead of forcing one over the lot (#97).
  utterance.lang = speechLang(player.chunks[index]);
  utterance.rate = ttsRate;
  utterance.onend = () => {
    if (seq !== player.seq) return; // cancelled/skipped — a newer speak owns state
    if (player.index + 1 < player.chunks.length) speakChunk(player.index + 1);
    else stopSpeaking();
  };
  utterance.onerror = () => {
    if (seq === player.seq) stopSpeaking();
  };
  player.utterance = utterance;
  speechSynthesis.resume(); // cancel-while-paused leaves WebKit stuck paused
  speechSynthesis.speak(utterance);
  syncTtsDock(); // a skip clears the paused state — keep the dock icon in sync
}

function togglePause() {
  if (player.paused) {
    speechSynthesis.resume();
    player.paused = false;
    acquireWakeLock();
  } else {
    speechSynthesis.pause();
    player.paused = true;
    releaseWakeLock();
  }
  player.box.classList.toggle("paused", player.paused);
  syncTtsDock();
}

function skipChunk(delta) {
  if (!player.box) return;
  const next = Math.min(player.chunks.length - 1, Math.max(0, player.index + delta));
  speakChunk(next);
}

function cycleRate() {
  ttsRate = TTS_RATES[(TTS_RATES.indexOf(ttsRate) + 1) % TTS_RATES.length];
  localStorage.setItem(TTS_RATE_KEY, String(ttsRate));
  if (player.box) {
    player.box.querySelector(".t-rate").textContent = rateLabel();
    speakChunk(player.index); // rate is fixed per utterance — restart the chunk
  }
  syncTtsDock();
}

// A persistent transport for the ACTIVE read-aloud, docked in the composer
// button row so playback stays reachable while you scroll away or type (#106).
// It drives the same global `player` as the per-answer pill — the inline
// speaker still STARTS playback; this bar just mirrors + controls whatever is
// already playing, so there's one source of truth, not a second player.
function buildTtsDock() {
  const dock = $("tts-dock");
  if (!dock || !TTS_OK) return;
  const mkBtn = (cls, label, ...icons) => {
    const b = document.createElement("button");
    b.type = "button"; // inside the composer <form> — must not submit
    b.className = cls;
    b.title = label;
    b.setAttribute("aria-label", label);
    b.append(...icons);
    return b;
  };
  const prev = mkBtn("td-prev", "previous paragraph", skipSvg(false));
  const main = mkBtn("td-main", "pause / resume", pauseIcon(), playIcon());
  const next = mkBtn("td-next", "next paragraph", skipSvg(true));
  const rate = mkBtn("td-rate", "reading speed");
  rate.textContent = rateLabel();
  const stop = mkBtn("td-stop", "stop reading", xIcon());
  prev.onclick = () => skipChunk(-1);
  main.onclick = () => togglePause();
  next.onclick = () => skipChunk(1);
  rate.onclick = cycleRate;
  stop.onclick = stopSpeaking;
  dock.append(prev, main, next, rate, stop);
}

// Reflect the global player onto the composer dock: visible only while a
// read-aloud is active, with the input pushed to its own row (#97 tall
// mechanics) so the transport shares the button row with + / mic / send.
function syncTtsDock() {
  const dock = $("tts-dock");
  if (!dock) return;
  const active = !!player.box;
  dock.hidden = !active;
  $("composer").classList.toggle("tts-on", active);
  dock.classList.toggle("paused", player.paused);
  const rate = dock.querySelector(".td-rate");
  if (rate) rate.textContent = rateLabel();
}

if (TTS_OK) buildTtsDock();

// ---- approval cards ------------------------------------------------------
function onApprovalRequest(event) {
  closeAnswer();
  // A card means the user is deciding — mark the pending run_command step so
  // the trace can say "Approved" (manual) vs "Auto-approved" later (#2).
  if (event.kind === "command" && currentTrace && currentTrace.pending
      && currentTrace.pending.name === "run_command") {
    currentTrace.pending.manual = true;
  }
  // An expanded live timeline eats the vertical space the approval card needs,
  // forcing a scroll (#65). Auto-collapse it and remember to restore once all
  // pending cards are resolved — unless the user already collapsed it.
  if (currentTrace && currentTrace.el.classList.contains("live")
      && currentTrace.el.classList.contains("open")) {
    currentTrace.el.classList.remove("open");
    currentTrace.autoCollapsed = true;
  }
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = event.id;
  if (event.kind === "command") {
    card.dataset.summary = event.command;
    buildCommandCard(card, event);
  } else if (event.kind === "write") {
    card.dataset.summary = `${event.verb} ${event.target}`;
    buildWriteCard(card, event);
  } else if (event.kind === "tool") {
    card.dataset.summary = `run ${event.tool}`;
    buildToolCard(card, event);
  } else if (event.kind === "import") {
    card.dataset.summary = `import ${event.skill}`;
    buildImportCard(card, event);
  } else {
    card.dataset.summary = `read ${event.path}`;
    buildReadCard(card, event);
  }
  // Surface the single-key shortcuts (handled by the global keydown). Every
  // shortcut button gets a tooltip; the prominent verdict buttons also get a
  // VISIBLE key badge, but only with a physical keyboard (FINE_POINTER) — a
  // badge is noise on a touch device that can't use it. A builder-set title is
  // kept and the key appended; otherwise the button's own label seeds it (so
  // Install/Trust dir read correctly, not "Approve").
  for (const sc of CARD_SHORTCUTS) {
    const K = sc.key.toUpperCase();
    for (const b of card.querySelectorAll(sc.selector)) {
      b.title = b.title ? `${b.title} (${K})` : `${b.textContent || "Action"} (${K})`;
      if (sc.badge && FINE_POINTER) {
        const kb = document.createElement("span");
        kb.className = "key-badge";
        kb.textContent = K;
        kb.setAttribute("aria-hidden", "true");
        b.appendChild(kb);
      }
    }
  }
  cards.set(event.id, card);
  pendingCards += 1;
  refreshStatusline();
  messagesEl.appendChild(card);
  scrollToEnd(true);
  // Modal-like focus (desktop only): the composer usually holds focus, so a
  // bare "A" would type into it instead of approving. Move focus to the card
  // container (NOT the feedback field — that would re-arm editingNow and defeat
  // the shortcut) so the single-key verdicts work immediately. Touch devices
  // have no physical keyboard and popping focus there just fights the composer.
  if (FINE_POINTER) {
    card.tabIndex = -1;
    card.focus({ preventScroll: true });
  }
  notify("aish — approval needed", card.dataset.summary);
}

function title(card, html) {
  const el = document.createElement("div");
  el.className = "card-title";
  el.append(...html);
  card.appendChild(el);
  return el;
}

function buttonRow(card, specs) {
  const row = document.createElement("div");
  row.className = "buttons";
  for (const [label, cls, fn, tooltip] of specs) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    if (tooltip) b.title = tooltip;
    b.onclick = fn;
    row.appendChild(b);
  }
  card.appendChild(row);
  return row;
}

function answerCard(id, action, extra) {
  send({ type: "approval", id, action, ...extra });
  const card = cards.get(id);
  if (card) {
    for (const b of card.querySelectorAll("button, input, textarea")) b.disabled = true;
  }
  // Hand keyboard focus on: to the next still-pending card if one is stacked,
  // otherwise back to the composer (desktop only — mirrors the modal focus on
  // appear). activeApprovalCard() already skips this now-disabled card.
  if (FINE_POINTER) {
    const next = activeApprovalCard();
    if (next) { next.tabIndex = -1; next.focus({ preventScroll: true }); }
    else if (typeof input !== "undefined" && input) input.focus();
  }
}

// #13/#34: optional feedback typed straight into the approval card. The text
// rides along with WHICHEVER button is pressed — on Deny it explains the
// refusal, on any approval it reaches the model as guidance for this and
// future actions. Typing feedback implies no verdict: Enter just dismisses
// the keyboard, the user still picks a button.
function feedbackField() {
  // One line by default, auto-growing as you type up to a few lines then
  // scrolling (#144): a permanent 2-row box ate vertical space (esp. on mobile)
  // before a comment was even wanted. Enter still inserts a newline; the verdict
  // comes from a button press (feedbackExtra reads .value), nothing here submits.
  const ta = document.createElement("textarea");
  ta.className = "feedback";
  ta.rows = 1;
  ta.placeholder = "Optional comment";
  ta.autocomplete = "off";
  // Mirror resizeInput's grow: reset then set to scrollHeight, capped (~5 lines).
  const grow = () => {
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 140)}px`;
  };
  ta.addEventListener("input", grow);
  return ta;
}

function feedbackExtra(input) {
  const comment = input.value.trim();
  return comment ? { comment } : {};
}

const CARD_TRIANGLE = '<svg viewBox="0 0 24 24"><path d="M12 3.5 21 19H3z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v3.5M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>';
const CARD_SHIELD = '<svg viewBox="0 0 24 24"><path d="M12 3.5l7 2.5v5c0 4.2-2.9 7.5-7 9-4.1-1.5-7-4.8-7-9V6z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>';

const FOLDER_SVG = '<svg viewBox="0 0 24 24"><path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';

// Each scope segment maps to the SAME wire action the old per-scope buttons
// sent (server contract in make_web_approvers): the segment picks what an
// Approve remembers, it does not change the message shape.
const SCOPE_LABELS = {
  approve: "Just once",         // plain approve — this command, this time
  approve_session: "Session",   // allowlist the shown prefix(es) for this session
  approve_always: "Always",     // persist the prefix(es) to the allowlist file
  approve_trust: "Trust dir",   // trust the escaping directory for this session
};

// The explanatory sentence under the segmented control. Dynamic parts
// (prefixes, dirs) go in via textContent — never innerHTML — since they are
// derived from the model-proposed command.
function scopeExplain(action, prefixText, escapeText) {
  const frag = document.createDocumentFragment();
  const mono = (t) => { const s = document.createElement("span"); s.className = "mono"; s.textContent = t; return s; };
  const strong = (t) => { const b = document.createElement("b"); b.textContent = t; return b; };
  if (action === "approve_session") {
    // "Session" is scoped to this conversation's approver (server_prefixes),
    // not the process — so it lasts for this chat, not "until restart".
    frag.append("Also auto-approve ", mono(prefixText), " for the rest of this session.");
  } else if (action === "approve_always") {
    frag.append("Save ", mono(prefixText), " to the allowlist — it persists across sessions.");
  } else if (action === "approve_trust") {
    frag.append("Trust ", mono(escapeText), " for this session — anything inside then runs without asking.");
  } else {
    // Default (Just once) mirrors the design: the safe choice, then a hint at
    // what the broader segments would do.
    frag.append("Approve ", strong("only this command, this time."));
    if (prefixText && escapeText) {
      frag.append(" Broader scopes allowlist ", mono(prefixText), " or trust ", mono(escapeText), ".");
    } else if (prefixText) {
      frag.append(" Broader scopes allowlist ", mono(prefixText), ".");
    } else if (escapeText) {
      frag.append(" A broader scope trusts ", mono(escapeText), ".");
    }
  }
  return frag;
}

function buildCommandCard(card, event) {
  // #107: reserve the orange (danger) accent for DESTRUCTIVE commands so the
  // warning color keeps its punch; a standard command uses the calmer blue
  // (info) accent — the same as write/diff cards — with a shield icon. This
  // fights the "everything's orange → approve reflex" warning fatigue. It's
  // still an approval gate; the card is just not screaming when it needn't.
  const destructive = Boolean(event.destructive);
  card.classList.add("approval-card", destructive ? "danger" : "info");
  const head = document.createElement("div");
  head.className = destructive ? "card-head danger" : "card-head";
  head.innerHTML =
    `<span class="card-ico">${destructive ? CARD_TRIANGLE : CARD_SHIELD}</span>` +
    `<span class="card-htext"><span class="card-htitle">Approval needed</span>` +
    `<span class="card-hsub"></span></span>`;
  head.querySelector(".card-hsub").textContent =
    destructive ? "Destructive — review before running" : "Runs a shell command";
  card.appendChild(head);

  // $ command box: editable in place via the pencil, plus copy.
  const box = document.createElement("div");
  box.className = "cmd-box";
  const dollar = document.createElement("span");
  dollar.className = "cmd-dollar";
  dollar.textContent = "$";
  const code = document.createElement("span");
  code.className = "cmd-text mono";
  code.textContent = event.command;
  highlightCommand(code); // bash syntax colors (#90); reverted while editing below
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "cmd-icon";
  editBtn.title = "Edit the command before running";
  editBtn.appendChild(pencilIcon());
  editBtn.onclick = () => toggleEdit();
  // Group the edit + copy icons so they can stick to the top of the box: on a
  // long multi-line command they stay top-right in view instead of scrolling
  // away or sitting mid-height (#88).
  const cmdTools = document.createElement("div");
  cmdTools.className = "cmd-tools";
  cmdTools.append(editBtn, copyChip(() => code.textContent, "copy command"));
  box.append(dollar, code, cmdTools);
  card.appendChild(box);

  function toggleEdit() {
    if (code.isContentEditable) {
      code.contentEditable = "false";
      code.classList.remove("editing");
      editBtn.classList.remove("active");
      highlightCommand(code); // re-highlight the (possibly edited) command
      return;
    }
    // Editing wants plain text: collapse the highlight spans back to one text
    // node so the caret and contentEditable behave normally.
    code.textContent = code.textContent;
    code.classList.remove("hljs");
    code.contentEditable = "plaintext-only";
    code.classList.add("editing");
    editBtn.classList.add("active");
    code.focus();
    const range = document.createRange();
    range.selectNodeContents(code);
    range.collapse(false);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // where it runs
  const where = document.createElement("div");
  where.className = "card-where";
  where.innerHTML = FOLDER_SVG;
  where.append("runs in ");
  const wpath = document.createElement("span");
  wpath.className = "where-path";
  wpath.textContent = abbreviatePath(currentCwd || "");
  where.appendChild(wpath);
  card.appendChild(where);

  const escapes = event.escapes || [];
  if (escapes.length) card.appendChild(escapeNote(escapes));

  const feedback = feedbackField();
  card.appendChild(feedback);

  // Scope segments, driven by what the backend actually offered: "Just once"
  // is always available; Session/Always need allowlist prefixes; Trust dir
  // needs an escaping directory. With nothing but "Just once", the control is
  // pointless — omit it and let Approve mean a plain approve.
  const prefixText = (event.prefixes || []).join(", ");
  const escapeText = escapes.join(", ");
  const actions = ["approve"];
  if (prefixText) actions.push("approve_session", "approve_always");
  if (escapes.length) actions.push("approve_trust");
  let scopeAction = "approve";
  if (actions.length > 1) {
    const scope = document.createElement("div");
    scope.className = "scope";
    const label = document.createElement("div");
    label.className = "scope-label";
    label.textContent = "If approved, remember for";
    const seg = document.createElement("div");
    seg.className = "segmented";
    const explain = document.createElement("div");
    explain.className = "scope-explain";
    const select = (action, btn) => {
      scopeAction = action;
      for (const b of seg.children) b.classList.toggle("active", b === btn);
      explain.replaceChildren(scopeExplain(action, prefixText, escapeText));
    };
    let firstBtn = null;
    for (const action of actions) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "seg";
      b.textContent = SCOPE_LABELS[action];
      b.onclick = () => select(action, b);
      seg.appendChild(b);
      firstBtn = firstBtn || b;
    }
    scope.append(label, seg, explain);
    card.appendChild(scope);
    select("approve", firstBtn);
  }

  const actionsRow = document.createElement("div");
  actionsRow.className = "card-actions";
  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "approve";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => {
    const edited = code.textContent.trim();
    if (edited && edited !== event.command.trim()) {
      // An edited command flows through the "edit" action exactly as before;
      // the server re-checks the denylist on it. Editing takes precedence over
      // the scope segment (the wire has no edit+scope combination).
      answerCard(event.id, "edit", { command: edited, ...feedbackExtra(feedback) });
    } else {
      answerCard(event.id, scopeAction, feedbackExtra(feedback));
    }
  };
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "deny";
  denyBtn.textContent = "Deny";
  denyBtn.onclick = () => answerCard(event.id, "deny", feedbackExtra(feedback));
  actionsRow.append(approveBtn, denyBtn);
  card.appendChild(actionsRow);
}

function buildWriteCard(card, event) {
  card.classList.add("approval-card", "info");
  const head = document.createElement("div");
  head.className = "card-head sep";
  const ico = document.createElement("span");
  ico.className = "card-ico";
  ico.appendChild(pencilIcon());
  const htext = document.createElement("span");
  htext.className = "card-htext";
  const htitle = document.createElement("span");
  htitle.className = "card-htitle";
  htitle.textContent = event.verb === "create" ? "Create file" : "Edit file";
  const hsub = document.createElement("span");
  hsub.className = "card-hsub mono";
  hsub.textContent = relTarget(event.target);
  hsub.title = event.target; // full path on hover
  htext.append(htitle, hsub);
  const added = document.createElement("span");
  added.className = "card-count add";
  added.textContent = `+${event.added}`;
  const removed = document.createElement("span");
  removed.className = "card-count del";
  removed.textContent = `−${event.removed}`;
  head.append(ico, htext, added, removed);
  card.appendChild(head);

  card.appendChild(renderDiff(event.diff || ""));

  const feedback = feedbackField();
  card.appendChild(feedback);

  const actionsRow = document.createElement("div");
  actionsRow.className = "card-actions even";
  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "approve";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => answerCard(event.id, "approve", feedbackExtra(feedback));
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "deny";
  denyBtn.textContent = "Deny";
  denyBtn.onclick = () => answerCard(event.id, "deny", feedbackExtra(feedback));
  actionsRow.append(approveBtn, denyBtn);
  card.appendChild(actionsRow);
}

// A mutating plugin tool (#141) reuses the write card's shape: header names
// the tool, the body shows the structured args, feedback rides along (deny =
// stop, approve+comment = adjust), and Approve/Deny answer the same way.
function buildToolCard(card, event) {
  card.classList.add("approval-card", "info");
  const head = document.createElement("div");
  head.className = "card-head sep";
  const ico = document.createElement("span");
  ico.className = "card-ico";
  ico.appendChild(wrenchIcon());
  const htext = document.createElement("span");
  htext.className = "card-htext";
  const htitle = document.createElement("span");
  htitle.className = "card-htitle";
  htitle.textContent = "Run tool";
  const hsub = document.createElement("span");
  hsub.className = "card-hsub mono";
  hsub.textContent = event.tool;
  htext.append(htitle, hsub);
  head.append(ico, htext);
  card.appendChild(head);

  // Ground-truth preview (#157): a human-legible description of an otherwise
  // opaque (e.g. id-addressed) action, resolved by the tool itself. Shown
  // prominently; the raw args become a secondary detail below.
  if (event.preview) {
    const prev = document.createElement("div");
    prev.className = "tool-preview";
    prev.textContent = event.preview;
    card.appendChild(prev);
  }

  const argsWrap = document.createElement("div");
  argsWrap.className = "tool-args";
  if (event.preview) argsWrap.classList.add("secondary");
  const entries = Object.entries(event.args || {});
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "tool-arg-empty";
    empty.textContent = "(no arguments)";
    argsWrap.appendChild(empty);
  } else {
    for (const [k, v] of entries) argsWrap.appendChild(toolArgRow(k, v));
  }
  card.appendChild(argsWrap);

  const feedback = feedbackField();
  card.appendChild(feedback);

  const actionsRow = document.createElement("div");
  actionsRow.className = "card-actions even";
  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "approve";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => answerCard(event.id, "approve", feedbackExtra(feedback));
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "deny";
  denyBtn.textContent = "Deny";
  denyBtn.onclick = () => answerCard(event.id, "deny", feedbackExtra(feedback));
  actionsRow.append(approveBtn, denyBtn);
  card.appendChild(actionsRow);
}

// One tool argument as a labeled field. A string is shown RAW (real line breaks,
// no JSON escaping) so an email/issue body reads like text, not one \n-littered
// line; a multi-line value drops to a block layout (label above, value below).
// Non-strings render as pretty JSON.
function toolArgRow(key, value) {
  const row = document.createElement("div");
  row.className = "tool-arg";
  const keyEl = document.createElement("span");
  keyEl.className = "tool-arg-key";
  keyEl.textContent = key;
  const valEl = document.createElement("span");
  valEl.className = "tool-arg-val mono";
  valEl.textContent = prettyArgValue(value);
  if (typeof value === "string" && value.includes("\n")) row.classList.add("block");
  row.append(keyEl, valEl);
  return row;
}

function prettyArgValue(v) {
  if (typeof v === "string") return v;
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}

// Consolidated review of a whole imported skill (#139): every file's full
// contents (syntax-highlighted, not a diff), risk flags up top, one decision —
// so untrusted code is actually reviewable, not rubber-stamped file-by-file.
function buildImportCard(card, event) {
  card.classList.add("approval-card", "info");
  const head = document.createElement("div");
  head.className = "card-head sep";
  const ico = document.createElement("span");
  ico.className = "card-ico";
  ico.appendChild(wrenchIcon());
  const htext = document.createElement("span");
  htext.className = "card-htext";
  const htitle = document.createElement("span");
  htitle.className = "card-htitle";
  htitle.textContent = "Import skill";
  const hsub = document.createElement("span");
  hsub.className = "card-hsub mono";
  hsub.textContent = event.skill;
  htext.append(htitle, hsub);
  head.append(ico, htext);
  card.appendChild(head);

  if (event.description) {
    const desc = document.createElement("div");
    desc.className = "import-desc";
    desc.textContent = event.description;
    card.appendChild(desc);
  }

  const files = event.files || [];
  const meta = document.createElement("div");
  meta.className = "import-meta";
  meta.textContent = `${files.length} file${files.length === 1 ? "" : "s"} → ${event.dest || ""}`;
  card.appendChild(meta);

  if ((event.flags || []).length) {
    const warn = document.createElement("div");
    warn.className = "import-flags";
    const t = document.createElement("div");
    t.className = "import-flags-title";
    t.textContent = "⚠ Review closely";
    warn.appendChild(t);
    for (const flag of event.flags) {
      const row = document.createElement("div");
      row.className = "import-flag";
      row.textContent = flag;
      warn.appendChild(row);
    }
    card.appendChild(warn);
  }
  if ((event.skipped || []).length) {
    const sk = document.createElement("div");
    sk.className = "import-skipped";
    sk.textContent = `Binary assets skipped (not installed): ${event.skipped.join(", ")}`;
    card.appendChild(sk);
  }

  for (const f of files) {
    const fileHead = document.createElement("div");
    fileHead.className = "import-file-head mono";
    fileHead.textContent = f.path + (f.executable ? "  •exec" : "");
    card.appendChild(fileHead);
    const pre = document.createElement("pre");
    pre.className = "import-file";
    const code = document.createElement("code");
    if (f.lang) code.dataset.lang = f.lang;
    code.textContent = f.content;
    pre.appendChild(code);
    card.appendChild(pre);
  }
  highlightFences(card); // reuse the vendored hljs — real syntax highlighting

  const feedback = feedbackField();
  card.appendChild(feedback);

  const actionsRow = document.createElement("div");
  actionsRow.className = "card-actions even";
  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "approve";
  approveBtn.textContent = "Install";
  approveBtn.onclick = () => answerCard(event.id, "approve", feedbackExtra(feedback));
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "deny";
  denyBtn.textContent = "Deny";
  denyBtn.onclick = () => answerCard(event.id, "deny", feedbackExtra(feedback));
  actionsRow.append(approveBtn, denyBtn);
  card.appendChild(actionsRow);
}

function wrenchIcon() {
  return svgIcon("i-wrench", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", {
      d: "M14.5 6a3.5 3.5 0 0 0-4.6 4.3l-4.8 4.8a1.5 1.5 0 0 0 2.1 2.1l4.8-4.8A3.5 3.5 0 0 0 18 8.5l-2 2-2-2 2-2A3.5 3.5 0 0 0 14.5 6Z",
    }));
    svg.appendChild(g);
  });
}

// The card header shows the file. In-project: the path relative to the working
// directory (design shows `config/http.py`). Outside the project: the FULL
// location, home-abbreviated (~/…) — a write to global config must NOT read
// like a file in the current project, or the user can't tell where it lands
// (#141 create_tool writes to ~/.config/aish/tools).
function relTarget(target) {
  const cwd = currentCwd || "";
  if (cwd && target.startsWith(cwd + "/")) return target.slice(cwd.length + 1);
  const home = target.match(/^\/(?:Users|home)\/[^/]+\//);
  return home ? "~/" + target.slice(home[0].length) : target;
}

// A unified diff rendered the way the design shows it (Screen 2d): no
// `---/+++/@@` plumbing (the filename is already in the header), a line-number
// gutter (old numbers for context/removals, new numbers for additions), and a
// tinted row per add/remove. `@@` hunks only seed the counters; a thin divider
// marks a gap between hunks.
function renderDiff(text) {
  const diff = document.createElement("div");
  diff.className = "diff";
  // Rows live in an inner box sized to the WIDEST line (`width: max-content`,
  // 100% floor); every row then fills that box, so each line's tinted
  // background paints uniformly across the full scroll width — like a terminal —
  // instead of stopping at its own (or the viewport's) edge and exposing bare
  // panel background when scrolled right (#68).
  const inner = document.createElement("div");
  inner.className = "diff-inner";
  diff.appendChild(inner);
  // Copy grabs the resulting file CONTENT (added + context lines, no +/-
  // markers or removed lines) — for a create that is the whole new file, for an
  // edit the post-edit view — which is far more useful to paste than raw diff.
  const contentLines = [];
  let oldNo = 0;
  let newNo = 0;
  let emitted = false;
  const rowEl = (cls, no, body) => {
    const row = document.createElement("div");
    row.className = "dl " + cls;
    const g = document.createElement("span");
    g.className = "dl-no";
    g.textContent = no == null ? "" : String(no);
    const t = document.createElement("span");
    t.className = "dl-tx";
    t.textContent = body.length ? body : " ";
    row.append(g, t);
    inner.appendChild(row);
    emitted = true;
  };
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop(); // trailing newline artifact
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      oldNo = parseInt(hunk[1], 10);
      newNo = parseInt(hunk[2], 10);
      if (emitted) {
        const sep = document.createElement("div");
        sep.className = "dl gap";
        sep.textContent = "⋯";
        inner.appendChild(sep);
      }
      continue;
    }
    if (line.startsWith("+")) {
      rowEl("add", newNo++, line.slice(1));
      contentLines.push(line.slice(1));
    } else if (line.startsWith("-")) rowEl("del", oldNo++, line.slice(1));
    else if (line.startsWith("\\")) rowEl("ctx", null, line); // "\ No newline…"
    else {
      const body = line.startsWith(" ") ? line.slice(1) : line;
      rowEl("ctx", oldNo, body);
      contentLines.push(body);
      oldNo++;
      newNo++;
    }
  }

  // Pin wrap + copy top-right of a non-scrolling wrapper (tools stay put while
  // the diff scrolls sideways). Seed wrap from the global top-bar preference;
  // the per-card toggle then owns it (see syncDiffWrap for global overrides).
  const wrap = document.createElement("div");
  wrap.className = "diff-wrap";
  const softed = document.body.classList.contains("wrap");
  if (softed) diff.classList.add("diff-soft");
  const tools = document.createElement("div");
  tools.className = "term-tools";
  tools.append(
    diffWrapBtn(diff, softed),
    copyChip(() => contentLines.join("\n"), "copy file content"),
  );
  wrap.append(tools, diff);
  return wrap;
}

// The diff card's own wrap toggle: flips the diff between horizontal scroll
// (`white-space: pre`) and soft wrap (`pre-wrap`) via the `.diff-soft` class,
// independent of the global body.wrap (kept in sync by syncDiffWrap).
function diffWrapBtn(diff, on) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "term-tool term-wrap diff-wrap-btn" + (on ? " on" : "");
  b.title = "Wrap lines";
  b.innerHTML = WRAP_SVG;
  b.onclick = () => {
    b.classList.toggle("on");
    diff.classList.toggle("diff-soft");
  };
  return b;
}

// The out-of-roots warning shown on command/read cards whose target lives
// outside the session roots — the "Trust directory" button's context.
function escapeNote(escapes) {
  const note = document.createElement("div");
  note.className = "escape-note";
  note.textContent = `⚠ outside the session roots: ${escapes.join(", ")}`;
  return note;
}

function buildReadCard(card, event) {
  // A read gate is still an "attention needed" card — the model wants a file
  // that is either sensitive (may hold secrets) or outside the project roots.
  // Same orange accent + structure as the command card so the two read as one
  // family; only the header wording and the missing $-command box differ.
  card.classList.add("approval-card", "danger");
  const outside = event.reason === "outside";
  const head = document.createElement("div");
  head.className = "card-head danger";
  head.innerHTML =
    `<span class="card-ico">${outside ? CARD_SHIELD : CARD_TRIANGLE}</span>` +
    `<span class="card-htext"><span class="card-htitle">Read file</span>` +
    `<span class="card-hsub"></span></span>`;
  head.querySelector(".card-hsub").textContent =
    outside ? "Outside the project directory" : "Sensitive — may contain secrets";
  card.appendChild(head);

  // The path in the same inset the command card uses for its $-line, minus the
  // shell dollar (nothing runs) — just the mono path and a copy chip.
  const box = document.createElement("div");
  box.className = "cmd-box";
  const code = document.createElement("span");
  code.className = "cmd-text mono";
  code.textContent = event.path;
  box.append(code, copyChip(() => code.textContent, "copy path"));
  card.appendChild(box);

  const escapes = event.escapes || [];
  if (escapes.length) card.appendChild(escapeNote(escapes));

  const feedback = feedbackField();
  card.appendChild(feedback);

  const actionsRow = document.createElement("div");
  actionsRow.className = escapes.length ? "card-actions" : "card-actions even";
  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "approve";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => answerCard(event.id, "approve", feedbackExtra(feedback));
  actionsRow.appendChild(approveBtn);
  if (escapes.length) {
    const trustBtn = document.createElement("button");
    trustBtn.type = "button";
    trustBtn.className = "trust";
    trustBtn.textContent = "Trust dir";
    trustBtn.title = `add ${escapes.join(", ")} to the session roots until the session closes`;
    trustBtn.onclick = () => answerCard(event.id, "approve_trust", feedbackExtra(feedback));
    actionsRow.appendChild(trustBtn);
  }
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "deny";
  denyBtn.textContent = "Deny";
  denyBtn.onclick = () => answerCard(event.id, "deny", feedbackExtra(feedback));
  actionsRow.appendChild(denyBtn);
  card.appendChild(actionsRow);
}

function onApprovalResolved(event) {
  // The activity trace already records the command and its "approved: <comment>"
  // outcome, so the card just disappears once decided — no lingering verdict
  // block duplicating what the timeline shows.
  const card = cards.get(event.id);
  if (!card) return;
  pendingCards = Math.max(0, pendingCards - 1);
  refreshStatusline();
  card.remove();
  cards.delete(event.id);
  // Once nothing is left to decide, restore the timeline we auto-collapsed for
  // the card (#65) — but only if the user hasn't taken over its open state.
  if (pendingCards === 0 && currentTrace && currentTrace.autoCollapsed) {
    currentTrace.el.classList.add("open");
    currentTrace.autoCollapsed = false;
    pinTrace(currentTrace);
  }
}

// ---- composer + autocomplete ---------------------------------------------
// `let`, not `const`: terminal mode swaps this node for a fresh element (#156),
// so the reference is reassigned by swapComposerInput.
let input = $("input");

// Half-typed text must survive the reloads the app performs on itself (rev
// mismatch after a server upgrade) and PWA relaunches. Saved while typing,
// plus on pagehide to catch programmatically-set values (quick replies,
// history recall) that don't fire input events; cleared once the text is
// actually sent.
input.value = localStorage.getItem("aish-draft") || "";
if (input.value) requestAnimationFrame(() => resizeInput()); // grow to fit a multi-line draft
function saveDraft() {
  if (input.value) localStorage.setItem("aish-draft", input.value);
  else localStorage.removeItem("aish-draft");
}
addEventListener("pagehide", saveDraft);

// Prompt history recall (terminal/Slack convention): ArrowUp in an empty
// composer steps back through earlier prompts, ArrowDown forward to the
// saved draft. Seeded from replayed user events, so it survives reconnects.
const promptHistory = [];
let historyIndex = null; // null = not navigating
let historyDraft = "";

function stripAttachmentNotes(text) {
  return text
    .split("\n")
    .filter((line) => !/^\[(attached file|image attached|document attached):/.test(line))
    .join("\n")
    .trim();
}

function rememberPrompt(text) {
  if (text && promptHistory[promptHistory.length - 1] !== text) promptHistory.push(text);
  if (promptHistory.length > 100) promptHistory.shift();
  historyIndex = null;
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.24)}px`;
  // Once the text grows past a couple of lines, let it take the full composer
  // width with the buttons tucked onto a row below (#97) — a narrow multi-line
  // box beside the buttons wastes the screen. Not in terminal mode.
  //
  // Sticky (#114): going full-width makes the box wider, so the SAME text wraps
  // to fewer lines and scrollHeight drops back under the threshold — a bare
  // scrollHeight>72 test then flip-flops tall/short on every keystroke. Once
  // tall, stay tall until the input is fully cleared (or terminal mode takes
  // over), so it only collapses back when you've emptied it.
  const composer = $("composer");
  const stayTall = composer.classList.contains("tall") && input.value !== "";
  composer.classList.toggle("tall", !cmdMode && (input.scrollHeight > 72 || stayTall));
  updateEmptyHint(); // draft state gates the empty-chat hint (#132)
}

// Put a previous prompt's text back in the composer. An EXPLICIT action (the
// reuse chip on the message), not a click on the whole bubble — the big bubble
// surface made stray taps clobber a draft (#155). Only fills an empty composer.
function refillComposer(text) {
  text = stripAttachmentNotes(text);
  if (!text) return;
  if (input.value.trim() && input.value.trim() !== text) {
    showToast("clear the input first to reuse this prompt");
    return;
  }
  input.value = text;
  input.setSelectionRange(text.length, text.length);
  resizeInput();
  input.focus();
}

// A chip (beside copy) that refills the composer with the message text.
function reuseChip(getText) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip"; // same styling, sits next to the copy chip
  btn.title = "reuse this prompt";
  btn.setAttribute("aria-label", "reuse this prompt");
  btn.append(pencilIcon());
  btn.onclick = () => refillComposer(getText());
  return btn;
}

function recallHistory(key) {
  if (key === "ArrowUp") {
    if (!promptHistory.length || (input.value !== "" && historyIndex === null)) return false;
    if (historyIndex === null) {
      historyDraft = input.value;
      historyIndex = promptHistory.length;
    }
    if (historyIndex > 0) historyIndex -= 1;
    input.value = promptHistory[historyIndex];
  } else {
    if (historyIndex === null) return false;
    historyIndex += 1;
    if (historyIndex >= promptHistory.length) {
      historyIndex = null;
      input.value = historyDraft;
    } else {
      input.value = promptHistory[historyIndex];
    }
  }
  const end = input.value.length;
  input.setSelectionRange(end, end);
  resizeInput();
  return true;
}

const SLASH_COMMANDS = [
  ["/model", "switch model — opens the searchable picker"],
  ["/resume", "search & resume an earlier session"],
  ["/delete", "delete an earlier session (trash icon in the drawer)"],
  ["/new", "fresh conversation in a new session"],
  ["/fork", "branch this conversation into a new session (original untouched)"],
  ["/learn", "save this conversation's learnings as skills/memory"],
  ["/feedback", "file a bug or idea as a GitHub issue"],
  ["/cd", "change working directory (re-anchors approval root)"],
  ["/add-dir", "allow auto-approved work in another tree"],
  ["/jobs", "list background jobs"],
  ["/session", "show this session's log path (copyable)"],
  ["/mic", "test speech recognition (mic diagnostic)"],
  ["/help", "about aish web"],
];

const suggest = { items: [], index: 0, kind: null, fragment: "" };

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  submitInput();
});

// The composer's listeners live in named functions so they can be re-bound to
// the fresh #input node terminal mode swaps in (attachInputListeners, #156).
function onInputKeydown(e) {
  if (!$("suggest").hidden) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      suggest.index = (suggest.index + (e.key === "ArrowDown" ? 1 : -1)
        + suggest.items.length) % suggest.items.length;
      paintSuggest();
      return;
    }
    if (e.key === "Tab" || e.key === "Enter") {
      const chosen = suggest.items[suggest.index];
      // Enter on an exactly-typed command/slash submits instead of re-completing.
      const exact = (suggest.kind === "slash" || suggest.kind === "cmd")
        && chosen[0] === input.value.trim();
      if (e.key === "Tab" || !exact) {
        e.preventDefault();
        acceptSuggestion(chosen);
        return;
      }
    }
    if (e.key === "Escape") {
      // First Esc only closes the suggestion popup; a second one leaves the
      // mode (#143) — so don't let this bubble to the global escapeExit.
      e.stopPropagation();
      hideSuggest();
      return;
    }
  }
  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && recallHistory(e.key)) {
    e.preventDefault();
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitInput();
  }
}

// iOS soft keyboards don't fire a reliable Enter keydown on a <textarea> — the
// return key arrives as a beforeinput/insertLineBreak. In terminal mode that
// key must RUN the command like a real shell, not drop a newline. Desktop is
// handled by the keydown above (which cancels this default), so no double-run.
function onInputBeforeInput(e) {
  if (cmdMode && e.inputType === "insertLineBreak") {
    e.preventDefault();
    submitCommand();
  }
}

function onInputInput() {
  // `!` as the sole character of an empty composer enters terminal mode (#100),
  // consuming the `!` — the mode's prompt implies it from then on.
  if (!cmdMode && input.value === "!") { enterCmdMode(); return; }
  // Terminal mode owns the input: no chat draft or history-recall, but it has
  // its own command/file autocomplete (updateSuggest branches on cmdMode).
  if (cmdMode) { resizeInput(); updateSuggest(); return; }
  if (!input.value) pendingSpeak = false; // cleared a dictated draft → don't speak the reply
  historyIndex = null; // typing leaves history-recall mode
  saveDraft();
  resizeInput();
  updateSuggest();
}

// Bind every composer listener to `el`. Terminal mode swaps #input for a fresh
// node (so iOS reads the terminal keyboard attributes at first focus — #156),
// and this re-binds the same handlers to whichever node is currently active.
function attachInputListeners(el) {
  el.addEventListener("focus", updateScrollButton);  // arrows hide on focus (#115)
  el.addEventListener("blur", updateScrollButton);
  el.addEventListener("keydown", onInputKeydown);
  el.addEventListener("beforeinput", onInputBeforeInput);
  el.addEventListener("input", onInputInput);
}
attachInputListeners(input);

function atFragment(text) {
  const at = text.lastIndexOf("@");
  if (at < 0 || (at > 0 && !/\s/.test(text[at - 1]))) return null;
  const fragment = text.slice(at + 1);
  return /\s/.test(fragment) ? null : fragment;
}

const requestFiles = debounce((query) => send({ type: "files", query }), 120);

// Common shell commands offered as first-word completions in terminal mode.
// Fallback first-word completions until this user has run enough ! commands to
// build a personal history (#104). Once cmdHistory (from hello) is non-empty it
// takes over — the palette is the user's own successful commands, aliases and all.
const SHELL_COMMANDS = [
  "cat", "cd", "cp", "curl", "echo", "find", "git", "grep", "head", "less",
  "ls", "make", "mkdir", "mv", "node", "npm", "pwd", "python", "rm", "tail",
  "touch", "uv",
];
let cmdHistory = []; // user's own successful commands, most-run first, from hello

function updateSuggest() {
  if (cmdMode) { updateCmdSuggest(); return; }
  const text = input.value;
  const before = text.slice(0, input.selectionStart ?? text.length);
  if (text.startsWith("/") && !text.includes("\n") && !before.includes(" ")) {
    const items = SLASH_COMMANDS.filter(([cmd]) => cmd.startsWith(before));
    if (items.length) {
      suggest.items = items;
      suggest.index = 0;
      suggest.kind = "slash";
      paintSuggest();
      return;
    }
  } else if (!text.startsWith("/")) {
    const fragment = atFragment(before);
    if (fragment !== null) {
      suggest.fragment = fragment;
      suggest.kind = "file";
      requestFiles(fragment);
      return; // popover shows when file_list arrives
    }
  }
  hideSuggest();
}

// Terminal-mode completion: first word → shell command list; later tokens →
// files/folders in the current dir via the existing `files` backend, no `@`.
function updateCmdSuggest() {
  const text = input.value;
  const before = text.slice(0, input.selectionStart ?? text.length);
  if (!before.includes(" ")) {
    // History-first: prefix-match the user's own commands, keeping their stored
    // (correctly-cased) form. Case-insensitive match so iOS autocapitalization
    // (`Ls`, `Git`) still hits. Falls back to the static list only until any
    // history exists — the palette is already frequency+recency ranked (#104).
    const source = cmdHistory.length ? cmdHistory : SHELL_COMMANDS;
    const lower = before.toLowerCase();
    const items = before
      ? source.filter((c) => c.toLowerCase().startsWith(lower)).map((c) => [c, ""])
      : [];
    if (items.length) {
      suggest.items = items;
      suggest.index = 0;
      suggest.kind = "cmd";
      paintSuggest();
      return;
    }
    hideSuggest();
    return;
  }
  const token = before.slice(before.lastIndexOf(" ") + 1);
  if (!token) { hideSuggest(); return; } // don't query on a bare argument space
  suggest.fragment = token;
  suggest.kind = "cmdfile";
  requestFiles(token); // popover shows when file_list arrives
}

function onFileList(event) {
  const fileKind = suggest.kind === "file" || suggest.kind === "cmdfile";
  if (!fileKind || event.query !== suggest.fragment) return;
  if (!event.files.length) { hideSuggest(); return; }
  suggest.items = event.files.map((path) => [path, ""]);
  suggest.index = 0;
  paintSuggest();
}

function paintSuggest() {
  const box = $("suggest");
  box.replaceChildren();
  suggest.items.forEach(([label, desc], i) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "suggest-row" + (i === suggest.index ? " active" : "");
    const name = document.createElement("span");
    name.className = "mono";
    name.textContent = label;
    row.appendChild(name);
    if (desc) {
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = desc;
      row.appendChild(meta);
    }
    row.onclick = () => acceptSuggestion(suggest.items[i]);
    box.appendChild(row);
  });
  box.hidden = !suggest.items.length;
}

function hideSuggest() {
  $("suggest").hidden = true;
  suggest.items = [];
  suggest.kind = null;
}

function acceptSuggestion([value]) {
  const kind = suggest.kind; // hideSuggest() clears it below
  if (kind === "slash") {
    input.value = value + " ";
  } else if (kind === "cmd") {
    // Replace the first word (everything before the caret) with the command.
    const pos = input.selectionStart ?? input.value.length;
    input.value = value + " " + input.value.slice(pos);
    const caret = value.length + 1;
    input.setSelectionRange(caret, caret);
  } else {
    // file (@-mention) and cmdfile (bare path token): replace the current
    // token. Mentions keep their leading @; command args have no prefix.
    const pos = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, pos);
    const start = kind === "cmdfile"
      ? before.lastIndexOf(" ") + 1
      : before.lastIndexOf("@") + 1;
    const inserted = value.endsWith("/") ? value : value + " ";
    input.value = input.value.slice(0, start) + inserted + input.value.slice(pos);
    const caret = start + inserted.length;
    input.setSelectionRange(caret, caret);
  }
  hideSuggest();
  input.focus();
  if (kind !== "slash") updateSuggest();
}

function submitInput() {
  hideSuggest();
  if (dictating) stopDictation(); // tapping send finishes an in-progress dictation
  if (cmdMode) { submitCommand(); return; }
  let text = input.value.trim();
  if (text.startsWith("/")) {
    // Only clear once the command is handled/sent — an unknown command, an
    // ambiguous prefix, or a failed send keeps the text so it isn't lost (#101).
    if (handleSlash(text)) {
      rememberPrompt(text); // slash commands never echo back as user events
      input.value = "";
      localStorage.removeItem("aish-draft");
      resizeInput(); // #117: also drop the .tall class now the input is empty
    }
    return;
  }
  if (!text && !attachments.length) return;
  // The server decides per-backend whether attachments go to the model
  // natively (vision) or as path notes for the gated tools.
  if (send({ type: "task", text, attachments: attachments.map((a) => a.path) })) {
    if (pendingSpeak) { speakNextReply = true; pendingSpeak = false; } // dictated → speak reply
    maybeRequestNotifyPermission();
    input.value = "";
    localStorage.removeItem("aish-draft");
    resizeInput(); // #117: recompute height AND drop the .tall class now it's empty
    attachments = [];
    renderAttachments();
    scrollToEndSettled();
  }
}

// Terminal mode submit: `exit` leaves the mode; anything else runs as a shell
// command via the existing `!` path and keeps the mode active for the next one.
function submitCommand() {
  const text = input.value.trim();
  if (text === "exit" || text === "quit") { exitCmdMode(); return; }
  if (!text) { input.focus(); return; }
  if (send({ type: "task", text: `!${text}`, attachments: [] })) {
    rememberPrompt(text);
    input.value = "";
    resizeInput();
    scrollToEndSettled();
  }
  input.focus(); // stay in the terminal, ready for the next command
}

const SLASH_ALL = SLASH_COMMANDS.map(([cmd]) => cmd).concat(["/clear", "/branch", "/dir-add", "/quit", "/exit"]);

function handleSlash(text) {
  let [command, ...rest] = text.split(/\s+/);
  const arg = rest.join(" ");
  if (!SLASH_ALL.includes(command)) {
    const matches = SLASH_ALL.filter((cmd) => cmd.startsWith(command));
    if (matches.length === 1) command = matches[0];
    else if (matches.length > 1) {
      showToast(`ambiguous — ${matches.join(" or ")}?`);
      return false; // keep the text so the user can disambiguate (#101)
    }
  }
  // Return whether the input may be cleared: true once the command is handled
  // or its message reaches the backend; false if it couldn't be sent or was
  // unknown, so submitInput preserves the typed text instead of losing it.
  switch (command) {
    case "/model": openModelSheet(arg); return true;
    case "/resume": case "/delete": openSessionsSheet(arg); return true;
    case "/new": case "/clear": return send({ type: "new" });
    case "/fork": case "/branch": return send({ type: "fork" });
    case "/cd": return arg ? send({ type: "cd", path: arg }) : (openDirSheet(), true);
    case "/add-dir": case "/dir-add":
      return arg ? send({ type: "add_dir", path: arg }) : (openSheet("workspace-sheet"), true);
    case "/learn": case "/feedback": {
      // Run as a task: the server swaps the text for the expanded prompt
      // (cli.parse_learn / parse_feedback) while the transcript shows what was
      // typed. Include attachments — /feedback WITH files uses the classic
      // upload flow, and without this they were silently dropped (#152).
      const sent = send({ type: "task", text, attachments: attachments.map((a) => a.path) });
      if (sent) { attachments = []; renderAttachments(); scrollToEndSettled(); }
      return sent;
    }
    case "/jobs": openSheet("workspace-sheet"); return send({ type: "jobs" });
    case "/session": copyLogPath(); return true; // path came in on hello (#146)
    case "/mic": openMicSheet(); return true;
    case "/help": openSheet("workspace-sheet"); return true;
    case "/quit": case "/exit": showToast("just close the tab — sessions persist"); return true;
    case "/debug": reportViewport("manual"); showToast("viewport state sent to server log"); return true;
    default: showToast(`unknown command ${command}`); return false;
  }
}

// ---- attachments ---------------------------------------------------------
let attachments = []; // {name, path}

// The + button opens the composer actions popover (attach / reference / slash
// / photo / feedback / terminal); it sits above the button, iOS-style.
//
// The menu is position:fixed and placed with viewport coordinates read from the
// + button's rect. On iPhone, tapping + blurs the input, so the keyboard
// dismisses and the visual viewport slides back DOWN — a placement captured
// before that settles leaves the menu stranded mid-screen (#103). So the
// coordinates are recomputed from the CURRENT button rect: once on open, again
// on the next frames, and live on every visualViewport resize/scroll while the
// menu is open. offsetHeight is read after the menu is laid out (hidden-then-
// measured) because the 6-item menu is taller than the compose bar.
function positionComposerMenu() {
  const menu = $("composer-actions");
  if (menu.hidden) return; // settle timers can fire after the menu was closed
  const anchor = $("attach").getBoundingClientRect();
  menu.style.left = `${anchor.left}px`;
  menu.style.top = `${anchor.top - menu.offsetHeight - 6}px`;
}

let composerMenuTracking = false;

function stopComposerMenuTracking() {
  if (!composerMenuTracking) return;
  composerMenuTracking = false;
  if (window.visualViewport) {
    visualViewport.removeEventListener("resize", positionComposerMenu);
    visualViewport.removeEventListener("scroll", positionComposerMenu);
  }
}

function openComposerMenu() {
  const menu = $("composer-actions");
  menu.style.visibility = "hidden"; // measure offsetHeight before placing
  menu.hidden = false;
  positionComposerMenu();
  menu.style.visibility = "";
  $("backdrop").hidden = false;
  if (window.visualViewport && !composerMenuTracking) {
    composerMenuTracking = true;
    visualViewport.addEventListener("resize", positionComposerMenu);
    visualViewport.addEventListener("scroll", positionComposerMenu);
  }
  // The keyboard dismissal (a side effect of blurring the input) can settle
  // WITHOUT any visualViewport event, so re-place across the animation window
  // too — each call is a cheap no-op once the rect has stopped moving.
  requestAnimationFrame(positionComposerMenu);
  for (const ms of [50, 150, 350, 700]) setTimeout(positionComposerMenu, ms);
}

$("attach").onclick = () => {
  if (cmdMode) { exitCmdMode(); return; } // the + is an × while in terminal mode
  const menu = $("composer-actions");
  if (!menu.hidden) { closeSheets(); return; }
  openComposerMenu();
};

$("composer-actions").addEventListener("click", (e) => {
  const item = e.target.closest(".action-item");
  if (!item) return;
  closeSheets();
  switch (item.dataset.act) {
    case "attach": $("file-input").click(); break;
    case "photo": $("photo-input").click(); break;
    case "reference": composerInsert("@"); break;
    case "slash": composerInsert("/"); break;
    // Prefill the trigger and let the user add detail (or just send) — the
    // server expands /feedback into the issue-filing flow (parse_feedback).
    case "feedback": composerInsert("/feedback "); break;
    case "terminal": enterCmdMode(); break; // the `!` shell-command input mode
    // The global console has its own top-bar button + Ctrl+\; not in this menu.
  }
});

// ---- terminal / command mode (#100) --------------------------------------
// A composer mode that runs shell commands multi-turn. It reuses the existing
// `!command` path (server _launch runs it as the user's own action, no model,
// no approval gate) — this layer is purely the terminal-styled input, the
// dynamic `dir $` prompt, and enter/exit affordances. Autocomplete is a
// separate follow-up (issue #100, step 2).
let cmdMode = false;

function cmdPromptLabel() {
  // Just `$ ` — the directory name ate too much width on mobile, and the
  // top-bar chip already shows the cwd.
  return "$ ";
}

function refreshCmdPrompt() {
  if (cmdMode) $("cmd-prompt").textContent = cmdPromptLabel();
}

// iOS reads autocapitalize/autocorrect/spellcheck when the field gains focus,
// so a blur+refocus is needed for a mid-focus mode switch to take effect —
// otherwise the terminal keyboard would still capitalise and autocorrect
// commands. `raw` = command-line typing; false restores chat-message typing.
function setInputTyping(raw) {
  input.setAttribute("autocapitalize", raw ? "none" : "sentences");
  input.setAttribute("autocorrect", raw ? "off" : "on");
  input.setAttribute("spellcheck", raw ? "false" : "true");
  if (document.activeElement === input) { input.blur(); input.focus(); }
}

function enterCmdMode() {
  if (cmdMode) return;
  cmdMode = true;
  $("composer").classList.add("cmd-mode");
  $("cmd-prompt").hidden = false;
  refreshCmdPrompt();
  $("attach").setAttribute("aria-label", "exit terminal mode");
  swapComposerInput(true); // fresh #input with terminal keyboard attrs (#156)
  resizeInput();
  setInputTyping(true);
}

function exitCmdMode() {
  if (!cmdMode) return;
  cmdMode = false;
  $("composer").classList.remove("cmd-mode");
  $("cmd-prompt").hidden = true;
  hideSuggest();
  $("attach").setAttribute("aria-label", "actions");
  swapComposerInput(false); // fresh #input back to prose keyboard attrs (#156)
  resizeInput();
  setInputTyping(false);
}

// Replace #input with a fresh <textarea> that carries the target mode's keyboard
// attributes FROM BIRTH, then focus it. This is the reliable iOS fix for #156:
// iOS reads autocorrect/autocapitalize/spellcheck (and enterkeyhint) only when a
// field is first focused and ignores changes to an already-focused field — so
// terminal mode can't just flip attributes on the shared composer. Swapping in a
// new node and focusing it WITHIN the `!` keystroke (or exit tap) gesture makes
// iOS pick up the terminal keyboard immediately. The clone keeps id="input", so
// all composer CSS applies unchanged; every listener re-binds to the new node.
function swapComposerInput(cmd) {
  const fresh = input.cloneNode(false);
  fresh.value = "";
  if (cmd) {
    fresh.setAttribute("autocorrect", "off");
    fresh.setAttribute("autocapitalize", "off");
    fresh.setAttribute("spellcheck", "false");
    fresh.setAttribute("enterkeyhint", "go"); // iOS return key reads "Go" — runs the command
    fresh.placeholder = "";
  } else {
    fresh.setAttribute("autocorrect", "on");
    fresh.setAttribute("autocapitalize", "sentences");
    fresh.setAttribute("spellcheck", "true");
    fresh.removeAttribute("enterkeyhint");
    fresh.placeholder = "Ask aish";
  }
  input.replaceWith(fresh);
  input = fresh;
  attachInputListeners(input);
  input.focus(); // synchronous, within the user gesture → iOS shows the new keyboard
}

// Insert a trigger char and fire the input flow (mention / slash suggestions).
function composerInsert(ch) {
  input.focus();
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.setRangeText(ch, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

// Esc leaves whichever full-input mode is active (#143): it closes the
// interactive PTY overlay (killing the process — no dangling shell) or exits
// terminal mode, then returns to the normal composer. Order matters: the PTY
// overlay sits on top of terminal mode. Returns true when it acted, so callers
// swallow the key; when neither mode is active it does nothing and Esc is left
// to its other handlers (suggest dismissal, sheet close). The on-screen "esc"
// key still sends \x1b for programs that need it — only the hardware key means
// "leave". [ESC-EXIT-START]
function escapeExit() {
  // Esc leaves the OLD `!` terminal-input mode. It deliberately does NOT touch
  // the global console: there Esc is a real key (vim/tmux/less/…), so it passes
  // through to the PTY — close the console with its button or Ctrl+\ instead.
  if (cmdMode) { exitCmdMode(); return true; } // exitCmdMode already focuses input
  return false;
}
// [ESC-EXIT-END]

// ---- global interactive console overlay controller (#148 follow-up) ------
// ONE "Quake console" for the whole app: a floating overlay above whatever chat
// is shown, openable from any chat and untouched by chat-switches. A real
// terminal — xterm.js (vendored global) renders console_out and captures
// keystrokes, sent to the server's ONE PTY as `console_in` and NEVER echoed
// locally (the PTY echoes, so password prompts mask for free). Everything here
// is user-driven; the model has no path to console_in (the server enforces
// that too). Close = HIDE (the console keeps running server-side); a separate
// Kill destroys it. Backed by tmux server-side so it survives aish-web restarts.
let consoleTerm = null; // the xterm.js Terminal while the overlay is open, else null
let consoleFit = null;
let consoleOpen = false;
// The text of the most recent tmux copy (arrives via OSC 52). iOS blocks the
// clipboard write from that incoming-data handler (no user gesture), so we stash
// it here and let the key-row Copy button write it within the user's tap.
let lastConsoleClip = null;
// The Ctrl chip is a 3-state sticky: "off" → tap → "armed" (one key sent as
// Ctrl+key, then off) → double-tap → "locked" (EVERY key is Ctrl+key until you
// tap to unlock — for repeated C-F page-downs / long Ctrl combos).
let consoleCtrlMode = "off"; // "off" | "armed" | "locked"
let lastCtrlTap = 0;
// Select-region mode (touch): a phone can't drive tmux's drag-select (iOS only
// synthesizes taps, not the continuous move tmux needs), so this mode paints a
// native selection directly over the DOM rows from finger position instead. The
// scroll hijack is suspended while it's on.
let consoleSelectMode = false;

// Catppuccin Mocha — matches the CSS palette applied to the command/code
// surfaces, so the interactive terminal reads as one theme with them.
const CATPPUCCIN_MOCHA = {
  background: "#1e1e2e", foreground: "#cdd6f4",
  cursor: "#f5e0dc", cursorAccent: "#1e1e2e", selectionBackground: "#585b70",
  black: "#45475a", red: "#f38ba8", green: "#a6e3a1", yellow: "#f9e2af",
  blue: "#89b4fa", magenta: "#cba6f7", cyan: "#94e2d5", white: "#bac2de",
  brightBlack: "#585b70", brightRed: "#f38ba8", brightGreen: "#a6e3a1",
  brightYellow: "#f9e2af", brightBlue: "#89b4fa", brightMagenta: "#f5c2e7",
  brightCyan: "#94e2d5", brightWhite: "#a6adc8",
};

function setConsoleStatus(text, exited) {
  const el = $("pty-status");
  el.textContent = text || "";
  el.classList.toggle("exited", Boolean(exited));
}

function setConsoleCtrlMode(mode) {
  consoleCtrlMode = mode;
  const chip = document.querySelector('.pty-keys button[data-key="ctrl"]');
  if (chip) {
    chip.classList.toggle("armed", mode === "armed");
    chip.classList.toggle("locked", mode === "locked");
  }
}

function setConsoleSelectMode(on, quiet) {
  consoleSelectMode = on;
  const chip = document.querySelector('.pty-keys button[data-key="select"]');
  if (chip) chip.classList.toggle("on", on);
  const scr = $("pty-screen");
  if (scr) scr.classList.toggle("selecting", on);
  if (on) {
    // The paint writes into the DOCUMENT selection, which won't take while the
    // xterm textarea is focused (keyboard up). Blur it — dropping the keyboard —
    // so the selection sticks. Tapping the chip did this by side effect; a hold
    // (on the terminal itself) left the textarea focused, so its drag never
    // selected. This unifies both entry paths.
    const ta = consoleTerminalTextarea();
    if (ta) ta.blur();
    if (!quiet) showToast("Select mode — drag to select · tap Select again to scroll");
  } else if (window.getSelection) {
    // Clear any lingering selection on exit so the scroll handler resumes cleanly
    // (it stands down whenever text is selected).
    window.getSelection().removeAllRanges();
  }
}

let consoleLockY = 0; // page scroll offset captured while the console freezes it
let lastConsoleSpawn = 0; // when the current console attached — crash-loop guard for auto-respawn

// Press feedback for the on-screen key chips: a haptic tick (where the platform
// exposes it — Android; iOS Safari has no web vibrate) plus a visual blink that
// works everywhere.
function consoleKeyFeedback(btn) {
  if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) { /* ignore */ } }
  btn.classList.remove("flash");
  void btn.offsetWidth; // reflow so the animation restarts on rapid repeats
  btn.classList.add("flash");
}

// The current console selection as text. Desktop uses xterm's own mouse-driven
// selection; touch uses the browser's NATIVE selection over the DOM rows (which
// xterm's getSelection() doesn't see), so prefer that when it's inside the
// terminal. Callers: Copy, Share, and the Share-button visibility.
function consoleHasNativeSelection() {
  const s = window.getSelection && window.getSelection();
  if (!s || s.isCollapsed || !s.toString().trim()) return false;
  const scr = $("pty-screen");
  const node = s.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentNode);
  return Boolean(scr && el && scr.contains(el));
}
function consoleSelectionText() {
  if (consoleHasNativeSelection()) return window.getSelection().toString();
  return consoleTerm ? consoleTerm.getSelection() : "";
}

function consoleCopy() {
  if (!consoleTerm) return;
  // Whatever is selected, else the whole screen — dragging to select is hard on
  // touch, so "copy" with no selection grabs everything. copyText() has the
  // execCommand fallback that works on the plain-http LAN server (no clipboard API).
  let text = consoleSelectionText();
  // A fresh tmux copy (drag / double-tap → OSC 52) that iOS wouldn't let us
  // write passively: consume it here, inside this tap's gesture. Consume-once so
  // a later "copy everything" tap doesn't resurrect a stale selection.
  if (!text && lastConsoleClip) { text = lastConsoleClip; lastConsoleClip = null; }
  if (!text) { consoleTerm.selectAll(); text = consoleTerm.getSelection(); consoleTerm.clearSelection(); }
  if (!text || !text.trim()) { showToast("nothing to copy"); return; }
  copyText(text).then((ok) => showToast(ok ? "copied" : "copy blocked"));
  if (consoleSelectMode) setConsoleSelectMode(false); // painted a region → done, back to scroll
}

function consolePaste() {
  // readText only works in a secure context; on plain http fall back to a native
  // long-press paste onto the terminal (xterm forwards it as console_in).
  if (navigator.clipboard && navigator.clipboard.readText && window.isSecureContext) {
    navigator.clipboard.readText().then(
      (t) => { if (t) consoleSend(t); if (consoleTerm) consoleTerm.focus(); },
      () => { if (consoleTerm) consoleTerm.focus(); showToast("long-press the terminal, then Paste"); });
  } else {
    if (consoleTerm) consoleTerm.focus();
    showToast("long-press the terminal, then Paste");
  }
}

function consoleTerminalTextarea() {
  const s = $("pty-screen");
  return s ? s.querySelector(".xterm-helper-textarea") : null;
}

// The ⌨ chip toggles the soft keyboard: focus summons it, blur hides it (the
// only reliable way to dismiss the iOS keyboard).
function consoleToggleKeyboard() {
  const ta = consoleTerminalTextarea();
  if (ta && document.activeElement === ta) ta.blur();
  else if (consoleTerm) consoleTerm.focus();
}

let consoleFontSize = Math.min(28, Math.max(8, +localStorage.getItem("ptyFontSize") || 13));
function consoleFontStep(delta) {
  consoleFontSize = Math.max(8, Math.min(28, consoleFontSize + delta));
  localStorage.setItem("ptyFontSize", String(consoleFontSize));
  if (consoleTerm) { consoleTerm.options.fontSize = consoleFontSize; consoleFitAndResize(); }
}

function consoleSend(data) {
  if (consoleOpen) send({ type: "console_in", data });
}

// [OSC52-DECODE-START]
// Decode an OSC 52 clipboard payload ("<selection>;<base64>") to its text, or
// null for a clipboard-READ request ("?") or malformed data. base64 → utf-8 via
// atob + escape (atob yields a binary string; escape/decodeURIComponent widen
// it back to the original multibyte characters).
function oscClipboardText(data) {
  const semi = data.indexOf(";");
  const b64 = semi >= 0 ? data.slice(semi + 1) : "";
  if (!b64 || b64 === "?") return null;
  try { return decodeURIComponent(escape(atob(b64))); } catch (e) { return null; }
}
// [OSC52-DECODE-END]

// [CONSOLE-LINKS-START]
// A link provider for the console terminal that finds URLs even when they wrap
// across rows tmux did NOT mark as soft-wrapped (#153). We reconstruct a logical
// line by joining a row with its neighbours whenever the row is packed to the
// right margin (no trailing space) — the geometry signal for a hard wrap — OR
// xterm flagged the next row isWrapped (native soft-wrap). Then we scan the
// joined text for http(s) URLs and map each match back to buffer coordinates.
function consoleLinkProvider(term, onOpen) {
  const URL_RE = /https?:\/\/[^\s"'`<>(){}\[\]|\\^]+/g;
  const stripTrailing = (s) => s.replace(/[)\].,;:!?'"]+$/, ""); // drop sentence punctuation
  const MAX_ROWS = 40; // cap the join so a screenful of full lines can't run away
  const lineStr = (buf, y, trim) => { const ln = buf.getLine(y); return ln ? ln.translateToString(trim) : null; };
  return {
    provideLinks(yOneBased, cb) {
      const buf = term.buffer.active;
      const cols = term.cols;
      const y0 = yOneBased - 1;
      if (lineStr(buf, y0, true) == null) { cb(undefined); return; }
      // Row y continues onto y+1 when the next row is a soft-wrap OR this row is
      // filled to the last column (tmux hard-wrap — no trailing space to trim).
      const continues = (y) => {
        const cur = buf.getLine(y), nxt = buf.getLine(y + 1);
        if (!cur || !nxt) return false;
        if (nxt.isWrapped) return true;
        return cur.translateToString(true).length >= cols;
      };
      let top = y0, bottom = y0;
      while (top > 0 && (y0 - top) < MAX_ROWS && continues(top - 1)) top--;
      while ((bottom - y0) < MAX_ROWS && continues(bottom)) bottom++;
      // Concatenate the block; a row's chars map 1:1 to its columns (ASCII URLs),
      // so a global string index maps to (row, col) via the recorded offsets.
      const parts = [], starts = [];
      let acc = "";
      for (let y = top; y <= bottom; y++) {
        starts.push(acc.length);
        const s = lineStr(buf, y, true) || "";
        parts.push(s);
        acc += s;
      }
      const mapIdx = (gi) => {
        let r = 0;
        while (r + 1 < starts.length && starts[r + 1] <= gi) r++;
        return { y: top + r, x: gi - starts[r] };
      };
      const links = [];
      let m;
      URL_RE.lastIndex = 0;
      while ((m = URL_RE.exec(acc))) {
        const text = stripTrailing(m[0]);
        if (!text) continue;
        try { new URL(text); } catch (e) { continue; } // reject non-URL matches
        const s = mapIdx(m.index), e = mapIdx(m.index + text.length - 1);
        links.push({
          text,
          range: { start: { x: s.x + 1, y: s.y + 1 }, end: { x: e.x + 1, y: e.y + 1 } },
          activate: (ev, uri) => onOpen(ev, uri),
        });
      }
      cb(links.length ? links : undefined);
    },
  };
}
// [CONSOLE-LINKS-END]

// Reflow to the current overlay size and tell the server (TIOCSWINSZ), so the
// program wraps where xterm shows it.
function consoleFitAndResize() {
  if (!consoleOpen || !consoleFit || !consoleTerm) return;
  try { consoleFit.fit(); } catch (e) { /* container not laid out yet */ }
  send({ type: "console_resize", cols: consoleTerm.cols, rows: consoleTerm.rows });
}

// Toggle the Quake console: open if hidden, hide if shown. The single entry
// point for the top-bar icon, the ＋-menu item, and the Ctrl/Cmd+\ shortcut.
function toggleConsole() {
  if (consoleOpen) { hideConsole(); return; }
  openConsole();
}

function openConsole() {
  closeSheets();
  // Ask the server to attach us to the GLOBAL console (spawns it on first open,
  // reattaches to the surviving tmux session after a restart). No command — the
  // console's command is fixed server-side.
  if (!send({ type: "console_open" })) {
    showToast("not connected — reconnecting…");
    return;
  }
  if (consoleOpen) return; // already showing (e.g. a reconnect reattach)
  if (location.hash !== "#console") history.replaceState(null, "", "#console"); // deep-link: survives reload/restart
  $("pty-overlay").hidden = false;
  // Freeze the page behind the overlay at its current scroll offset (iOS: a
  // position:fixed body is the only reliable lock; restored on close).
  consoleLockY = window.scrollY || 0;
  document.body.style.top = `-${consoleLockY}px`;
  document.body.classList.add("console-open");
  $("pty-share").hidden = true;
  setConsoleStatus("attaching…");

  const screen = $("pty-screen");
  screen.textContent = "";
  consoleTerm = new Terminal({
    cursorBlink: true,
    // The config-served mono font (aish-mono) if present, else system mono —
    // the same var(--mono) stack the code blocks and command output use.
    fontFamily: '"aish-mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
    fontSize: consoleFontSize,
    scrollback: 5000,
    // Mouse reporting is forwarded to the program when it asks for it (tmux/vim
    // mouse mode), so a swipe scrolls the app's panes; otherwise the touch
    // scrolls xterm's own scrollback.
    theme: CATPPUCCIN_MOCHA,
  });
  consoleFit = new FitAddon.FitAddon();
  consoleTerm.loadAddon(consoleFit);
  // Clickable URLs (#148/#153): auth CLIs print a login URL that wraps across
  // rows. xterm's own web-links addon only rejoins rows it sees as soft-wrapped
  // (isWrapped) — but our console runs inside tmux, which repaints its pane with
  // absolute cursor moves, so a wrapped URL arrives as SEPARATE, non-wrapped
  // rows the addon can't join. consoleLinkProvider joins by GEOMETRY instead.
  consoleTerm.registerLinkProvider(
    consoleLinkProvider(consoleTerm, (event, uri) => window.open(uri, "_blank", "noopener"))
  );
  // Clipboard bridge (#153): with tmux `set-clipboard on`, a copy on the REMOTE
  // terminal (a mouse-drag selection, `y` in copy-mode, vim, …) emits an OSC 52
  // sequence carrying the text. xterm.js ignores OSC 52 by default; handle it and
  // write the decoded text to the LOCAL desktop clipboard. A "?" payload is a
  // clipboard-READ request — we never answer it (don't leak the clipboard back).
  consoleTerm.parser.registerOscHandler(52, (data) => {
    const text = oscClipboardText(data);
    if (text) {
      // Stash it so the Copy button can grab it within a real tap, then TRY an
      // immediate write — succeeds on desktop; on iOS (no user gesture here) it
      // fails, so tell the user to tap Copy, which does have the gesture.
      lastConsoleClip = text;
      copyText(text).then((ok) =>
        showToast(ok ? "copied to clipboard" : "selection ready — tap Copy to grab it"));
    }
    return true;
  });
  consoleTerm.open(screen);
  // Focus guard: while Select mode is on, the terminal textarea must stay
  // BLURRED (keyboard down) or the document selection won't stick. xterm
  // re-focuses its textarea on every touch (its mousedown handler), so a
  // one-time blur loses the race — the keyboard springs back. Re-blur on any
  // focus while selecting; harmless (and inactive) the rest of the time.
  const _selTa = consoleTerminalTextarea();
  if (_selTa) {
    _selTa.addEventListener("focus", () => {
      if (!consoleSelectMode) return;
      _selTa.blur();
      setTimeout(() => { if (consoleSelectMode && document.activeElement === _selTa) _selTa.blur(); }, 0);
    });
  }
  // THE input path; the model never reaches it. The sticky Ctrl chip (#148):
  // while "armed" or "locked" the next single char is sent as its control code
  // (Ctrl-A tmux prefix, Ctrl-F page down, …); "armed" is one-shot (disarms
  // after the key), "locked" persists until the chip is tapped again.
  consoleTerm.onData((data) => {
    if (consoleCtrlMode !== "off" && data.length === 1) {
      const c = data.toUpperCase().charCodeAt(0);
      if (c >= 64 && c <= 95) data = String.fromCharCode(c & 0x1f); // ^@ .. ^_
      if (consoleCtrlMode === "armed") setConsoleCtrlMode("off"); // one-shot
    }
    consoleSend(data);
  });
  // Hardware Esc HIDES the overlay (#143) instead of going to the program; the
  // on-screen "esc" chip still sends \x1b. Returning false stops xterm from
  // handling the key (and lets it bubble to the global Esc handler too).
  consoleTerm.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    // Esc is a REAL terminal key here (vim/tmux/less) — let xterm send it to the
    // PTY; the console is closed via its button or Ctrl+\, never Esc.
    // stopPropagation on Ctrl+\ so the document handler doesn't re-toggle the
    // console open right after we close it.
    if ((e.metaKey || e.ctrlKey) && e.key === "\\") { e.preventDefault(); e.stopPropagation(); hideConsole(); input.focus(); return false; }
    // Cmd/Ctrl+Shift+C to copy the xterm selection — never plain Ctrl+C (that's
    // SIGINT to the program). Cmd (meta) alone works too on macOS. Paste is NOT
    // intercepted: xterm handles the browser paste event natively, and grabbing
    // it here too pasted the text twice (#153).
    const copyCombo = (e.metaKey && !e.ctrlKey) || (e.ctrlKey && e.shiftKey);
    if (copyCombo && (e.key === "c" || e.key === "C")) { consoleCopy(); return false; }
    return true;
  });
  consoleTerm.onSelectionChange(() => {
    $("pty-share").hidden = !(consoleTerm && consoleTerm.getSelection().trim());
  });
  consoleOpen = true;
  lastConsoleSpawn = Date.now();
  consoleReflowViewport();
  requestAnimationFrame(consoleReflowViewport); // once the overlay has laid out
  setTimeout(consoleReflowViewport, 120); // and after the mobile keyboard settles
  // A webfont (the optional PragmataPro) loads async; xterm measures cell size at
  // open, so refit once it's ready or the grid is sized to the fallback metrics.
  if (document.fonts && document.fonts.load) {
    document.fonts.load(`16px "aish-mono"`).then(
      () => { if (consoleOpen) consoleReflowViewport(); }, () => {});
  }
  consoleTerm.focus();
}

// Hide/detach the overlay — the console keeps running server-side (reopening
// shows current state). Tells the server to stop fanning output at us.
function hideConsole() {
  if (!consoleOpen) return;
  if (padOpen()) closeConsolePad(); // never leave the mic live behind a hidden overlay
  consoleOpen = false;
  send({ type: "console_close" });
  if (location.hash === "#console") history.replaceState(null, "", location.pathname + location.search);
  const ov = $("pty-overlay");
  ov.hidden = true;
  document.body.classList.remove("console-open");
  document.body.style.top = "";
  window.scrollTo(0, consoleLockY || 0); // restore the page scroll we froze
  ov.style.height = ""; ov.style.top = ""; ov.style.paddingBottom = ""; // drop viewport-fit inline styles
  $("pty-share").hidden = true;
  setConsoleCtrlMode("off");
  if (consoleSelectMode) setConsoleSelectMode(false);
  if (consoleTerm) { consoleTerm.dispose(); consoleTerm = null; consoleFit = null; }
}

function onConsoleStarted(event) {
  if (!consoleOpen || !consoleTerm) return;
  consoleTerm.reset();
  setConsoleStatus(`${promptDir(event.cwd)} · ${event.command}`);
  consoleFitAndResize();
}

function onConsoleOut(data) {
  if (consoleTerm) consoleTerm.write(data);
}

function onConsoleExit(code) {
  if (!consoleOpen) return; // overlay already hidden — nothing to do
  // The shell/tmux ended (the user typed `exit`, or detached). Respawn in place
  // so they don't have to close & reopen — `tmux new-session -A` reattaches a
  // surviving session (detach) or starts a fresh one (exit). Guard a crash-loop:
  // if it dies again almost immediately, stop and leave the message up.
  if (Date.now() - lastConsoleSpawn < 1500) {
    setConsoleStatus(`exited (${code}) — tap the console button to retry`, true);
    if (consoleTerm) { consoleTerm.dispose(); consoleTerm = null; consoleFit = null; }
    consoleOpen = false;
    return;
  }
  if (consoleTerm) { consoleTerm.dispose(); consoleTerm = null; consoleFit = null; }
  consoleOpen = false; // let openConsole() run its full attach path again
  openConsole();
}

// Wire the overlay's controls once at load. Share sends the xterm SELECTION to
// the chat as model context (the only path from the terminal to the model, and
// only on this explicit user action — see onSelectionChange for the button's
// visibility).
$("pty-close").onclick = () => hideConsole();
$("pty-font-dec").onclick = () => consoleFontStep(-1);
$("pty-font-inc").onclick = () => consoleFontStep(1);
// The contextual Copy button in the console header: it appears only when text
// is selected, so it copies THAT selection (never "all"), then clears it and
// leaves Select mode. Handled on POINTERDOWN with preventDefault: tapping a
// button otherwise collapses the current selection before a click handler runs,
// so we'd read nothing (and neither copy nor exit). preventDefault keeps the
// selection intact, and pointerdown is still a user gesture for the clipboard.
$("pty-share").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const sel = consoleSelectionText().trim();
  if (sel) copyText(sel).then((ok) => showToast(ok ? "copied" : "copy blocked"));
  else showToast("nothing selected");
  if (window.getSelection) window.getSelection().removeAllRanges();
  if (consoleSelectMode) setConsoleSelectMode(false); // always leave Select mode
  $("pty-share").hidden = true;
});
// Native (touch) selection changes don't fire xterm's onSelectionChange, so
// mirror the Copy button's visibility off the document selection too — checking
// the combined state so a collapsed native selection hides it (unless xterm
// still holds one from a desktop mouse drag).
document.addEventListener("selectionchange", () => {
  if (!consoleOpen) return;
  $("pty-share").hidden = !consoleSelectionText().trim();
});

document.querySelector(".pty-keys").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-key]");
  if (!btn) return;
  consoleKeyFeedback(btn); // blink + haptic so a tap registers without key travel
  if (btn.dataset.key === "kb") { consoleToggleKeyboard(); return; }
  if (btn.dataset.key === "ctrl") {
    const now = Date.now();
    if (now - lastCtrlTap < 350) setConsoleCtrlMode("locked"); // double-tap → lock Ctrl
    else setConsoleCtrlMode(consoleCtrlMode === "off" ? "armed" : "off"); // tap: arm / unlock
    lastCtrlTap = now;
    if (consoleTerm) consoleTerm.focus();
    return;
  }
  if (btn.dataset.key === "select") { setConsoleSelectMode(!consoleSelectMode); return; }
  if (btn.dataset.key === "copy") { consoleCopy(); return; }
  if (btn.dataset.key === "paste") { consolePaste(); return; }
  const CTRL = { tab: "\t", esc: "\x1b" }; // arrows: the drag pad below
  const seq = CTRL[btn.dataset.key];
  if (seq != null) consoleSend(seq);
  if (consoleTerm) consoleTerm.focus();
});

// Keep the overlay inside the VISIBLE viewport and refit xterm on any viewport
// change — the iOS soft keyboard show/hide fires visualViewport resize (not
// window resize), and a fixed element does not shrink above the keyboard on its
// own, so without this the keys row hides behind the keyboard and the terminal
// keeps its old cols/rows. Rotation comes through window resize/orientationchange.
function consoleReflowViewport() {
  if (!consoleOpen) return;
  const ov = $("pty-overlay");
  if (window.visualViewport) {
    ov.style.height = `${visualViewport.height}px`;
    // Pin to the top — the body is already frozen (body.console-open). Do NOT
    // follow visualViewport.offsetTop: a swipe rubber-bands the visual viewport
    // and, tracking offsetTop, the whole overlay slid down as if scrolling (the
    // arrow-drag "console goes down" bug). offsetTop is ~0 with the body fixed.
    ov.style.top = "0px";
    // The keyboard already covers the home-indicator safe area, so drop the
    // overlay's bottom inset while it's up — otherwise it renders as a dead
    // black gap between the key row and the keyboard (#151). Restored (「」) when
    // the keyboard is down so the keys still clear the home indicator.
    const kbUp = visualViewport.height < window.innerHeight - 80;
    ov.style.paddingBottom = kbUp ? "0px" : "";
  }
  if (padOpen()) resizePadInput(); // the pad's max height follows the visible viewport
  consoleFitAndResize();
}
window.addEventListener("resize", consoleReflowViewport);
window.addEventListener("orientationchange", () => setTimeout(consoleReflowViewport, 250));
if (window.visualViewport) {
  visualViewport.addEventListener("resize", consoleReflowViewport);
  visualViewport.addEventListener("scroll", consoleReflowViewport);
}
// Block browser pinch-zoom (#148): the font A-/A+ resize the terminal instead of
// zooming the page. The viewport meta covers standalone PWAs; this catches the
// iOS Safari pinch gesture it sometimes ignores. Double-tap zoom is handled by
// the viewport meta / touch-action.
document.addEventListener("gesturestart", (e) => e.preventDefault());
document.addEventListener("gesturechange", (e) => e.preventDefault());

// iOS terminal scroll: a touch swipe doesn't scroll xterm on its own, so
// translate it into a real WHEEL event and let xterm handle it EXACTLY like the
// desktop mouse wheel — scrollback in normal mode, and forwarded as mouse-wheel
// to the program (tmux / vim / less) when it has mouse mode on. preventDefault
// so the page behind the console never moves. Wired once on the persistent
// #pty-screen, guarded by consoleOpen. (#151 follow-up)
//
// Text selection (#151 follow-up): we ONLY hijack a real finger DRAG. A tap, double-tap, or
// stationary long-press is never preventDefault'd, so iOS's own selection
// gestures (double-tap a word, long-press) fire untouched on the DOM-rendered
// rows (user-select re-enabled for touch in style.css). Once something IS
// selected we stand down entirely, so dragging the selection handles extends it
// and the selection survives for Copy/Share. Nobody drags to scroll after
// holding/double-tapping, so "a plain drag = scroll" has no false positives.
(() => {
  const screen = $("pty-screen");
  const SLOP = 10; // px of travel before a touch is judged a deliberate scroll drag
  const HOLD_MS = 650; // stationary hold that turns on Select mode — kept just
                       // past iOS's ~500ms long-press so the rows are still
                       // user-select:none through its window (iOS won't grab it)
  let touchY = 0, startX = 0, startY = 0, scrolling = false;
  let selAnchor = null; // collapsed caret Range where a select-mode drag began
  // Hold-to-select: a stationary hold turns ON the same Select mode the chip
  // gives (crosshair, paint, handles). A scroll drag that starts moving before
  // the timer fires cancels it, so scrolling wins whenever the finger is moving;
  // to leave Select mode and scroll again, tap the Select chip (it glows) or Copy.
  let holdTimer = 0, touching = false;

  // The text caret under a screen point (WebKit vs standard spelling).
  const caretAt = (x, y) => {
    if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      if (!p) return null;
      const r = document.createRange(); r.setStart(p.offsetNode, p.offset); r.collapse(true); return r;
    }
    return null;
  };
  // Paint the selection from the drag's anchor caret to the caret under the
  // finger, ordering the two so the range is valid whichever way you drag.
  const paintTo = (x, y) => {
    const focus = caretAt(x, y);
    if (!selAnchor || !focus) return;
    const range = document.createRange();
    if (selAnchor.compareBoundaryPoints(Range.START_TO_START, focus) <= 0) {
      range.setStart(selAnchor.startContainer, selAnchor.startOffset);
      range.setEnd(focus.startContainer, focus.startOffset);
    } else {
      range.setStart(focus.startContainer, focus.startOffset);
      range.setEnd(selAnchor.startContainer, selAnchor.startOffset);
    }
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(range);
  };

  screen.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { scrolling = false; selAnchor = null; return; }
    const t = e.touches[0];
    if (consoleSelectMode) {
      e.preventDefault(); // keep the touch from reaching tmux/scroll — we own it
      const sel = window.getSelection();
      // If a selection already exists, a touch near either END grabs THAT handle
      // and re-anchors on the opposite (fixed) end — so you adjust one side
      // independently, like iOS's selection handles, instead of restarting.
      if (sel && !sel.isCollapsed && sel.rangeCount) {
        const range = sel.getRangeAt(0);
        const rects = range.getClientRects();
        if (rects.length) {
          const first = rects[0], last = rects[rects.length - 1];
          const startPt = { x: first.left, y: first.top + first.height / 2 };
          const endPt = { x: last.right, y: last.top + last.height / 2 };
          const d = (p) => Math.hypot(t.clientX - p.x, t.clientY - p.y);
          const dStart = d(startPt), dEnd = d(endPt);
          const GRAB = 60; // generous touch target around each handle
          if (Math.min(dStart, dEnd) <= GRAB) {
            const grabStart = dStart <= dEnd;
            const fixed = range.cloneRange();
            fixed.collapse(!grabStart); // pin the opposite end; drag moves this one
            selAnchor = fixed;
            return; // keep the selection; touchmove drags the grabbed handle
          }
        }
      }
      // No selection, or a touch away from both handles → start a fresh region.
      selAnchor = caretAt(t.clientX, t.clientY);
      if (sel) sel.removeAllRanges();
      return;
    }
    touchY = t.clientY; startX = t.clientX; startY = t.clientY; scrolling = false;
    // Hold still ~0.5s (no scroll drag claimed it first) → turn ON Select mode,
    // anchored here so the SAME finger can drag on to paint. A move past SLOP
    // before then cancels it (see touchmove), so a real scroll is never caught.
    touching = true;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(() => {
      if (!touching || scrolling || consoleSelectMode || !consoleOpen) return;
      setConsoleSelectMode(true);
      selAnchor = caretAt(startX, startY);
      if (navigator.vibrate) { try { navigator.vibrate(10); } catch (err) { /* ignore */ } }
    }, HOLD_MS);
  }, { passive: false });
  screen.addEventListener("touchmove", (e) => {
    if (!consoleOpen || e.touches.length !== 1) return;
    const t = e.touches[0];
    if (consoleSelectMode) {
      // Paint an arbitrary region from finger position — independent of tmux,
      // which touch can't drive as a drag.
      e.preventDefault();
      if (!selAnchor) { selAnchor = caretAt(t.clientX, t.clientY); return; }
      paintTo(t.clientX, t.clientY);
      return;
    }
    // NOT in select mode → a finger drag scrolls; scrolling must never be
    // wedged off.
    if (!scrolling) {
      if (Math.abs(t.clientX - startX) < SLOP && Math.abs(t.clientY - startY) < SLOP) return;
      scrolling = true;
      clearTimeout(holdTimer); // moved before the hold armed → it's a scroll
      touchY = t.clientY; // anchor here so the SLOP travel isn't scrolled in one jump
      // A deliberate scroll drag: drop any leftover selection so it can't stand
      // the handler down (a stale selection used to freeze scrolling).
      const s = window.getSelection && window.getSelection();
      if (s && s.rangeCount) s.removeAllRanges();
    }
    e.preventDefault(); // the swipe drives the terminal, not the page
    const y = t.clientY;
    const deltaY = touchY - y; // natural: swipe up → wheel down (toward newer)
    touchY = y;
    if (!deltaY) return;
    // Dispatch on the touched element so it routes through xterm's own wheel
    // listeners (scrollback OR mouse-mode forwarding), same as a desktop wheel.
    (e.target || screen).dispatchEvent(
      new WheelEvent("wheel", { deltaY, deltaMode: 0, bubbles: true, cancelable: true })
    );
  }, { passive: false });
  const end = (e) => {
    // In Select mode, swallow the lift so iOS doesn't synthesize a click that
    // re-focuses the textarea (bringing the keyboard back). The focus guard
    // above is the backstop; this avoids the flash.
    if (consoleSelectMode && e && e.cancelable) e.preventDefault();
    scrolling = false; selAnchor = null; touching = false; clearTimeout(holdTimer);
  };
  screen.addEventListener("touchend", end, { passive: false });
  screen.addEventListener("touchcancel", end);
})();

// Blink-style arrow pad (#148): ONE key you drag for direction — drag ↑/↓/←/→
// sends that arrow key, and holding in a direction repeats it (a compact d-pad
// instead of four chips, and it gives ← → which we lacked). Touch-only — the
// key row is hidden on desktop, where a real keyboard has arrows. A tap without
// a drag does nothing.
(() => {
  const btn = document.querySelector('.pty-keys button[data-key="arrows"]');
  if (!btn) return;
  const ARROW = { up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D" };
  const THRESH = 14;         // px before a direction registers
  const INITIAL_DELAY = 450; // hold this long before auto-repeat starts, so a
                             // quick swipe sends exactly ONE arrow, not two
  const NEAR_MS = 320, FAR_MS = 45; // repeat interval near vs far from the button
  const FAR_DIST = 170;      // distance (px, from the button) that reaches FAR_MS
  // Direction and distance are measured from the fixed touch-START point (no
  // re-anchoring), so "how far from the button" grows as you drag out — the
  // farther you hold, the faster it repeats.
  let ox = 0, oy = 0, dir = null, curDist = 0, timer = null;
  const dirOf = (dx, dy) => {
    if (Math.abs(dx) < THRESH && Math.abs(dy) < THRESH) return null;
    return Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
  };
  // A DOUBLE-TAP (two taps that sent no arrow) opens the dictation scratchpad —
  // free real estate on this key, since a tap without a drag does nothing.
  const DOUBLE_MS = 400;
  let pressed = false, lastTap = 0;
  const press = (d) => {
    if (d && consoleOpen) { pressed = true; consoleSend(ARROW[d]); consoleKeyFeedback(btn); }
  };
  const intervalFor = (dist) => {
    const t = Math.min(1, Math.max(0, (dist - THRESH) / (FAR_DIST - THRESH)));
    return Math.round(NEAR_MS - t * (NEAR_MS - FAR_MS)); // farther → shorter → faster
  };
  const clear = () => { clearTimeout(timer); timer = null; };
  const armRepeat = () => { // wait INITIAL_DELAY, then repeat, re-timed by distance each tick
    clear();
    timer = setTimeout(function tick() {
      if (!dir) return;
      press(dir);
      timer = setTimeout(tick, intervalFor(curDist));
    }, INITIAL_DELAY);
  };
  const stop = () => { clear(); dir = null; };
  btn.addEventListener("touchstart", (e) => {
    e.preventDefault();
    ox = e.touches[0].clientX; oy = e.touches[0].clientY; dir = null; curDist = 0; pressed = false; clear();
  }, { passive: false });
  btn.addEventListener("touchmove", (e) => {
    e.preventDefault();
    const dx = e.touches[0].clientX - ox, dy = e.touches[0].clientY - oy;
    curDist = Math.hypot(dx, dy);
    const d = dirOf(dx, dy);
    if (!d) { stop(); return; }                        // back near the button: cancel
    if (d !== dir) { dir = d; press(d); armRepeat(); } // new direction: one press, (re)start the delay
  }, { passive: false });
  btn.addEventListener("touchend", (e) => {
    e.preventDefault();
    stop();
    if (pressed) { lastTap = 0; return; } // a drag, not a tap
    const now = Date.now();
    if (now - lastTap < DOUBLE_MS) { lastTap = 0; consoleKeyFeedback(btn); openConsolePad(); }
    else lastTap = now;
  }, { passive: false });
  btn.addEventListener("touchcancel", stop);
})();

// Blink-style symbol keys (#153): each key shows a primary glyph with a smaller
// grey secondary above it. TAP → primary; swipe DOWN on the key → the grey
// secondary (armed while held, sent on release). A horizontal drag scrolls the
// row instead of sending anything. Injected before the copy/paste icons so they
// scroll together in the middle section.
function buildPtySymbols() {
  const mid = $("pty-syms");
  if (!mid || mid.dataset.built) return;
  // [primary (tap), secondary (swipe down)] — the iOS/Blink SmarterKeys set.
  const SYMS = [
    ["`", "~"], ["@", "#"], ["$", "^"], [";", ":"], ["-", "_"], ["=", "+"],
    ["[", "{"], ["]", "}"], ["\\", "|"], ["<", "*"], [">", '"'], ["/", "?"],
  ];
  const anchor = mid.firstChild; // the copy/paste buttons stay after the symbols
  for (const [s1, s2] of SYMS) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pty-sym";
    b.dataset.sym = s1; b.dataset.sym2 = s2;
    const top = document.createElement("span"); top.className = "pty-sym2"; top.textContent = s2;
    const bot = document.createElement("span"); bot.className = "pty-sym1"; bot.textContent = s1;
    b.append(top, bot);
    mid.insertBefore(b, anchor);
  }
  mid.dataset.built = "1";
}
buildPtySymbols();

// Swipe-down interaction for the symbol keys. Wired once on the mid container
// (delegation); copy/paste live here too but aren't .pty-sym, so they fall
// through to the .pty-keys click handler unchanged.
(() => {
  const mid = $("pty-syms");
  if (!mid) return;
  const DOWN = 12;    // px downward before the secondary arms
  const HSCROLL = 10; // px horizontal → treat as a row scroll, not a key press
  let cur = null, sx = 0, sy = 0, armed = false, scrolling = false;
  const arm = (on) => { armed = on; if (cur) cur.classList.toggle("sym-armed", on); };
  const reset = () => { if (cur) cur.classList.remove("sym-armed"); cur = null; armed = false; scrolling = false; };
  mid.addEventListener("touchstart", (e) => {
    const btn = e.target.closest(".pty-sym");
    if (!btn) { cur = null; return; }
    cur = btn; sx = e.touches[0].clientX; sy = e.touches[0].clientY; armed = false; scrolling = false;
  }, { passive: true });
  mid.addEventListener("touchmove", (e) => {
    if (!cur) return;
    const dx = e.touches[0].clientX - sx, dy = e.touches[0].clientY - sy;
    if (!scrolling && Math.abs(dx) > HSCROLL && Math.abs(dx) > Math.abs(dy)) {
      scrolling = true; arm(false); // horizontal wins → let the row scroll, cancel the key
    }
    if (scrolling) return; // native pan-x owns the horizontal scroll
    if (dy > DOWN && Math.abs(dy) > Math.abs(dx)) { e.preventDefault(); if (!armed) arm(true); }
    else if (armed && dy < DOWN) arm(false); // dragged back up before release
  }, { passive: false });
  mid.addEventListener("touchend", (e) => {
    if (cur && !scrolling) {
      // Cancel iOS's synthesized mouse/click: without this the button's
      // emulated mousedown steals focus from the xterm textarea AFTER our
      // focus() below, and iOS dismisses the soft keyboard on every key.
      e.preventDefault();
      consoleSend(armed ? cur.dataset.sym2 : cur.dataset.sym);
      consoleKeyFeedback(cur);
      if (consoleTerm) consoleTerm.focus();
    }
    reset();
  }, { passive: false });
  mid.addEventListener("touchcancel", reset);
})();

$("file-input").addEventListener("change", async () => {
  for (const file of $("file-input").files) await uploadFile(file);
  $("file-input").value = "";
});

$("photo-input").addEventListener("change", async () => {
  for (const file of $("photo-input").files) await uploadFile(file);
  $("photo-input").value = "";
});

// ---- message queue chips -------------------------------------------------
// A message sent while the agent is busy waits its turn; show it above the
// composer so it can be seen and cancelled (server-side dequeue).
function addQueueChip(text) {
  const list = $("queue-list");
  const chip = document.createElement("div");
  chip.className = "queue-chip";
  chip.dataset.text = text;
  chip.innerHTML =
    '<svg class="queue-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 8v4.3l2.6 1.6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    '<span class="queue-body"><span class="queue-text"></span><span class="queue-sub">Queued · sends when aish finishes</span></span>' +
    '<button class="queue-edit" type="button" aria-label="edit queued message"><svg viewBox="0 0 24 24"><path d="M12 19.5h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M15.5 5.2a1.7 1.7 0 0 1 2.4 2.4l-8.3 8.3-3.2.8.8-3.2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg></button>' +
    '<button class="queue-remove" type="button" aria-label="remove from queue"><svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg></button>';
  chip.querySelector(".queue-text").textContent = text;
  chip.querySelector(".queue-remove").onclick = () => {
    send({ type: "dequeue", text });
    removeQueueChip(text);
  };
  // Edit: pull the message back into the composer to revise & resend (#14).
  chip.querySelector(".queue-edit").onclick = () => {
    send({ type: "dequeue", text });
    removeQueueChip(text);
    input.value = input.value ? `${text}\n${input.value}` : text;
    resizeInput();
    input.focus();
  };
  list.appendChild(chip);
  list.hidden = false;
  scrollToEnd();
}

function removeQueueChip(text) {
  const list = $("queue-list");
  const chip = [...list.children].find((c) => c.dataset.text === text);
  if (chip) chip.remove();
  if (!list.children.length) list.hidden = true;
}

// A pending working-directory change (#92) shows as a single card pinned to the
// TOP of the queue — it applies first, before any queued messages. There is at
// most one; a second cd updates the existing card in place. Edit reopens the
// directory picker; Remove clears the pending change server-side.
function addCwdChip(path) {
  const list = $("queue-list");
  let chip = list.querySelector(".queue-chip.cwd");
  if (!chip) {
    chip = document.createElement("div");
    chip.className = "queue-chip cwd";
    chip.innerHTML =
      '<svg class="queue-ico" viewBox="0 0 24 24"><path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>' +
      '<span class="queue-body"><span class="queue-text"></span><span class="queue-sub">Queued · applies first</span></span>' +
      '<button class="queue-edit" type="button" aria-label="change queued directory"><svg viewBox="0 0 24 24"><path d="M12 19.5h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M15.5 5.2a1.7 1.7 0 0 1 2.4 2.4l-8.3 8.3-3.2.8.8-3.2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg></button>' +
      '<button class="queue-remove" type="button" aria-label="cancel directory change"><svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg></button>';
    // Edit reopens the directory picker (#92) — selecting there re-sends `cd`,
    // which overwrites pending_cwd and re-emits, updating this same card.
    chip.querySelector(".queue-edit").onclick = () => openDirSheet();
    chip.querySelector(".queue-remove").onclick = () => {
      send({ type: "dequeue_cwd" });
      removeCwdChip();
    };
    list.insertBefore(chip, list.firstChild); // pinned above message chips
  }
  chip.querySelector(".queue-text").textContent = `Change directory to ${abbreviatePath(path)}`;
  list.hidden = false;
  scrollToEnd();
}

function removeCwdChip() {
  const list = $("queue-list");
  const chip = list.querySelector(".queue-chip.cwd");
  if (chip) chip.remove();
  if (!list.children.length) list.hidden = true;
}

async function uploadFile(file) {
  const query = new URLSearchParams({ name: file.name });
  if (token) query.set("token", token);
  let response;
  try {
    response = await fetch(`${BASE}upload?${query}`, { method: "POST", body: file });
  } catch {
    showToast(`upload failed: ${file.name}`);
    return;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    showToast(`upload failed: ${body.error || response.status}`);
    return;
  }
  const { path } = await response.json();
  attachments.push({ name: file.name, path });
  renderAttachments();
}

function renderAttachments() {
  const box = $("attachments");
  box.replaceChildren();
  box.hidden = !attachments.length;
  attachments.forEach((attachment, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.textContent = attachment.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "✕";
    remove.onclick = () => { attachments.splice(i, 1); renderAttachments(); };
    chip.appendChild(remove);
    box.appendChild(chip);
  });
}

// ---- sheets --------------------------------------------------------------
function openSheet(id) {
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  $(id).hidden = false;
  $("backdrop").hidden = false;
}

// ---- mic / speech-recognition diagnostic (/mic) --------------------------
// A throwaway probe for whether webkitSpeechRecognition actually works in THIS
// runtime — crucially, when opened inside the installed PWA (standalone), where
// iOS has historically broken it. Shows support, the run context, live interim
// + final transcript, and every recognition event, with an en/pl language
// switch so both languages can be checked. Gates the Car Mode feature (#97).
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let micRec = null;
let micLang = "en-US";
let micListening = false;

function micLogLine(msg) {
  const log = $("mic-log");
  const t = new Date().toLocaleTimeString([], { hour12: false }) +
    "." + String(Date.now() % 1000).padStart(3, "0");
  log.textContent += `${t}  ${msg}\n`;
  log.scrollTop = log.scrollHeight;
}

function micContext() {
  const standalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  const support = SpeechRec
    ? '<span class="ok">SpeechRecognition: supported</span>'
    : '<span class="bad">SpeechRecognition: NOT available</span>';
  const ctx = standalone
    ? '<span class="ok">running in: installed PWA (standalone)</span>'
    : "running in: browser tab";
  $("mic-support").innerHTML = `${support}<br>${ctx}`;
}

function openMicSheet() {
  openSheet("mic-sheet");
  micContext();
  $("mic-interim").textContent = "—";
  $("mic-final").textContent = "—";
  $("mic-log").textContent = "";
  micLogLine(`ready · lang ${micLang}`);
}

function startMic() {
  if (!SpeechRec) { micLogLine("cannot start: no SpeechRecognition in this browser"); return; }
  try {
    micRec = new SpeechRec();
    micRec.lang = micLang;
    micRec.continuous = true;
    micRec.interimResults = true;
    micRec.onstart = () => micLogLine("onstart (mic live — say something)");
    micRec.onaudiostart = () => micLogLine("onaudiostart");
    micRec.onspeechstart = () => micLogLine("onspeechstart");
    micRec.onspeechend = () => micLogLine("onspeechend");
    micRec.onresult = (e) => {
      let interim = "", final = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const chunk = e.results[i][0].transcript;
        if (e.results[i].isFinal) final += chunk; else interim += chunk;
      }
      if (interim) $("mic-interim").textContent = interim;
      if (final) {
        $("mic-final").textContent = final;
        micLogLine(`onresult FINAL: ${final.trim()}`);
      }
    };
    micRec.onerror = (e) => micLogLine(`onerror: ${e.error}${e.message ? " — " + e.message : ""}`);
    micRec.onend = () => {
      micListening = false;
      setMicToggle();
      micLogLine("onend (recognition stopped — iOS often ends after a silence)");
    };
    micRec.start();
    micListening = true;
    setMicToggle();
  } catch (err) {
    micLogLine(`start() threw: ${err.name} — ${err.message}`);
    micListening = false;
    setMicToggle();
  }
}

function stopMic() {
  if (micRec) {
    try { micRec.stop(); } catch { /* already stopped */ }
    micRec = null;
  }
  micListening = false;
  setMicToggle();
}

function setMicToggle() {
  const btn = $("mic-toggle");
  if (!btn) return;
  btn.textContent = micListening ? "Stop" : "Start listening";
  btn.classList.toggle("listening", micListening);
}

$("mic-toggle").onclick = () => (micListening ? stopMic() : startMic());
for (const btn of document.querySelectorAll(".mic-lang")) {
  btn.onclick = () => {
    micLang = btn.dataset.lang;
    for (const b of document.querySelectorAll(".mic-lang")) b.classList.toggle("active", b === btn);
    micLogLine(`language → ${micLang}`);
    if (micListening) { stopMic(); startMic(); } // restart so the new lang takes
  };
}

// ---- dictation (mic-to-composer, #97) ------------------------------------
// A mic button that dictates into the composer instead of typing. It keeps
// listening THROUGH pauses (iOS ends recognition on silence — we restart it)
// and finishes only when you say "stop" or tap the mic, so there's time to
// think mid-sentence. Interim words stream into the input live. When a dictated
// message is sent, its reply is auto-read via TTS (voice in → voice out), the
// reply language auto-detected by the existing TTS heuristic. Language can't be
// auto-detected for input, so a small EN/PL chip picks it.
let dictating = false;
let dictateRec = null;
let dictateLang = localStorage.getItem("aish-dict-lang") || "en-US";
let dictateBase = "";     // composer text present before dictation began (append)
let dictateFinal = "";    // committed transcript from COMPLETED recognition sessions
let dictateSession = "";  // the CURRENT session's transcript — rebuilt from results
                          // each event (never appended), so iOS re-firing the
                          // cumulative transcript can't duplicate it (#97). Merged
                          // into dictateFinal when the session ends.
let dictateEnded = false; // set on stop-word / tap so onend won't restart
let pendingSpeak = false; // current composer text came from dictation
let speakNextReply = false; // armed when a dictated message is sent
// Where the transcript lands: the chat composer, or the console's dictation
// scratchpad. The engine is shared — only the target element and the
// send-side behaviour differ.
let dictateTarget = "composer"; // "composer" | "pad"
let dictHold = false;     // pad: you're typing — don't let a result overwrite you

function dictEl() {
  return dictateTarget === "pad" ? $("pad-input") : input;
}

function setDictLang() {
  const label = dictateLang === "pl-PL" ? "PL" : "EN";
  $("dict-lang").textContent = label;
  $("pad-lang").textContent = label;
}

// Both mic buttons (composer + scratchpad) show the same recording state.
function setDictRecording(on) {
  $("dictate").classList.toggle("recording", on);
  $("pad-mic").classList.toggle("recording", on);
}

function toggleDictLang() {
  dictateLang = dictateLang === "pl-PL" ? "en-US" : "pl-PL";
  localStorage.setItem("aish-dict-lang", dictateLang);
  setDictLang();
  showToast(dictateLang === "pl-PL" ? "Dictation: Polski" : "Dictation: English");
  if (dictating) restartDictation();
}

// iOS mutes speechSynthesis unless it was first spoken inside a user gesture,
// and the reply is spoken seconds later (not in a gesture) — so we "unlock" it
// with a silent utterance on the mic tap. After that, later speak() calls play.
function primeTts() {
  if (!TTS_OK) return;
  try {
    const u = new SpeechSynthesisUtterance(" ");
    u.volume = 0;
    speechSynthesis.speak(u);
  } catch { /* best-effort unlock */ }
}

function dictJoin(a, b) {
  return a && b && !/\s$/.test(a) ? `${a} ${b}` : a + b;
}

function renderDictation(interim) {
  if (dictHold) return; // mid-edit in the scratchpad: your keystrokes win
  const el = dictEl();
  el.value = dictJoin(dictateBase, dictJoin(dictJoin(dictateFinal, dictateSession), interim));
  if (dictateTarget === "pad") resizePadInput();
  else resizeInput(); // note: never touch the "Ask aish" placeholder
  // Once the textarea hits its max height it scrolls internally — keep the
  // newest dictated words in view instead of stranding you at the top (#97).
  el.scrollTop = el.scrollHeight;
}

// A trailing standalone "stop" ends dictation (deliberately NOT a silence timer,
// so pauses don't cut you off). Returns the text with that word removed.
function stripStopWord(text) {
  const m = text.match(/(^|\s)stop[\s.!?,]*$/i);
  return m ? text.slice(0, m.index).trimEnd() : text;
}

function beginRec() {
  dictateRec = new SpeechRec();
  dictateRec.lang = dictateLang;
  dictateRec.continuous = true;
  dictateRec.interimResults = true;
  dictateRec.onresult = (e) => {
    // Rebuild THIS session's transcript from all its results every event — never
    // append per-event. Desktop Chrome accumulates finals across the session, and
    // iOS re-fires the growing cumulative transcript (often flagged final and
    // with resultIndex stuck at 0); rebuilding is correct for both and stops the
    // iOS duplication (#97).
    let final = "", interim = "";
    for (let i = 0; i < e.results.length; i++) {
      const chunk = e.results[i][0].transcript;
      if (e.results[i].isFinal) final += chunk + " "; else interim += chunk;
    }
    dictateSession = final.trim(); // REPLACE, not append
    const combined = dictJoin(dictateFinal, dictateSession);
    const stripped = stripStopWord(combined);
    if (stripped !== combined) { // trailing "stop" ends dictation
      dictateFinal = stripped; dictateSession = ""; stopDictation(); return;
    }
    renderDictation(interim);
  };
  dictateRec.onerror = (e) => {
    if (e.error === "no-speech" || e.error === "aborted") return; // benign; onend restarts
    showToast(e.error === "not-allowed" ? "microphone permission denied" : `mic: ${e.error}`);
    stopDictation();
  };
  dictateRec.onend = () => {
    // Commit this session's transcript before any restart — iOS ends on silence
    // and a restarted session's results start empty, so without this the text so
    // far would be lost (and, before the rebuild fix, re-accumulated).
    if (dictateSession) { dictateFinal = dictJoin(dictateFinal, dictateSession); dictateSession = ""; }
    // iOS ends on silence — keep listening through pauses unless we ended on purpose.
    if (dictating && !dictateEnded) { try { dictateRec.start(); } catch { beginRec(); } }
  };
  try { dictateRec.start(); }
  catch { showToast("couldn't start the mic"); stopDictation(); }
}

function startDictation(target = "composer") {
  if (!SpeechRec || dictating) return;
  primeTts(); // unlock iOS audio now (this runs inside the mic-tap gesture)
  dictateTarget = target;
  dictHold = false;
  dictateBase = dictEl().value.trim();
  dictateFinal = "";
  dictateSession = "";
  dictateEnded = false;
  dictating = true;
  setDictRecording(true);
  beginRec();
}

function restartDictation() { // e.g. after a language switch mid-dictation
  if (dictateSession) { dictateFinal = dictJoin(dictateFinal, dictateSession); dictateSession = ""; }
  if (dictateRec) { dictateRec.onend = null; try { dictateRec.stop(); } catch { /* gone */ } }
  beginRec();
}

function stopDictation() {
  dictateEnded = true;
  dictating = false;
  if (dictateRec) {
    dictateRec.onend = null; // don't let the pending onend restart us
    try { dictateRec.stop(); } catch { /* already stopped */ }
    dictateRec = null;
  }
  setDictRecording(false);
  renderDictation(""); // commit the final text, drop any trailing interim
  const el = dictEl();
  // TTS is a chat notion: a scratchpad line goes to a terminal, not to a model.
  if (dictateTarget === "composer" && el.value.trim()) pendingSpeak = true;
  el.focus();
}

// Voice-in → voice-out: after a dictated message's reply lands, read it aloud.
function maybeSpeakReply() {
  if (!speakNextReply) return;
  speakNextReply = false;
  if (!TTS_OK) return;
  const answers = document.querySelectorAll(".msg.answer");
  const el = answers[answers.length - 1];
  const box = el && el.querySelector(".tts");
  if (box) startPlayback(box, el);
}

if (SpeechRec) {
  $("dictate").hidden = false;
  setDictLang();
  // Tap = start/stop dictation; hold (500ms) = switch input language. A hold
  // suppresses the tap so it doesn't also toggle dictation.
  const mic = $("dictate");
  let holdTimer = null;
  let wasHold = false;
  mic.addEventListener("pointerdown", () => {
    wasHold = false;
    holdTimer = setTimeout(() => { wasHold = true; toggleDictLang(); }, 500);
  });
  const cancelHold = () => clearTimeout(holdTimer);
  mic.addEventListener("pointerup", () => {
    clearTimeout(holdTimer);
    if (!wasHold) (dictating ? stopDictation() : startDictation());
  });
  mic.addEventListener("pointercancel", cancelHold);
  mic.addEventListener("pointerleave", cancelHold);
}

// ---- console dictation scratchpad ----------------------------------------
// Dictating straight into the terminal is unusable: iOS keyboard dictation
// REWRITES the whole field on every recognition update (it re-punctuates and
// corrects earlier words), and xterm forwards each rewrite to the PTY, which
// cannot retract bytes it already wrote — so the line comes out as every
// interim revision concatenated. This pad stages the text instead: nothing
// reaches the PTY until you tap Send, which also means a spoken command can be
// read and corrected before it runs. The transcription engine is the composer's
// (#97), retargeted via dictateTarget.
function resizePadInput() {
  const el = $("pad-input");
  const overlay = $("pty-overlay");
  // Grow with the text, but never past ~40% of the overlay (the visual viewport
  // height while the keyboard is up) — beyond that it scrolls internally.
  const cap = Math.max(80, Math.round((overlay.clientHeight || window.innerHeight) * 0.4));
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, cap)}px`;
  positionPad();
}

// The pad floats above the key row rather than taking flex space, so opening it
// never re-fits xterm (which would resize the remote tmux pane). That means its
// offset from the bottom is the key row's height — 0 on desktop, where the row
// is display:none and offsetParent is null.
function positionPad() {
  const keys = document.querySelector(".pty-keys");
  const h = keys && keys.offsetParent !== null ? keys.offsetHeight : 0;
  $("pty-pad").style.bottom = `${h}px`;
}

function padOpen() {
  return !$("pty-pad").hidden;
}

function openConsolePad() {
  if (!consoleOpen) return;
  const pad = $("pty-pad");
  const el = $("pad-input");
  if (!pad.hidden) { el.focus(); return; }
  pad.hidden = false;
  $("pad-history-list").hidden = true;
  setDictLang();
  resizePadInput();
  // Focus inside the opening gesture so iOS raises the keyboard immediately.
  el.focus();
  if (SpeechRec) startDictation("pad"); // the pad exists to be spoken into
}

function closeConsolePad() {
  if (dictating && dictateTarget === "pad") stopDictation();
  dictateTarget = "composer";
  dictHold = false;
  clearTimeout(padEditTimer);
  $("pad-input").value = "";
  $("pad-history-list").hidden = true;
  $("pty-pad").hidden = true;
  if (consoleTerm) consoleTerm.focus();
}

// [PAD-HISTORY-START]
// The last 10 sends, kept so a line that landed in the wrong place (vim in
// normal mode, a program that wasn't reading stdin) can be re-sent instead of
// re-dictated — losing a long spoken sentence to a bad submit is the exact
// annoyance this pad is meant to remove.
const PAD_HISTORY_KEY = "aish-pad-history";
const PAD_HISTORY_MAX = 10;

function padHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(PAD_HISTORY_KEY));
    return Array.isArray(raw) ? raw.filter((t) => typeof t === "string" && t) : [];
  } catch {
    return []; // corrupt/absent — history is a convenience, never an error
  }
}

function padHistoryPush(text) {
  const kept = padHistory().filter((t) => t !== text); // re-sending moves it to the top
  kept.unshift(text);
  try {
    localStorage.setItem(PAD_HISTORY_KEY, JSON.stringify(kept.slice(0, PAD_HISTORY_MAX)));
  } catch { /* storage full / private mode */ }
}
// [PAD-HISTORY-END]

function togglePadHistory() {
  const box = $("pad-history-list");
  if (!box.hidden) { box.hidden = true; return; }
  box.replaceChildren();
  const items = padHistory();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "pad-history-empty";
    empty.textContent = "no previous dictations yet";
    box.appendChild(empty);
  }
  for (const text of items) {
    const entry = document.createElement("button");
    entry.type = "button";
    entry.textContent = text.replace(/\s+/g, " ");
    entry.title = text;
    entry.onclick = () => {
      $("pad-input").value = text; // load, never auto-send — you may want to edit
      box.hidden = true;
      resizePadInput();
      $("pad-input").focus();
    };
    box.appendChild(entry);
  }
  box.hidden = false;
}

// [PAD-SEND-START]
function padSend(withEnter = true) {
  const text = $("pad-input").value.replace(/\s+$/, "");
  if (!text) { showToast("nothing to send"); return; }
  padHistoryPush(text);
  consoleSend(withEnter ? `${text}\r` : text);
  closeConsolePad();
}
// [PAD-SEND-END]

// Typing a correction while the mic is live: freeze rendering so the next
// recognition event can't overwrite the edit, then re-baseline on the edited
// text and restart recognition, so new speech appends to what you wrote.
// Debounced — a restart per keystroke would shred the recognition session.
let padEditTimer = 0;
const PAD_EDIT_SETTLE = 700;

function padManualEdit() {
  resizePadInput();
  if (!dictating || dictateTarget !== "pad") return;
  dictHold = true;
  clearTimeout(padEditTimer);
  padEditTimer = setTimeout(() => {
    dictateBase = $("pad-input").value;
    dictateFinal = "";
    dictateSession = "";
    dictHold = false;
    restartDictation();
  }, PAD_EDIT_SETTLE);
}

$("pad-close").onclick = () => closeConsolePad();
$("pad-history").onclick = () => togglePadHistory();
$("pad-lang").onclick = () => toggleDictLang();
$("pad-mic").onclick = () => {
  if (dictating) {
    stopDictation();
    if (dictateTarget === "pad") return; // it was ours — the tap just stopped it
  }
  startDictation("pad");
};
$("pad-input").addEventListener("input", padManualEdit);
$("pad-input").addEventListener("keydown", (e) => {
  // Enter sends (a scratchpad line is a terminal line); Shift+Enter for a
  // literal newline. Esc closes without sending.
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); padSend(true); }
  else if (e.key === "Escape") { e.preventDefault(); closeConsolePad(); }
});
// Tap = send + Enter; hold = insert the text WITHOUT Enter, for filling in a
// prompt you still want to review in the program itself.
(() => {
  const btn = $("pad-send");
  let holdTimer = null;
  let wasHold = false;
  btn.addEventListener("pointerdown", () => {
    wasHold = false;
    holdTimer = setTimeout(() => { wasHold = true; padSend(false); showToast("inserted without Enter"); }, 550);
  });
  const cancelHold = () => clearTimeout(holdTimer);
  btn.addEventListener("pointerup", () => {
    clearTimeout(holdTimer);
    if (!wasHold) padSend(true);
  });
  btn.addEventListener("pointercancel", cancelHold);
  btn.addEventListener("pointerleave", cancelHold);
})();

function closeSheets() {
  // Blur a focused sheet input before hiding it: merely hiding leaves iOS to
  // dismiss the keyboard on its own schedule, and the layout-viewport pan it
  // caused can then settle without any visualViewport event (#8).
  const active = document.activeElement;
  if (active && active.closest(".sheet, .screen")) active.blur();
  if (micListening) stopMic(); // don't leave the mic live after closing /mic
  stopComposerMenuTracking(); // drop the #103 viewport listeners with the menu
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  for (const menu of document.querySelectorAll(".popover-menu")) menu.hidden = true;
  $("sessions-sheet").hidden = true; // the full-page Sessions view
  $("backdrop").hidden = true;
  snapViewportSoon();
}
for (const b of document.querySelectorAll("[data-close]")) {
  b.onclick = closeSheets;
}
$("backdrop").onclick = closeSheets;

$("sessions-new").onclick = () => { send({ type: "new" }); closeSheets(); };

// Single-key shortcuts for approval-card actions, one row per distinct button.
// Keys MUST be unique (a card never binds one key to two actions) — enforced by
// tests/js/test_approval_shortcut.js, which also checks every primary verdict
// button (.approve/.deny/.trust) and the Edit toggle is covered here. Scope
// segments (.seg) and copy (.copy-chip) are deliberately left to Tab: they
// refine or duplicate the Approve verdict rather than being verdicts of their
// own, and their natural mnemonics collide (Always vs Approve).
const CARD_SHORTCUTS = [
  { key: "a", selector: "button.approve", badge: true }, // Approve / Install / Just once
  { key: "d", selector: "button.deny", badge: true },    // Deny
  { key: "t", selector: "button.trust", badge: true },   // Trust dir (read card)
  { key: "e", selector: "button.cmd-icon" },             // Edit (icon button — tooltip only)
];

// The last approval card still awaiting a verdict (its Approve button is only
// disabled once answerCard resolves it). Newest-first so stacked cards clear
// bottom-up, matching where the eye is.
function activeApprovalCard() {
  const list = [...messagesEl.querySelectorAll(".card")];
  for (let i = list.length - 1; i >= 0; i--) {
    const approve = list[i].querySelector("button.approve");
    if (approve && !approve.disabled) return list[i];
  }
  return null;
}
// [ACTIVE-APPROVAL-CARD-END]

document.addEventListener("keydown", (e) => {
  // Esc leaves terminal mode / the PTY overlay first (#143); only if neither is
  // active does it fall through to dismissing an open sheet.
  if (e.key === "Escape" && escapeExit()) { e.preventDefault(); return; }
  if (e.key === "Escape" && (!$("backdrop").hidden || !$("sessions-sheet").hidden)) closeSheets();

  // Approval-card single-key actions (TUI convention): A = approve, D = deny,
  // T = trust dir, E = edit. Only while a card is pending and the user isn't
  // typing into the comment field or editing a command — clicking routes through
  // the button's own onclick so edit/scope/feedback handling stays in one place.
  // A key only acts when THIS card actually has that button, so T/E fall through
  // harmlessly on cards that lack them.
  if (!e.metaKey && !e.ctrlKey && !e.altKey && !editingNow()) {
    const sc = CARD_SHORTCUTS.find((s) => s.key === e.key.toLowerCase());
    if (sc) {
      const btn = activeApprovalCard()?.querySelector(sc.selector);
      if (btn && !btn.disabled) {
        e.preventDefault();
        btn.click();
        return;
      }
    }
  }

  // Primary navigation shortcuts. Ctrl/Cmd+N = new chat, Ctrl/Cmd+O = search
  // (open) sessions, Ctrl/Cmd+P = export (print) the session to PDF. The older
  // Cmd/Ctrl+Shift+O (new) / Shift+P (search) command-palette combos still work.
  if ((e.metaKey || e.ctrlKey) && !e.altKey) {
    const key = e.key.toLowerCase();
    if (key === "n" || (e.shiftKey && key === "o")) {
      e.preventDefault();
      send({ type: "new" });
      closeSheets();
      return;
    }
    if (key === "o" || (e.shiftKey && key === "p")) {
      e.preventDefault();
      openSessionsSheet("");
      return;
    }
    if (!e.shiftKey && key === "p") {
      e.preventDefault();
      exportSessionPdf();
      return;
    }
  }
  // Cmd/Ctrl+\ toggles the global "Quake console" (#148 follow-up). When the
  // overlay itself has focus, xterm's own key handler catches this first; this
  // is the OPEN path from anywhere else in the app.
  if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
    e.preventDefault();
    toggleConsole();
  }
});

// Desktop only: auto-focusing on a phone would pop the keyboard over the
// content on every reconnect.
const FINE_POINTER = matchMedia("(pointer: fine)").matches;

// Grabber: drag down to dismiss (pointer events cover touch and mouse).
for (const sheet of document.querySelectorAll(".sheet")) {
  const handle = sheet.querySelector(".grabber");
  if (!handle) continue;
  let startY = null;
  handle.addEventListener("pointerdown", (e) => {
    startY = e.clientY;
    sheet.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener("pointermove", (e) => {
    if (startY === null) return;
    const dy = Math.max(0, e.clientY - startY);
    sheet.style.transform = `translateY(${dy}px)`;
  });
  const finish = (e) => {
    if (startY === null) return;
    const dy = e.clientY - startY;
    startY = null;
    sheet.classList.remove("dragging");
    sheet.style.transform = "";
    if (dy > 80) closeSheets();
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Arrow/Enter navigation for sheet result lists (same semantics as the
// TUI picker: first row is the best match, Enter takes the highlight).
// No row is pre-highlighted — a default cursor on row 0 reads as "you are
// here" (#29); Enter still takes the top match, arrows start from it.
function setActiveRow(rows, index) {
  rows.forEach((row, i) => row.classList.toggle("active", i === index));
  if (rows[index]) rows[index].scrollIntoView({ block: "nearest" });
}

function attachListNav(searchEl, listEl) {
  searchEl.addEventListener("keydown", (e) => {
    const rows = [...listEl.querySelectorAll(".row")];
    if (!rows.length) return;
    const index = rows.findIndex((row) => row.classList.contains("active"));
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      const next = index < 0
        ? (step === 1 ? 0 : rows.length - 1)
        : (index + step + rows.length) % rows.length;
      setActiveRow(rows, next);
    } else if (e.key === "Enter") {
      e.preventDefault();
      (index >= 0 ? rows[index] : rows[0]).click();
    }
  });
}

// wrap mode: device-local ergonomics (like the token), not session state.
// Applied here, before any replay renders, so history draws in the chosen mode.
const WRAP_KEY = "aish-wrap";
if (localStorage.getItem(WRAP_KEY) === "1") document.body.classList.add("wrap");

// Reading-text size (#118): a multiplier on the --fs CSS variable, stepped from
// the ⋯ menu's A−/A+ and persisted. The transcript REFLOWS at the new size (the
// scaled surfaces keep their width), unlike pinch-zoom which magnifies a region.
const FONT_SCALE_KEY = "aish-font-scale";
const FS_MIN = 0.8, FS_MAX = 2.0, FS_STEP = 0.1;
let fontScale = parseFloat(localStorage.getItem(FONT_SCALE_KEY));
if (!(fontScale >= FS_MIN && fontScale <= FS_MAX)) fontScale = 1;
function applyFontScale() {
  document.documentElement.style.setProperty("--fs", String(fontScale));
  const el = $("fs-state");
  if (el) el.textContent = `${Math.round(fontScale * 100)}%`;
  localStorage.setItem(FONT_SCALE_KEY, String(fontScale));
}
function stepFontScale(delta) {
  fontScale = Math.min(FS_MAX, Math.max(FS_MIN, Math.round((fontScale + delta) * 100) / 100));
  applyFontScale();
}
applyFontScale();
// stopPropagation so stepping doesn't bubble to the menu's close-on-click.
$("fs-dec").onclick = (e) => { e.stopPropagation(); stepFontScale(-FS_STEP); };
$("fs-inc").onclick = (e) => { e.stopPropagation(); stepFontScale(FS_STEP); };
// Toggling wrap reflows every monospace block, so content heights above the
// viewport change and the reader would land on different text (#21). Anchor
// on the message at the top of the viewport and put it back at the same
// on-screen offset after the reflow; a reader pinned to the tail stays there.
function topVisibleAnchor() {
  const top = messagesEl.getBoundingClientRect().top;
  for (const el of messagesEl.children) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom > top) return { el, offset: rect.top - top };
  }
  return null;
}

function restoreAnchor(anchor) {
  const top = messagesEl.getBoundingClientRect().top;
  messagesEl.scrollTop += anchor.el.getBoundingClientRect().top - top - anchor.offset;
  updateScrollButton();
}

function toggleWrap() {
  const wasAtBottom = nearBottom();
  const anchor = topVisibleAnchor();
  const on = document.body.classList.toggle("wrap");
  localStorage.setItem(WRAP_KEY, on ? "1" : "0");
  syncTermWrap(on); // global overrides every command block's local wrap + button
  syncDiffWrap(on); // ...and every diff card's local wrap + button
  // Reading layout right after the class toggle forces a synchronous
  // reflow, so the restored offset is computed against final geometry.
  if (wasAtBottom) scrollToEnd(true);
  else if (anchor) restoreAnchor(anchor);
  showToast(on ? "wrap on" : "wrap off");
}

// ---- swipe pager between open sessions -----------------------------------
// Horizontal pager gesture (the iOS Weather-app model): drag the transcript
// sideways and it follows the finger; a pill names the target chat and
// turns blue once release would switch. Pages are the recent chats —
// open or not, resume loads cold ones from disk — ordered oldest→newest
// by last interaction (hello.pager) and confined to the current chat's
// lane (Recent vs Automated, see pagerLane), with Safari's semantics:
// swipe right = back = older chat, swipe left = forward = newer — or a
// brand-new chat once past the newest. Touches near the screen edges are
// left to Safari's back/forward gesture, and pans starting inside
// horizontally scrollable output stay scrolls.
let pagerSessions = []; // [{name, title}] oldest→newest, from hello
let currentSession = null;
let currentLogPath = ""; // absolute JSONL log path for this session, from hello (#146)
let swipeInFrom = 0; // set on commit; onReplay animates the new page in

const EDGE_GUARD = 28; // px — Safari's back/forward gesture zone
const DECIDE_AT = 12; // px of travel before the gesture picks an axis
const COMMIT_AT = 0.3; // fraction of width that arms release-to-switch
const DECIDE_WITHIN = 350; // ms — slower starts are long-press/selection

// Text selection must win over paging: dragging selection handles (or the
// drag right after a long-press) produces the same touch stream as a swipe.
function selectionActive() {
  const selection = document.getSelection();
  return Boolean(selection && !selection.isCollapsed);
}

const swipe = {
  tracking: false, horizontal: false, blocked: false,
  startX: 0, startY: 0, dx: 0, width: 1, startTime: 0,
};

// PAGER_LANE_START
// The pager pages within ONE lane, matching the Sessions screen's Recent /
// Automated tabs (#160): swiping through chats you started must never land on
// a triggered session, or the reverse. Lane = the origin bucket, same rule as
// partitionSessions. A page the client doesn't know (a brand-new chat not yet
// on disk) is a Recent chat — new chats are always yours.
function pagerLane(page) {
  return page && page.origin && page.origin !== "user" ? "automated" : "recent";
}

function laneNeighbor(pages, current, direction) {
  const lane = pagerLane(pages.find((s) => s.name === current));
  const inLane = pages.filter((s) => pagerLane(s) === lane);
  const index = inLane.findIndex((s) => s.name === current);
  return index < 0 ? null : inLane[index + direction] || null;
}
// PAGER_LANE_END

// [PAGER-SOURCE-START]
// Where the pager's pages come from. Normally `hello.pager` — but that only
// exists once a hello has landed, so a cold OFFLINE launch had no pages at all
// and every swipe just rubber-banded (#165 follow-up). The mirror already holds
// name/title/origin/ts for every cached chat, which is exactly what a page is,
// so derive the list from it whenever the server's own list can't be trusted:
// offline (where it is absent or stale), or before the first hello.
//
// Server parity: `pager_titles` returns the 30 most recent, oldest→newest.
const PAGER_LIMIT = 30;

function offlinePagerPages() {
  return [...offlineMeta.values()]
    .sort((a, b) => (a.ts || 0) - (b.ts || 0))
    .slice(-PAGER_LIMIT)
    .map((meta) => ({ name: meta.name, title: meta.title, origin: meta.origin }));
}

function pagerPages() {
  if (!offlineMode && pagerSessions.length) return pagerSessions;
  const cached = offlinePagerPages();
  // Offline with an empty mirror (nothing synced yet): fall back rather than
  // returning nothing, so behaviour is never worse than before.
  return cached.length ? cached : pagerSessions;
}
// [PAGER-SOURCE-END]

function sessionNeighbor(direction) {
  return laneNeighbor(pagerPages(), currentSession, direction);
}

// Safari semantics: back (swipe right, -1) = older chat, forward (swipe
// left, +1) = newer — and one page past the newest is a fresh chat, gated
// on the current one having content so empties never stack up.
const NEW_CHAT_TARGET = { fresh: true, title: "New chat" };

function swipeTarget(direction) {
  const neighbor = sessionNeighbor(direction);
  if (neighbor) return neighbor;
  // A new chat is always a Recent chat, so it only exists past the newest page
  // of the Recent lane — the Automated lane simply ends (you can't hand-start
  // a triggered session).
  // Starting a new chat needs the server. Offering that page offline would
  // slide the transcript away and snap straight back, which reads as a broken
  // gesture — end the lane instead.
  if (offlineMode) return null;
  const lane = pagerLane(pagerPages().find((s) => s.name === currentSession));
  return direction === 1 && sessionTitled && lane === "recent" ? NEW_CHAT_TARGET : null;
}

function scrollsHorizontally(node) {
  for (; node && node !== messagesEl; node = node.parentElement) {
    if (node.scrollWidth > node.clientWidth + 1) {
      const overflow = getComputedStyle(node).overflowX;
      if (overflow === "auto" || overflow === "scroll") return true;
    }
  }
  return false;
}

function updateSwipeHint(target, dx, commitPx) {
  const hint = $("swipe-hint");
  if (!target) { hint.hidden = true; return; }
  hint.hidden = false;
  hint.classList.toggle("prev", dx > 0);
  hint.classList.toggle("commit", Math.abs(dx) > commitPx);
  $("swipe-hint-title").textContent = target.title || "New chat";
  hint.style.opacity = Math.min(Math.abs(dx) / 60, 1);
}

messagesEl.addEventListener("touchstart", (event) => {
  if (event.touches.length !== 1) { swipe.tracking = false; return; }
  const touch = event.touches[0];
  swipe.tracking =
    touch.clientX > EDGE_GUARD &&
    touch.clientX < innerWidth - EDGE_GUARD &&
    !selectionActive() &&
    !scrollsHorizontally(event.target);
  swipe.horizontal = false;
  swipe.blocked = false;
  swipe.dx = 0;
  swipe.startX = touch.clientX;
  swipe.startY = touch.clientY;
  swipe.startTime = event.timeStamp;
  swipe.width = messagesEl.clientWidth;
}, { passive: true });

messagesEl.addEventListener("touchmove", (event) => {
  if (!swipe.tracking || swipe.blocked) return;
  const touch = event.touches[0];
  const dx = touch.clientX - swipe.startX;
  const dy = touch.clientY - swipe.startY;
  if (!swipe.horizontal) {
    // A selection appearing mid-touch (long-press) or a slow start means
    // the finger is selecting text, not paging — stand down for this touch.
    if (selectionActive() || event.timeStamp - swipe.startTime > DECIDE_WITHIN) {
      swipe.blocked = true;
      return;
    }
    if (Math.abs(dx) < DECIDE_AT && Math.abs(dy) < DECIDE_AT) return;
    // Mostly-vertical (or diagonal) start: it's a scroll, stand down for the
    // rest of this touch — a late preventDefault can't stop iOS anyway.
    if (Math.abs(dx) < Math.abs(dy) * 1.4) { swipe.blocked = true; return; }
    swipe.horizontal = true;
  }
  event.preventDefault(); // page-drag now, not a scroll
  const target = swipeTarget(dx < 0 ? 1 : -1);
  swipe.dx = target ? dx : dx / 3; // rubber-band where no page exists
  messagesEl.style.transition = "none";
  messagesEl.style.transform = `translateX(${swipe.dx}px)`;
  updateSwipeHint(target, dx, swipe.width * COMMIT_AT);
}, { passive: false });

function commitPage(direction, target, width) {
  // Ask the server before sliding the page away: the off-screen state is
  // only safe while a replay is coming to bring the next page in. On a dead
  // socket (server restart mid-deploy, tab detached by another device)
  // send() fails — snap home instead of leaving the app blank.
  // Offline, the mirror can bring an existing page in — so paging through past
  // chats keeps working, which is the gesture this app is navigated by. A NEW
  // chat still needs the server, so that page snaps back.
  if (!target.fresh && offlineMode) {
    swipeInFrom = direction; // read by the onReplay openCachedSession triggers
    messagesEl.style.transform = `translateX(${-direction * width}px)`;
    openCachedSession(target.name).then((ok) => {
      if (!ok) { swipeInFrom = 0; messagesEl.style.transform = ""; }
    });
    return;
  }
  const requested = target.fresh
    ? send({ type: "new" })
    : send({ type: "resume", path: target.name });
  if (!requested) {
    messagesEl.style.transform = "";
    return;
  }
  swipeInFrom = direction; // the landing replay animates in from this side
  messagesEl.style.transform = `translateX(${-direction * width}px)`;
}

function snapBack(direction, target, dx) {
  messagesEl.style.transform = "";
  if (!target && direction === -1 && Math.abs(dx) > 60) {
    showToast("no older chats — tap the title to search all sessions");
  }
}

function endSwipe(event) {
  const wasHorizontal = swipe.horizontal;
  swipe.tracking = false;
  swipe.horizontal = false;
  if (!wasHorizontal) return;
  $("swipe-hint").hidden = true;
  const dx = swipe.dx;
  const direction = dx < 0 ? 1 : -1;
  const target = swipeTarget(direction);
  const flick =
    Math.abs(dx) > 48 && event.timeStamp - swipe.startTime < 250;
  messagesEl.style.transition = "transform 0.18s ease-out";
  if (target && (Math.abs(dx) > swipe.width * COMMIT_AT || flick)) {
    commitPage(direction, target, swipe.width);
  } else {
    snapBack(direction, target, dx);
  }
}
messagesEl.addEventListener("touchend", endSwipe);
messagesEl.addEventListener("touchcancel", endSwipe);

// ---- trackpad pager (macOS Safari) ---------------------------------------
// A two-finger horizontal swipe arrives as a wheel-event stream, not
// touches. There is no lift-off signal, so the gesture ends when the
// stream goes quiet — or immediately, once the drag crosses the commit
// threshold (waiting out the momentum tail would feel sluggish). The
// first horizontal-dominant event decides the stream's fate: cancelled
// from event one it stays ours; uncancelled, Safari starts its own
// back/forward navigation and no later preventDefault can stop it.
const WHEEL_GAP = 120; // ms of silence = stream over (fingers up, no momentum)
const WHEEL_DRAW_AT = 4; // px of claimed travel before the drag is drawn
// Full COMMIT_AT on a wide desktop window is a lot of trackpad travel; cap it.
const WHEEL_COMMIT_MAX = 200; // px

const wheel = {
  active: false, blocked: false, committed: false,
  dx: 0, pendX: 0, width: 1, endTimer: 0,
};

function wheelStreamOver() {
  const wasActive = wheel.active;
  const dx = wheel.dx;
  wheel.active = false;
  wheel.blocked = false;
  wheel.committed = false;
  wheel.pendX = 0;
  if (!wasActive) return;
  $("swipe-hint").hidden = true;
  messagesEl.style.transition = "transform 0.18s ease-out";
  const direction = dx < 0 ? 1 : -1;
  snapBack(direction, swipeTarget(direction), dx);
}

messagesEl.addEventListener("wheel", (event) => {
  clearTimeout(wheel.endTimer);
  wheel.endTimer = setTimeout(wheelStreamOver, WHEEL_GAP);
  if (wheel.committed) {
    // A page was already committed on this gesture: swallow the momentum
    // tail until the stream goes quiet. A fixed cooldown is not enough —
    // a brisk swipe coasts for well over a second, and the leftovers would
    // restart the pager and commit a second page nobody asked for.
    event.preventDefault();
    return;
  }
  if (wheel.blocked) return; // vertical scroll or opted out — native until quiet
  if (!wheel.active) {
    if (Math.abs(event.deltaX) <= Math.abs(event.deltaY)) {
      // Vertical-dominant start: a scroll. Stand down for the whole stream,
      // matching the touch pager's axis lock.
      if (event.deltaY !== 0) wheel.blocked = true;
      return;
    }
    if (selectionActive() || scrollsHorizontally(event.target)) {
      wheel.blocked = true;
      return;
    }
    // Horizontal-dominant, however faint: claim it from this very event —
    // one uncancelled 1px event is all Safari needs to start its own
    // history swipe (the tab "goes back"). The drag isn't drawn until the
    // claimed travel adds up; a vertical-dominant event arriving while
    // still pending hands the stream back to native scrolling above.
    event.preventDefault();
    wheel.pendX -= event.deltaX;
    if (Math.abs(wheel.pendX) < WHEEL_DRAW_AT) return;
    wheel.active = true;
    wheel.dx = wheel.pendX;
    wheel.pendX = 0;
    wheel.width = messagesEl.clientWidth;
  } else {
    event.preventDefault(); // ours now — keeps Safari's history swipe out
    // Scrolling right (deltaX > 0) drags the page left, like a leftward touch.
    wheel.dx -= event.deltaX;
  }
  const dx = wheel.dx;
  const direction = dx < 0 ? 1 : -1;
  const target = swipeTarget(direction);
  wheel.dx = target ? dx : dx / 3; // rubber-band where no page exists
  messagesEl.style.transition = "none";
  messagesEl.style.transform = `translateX(${wheel.dx}px)`;
  const commitPx = Math.min(wheel.width * COMMIT_AT, WHEEL_COMMIT_MAX);
  updateSwipeHint(target, dx, commitPx);
  if (target && Math.abs(wheel.dx) > commitPx) {
    wheel.active = false;
    wheel.committed = true; // endTimer stays armed: quiet ends the gesture
    $("swipe-hint").hidden = true;
    messagesEl.style.transition = "transform 0.18s ease-out";
    commitPage(direction, target, wheel.width);
  }
}, { passive: false });

// ---- keyboard pager ------------------------------------------------------
// Ctrl+H / Ctrl+L (vim: h = left, l = right) page like the swipe: H = back
// = older chat, L = forward = newer, past the newest = fresh chat. Ctrl,
// not Cmd — Cmd+H hides the window and Cmd+L is the address bar. This
// shadows the text field's emacs-style Ctrl+H (delete backward); Backspace
// still deletes.
document.addEventListener("keydown", (event) => {
  if (!event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  if (event.key !== "h" && event.key !== "l") return;
  event.preventDefault(); // even at the pager's edge, never delete-backward
  // One page per deliberate press: no key-repeat runs, and the previous
  // switch must land (its replay resets swipeInFrom) before the next.
  if (event.repeat || swipeInFrom || !$("backdrop").hidden) return;
  const direction = event.key === "l" ? 1 : -1;
  const target = swipeTarget(direction);
  if (!target) return;
  messagesEl.style.transition = "transform 0.18s ease-out";
  commitPage(direction, target, messagesEl.clientWidth);
});

// sessions
$("back-chip").onclick = () => openSessionsSheet("");
$("session-chip").onclick = () => openSessionMenu();
$("new-chip").onclick = () => requestNewChat();
$("console-btn").onclick = () => toggleConsole(); // global Quake console (#148)

$("connbar").onclick = () => reconnect();
$("offlinebar").onclick = () => reconnect(); // same affordance, one bar (#165)

// ---- session title menu -------------------------------------------------
// The tappable title opens a small menu of session actions (iOS Messages
// convention: settings live behind the title, not a floating overflow chip).
function openSessionMenu() {
  const menu = $("session-menu");
  const del = menu.querySelector('[data-act="delete"]');
  if (del) resetDeleteChat(del); // never open still armed from a prior dismissal
  const clear = menu.querySelector('[data-act="clear-offline"]');
  if (clear) resetClearOffline(clear);
  $("wrap-state").textContent = document.body.classList.contains("wrap") ? "On" : "Off";
  // "On" only for a deliberate pin. Everything is mirrored by default, so
  // reporting "On" for merely-cached chats would make the toggle meaningless.
  // Read from the store, never from the in-memory mirror — see offlineIsPinned.
  refreshOfflinePinLabel();
  // Measure while shown-but-invisible so width is known before centering.
  menu.style.visibility = "hidden";
  menu.hidden = false;
  const anchor = $("session-chip").getBoundingClientRect();
  const width = menu.offsetWidth;
  let left = anchor.left + anchor.width / 2 - width / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
  menu.style.left = `${left}px`;
  menu.style.top = `${anchor.bottom + 6}px`;
  menu.style.visibility = "";
  $("backdrop").hidden = false;
}

// Inline rename: a small titled input anchored under the chat title, opened
// from the session menu. Optimistically updates the header; the server's
// session_renamed confirms and refreshes the drawer.
function openRenameBox() {
  $("session-menu").hidden = true;
  const box = $("rename-box");
  const input = $("rename-input");
  const current = $("session-title").textContent;
  input.value = current === "New chat" ? "" : current;
  box.style.visibility = "hidden";
  box.hidden = false;
  const anchor = $("session-chip").getBoundingClientRect();
  const width = box.offsetWidth;
  let left = anchor.left + anchor.width / 2 - width / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
  box.style.left = `${left}px`;
  box.style.top = `${anchor.bottom + 6}px`;
  box.style.visibility = "";
  $("backdrop").hidden = false;
  input.focus();
  input.select();
}

$("rename-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const title = $("rename-input").value.trim();
  if (!title) { $("rename-input").focus(); return; }
  if (currentSession) send({ type: "rename_session", name: currentSession, title });
  setTitle(title); // optimistic; session_renamed reconfirms (and updates the drawer)
  closeSheets();
});
$("rename-cancel").onclick = () => closeSheets();

$("session-menu").addEventListener("click", (e) => {
  const item = e.target.closest(".menu-item");
  if (!item) return;
  // Deleting the current chat is destructive and unrecoverable, so it arms
  // in place (first tap → red "Confirm delete", second tap sends it) instead
  // of closing the menu — the same two-tap guard as the drawer trash icon.
  if (item.dataset.act === "delete") { armDeleteChat(item); return; }
  // Rename swaps the menu for an inline title field (keeps the backdrop) —
  // no blocking window.prompt, which would also trap automation.
  if (item.dataset.act === "rename") { openRenameBox(); return; }
  // Clearing every cached transcript is unrecoverable without a resync, so it
  // arms in place like Delete chat rather than firing on one tap.
  if (item.dataset.act === "clear-offline") { armClearOffline(item); return; }
  closeSheets(); // hides the menu + backdrop
  switch (item.dataset.act) {
    case "new": requestNewChat(); break;
    case "model": openModelSheet(""); break;
    case "cd": openDirSheet(); break;
    case "wrap": toggleWrap(); break;
    case "export": exportSessionPdf(); break;
    case "workspace": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
    case "copylog": copyLogPath(); break;
    case "offline": toggleOfflinePin(); break;
    case "reconnect": reconnect(); break;
  }
});

// Same two-tap arming as Delete chat, reusing its timing so the two
// irreversible items in this menu behave identically.
let clearOfflineTimer = null;
function resetClearOffline(item) {
  clearTimeout(clearOfflineTimer);
  clearOfflineTimer = null;
  item.classList.remove("armed");
  item.querySelector(".menu-label").textContent = "Clear offline copies";
}
function armClearOffline(item) {
  if (clearOfflineTimer) {
    resetClearOffline(item);
    closeSheets();
    offlineClearAll();
    return;
  }
  item.classList.add("armed");
  item.querySelector(".menu-label").textContent = "Confirm clear";
  clearOfflineTimer = setTimeout(() => resetClearOffline(item), 4000);
}

// The current-chat delete item's two-step confirm. The server refuses a
// running session and lands the client on a fresh chat when the active one is
// deleted, so no client-side special cases are needed here (see server
// _delete_session).
let deleteChatTimer = null;
function resetDeleteChat(item) {
  clearTimeout(deleteChatTimer);
  deleteChatTimer = null;
  item.classList.remove("armed");
  item.querySelector(".menu-label").textContent = "Delete chat";
}
function armDeleteChat(item) {
  if (deleteChatTimer) {
    resetDeleteChat(item);
    closeSheets();
    if (currentSession) send({ type: "delete_session", name: currentSession });
    return;
  }
  item.classList.add("armed");
  item.querySelector(".menu-label").textContent = "Confirm delete";
  deleteChatTimer = setTimeout(() => resetDeleteChat(item), 4000);
}

// ---- attention badge ----------------------------------------------------
// A background session that finished (or needs you) sets the durable badge on
// the ‹ Sessions button; opening the list clears it.
// ---- Sessions tabs (Recent / Automated) ----------------------------------
const SESSION_TAB_KEY = "aish-session-tab";
let sessionTab = (() => {
  try { return localStorage.getItem(SESSION_TAB_KEY) === "automated" ? "automated" : "recent"; }
  catch { return "recent"; }
})();
let lastSessionEvent = null; // last session_list, so tab switches re-render offline
let tabSwipeActive = false;   // a horizontal list swipe is in progress → rows stand down

// SESSIONS_PARTITION_START
// Split sessions into the two tabs and tally each tab's attention counters.
// Automated = a triggered origin (schedule/email/webhook, #160); everything
// else is a user chat. Counts are computed for BOTH tabs regardless of which
// is showing, so the inactive tab's badge still flags things needing you.
function partitionSessions(sessions) {
  const isActive = (s) => s.state === "running" || s.state === "waiting";
  const isAuto = (s) => Boolean(s.origin && s.origin !== "user");
  const groups = { recent: [], automated: [] };
  const counts = {
    recent: { running: 0, waiting: 0 },
    automated: { running: 0, waiting: 0 },
  };
  for (const s of sessions) {
    const key = isAuto(s) ? "automated" : "recent";
    groups[key].push(s);
    if (s.state === "running") counts[key].running++;
    else if (s.state === "waiting") counts[key].waiting++;
  }
  return { groups, counts, isActive };
}
// SESSIONS_PARTITION_END

const attentionSessions = new Set();
function refreshBadge() {
  const badge = $("back-badge");
  if (attentionSessions.size) {
    badge.textContent = String(attentionSessions.size);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}
// The list is rendered from the local mirror FIRST and replaced by the server's
// answer when it arrives. Online that just means the sheet paints instantly
// instead of after a round trip; offline it is the only source, and search
// keeps working because the ranking is a port of the server's own tiers
// (offlineRank) rather than a different, weaker matcher.
async function renderOfflineSessions(query) {
  const metas = await offlineList();
  if (!metas.length) return false;
  renderSessions({
    type: "session_list",
    current: currentSession,
    fromCache: true,
    sessions: offlineRank(metas, query || "").map((meta) => ({
      name: meta.name,
      title: meta.title,
      snippet: meta.snippet,
      ts: meta.ts,
      state: "", // liveness is a server fact; a mirror can only lie about it
      cwd: "",
      origin: meta.origin,
      pinned: meta.pinned,
    })),
  });
  return true;
}

function requestSessions(query) {
  // The server's rows are decorated with pin state from the in-memory mirror,
  // so make sure it is current before the response lands — otherwise the
  // "offline" badge silently goes missing in the window after a reload.
  offlineRefreshMetaMap();
  renderOfflineSessions(query);
  // Don't call send() while offline: its toast would fire on every keystroke
  // for a condition the offline bar already states.
  if (ws && ws.readyState === WebSocket.OPEN) send({ type: "sessions", query });
}

$("sessions-search").addEventListener(
  "input",
  debounce(() => requestSessions($("sessions-search").value), 150)
);

function openSessionsSheet(query) {
  // A full-page screen, not a bottom sheet — dismiss any open sheet/menu first,
  // no backdrop (it covers the whole chat).
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  for (const menu of document.querySelectorAll(".popover-menu")) menu.hidden = true;
  $("backdrop").hidden = true;
  $("sessions-sheet").hidden = false;
  attentionSessions.clear();
  refreshBadge();
  $("sessions-search").value = query;
  // Auto-focus only where a hardware keyboard is likely: on touch devices
  // focusing would throw the on-screen keyboard over the list before the
  // user has even seen it — there, browsing is the common case and a tap
  // on the field opts into searching.
  if (FINE_POINTER) {
    // Focus only after the sheet's layout settles: focusing synchronously lets
    // iOS measure the input at its pre-layout position and pan the whole
    // layout absurdly far to "reveal" it — the sheet then opens scrolled away
    // and stuck until the keyboard closes (#24). preventScroll stops the
    // browser's own reveal-scroll; the input is already visible.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        $("sessions-search").focus({ preventScroll: true });
        setTimeout(() => reportViewport("search-focused"), 600);
      })
    );
  }
  requestSessions(query);
}

// Only the states the user can act on. "idle but open in server memory" is
// an implementation detail — resume behaves identically either way.
const STATE_BADGES = {
  running: ["Running", "st-running"],
  waiting: ["Needs approval", "st-waiting"],
};

const DAY_MS = 86400000;
function dayStart(stamp) {
  const day = new Date(stamp);
  day.setHours(0, 0, 0, 0);
  return +day;
}

function sessionGroup(ts) {
  const now = Date.now();
  const ms = ts * 1000;
  if (now - ms < 8 * 3600 * 1000) return "Recent";
  const today = dayStart(now);
  const day = dayStart(ms);
  if (day >= today) return "Today";
  if (day >= today - DAY_MS) return "Yesterday";
  if (day >= today - 7 * DAY_MS) return "Previous 7 days";
  return "Older";
}

// Within the last hour, show relative time ("just now", "2m") — for something
// touched minutes ago an absolute clock reading forces mental arithmetic.
// Older entries fall back to a 24h absolute time (never AM/PM), gaining a
// weekday past midnight and a date past a week.
function sessionStamp(ts) {
  const ms = ts * 1000;
  const now = Date.now();
  const delta = now - ms;
  if (delta < 60 * 1000) return "just now";
  if (delta < 3600 * 1000) return `${Math.floor(delta / 60000)}m`;
  const date = new Date(ms);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const today = dayStart(now);
  const day = dayStart(date);
  if (day >= today) return time;
  if (day >= today - 6 * DAY_MS)
    return `${date.toLocaleDateString([], { weekday: "short" })} ${time}`;
  return `${date.toLocaleDateString([], { day: "numeric", month: "short" })}, ${time}`;
}

const SESSION_ICONS = {
  waiting: `<svg viewBox="0 0 24 24"><path d="M12 3.5 21 19H3z" fill="none" stroke="var(--orange)" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v3.6M12 16.4v.1" stroke="var(--orange)" stroke-width="1.9" stroke-linecap="round"/></svg>`,
  current: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="var(--blue)" stroke-width="1.8"/><path d="M8.5 12.5l2.3 2.3 4.7-5" stroke="var(--blue)" stroke-width="1.9" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  idle: `<svg viewBox="0 0 24 24"><path d="M5 6h13M5 12h14M5 18h9" stroke="var(--dim)" stroke-width="1.9" stroke-linecap="round"/></svg>`,
};

function sessionIcon(info, isCurrent) {
  const wrap = document.createElement("span");
  wrap.className = "row-icon";
  if (info.state === "running") {
    wrap.style.background = "var(--green-glow)";
    wrap.innerHTML = '<span class="spin"></span>';
  } else if (info.state === "waiting") {
    wrap.style.background = "var(--orange-glow)";
    wrap.innerHTML = SESSION_ICONS.waiting;
  } else if (isCurrent) {
    wrap.style.background = "var(--blue-glow)";
    wrap.innerHTML = SESSION_ICONS.current;
  } else {
    wrap.style.background = "var(--chip-bg)";
    wrap.innerHTML = SESSION_ICONS.idle;
  }
  return wrap;
}

function sessionRow(info, current) {
  const isCurrent = info.name === current;
  const row = document.createElement("button");
  row.className = "row session-row" + (isCurrent ? " current" : "");
  const body = document.createElement("span");
  body.className = "session-body";
  const head = document.createElement("span");
  head.className = "line";
  const title = document.createElement("span");
  title.className = "title";
  title.textContent = info.title;
  head.appendChild(title);
  const badgeSpec = STATE_BADGES[info.state];
  if (badgeSpec) {
    const badge = document.createElement("span");
    badge.className = `badge ${badgeSpec[1]}`;
    badge.textContent = badgeSpec[0];
    head.appendChild(badge);
  }
  // Provenance tag for triggered sessions (#160) so an automated chat is
  // legible even in the flat "Active now" / search views, not only under its
  // "Automated" section.
  if (info.origin && info.origin !== "user") {
    const tag = document.createElement("span");
    tag.className = "badge origin";
    tag.textContent = info.origin;
    head.appendChild(tag);
  }
  // Kept offline on purpose (#165): this chat survives the eviction sweep, so
  // it is here whether or not the server is.
  if (info.pinned) {
    const pin = document.createElement("span");
    pin.className = "badge offline-pin";
    pin.title = "kept available offline";
    pin.textContent = "offline";
    head.appendChild(pin);
  }
  body.appendChild(head);
  if (info.cwd) {
    const dir = document.createElement("span");
    dir.className = "session-dir mono";
    dir.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';
    dir.appendChild(document.createTextNode(abbreviatePath(info.cwd)));
    body.appendChild(dir);
  }
  if (info.snippet) {
    const snippet = document.createElement("span");
    snippet.className = "snippet";
    snippet.textContent = info.snippet;
    body.appendChild(snippet);
  }
  const right = document.createElement("span");
  right.className = "session-right";
  const stamp = document.createElement("span");
  stamp.className = "stamp";
  stamp.textContent = sessionStamp(info.ts);
  right.append(stamp, sessionDeleteControl(info));
  row.append(sessionIcon(info, isCurrent), body, right);
  return wrapSwipeDelete(row, info);
}

// iOS swipe-left-to-delete. The row rides over a red Delete button; a tap on
// an open row snaps it shut, a tap on a closed row resumes the session.
function wrapSwipeDelete(row, info) {
  const wrap = document.createElement("div");
  wrap.className = "swipe-wrap";
  const del = document.createElement("button");
  del.type = "button";
  del.className = "swipe-del";
  del.textContent = "Delete";
  del.onclick = (e) => { e.stopPropagation(); send({ type: "delete_session", name: info.name }); };
  wrap.append(del, row);
  let startX = null, dx = 0, open = false;
  const set = (x) => { row.style.transform = x ? `translateX(${x}px)` : ""; };
  row.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    startX = e.clientX; dx = 0;
  });
  row.addEventListener("pointermove", (e) => {
    if (startX === null) return;
    if (tabSwipeActive) { set(open ? -88 : 0); return; } // a tab swipe owns this gesture
    dx = e.clientX - startX;
    if (Math.abs(dx) > 6) row.classList.add("dragging");
    set(Math.min(0, Math.max(-100, (open ? -88 : 0) + dx)));
  });
  const finish = () => {
    if (startX === null) return;
    if (tabSwipeActive) { row.classList.remove("dragging"); set(open ? -88 : 0); startX = null; return; }
    const moved = Math.abs(dx) > 6;
    open = (open ? -88 : 0) + dx < -44;
    row.classList.remove("dragging");
    set(open ? -88 : 0);
    startX = null;
    if (moved) { row.dataset.swiped = "1"; requestAnimationFrame(() => delete row.dataset.swiped); }
  };
  row.addEventListener("pointerup", finish);
  row.addEventListener("pointercancel", finish);
  row.onclick = () => {
    if (row.dataset.swiped) return;      // this "click" was really a swipe
    if (open) { open = false; set(0); return; } // tap an open row → close it
    resumeSession(info.name); // the socket if there is one, else the mirror
    closeSheets();
  };
  return wrap;
}

function renderTabCounts(id, c) {
  const el = $(id);
  el.replaceChildren();
  const pill = (cls, n, label) => {
    const p = document.createElement("span");
    p.className = `seg-count ${cls}`;
    p.textContent = String(n);
    p.title = `${n} ${label}`;
    el.appendChild(p);
  };
  if (c.running) pill("running", c.running, "running");
  if (c.waiting) pill("waiting", c.waiting, "need approval");
}

function syncTabSelection() {
  for (const id of ["tab-recent", "tab-automated"]) {
    const btn = $(id);
    btn.setAttribute("aria-selected", btn.dataset.tab === sessionTab ? "true" : "false");
  }
}

function setSessionTab(tab) {
  if ((tab !== "recent" && tab !== "automated") || tab === sessionTab) return;
  sessionTab = tab;
  try { localStorage.setItem(SESSION_TAB_KEY, tab); } catch { /* private mode */ }
  if (lastSessionEvent) renderSessions(lastSessionEvent);
}

$("tab-recent").onclick = () => setSessionTab("recent");
$("tab-automated").onclick = () => setSessionTab("automated");

// Active (running / needs-approval) sessions float to the top under their own
// header; the rest keep the date grouping. Shared by both tabs.
function renderSessionList(sessions, current, isActive) {
  const list = $("sessions-list");
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "section-label";
    empty.textContent = sessionTab === "automated"
      ? "No automated sessions" : "No chats yet";
    list.appendChild(empty);
    return;
  }
  const active = sessions.filter(isActive);
  const rest = sessions.filter((s) => !isActive(s));
  if (active.length) {
    list.appendChild(sectionLabel("Active now"));
    for (const info of active) list.appendChild(sessionRow(info, current));
  }
  let lastGroup = null;
  for (const info of rest) {
    const group = sessionGroup(info.ts);
    if (group !== lastGroup) {
      list.appendChild(sectionLabel(group));
      lastGroup = group;
    }
    list.appendChild(sessionRow(info, current));
  }
}

function renderSessions(event) {
  lastSessionEvent = event;
  // Carry the mirror's own facts onto server-supplied rows, so "kept offline"
  // shows in the authoritative list too, not only in the cached one.
  if (!event.fromCache) {
    for (const info of event.sessions) {
      const meta = offlineMeta.get(info.name);
      if (meta) info.pinned = meta.pinned;
    }
  }
  const list = $("sessions-list");
  list.replaceChildren();
  // Ranked search results are ordered by relevance, so date/status grouping —
  // and the tab split — would lie. Hide the tabs and render a flat list.
  if ($("sessions-search").value.trim()) {
    $("sessions-tabs").hidden = true;
    if (!event.sessions.length) { list.textContent = "no matching sessions"; return; }
    for (const info of event.sessions) list.appendChild(sessionRow(info, event.current));
    return;
  }
  $("sessions-tabs").hidden = false;
  const { groups, counts, isActive } = partitionSessions(event.sessions);
  renderTabCounts("tab-recent-counts", counts.recent);
  renderTabCounts("tab-automated-counts", counts.automated);
  syncTabSelection();
  renderSessionList(groups[sessionTab], event.current, isActive);
}

// Horizontal swipe on the list switches tabs (a thumb gesture, matching the
// message-pager). It reuses that pager's axis disambiguation; once it locks
// horizontal it sets tabSwipeActive so a same-gesture per-row swipe-delete
// (which also reads horizontal pointer moves) stands down for this touch.
(function attachSessionTabSwipe() {
  const list = $("sessions-list");
  let sx = 0, sy = 0, t0 = 0, tracking = false, horizontal = false, blocked = false;
  list.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1 || $("sessions-tabs").hidden) { tracking = false; return; }
    const t = e.touches[0];
    sx = t.clientX; sy = t.clientY; t0 = e.timeStamp;
    tracking = true; horizontal = false; blocked = false;
  }, { passive: true });
  list.addEventListener("touchmove", (e) => {
    if (!tracking || blocked) return;
    const t = e.touches[0];
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if (horizontal) return;
    if (e.timeStamp - t0 > 300) { blocked = true; return; }   // slow start = scroll/select
    if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return;        // undecided yet
    if (Math.abs(dx) < Math.abs(dy) * 1.4) { blocked = true; return; } // mostly vertical
    horizontal = true;
    tabSwipeActive = true;
    for (const r of list.querySelectorAll(".session-row.dragging")) {
      r.style.transform = ""; r.classList.remove("dragging"); // undo any nascent row-swipe
    }
  }, { passive: true });
  const end = (e) => {
    if (tracking && horizontal) {
      const dx = (e.changedTouches[0] || {}).clientX - sx;
      if (Math.abs(dx) > 50) setSessionTab(dx < 0 ? "automated" : "recent");
    }
    tracking = false; horizontal = false;
    // Clear on the next tick so the row's own pointerup handler still sees it.
    setTimeout(() => { tabSwipeActive = false; }, 0);
  };
  list.addEventListener("touchend", end);
  list.addEventListener("touchcancel", end);
})();

const TRASH_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 7V5a1 1 0 0 1 ' +
  '1-1h2a1 1 0 0 1 1 1v2m-8 0l1 13h8l1-13M10 11v6m4-6v6"/></svg>';

function sessionDeleteControl(info) {
  // A span, not a nested <button> (the row itself is one). Deleting is
  // destructive and unrecoverable, so it takes two taps: the first arms the
  // control (turns into a red "Delete?"), the second sends the delete; it
  // disarms on timeout so a stray tap can't linger. The server refuses
  // running sessions and lands the client on a fresh chat when the current
  // one is deleted — no client-side special cases needed.
  const del = document.createElement("span");
  del.className = "row-delete";
  del.setAttribute("role", "button");
  del.setAttribute("aria-label", `delete session ${info.title || info.name}`);
  del.innerHTML = TRASH_SVG;
  let armed = false;
  let timer = null;
  del.onclick = (event) => {
    event.stopPropagation();
    if (armed) {
      clearTimeout(timer);
      send({ type: "delete_session", name: info.name });
      return;
    }
    armed = true;
    del.classList.add("armed");
    del.textContent = "Delete?";
    timer = setTimeout(() => {
      armed = false;
      del.classList.remove("armed");
      del.innerHTML = TRASH_SVG;
    }, 4000);
  };
  return del;
}

// models
$("model-chip").onclick = () => openModelSheet("");
$("model-search").addEventListener(
  "input",
  debounce(() => send({ type: "models", query: $("model-search").value }), 150)
);

function openModelSheet(query) {
  openSheet("model-sheet");
  $("model-search").value = query;
  $("model-search").focus();
  $("model-list").textContent = "loading models…";
  send({ type: "models", query });
}

const RECENT_MODELS_KEY = "aish-recent-models";
function recentModels() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_MODELS_KEY));
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}
function rememberModel(name) {
  const list = [name, ...recentModels().filter((n) => n !== name)].slice(0, 5);
  localStorage.setItem(RECENT_MODELS_KEY, JSON.stringify(list));
}

function renderModels(event) {
  const list = $("model-list");
  list.replaceChildren();
  const modelRow = (model) => {
    const isCurrent = model.name === event.current;
    const row = document.createElement("button");
    row.className = "row" + (isCurrent ? " current" : "");
    row.textContent = model.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = model.desc;
    row.appendChild(meta);
    if (isCurrent) {
      // A trailing checkmark on the active model (#121) — instant clarity on
      // top of the subtle .current row tint; the iOS "selected row" convention.
      const check = document.createElement("span");
      check.className = "row-check";
      check.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      row.appendChild(check);
    }
    row.onclick = () => {
      rememberModel(model.name);
      send({ type: "set_model", spec: model.name, save: $("model-save").checked });
    };
    return row;
  };
  // Browsing (no search): surface recently-chosen models up top.
  if (!$("model-search").value.trim()) {
    const recents = recentModels()
      .filter((n) => n !== event.current && event.models.some((m) => m.name === n));
    if (recents.length) {
      list.appendChild(sectionLabel("Recent"));
      for (const n of recents) list.appendChild(modelRow(event.models.find((m) => m.name === n)));
      list.appendChild(sectionLabel("All models"));
    }
  }
  for (const model of event.models) list.appendChild(modelRow(model));
}

function onModelChanged(event) {
  $("model-name").textContent = event.model;
  closeSheets();
  showToast(event.saved ? `model: ${event.model} (saved as default)` : `model: ${event.model}`);
}

// workspace
$("ws-cd-change").onclick = () => openDirSheet();
$("root-add").onclick = () => {
  const path = $("root-input").value.trim();
  if (path) { send({ type: "add_dir", path }); $("root-input").value = ""; }
};
$("jobs-refresh").onclick = () => send({ type: "jobs" });

attachListNav($("sessions-search"), $("sessions-list"));
attachListNav($("model-search"), $("model-list"));

function renderWorkspace(event) {
  if (event.home) homeDir = event.home;
  if (event.cwd) {
    currentCwd = event.cwd;
    $("ws-cwd").textContent = event.cwd;
    $("cwd-name").textContent = baseName(event.cwd);
    $("cwd-text").textContent = abbreviatePath(event.cwd);
    refreshCmdPrompt(); // keep the terminal prompt's `dir $` in sync after cd
  }
  if (event.roots) $("ws-roots").textContent = event.roots.join("\n");
}

// ---- cwd chip + directory picker -----------------------------------------
let homeDir = "";
let currentCwd = "";

function abbreviatePath(path) {
  let p = path;
  if (homeDir && (p === homeDir || p.startsWith(homeDir + "/"))) {
    p = "~" + p.slice(homeDir.length);
  }
  // middle-free truncation keeping the leaf — the informative part
  return p.length > 38 ? "…" + p.slice(-37) : p;
}

// The directory's leaf name — the context bar's bold primary line.
function baseName(path) {
  const leaf = path.replace(/\/+$/, "").split("/").pop();
  return leaf || path;
}

const RECENT_DIRS_KEY = "aish-recent-dirs";
function recentDirs() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_DIRS_KEY));
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}
function rememberDir(path) {
  const list = [path, ...recentDirs().filter((p) => p !== path)].slice(0, 6);
  localStorage.setItem(RECENT_DIRS_KEY, JSON.stringify(list));
}
// Drop a path from the recents list only — never touches the actual folder (#89).
function forgetDir(path) {
  localStorage.setItem(
    RECENT_DIRS_KEY, JSON.stringify(recentDirs().filter((p) => p !== path))
  );
}

let dirPath = "";       // directory the picker is browsing
let dirEntries = [];    // its subdirectories, as {name, items}
let dirFiles = [];      // its files (display only, non-navigable)
let dirTruncated = false; // listing hit the cap (huge folder)

async function dirsFetch(url, params) {
  if (token) params.set("token", token);
  const response = await fetch(`${BASE}${url.replace(/^\//, "")}?${params}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.status);
  return body;
}

async function browseDir(path, row = null) {
  $("dir-sheet").classList.add("browsing");
  // A browse can take up to the backend's timeout (iCloud/slow dirs). Show the
  // spinner on the tapped row when there is one (#87), else the centered overlay
  // (crumb/back/"choose another folder" have no row to mark).
  if (row) row.classList.add("loading");
  else setDirLoading(true);
  let body;
  try {
    body = await dirsFetch("/dirs", new URLSearchParams({ path }));
  } catch (err) {
    if (row) row.classList.remove("loading");
    else setDirLoading(false);
    renderDirUnlistable(path, err.message);
    return;
  }
  // A successful browse re-renders the list (below), discarding the marked row.
  setDirLoading(false);
  dirPath = body.path;
  dirEntries = body.dirs;
  dirFiles = body.files || [];
  dirTruncated = body.truncated || false;
  renderDirList();
}

function setDirLoading(on) {
  $("dir-list").classList.toggle("loading", on);
}

// A folder we couldn't list — the backend timed out reading it (an iCloud or
// networked folder the headless server can't materialize). We can't show its
// contents, but SELECTING it as the working dir doesn't need a listing (cd only
// stats the path), so keep it reachable via ".." and the "Use this folder"
// button, which still targets this path.
function renderDirUnlistable(path, msg) {
  dirPath = path;
  $("dir-sheet").classList.add("browsing");
  $("dir-current").textContent = abbreviatePath(path);
  $("dir-use-label").textContent = `Set working directory to “${baseName(path)}”`;
  renderDirCrumb();
  const list = $("dir-list");
  list.replaceChildren();
  if (dirPath !== "/") {
    list.appendChild(
      dirRow("..", null, (e) => browseDir(dirPath.replace(/\/[^/]+$/, "") || "/", e.currentTarget), "up")
    );
  }
  const note = document.createElement("div");
  note.className = "dir-empty";
  note.textContent =
    `Couldn't open this folder (${msg}) — it may be an iCloud or network folder ` +
    `the server can't read. You can still set it as the working directory below.`;
  list.appendChild(note);
}

// Step 1 of the picker: recent folders + "Choose another folder…" → the
// browser. Recents SELECT the directory directly; matches the design flow.
function showDirRecents() {
  $("dir-sheet").classList.remove("browsing");
  const list = $("dir-list");
  list.replaceChildren();
  const recents = recentDirs();
  if (recents.length) {
    list.appendChild(sectionLabel("Recent"));
    for (const p of recents) {
      const row = dirRow(baseName(p), abbreviatePath(p), () => selectDir(p), "recent");
      row.querySelector(".dir-chev").remove(); // a selection, not a descent
      if (p === currentCwd) row.classList.add("selected");
      // "Remove from Recents" ✕ — clears the history entry, NOT the folder (#89).
      const remove = document.createElement("span");
      remove.className = "dir-remove";
      remove.setAttribute("role", "button");
      remove.tabIndex = 0;
      remove.title = "Remove from Recents";
      remove.setAttribute("aria-label", `Remove ${baseName(p)} from Recents`);
      remove.innerHTML =
        '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
      const doRemove = (e) => { e.stopPropagation(); forgetDir(p); showDirRecents(); };
      remove.addEventListener("click", doRemove);
      remove.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") doRemove(e);
      });
      row.appendChild(remove);
      list.appendChild(row);
    }
  }
  const choose = document.createElement("button");
  choose.type = "button";
  choose.className = "dir-choose";
  choose.innerHTML =
    '<svg viewBox="0 0 24 24"><path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 10v5M9.5 12.5h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>Choose another folder…';
  choose.onclick = () => browseDir(currentCwd || homeDir || "/");
  list.appendChild(choose);
}

function selectDir(path) {
  rememberDir(path);
  send({ type: "cd", path });
  closeSheets();
}

const DIR_ICON_FOLDER = '<svg class="dir-ico" viewBox="0 0 24 24"><path d="M3.5 6.8a2 2 0 0 1 2-2h3.4l2 2.2h7.6a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="var(--folder-fill)" stroke="var(--blue)" stroke-width="1.6"/></svg>';
const DIR_ICON_UP = '<svg class="dir-ico" viewBox="0 0 24 24"><path d="M14.5 5.5 8 12l6.5 6.5" fill="none" stroke="var(--blue)" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const DIR_ICON_FILE = '<svg class="dir-ico" viewBox="0 0 24 24"><path d="M6.5 3.8h7l4 4v11.4a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1V4.8a1 1 0 0 1 1-1z" fill="none" stroke="var(--dim)" stroke-width="1.6" stroke-linejoin="round"/><path d="M13.2 3.8v4h4" fill="none" stroke="var(--dim)" stroke-width="1.6" stroke-linejoin="round"/></svg>';
const DIR_CHEVRON = '<svg class="dir-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" fill="none" stroke="var(--sep2)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function dirRow(label, meta, onTap, kind = "folder") {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "row dir-row" + (kind === "up" ? " up" : "") + (kind === "file" ? " file" : "");
  row.innerHTML = kind === "up" ? DIR_ICON_UP : kind === "file" ? DIR_ICON_FILE : DIR_ICON_FOLDER;
  const name = document.createElement("span");
  name.className = "folder";
  name.textContent = label;
  row.appendChild(name);
  if (meta) {
    const metaEl = document.createElement("span");
    metaEl.className = "meta";
    metaEl.textContent = meta;
    row.appendChild(metaEl);
  }
  if (kind === "folder" || kind === "recent") row.insertAdjacentHTML("beforeend", DIR_CHEVRON);
  if (kind === "file") row.disabled = true; // display only — not a destination
  else row.onclick = onTap;
  return row;
}

function sectionLabel(text) {
  const el = document.createElement("div");
  el.className = "section-label";
  el.textContent = text;
  return el;
}

// A tappable full-path breadcrumb at the top so you always know where you are
// and can jump back up quickly (#13).
function renderDirCrumb() {
  const crumb = $("dir-crumb");
  crumb.replaceChildren();
  const isHome = homeDir && (dirPath === homeDir || dirPath.startsWith(homeDir + "/"));
  const rel = (isHome ? dirPath.slice(homeDir.length) : dirPath).replace(/^\/+/, "");
  const segs = rel ? rel.split("/") : [];
  const seg = (label, path, last) => {
    const el = document.createElement(last ? "span" : "button");
    el.className = "crumb-seg" + (last ? " current" : "");
    el.textContent = label;
    if (!last) el.onclick = () => browseDir(path);
    crumb.appendChild(el);
  };
  seg(isHome ? "~" : "/", isHome ? homeDir : "/", segs.length === 0);
  let acc = isHome ? homeDir : "";
  segs.forEach((s, i) => {
    // The "/" root already reads as a separator, so skip it before the first
    // segment of an absolute path (avoids a leading "/ /").
    if (!(i === 0 && !isHome)) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "/";
      crumb.appendChild(sep);
    }
    acc = acc + "/" + s;
    seg(s, acc, i === segs.length - 1);
  });
}

function renderDirList() {
  $("dir-current").textContent = abbreviatePath(dirPath);
  $("dir-use-label").textContent = `Set working directory to “${baseName(dirPath)}”`;
  renderDirCrumb();
  const list = $("dir-list");
  list.replaceChildren();

  if (dirPath !== "/") {
    list.appendChild(
      dirRow("..", null, (e) => browseDir(dirPath.replace(/\/[^/]+$/, "") || "/", e.currentTarget), "up")
    );
  }
  const visible = dirEntries.filter(({ name }) => !name.startsWith("."));
  for (const { name, items } of visible) {
    // items may be null (symlink, unreadable, or counting budget spent) — show no count then.
    const meta = items == null ? null : items === 1 ? "1 item" : `${items} items`;
    list.appendChild(
      dirRow(name, meta, (e) =>
        browseDir(dirPath === "/" ? `/${name}` : `${dirPath}/${name}`, e.currentTarget))
    );
  }

  // Files are shown dimmed and non-navigable (distinguished by icon/dimming, no
  // header — #93) so the picker previews a folder's contents without becoming a
  // file browser.
  const visibleFiles = dirFiles.filter((n) => !n.startsWith("."));
  for (const name of visibleFiles) list.appendChild(dirRow(name, null, null, "file"));
  if (dirTruncated) {
    const note = document.createElement("div");
    note.className = "dir-empty";
    note.textContent = "Showing the first 1000 entries.";
    list.appendChild(note);
  }
}

function openDirSheet() {
  openSheet("dir-sheet");
  dirPath = currentCwd || homeDir || "/";
  showDirRecents(); // step 1: recents + "Choose another folder…"
}

$("cwd-chip").onclick = () => openDirSheet();
$("dir-back").onclick = () => showDirRecents(); // back to step 1 (recents) from browsing
$("dir-use").onclick = () => {
  if (!dirPath) return;
  rememberDir(dirPath);
  send({ type: "cd", path: dirPath });
  closeSheets();
};
// toast
let toastTimer;
// sticky keeps the toast up until the next showToast/hideToast — used for
// in-progress feedback (e.g. a slow PDF export) that a later call resolves.
function showToast(text, sticky = false) {
  const toast = $("toast");
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
}

function hideToast() {
  clearTimeout(toastTimer);
  $("toast").hidden = true;
}

// Copy to the clipboard with a toast, falling back to an execCommand copy off a
// hidden textarea when the async Clipboard API is unavailable (older WKWebView /
// non-secure context). Always shows the text so it's grabbable even if both fail.
function copyToClipboard(text, label = "copied") {
  const done = () => showToast(`${label}: ${text}`);
  const fallback = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* nothing more to try */ }
    ta.remove();
    done();
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, fallback);
  } else {
    fallback();
  }
}

// /session and the ⋯ menu's "Copy log path": copy the session's JSONL log path.
function copyLogPath() {
  if (!currentLogPath) { showToast("no session log yet"); return; }
  copyToClipboard(currentLogPath, "log path");
}

$("token-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("token-input").value.trim();
  if (!value) return;
  localStorage.setItem("aish-token", value);
  location.reload(); // reconnect with the new token from a clean slate
});

// Paint the last chat from the mirror, then open the socket. Not awaited: an
// IndexedDB hiccup must never delay the connection, and whichever finishes
// first is correct — a server replay overwrites the cached paint, and the
// cached paint checks serverPainted before touching the DOM.
offlineFirstPaint();
offlineRefreshMetaMap();
connect();
