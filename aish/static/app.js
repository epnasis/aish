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

// [OPEN-NEW-START]
// `?new` opens a FRESH chat instead of resuming the last one — for a Home
// Screen icon, or a Shortcut, that should always start clean (a shared photo
// landing in its own chat rather than on top of yesterday's conversation).
//
// It is consumed ONCE, here, and stripped from the URL before anything else
// reads it. That is the whole load-bearing part: a launch parameter that
// survived in the address bar would open a new chat on every reload, every
// rev-mismatch reload the app performs on itself, and every relaunch of a PWA
// that restores its last URL — turning one intent into an endless drip of
// empty chats. Stripping it also means a RECONNECT (which re-reads nothing but
// still calls connect()) cannot re-trigger it.
//
// It beats `?session=` if both are somehow given, and suppresses the remembered
// session on this one load: resuming a chat we are about to leave would paint
// it for a moment and then switch, which reads as a glitch.
let openNewOnLoad = new URLSearchParams(location.search).has("new");
if (openNewOnLoad) {
  const url = new URL(location.href);
  url.searchParams.delete("new");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}
// [OPEN-NEW-END]

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
    out: payload.out || 0, // last OUTPUT; 0 = fall back to `ts` (#203)
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
const OFFLINE_SYNC_AFTER_DONE_MS = 15 * 1000; // debounce after a finished turn
const FIRST_PAINT_GRACE_MS = 250; // let a fast LAN replay win the first paint outright
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

// [MIRROR-FORGET-START]
// Dropping this device's copy of a chat that is gone from the server. ONE
// function, because there are two ways to learn it — the sync notices the
// catalogue no longer lists it, and the socket says so outright
// (`session_deleted`, `no_such_session`) — and a chat half-forgotten by one of
// them is a ghost the other never gets to clean up.
async function offlineForget(name) {
  if (!name) return;
  await offlineSafe(idbDel("events", name));
  await offlineSafe(idbDel("meta", name));
  offlineMeta.delete(name);
}

// PURE: the cached chats the server's catalogue no longer lists.
//
// GONE IS GONE, pin included. A pin means "keep this copy for me" against
// EVICTION — the budget sweep that gives up chats to stay under quota — and it
// was read here as "keep it even if the chat no longer exists", which is not a
// promise anyone made: the delete confirmation says in as many words that the
// copy on your devices goes too. What it bought instead was a row no sync could
// ever remove.
function mirrorOrphans(cached, serverNames) {
  return Array.from(cached.keys()).filter((name) => !serverNames.has(name));
}
// [MIRROR-FORGET-END]

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
    // resurrects deleted chats is a surprise, not a feature ([MIRROR-FORGET]).
    for (const name of mirrorOrphans(offlineMeta, server)) await offlineForget(name);

    // The catalogue is newest-first, so a sync cut short by a dying connection
    // has still fetched the sessions most likely to be wanted.
    for (const info of index.sessions) {
      if (offlineMode) break;
      // The chat on screen is the one you least need mirrored right now, and
      // the one whose refetch costs the most (its mtime moves every turn, so
      // it re-syncs on every pass — a full server-side re-parse plus an
      // IndexedDB rewrite that grows with the chat). It catches up when the
      // app goes hidden (see visibilitychange), which is when the mirror is
      // actually about to be needed.
      if (info.name === currentSession && document.visibilityState === "visible") continue;
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

// [SESSION-ENTER-START]
// "The view is now chat X" has ONE owner, and this is it (#181 phase 2).
//
// Four paths reach that transition — a server hello, a mirror read with no
// socket, the boot paint from IndexedDB, and the speculative paint of a
// prefetched swipe — and each used to hand-roll its own subset of the same
// coupled facts. The subsets DIVERGED, which is the defect this fence exists to
// make impossible: the boot paint never reset the view fingerprint and never
// remembered where it landed, an unguarded localStorage write mid-hello could
// abort the whole rest of the hello in private mode (no workspace, no busy
// state, no boot-loader hide), and during any switch there was a window where
// the identity, the title, the URL and the DOM each named a different chat.
//
// So every coupled write happens HERE, in one fixed total order, on every path:
//
//   1. stash the outgoing view — before identity moves, since that DOM belongs
//      to the chat being left
//   2. reset the view fingerprint, IFF the name actually changes
//   3. identity (currentSession)
//   4. provenance (offlineViewing: is what we are about to paint authoritative)
//   5. title
//   6. persistence, GUARDED
//   7. the URL
//   8. the mirror's MRU stamp and the offline-pin toggle, which both follow
//      whatever chat is on screen
//
// A caller can no longer FORGET one of these — it can only call the owner. What
// legitimately differs rides in `opts`: `source` (who is painting, which is
// what decides whether the paint is authoritative), `title` (omit to leave the
// header alone), `stash` (only a hello leaves behind a settled view worth
// keeping).
//
// The fingerprint rule is load-bearing in BOTH directions. Reset it on a real
// change, or a cross-session fingerprint collision could keep the wrong DOM;
// do NOT reset it when the name is unchanged, because a prefetched swipe moves
// identity ahead of the hello ON PURPOSE — that later hello then reads as "same
// session" and its replay lands as a no-op instead of rendering a second time.
//
// One deliberate behaviour change came with the consolidation: the boot paint
// now writes `aish-session` and resets the fingerprint like everyone else. The
// key follows what is actually on screen — which the URL already did through
// deepLinkSession, and connect() prefers the URL anyway — so this closes a
// divergence rather than opening one.
let currentSession = null;

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

function enterSession(name, { source = "hello", title, stash = false } = {}) {
  if (stash) stashCurrentView();
  // A switch must never let the outgoing chat's fingerprint "noop" the incoming
  // replay; an unchanged name must never lose the one the prefetch just painted.
  if (name !== currentSession) { viewFp = ""; viewDirty = true; }
  currentSession = name;
  // Only the mirror paints a truncated copy, and only an unstashable view may
  // come from one — so provenance is a property of the SOURCE, not a flag each
  // path remembers to set (the boot paint never did, so its DOM was stashable
  // and reusable as if it had come off the socket).
  offlineViewing = source === "mirror" || source === "boot";
  if (title !== undefined) setTitle(title);
  try {
    localStorage.setItem("aish-session", name); // where a reconnect returns
  } catch { /* private mode: the chat is entered, just not remembered */ }
  deepLinkSession(name);
  markSeen(name);        // "the view is now X" IS "this device has seen X" ([SEEN])
  offlineTouch(name);    // MRU input to the mirror's eviction order
  refreshOfflinePinUi(); // the toggle belongs to the chat now on screen
  refreshRailCurrent();  // and so does the rail's "you are here" mark
}
// [SESSION-ENTER-END]

async function openCachedSession(name) {
  const cached = await offlineLoad(name);
  if (!cached) {
    showToast("that chat isn't available offline");
    return false;
  }
  enterSession(name, { source: "mirror", title: cached.meta.title || "aish" });
  onReplay({ events: cached.events, truncated: false });
  return true;
}

// Switching chats: the socket when there is one, the mirror when there isn't.
//
// LEAVING IS A LOCAL ACT, and that is the whole rule here. This used to send
// "switch me" and then, on anything but a warm-peek hit, do NOTHING until the
// server answered — no transcript, no title, no URL. The rail would slide away
// over the chat you had just left and leave it sitting there for the whole
// round trip, which on a slow link is indistinguishable from a tap that never
// registered: you tap again, or you start typing into a chat you believe you
// have already left. So every path below leaves immediately and paints the best
// copy this device can reach; the server's replay is a CORRECTION, never the
// first sign that the tap was heard.
function resumeSession(name) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (!send({ type: "resume", path: name })) return;
    // A warm peek paints the landing NOW instead of leaving the chat you are
    // leaving on screen for the whole round trip. Identity moves BEFORE the
    // hello on purpose — [SESSION-ENTER] preserves that — so the hello reads as
    // "same session" and its authoritative replay no-ops on the matching
    // fingerprint; a stale peek just misses and rebuilds.
    const pre = freshPrefetch(name);
    if (pre) {
      enterSession(name, { source: "prefetch", title: knownTitle(name) });
      onReplay({ events: pre.events, truncated: pre.truncated });
      return;
    }
    // Nothing warm in memory — only the two most recent chats are ever pre-warmed,
    // so this is the COMMON case for anything further down the rail. Leave the
    // outgoing chat anyway and let [PENDING-VIEW] find something to paint.
    enterSession(name, { source: "pending", title: knownTitle(name) });
    awaitPaint(name);
    return;
  }
  openCachedSession(name);
}

// [PENDING-VIEW-START]
// The transcript is BETWEEN chats: the tap has been honoured (identity, title
// and URL already name the chat you asked for) and nothing has painted it yet.
//
// Three things end that state, and the order is the point. The offline mirror
// this device already holds is tried FIRST — it holds nearly every chat, and
// until now it was consulted only when the socket was down, so on the one
// connection where it would help most (up, but slow) it sat unused. Then the
// server's authoritative replay, which lands through onReplay like any other
// and corrects whatever the mirror showed. Failing both, a placeholder that
// says what is happening — because the honest answer "still fetching this" is
// worth far more to a reader than the previous conversation left on screen
// pretending to be the new one.
const PENDING_VIEW_SLOW_MS = 6000;
let awaitingPaint = null; // name of the chat whose FIRST paint we are waiting for
let awaitingTimer = null;

function awaitPaint(name) {
  awaitingPaint = name;
  clearTimeout(awaitingTimer);
  renderLoadingTranscript();
  paintFromMirror(name);
  // A socket can report OPEN long after it died; the send then vanishes and no
  // event ever arrives to end the wait. Say so, and offer the one control that
  // fixes it, rather than spinning forever.
  awaitingTimer = setTimeout(() => {
    if (awaitingPaint === name) markPendingViewStalled();
  }, PENDING_VIEW_SLOW_MS);
}

// Any paint of the transcript ends the wait, whoever made it. Called from
// onReplay so no future painter has to remember to.
function paintLanded() {
  awaitingPaint = null;
  clearTimeout(awaitingTimer);
  awaitingTimer = null;
}

function renderLoadingTranscript() {
  // The outgoing chat's DOM is going away, so its live turn goes with it — the
  // same reasoning (and the same owner) as a replay that rebuilds.
  resetLiveTurn("rebuild");
  clearPendingSends();
  stopSpeaking();
  messagesEl.replaceChildren();
  clearQueueChips();
  const row = document.createElement("div");
  row.className = "msg notice loading-view";
  const spin = document.createElement("span");
  spin.className = "spin";
  const text = document.createElement("span");
  text.className = "loading-view-text";
  text.textContent = "Loading this chat…";
  row.append(spin, text);
  messagesEl.appendChild(row);
  scrollToEnd(true);
}

function markPendingViewStalled() {
  const row = messagesEl.querySelector(".loading-view");
  if (!row) return;
  row.querySelector(".spin")?.remove();
  row.querySelector(".loading-view-text").textContent =
    "Still loading — the connection may have stalled.";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "loading-view-retry";
  retry.textContent = "Reconnect";
  retry.onclick = () => { retry.remove(); reconnect(); };
  row.appendChild(retry);
}

// The mirror read is async, so the server can win the race while IndexedDB is
// still reading — and a cached copy must never overpaint an authoritative one.
// `awaitingPaint` IS that guard: onReplay clears it, so a paint that already
// happened silently cancels this one.
async function paintFromMirror(name) {
  const cached = await offlineLoad(name);
  if (!cached || awaitingPaint !== name) return;
  // Re-enter as `mirror` so the paint claims no fingerprint (#202): these events
  // came out of IndexedDB, capped and possibly in an older event shape, so the
  // replay that follows must rebuild over them rather than land "noop".
  enterSession(name, { source: "mirror", title: cached.meta.title || knownTitle(name) });
  onReplay({ events: cached.events, truncated: false });
}

// The chat we were waiting for cannot be painted at all. Leaving the spinner up
// would promise a transcript that is never coming.
function abandonPendingView(reason) {
  paintLanded();
  messagesEl.replaceChildren();
  addMsg("notice", reason);
}
// [PENDING-VIEW-END]

// The best title we already hold for a chat we are about to enter, so a warm
// paint doesn't flash the wrong name in the header. `undefined` leaves the
// header alone, which is what enterSession expects when nothing is known.
function knownTitle(name) {
  const known = recentSessions.find((s) => s.name === name);
  if (known && known.title) return known.title;
  const meta = offlineMeta.get(name);
  return (meta && meta.title) || undefined;
}

// First paint. Runs before connect() so the last chat is on screen while the
// socket is still opening — the same code path that made this cheap offline is
// what makes it fast online. A server hello/replay overwrites it moments later.
async function offlineFirstPaint() {
  try {
    // Nothing to pre-paint when a fresh chat was asked for ([OPEN-NEW]): the
    // last conversation would flash up and be replaced a moment later.
    if (openNewOnLoad) return;
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
    // Short grace so a fast (LAN) replay wins OUTRIGHT: painting the mirror
    // first meant rendering the whole chat twice back to back, with the
    // authoritative paint queued behind the cached one — the app filled
    // SLOWER at home, where the mirror is least needed. On a slow link the
    // replay misses the window and the cached paint proceeds barely delayed,
    // which is the case the mirror exists for.
    await new Promise((resolve) => setTimeout(resolve, FIRST_PAINT_GRACE_MS));
    if (serverPainted) return;
    // The owner anchors the pager, the reconnect and the mirror's bookkeeping
    // to what is actually on screen — this path used to do only half of it.
    enterSession(cached.meta.name, { source: "boot", title: cached.meta.title || "aish" });
    onReplay({ events: cached.events, truncated: false });
    hideBootLoader(); // the mirror painted before the socket — drop the spinner now
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

// The chat menu's Pin row, always from a real read. It shows "…" until the
// store answers, because a toggle that states the wrong value invites a tap
// that does the opposite of what you wanted — which is exactly how pinned chats
// were getting silently unpinned.
//
// It lives in the menu rather than the title bar: pinning is a per-chat setting
// you touch rarely, and the title row is the scarcest space in the app.
async function refreshOfflinePinUi() {
  const state = $("pin-state");
  if (!state) return;
  const name = currentSession;
  state.textContent = "…";
  let pinned;
  try {
    pinned = await offlineIsPinned(name);
  } catch {
    return; // stays "…": we genuinely don't know
  }
  if (name !== currentSession) return; // switched chats mid-read
  state.textContent = pinned ? "On" : "Off";
  $("menu-pin").classList.toggle("on", Boolean(pinned)); // the glyph is the signal
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
  refreshOfflinePinUi(); // reflect the new state on the toggle immediately
  showToast(current.pinned ? "pinned — kept at the top and offline" : "unpinned");
}

// There is deliberately no "clear offline copies" action. The mirror manages
// itself — capped, least-useful-first eviction, deleted chats dropped on the
// next sync — so clearing was a control for a problem that doesn't arise, and
// it also dropped the cached app shell, breaking "the app always opens offline"
// until the next successful load. Disposal and repair are both covered by
// deleting the installed app (or the browser's own clear-website-data).

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

// [RETIRE-START]
// The socket has ONE owner: the `ws` variable. connect() is reached from several
// places (initial load, the onclose backoff timer, reconnect(), a foregrounding
// phone) and each assigns a fresh socket to `ws` — but assigning the variable
// does NOT dispose the socket it replaced. An orphaned socket left OPEN keeps its
// onmessage handler alive and goes on delivering live events into handle(), so
// every user bubble and every timeline step renders once PER surviving socket
// (replay is immune — it replaceChildren()s — which is why only the live
// "working" state doubled). Neutralize the predecessor before it can double-feed:
// null its handlers so no stray onclose reschedules another connect, then close
// it. Idempotent and safe on a CONNECTING/CLOSING/CLOSED socket.
function retireSocket(sock) {
  if (!sock) return;
  sock.onmessage = null;
  sock.onopen = null;
  sock.onclose = null;
  sock.onerror = null;
  try { sock.close(); } catch { /* already closing/closed */ }
}
// [RETIRE-END]

// [SESSION-FIREWALL-START]
// Live deliveries are session-gated (#182). The bridge stamps every event it
// fans out with its session's name; an event naming a session other than the
// one on screen is dropped at the socket, BEFORE the dispatcher. This is the
// firewall the client-side switch window needs: a prefetched swipe (and an
// offline-mirror tap that later reconnects) moves `currentSession` before the
// server has processed the resume, and until the server moves this viewer
// between bridges the OLD session's live tail keeps arriving — its `done`
// used to render a bubble into the NEW chat's view. phase 3's answerAbandoned
// only covers one turn's leftovers; this gates every delivery.
//
// Unstamped events (client-direct messages: session_list, peek,
// errors, console_*) always pass — no `session` field means "not scoped".
// Cross-session BY DESIGN, and exempt:
//   hello           — the switch mechanism itself; it MOVES currentSession
//   session_changed — the roster plane (#204): a row about ANY chat, for every
//                     client whatever it is viewing. That this list kept
//                     growing one carve-out at a time is what said the roster
//                     needed its own channel rather than more exemptions
//   session_deleted — same, and dropping it strands a chat that is gone
//   session_state   — the roster plane's predecessor, kept for an old server
// session_renamed is exempt too: its handler is by-name and idempotent, and
// dropping it in the switch window would just desync a drawer/pager label.
// Replayed events never reach this gate (the replay loop feeds handle()
// directly) and carry no stamp anyway: the bridge stamps only the live
// delivery, keeping the recorded transcript byte-identical to a cold
// reconstruct_events replay.
const SESSION_CROSS_EVENTS = new Set([
  "hello", "session_changed", "session_deleted", "session_state", "session_renamed",
]);

function foreignSessionEvent(event, current) {
  if (!event.session || SESSION_CROSS_EVENTS.has(event.type)) return false;
  return event.session !== current;
}
// [SESSION-FIREWALL-END]

// [CONNECT-WIRE-START]
// The other half of the socket's ownership (see [RETIRE] above): this is the
// ONLY place that assigns `ws`. Fenced whole rather than just the wiring
// statements because a choreography test has to CALL it — the interleavings
// that produced #179 (a second connect over a live socket, a reconnect over a
// zombie, an out-of-order onclose) are only reachable by replacing a socket for
// real, not by re-reading the assignments. tests/js/test_choreo_socket_replace.js
// runs this function against a controllable fake WebSocket; test_ownership.js
// enforces that no third writer of `ws` appears outside this block.
function connect() {
  clearTimeout(reconnectTimer);
  // Exactly one live feed into handle(): drop whatever socket `ws` still points
  // at (a zombie, a slow CONNECTING attempt, a dead one an onclose hasn't
  // reported yet) before we overwrite the variable and lose the reference.
  retireSocket(ws);
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
  // `?new` ([OPEN-NEW]) names no session at all: we are about to ask for a
  // fresh one, and landing on the old chat first only to leave it reads as a
  // glitch.
  const lastSession = openNewOnLoad
    ? null
    : urlSession || localStorage.getItem("aish-session");
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
    if (openNewOnLoad) {
      // Cleared BEFORE the send, not after: this runs again on every
      // reconnect, and a flag still set there would open a chat each time the
      // phone woke up ([OPEN-NEW]).
      openNewOnLoad = false;
      act({ type: "new" }, { label: "the new chat" });
    }
  };
  ws.onmessage = (raw) => {
    const event = JSON.parse(raw.data);
    // Session firewall (#182, see [SESSION-FIREWALL]): a delivery for a chat
    // that is not on screen never reaches the dispatcher. Checked before the
    // dirty mark — a foreign event touches nothing in THIS view, and the
    // foreign session's own next replay accounts for it by fingerprint.
    if (foreignSessionEvent(event, currentSession)) return;
    // A live transcript-affecting event invalidates the rendered-view stash
    // (see [VIEWCACHE]). Marked HERE, not in handle(): the replay loop also
    // funnels through handle(), and replayed events are exactly what the
    // fingerprint already accounts for.
    if (!VIEW_SAFE_EVENTS.has(event.type)) viewDirty = true;
    handle(event);
  };
  ws.onclose = (event) => {
    // The socket that carried them is gone, so nothing will ever come back to
    // say whether they arrived. [PENDING-SEND] hands the text back to the
    // composer rather than leave a bubble claiming to be "Sending…" over a dead
    // connection. (A deliberate replacement nulls this handler in retireSocket,
    // so a reconnect we CHOSE never triggers it.)
    clearPendingSends();
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
      hideBootLoader(); // reveal the token form instead of a spinner over it
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
// [CONNECT-WIRE-END]

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
  // connect() now retires the previous socket itself (see retireSocket), so the
  // old hand-rolled teardown here is redundant — but keep the deliberate reset
  // of backoff/dot above; the socket disposal belongs to connect().
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
    ws.addEventListener("open", () => act({ type: "new" }, { label: "the new chat" }), { once: true });
    return;
  }
  act({ type: "new" }, { label: "the new chat" });
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

// [ACK-LEDGER-START]
// HANDING A REQUEST TO THE SOCKET IS NOT THE SAME AS IT HAPPENING, and in a
// PWA that is not pedantry: the socket can report OPEN long after it died,
// accepting everything and answering nothing, which is what a phone has after
// a sleep. `send()` returning true means "the browser took the bytes" and
// nothing more.
//
// The client used to treat it as "done" everywhere, and the UI said so — the
// approval card greyed out, the queue chip vanished, the header said
// "Stopping…", the title changed. Every one of those was a claim about the
// SERVER made on the strength of a local function call, and when the socket
// was a zombie every one of them was false: the gate stayed parked holding a
// command, the message you cancelled ran anyway, the task went on. A delete
// was how it surfaced (#210) — the log file was still there hours later.
//
// So: an ACT is a request that changes something over there, and it is
// outstanding until the server RECEIPTS it. The receipt is generic — the
// server stamps one for any message carrying a `rid`, at the dispatch point
// rather than in each handler, so no request type can be forgotten and a new
// one is covered before anyone thinks about it. It says "handled", never
// "succeeded"; what happened is carried by the events each feature already
// emits, and those still drive the UI.
//
// What an act owes the user when it goes unreceipted is a `lost` callback:
// UNDO WHATEVER THE UI CLAIMED. A claim that cannot be undone must not be
// made in the first place — that is the whole discipline, and it is why the
// call sites below hand over their own repair rather than this block guessing.
//
// Deliberately NOT here: retrying. The request may well have arrived and had
// its receipt die on the way back, so "unreceipted" means UNCONFIRMED, not
// "did not happen" — the wording says so, and every act is one the user can
// simply do again. Automatically re-issuing an approval, a stop or a delete
// on a guess is a worse failure than the one being fixed.
//
// Reads (`sessions`, `files`, `models`, `jobs`, `peek`) are deliberately not
// acts: a lost query paints nothing and asking again is free, so receipting
// them would buy a toast nobody needs.
const ACK_MS = 8000;         // how long a receipt may take before we speak up
const ACK_SWEEP_MS = 1000;   // granularity of the check — one timer, not N
let ackSeq = 0;
const outstanding = new Map(); // rid -> { label, due, lost }
let ackTimer = null;

// Send a state-changing request and hold it open until the server receipts it.
// `label` names the ACTION in the user's words ("the approval", "cancelling
// that message") — it is what they read if it goes missing. `lost` undoes
// whatever the UI claimed; omit it only when the UI claimed nothing.
function act(message, { label, lost } = {}) {
  const rid = `r${++ackSeq}`;
  if (!send({ ...message, rid })) {
    // Never even handed over. Say which action failed, not just that the
    // socket is down — the user asked for a THING, and `send`'s own notice
    // names the connection instead.
    if (label) showToast(`${label} didn't happen — ${offlineMode ? "you are offline" : "not connected"}`);
    if (lost) lost();
    return false;
  }
  outstanding.set(rid, { label, due: Date.now() + ACK_MS, lost });
  armAckSweep();
  return true;
}

function onAck(rid) {
  outstanding.delete(rid);
  if (!outstanding.size) { clearTimeout(ackTimer); ackTimer = null; }
}

function armAckSweep() {
  if (ackTimer) return;
  ackTimer = setTimeout(sweepAcks, ACK_SWEEP_MS);
}

// One sweep for all of them: a dead socket strands everything at once, and N
// toasts stomping each other in a single toast slot would tell the user less
// than one honest sentence.
function sweepAcks() {
  ackTimer = null;
  const now = Date.now();
  const lost = [];
  for (const [rid, item] of outstanding) {
    if (item.due > now) continue;
    outstanding.delete(rid);
    lost.push(item);
  }
  if (lost.length) {
    for (const item of lost) { if (item.lost) item.lost(); }
    const labels = lost.map((i) => i.label).filter(Boolean);
    const what = labels.length === 1 ? labels[0]
      : labels.length ? `${labels.length} actions` : "that";
    // "may not have" is the truth: the request could have arrived and had its
    // receipt die on the way back. Overstating it would teach the user to
    // distrust a message that is usually right.
    showToast(`${what} may not have reached aish — reconnecting`);
    reconnect(); // whatever it was, this socket is not carrying traffic
  }
  if (outstanding.size) armAckSweep();
}
// [ACK-LEDGER-END]

// [WAKE-START]
// Waking the app is the single most defect-dense moment in the client: the
// phone has been asleep, so deferred work never ran, and the socket may be dead
// without ever having said so. Two subsystems are reconciled here — the offline
// mirror and the connection — which is why this is a NAMED function registered
// rather than an anonymous listener: tests/js/test_choreo_wake.js drives it
// directly, and a listener you cannot call is a listener you cannot test.
// (The two other visibilitychange listeners in this file are unrelated — the
// viewport snap and the read-aloud wake lock — and are deliberately left alone;
// this is fencing, not refactoring for its own sake.)
function onPageWake() {
  if (document.hidden) {
    // Putting the app away is the moment the mirror matters: the sync loop
    // skips the on-screen chat while visible (it re-syncs every turn and the
    // user is looking at the live copy), so catch it up now — locking the
    // phone right after an answer still leaves that answer readable offline.
    // Called directly, not via offlineSyncSoon: a hidden page's timers may
    // never fire.
    offlineSyncOnce();
    return;
  }
  // Phone unlock: reconnect immediately instead of waiting out the backoff.
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
}
document.addEventListener("visibilitychange", onPageWake);
// [WAKE-END]

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
// This turn's answer was thrown away mid-stream by a replay (see resetLiveTurn):
// stale LIVE tokens for it are dropped, replayed ones re-open it. Rides with
// sawAnswer's turn boundaries — set only by the owner below, cleared by
// authoritative replayed content, by `done`, and by a LIVE `user` event (a
// replayed one is the rebuild of the abandoned turn itself, not a new one).
let answerAbandoned = false;
const cards = new Map(); // approval id -> card element

// [TURN-RESET-START]
// The live turn's render state — the bubble tokens stream into, its stable-prefix
// bookkeeping, the pending approval cards, and the open activity trace — is ONE
// cluster, so it gets ONE reset. It used to be zeroed by hand inside onReplay,
// which is reachable MID-STREAM from three paths that never asked whether a turn
// was running (the socket's own `replay`, an offline mirror tap, and the prefetch
// paint of a committed swipe), and that hand-rolled subset left two things out:
// the open trace was never closed, so a live one survived into the NEXT chat and
// collected its steps, and the half-streamed bubble was dropped while its tail
// kept arriving.
//
// `landing` is replayLanding's verdict, and the first thing this owns is that a
// "noop" landing resets NOTHING. "noop" means the DOM already IS this transcript
// — the reconnect re-replay after every phone unlock lands there, while a turn
// may genuinely still be running, and tearing that turn down would be the very
// bug this function exists to fix. Only a landing that REPLACES the transcript
// (reuse/rebuild) ends the live turn along with it.
function resetLiveTurn(landing) {
  if (landing === "noop") return;
  // A turn was streaming when this replay landed. The bubble goes with the DOM
  // that is about to be replaced, and the tail of the stream would paint a
  // TRUNCATED answer into a fresh one — and, by setting sawAnswer, suppress the
  // `done` render that carries the whole text. So abandon the answer: onToken
  // drops the stale LIVE tail, while REPLAYED tokens (this same replay usually
  // carries the partial answer — the server's transcript records them) are
  // authoritative and re-open the turn, rebuilding the bubble the live tail then
  // continues into. Worst case — a replay with no token for this turn — the
  // failure mode is "no live streaming until `done`", never a duplicated or
  // half-written answer.
  if (answerEl) answerAbandoned = true;
  answerEl = null;
  answerText = "";
  answerStableLen = 0;
  answerStableNodes = 0;
  sawAnswer = false;
  cards.clear();
  pendingCards = 0;
  renderedAnswers = 0; // fork ordinals restart with the rebuilt transcript
  finishTrace(); // the open trace belongs to the turn whose DOM just went away
  // The clock goes with the turn. The replay about to run re-derives it from the
  // transcript it paints, so leaving the old value would let one chat's origin
  // date another chat's card — the case where the replayed transcript is trimmed
  // past its own opening user event and sets nothing at all.
  turnStart = 0;
}
// [TURN-RESET-END]

function handle(event) {
  switch (event.type) {
    case "hello": onHello(event); break;
    case "replay": serverPainted = true; onReplay(event); break;
    case "user":
      closeAnswer();
      finishTrace(); // close any trace from a prior turn before the new one
      removeQueueChip(event.text); // a queued message that just started running
      // The server's own version of this turn supersedes the bubble we drew on
      // send ([PENDING-SEND]) — it carries the stamp, the turn id and any
      // attachment notes, so it replaces rather than merely confirms.
      if (!replaying) resolvePendingSend(event.text);
      retireQuickReplies();
      // The rerun button belongs to the last ANSWER: attachAnswerTools retires
      // it when the next answer lands. Don't retire it here on the user turn —
      // user-only turns (/cd, a bare !command) produce no answer and would
      // otherwise strip the last answer's rerun for good (survives replay too).

      sawAnswer = false;
      // A LIVE user turn always streams afresh. A REPLAYED one is a
      // reconstruction, not a new turn — and it is exactly what the replay that
      // just abandoned a half-streamed answer rebuilds, so clearing the flag
      // here would hand the still-arriving tail a bubble of its own (#181).
      if (!replaying) answerAbandoned = false;
      userCmdBlock = null; // a new turn supersedes any dangling ! command block
      taskErrored = false; // a new turn clears the prior error's red dot
      // The turn's origin, for the answer's timing readout and the live trace
      // card's clock. A live turn starts now; a replayed one carries its real
      // start from the server (replayedTurnStart).
      turnStart = replaying ? replayedTurnStart(event) : Date.now();
      setBusy(true);
      // A synthetic turn is aish's own text (a resume note, an automation's
      // trigger prompt), so it must not seed the chat title or the composer's
      // prompt history — both are records of what YOU asked (#171).
      // Stripped first: a photo sent with no words would otherwise title the
      // chat with the note aish wrote to itself, path and all.
      if (!sessionTitled && event.synthetic !== "resume") {
        setTitle((stripAttachmentNotes(event.text) || event.text).split("\n")[0]);
      }
      if (!event.synthetic) rememberPrompt(stripAttachmentNotes(event.text));
      lastUserPrompt = stripAttachmentNotes(event.text); // for error Retry
      // A user-direct `!` command is already fully shown by the terminal block
      // that follows — its prompt line carries the command. The extra blue
      // user-input bubble duplicated that and, on a restored session, rendered
      // the raw (multi-line) command as if typed into chat (#154). So skip the
      // bubble for `!` commands; the turn-boundary handling above still runs.
      // A synthetic turn takes the system-note row for the same reason: only
      // the rendering differs, every turn side effect above still fires.
      turnAnchorEl = event.text.startsWith("!")
        ? null
        : event.synthetic
          ? addSystemMsg(event.synthetic, event.text)
          : addUserMsg(event.text, event.at, event.turn);
      // Your own message always comes into view, even if you were scrolled up.
      if (!replaying) scrollToEnd(true);
      break;
    case "queued":
      // The agent was busy, so this message waits its turn as a chip instead of
      // a transcript bubble — the other way a send is accounted for.
      resolvePendingSend(event.text);
      addQueueChip(event.text);
      break;
    case "dequeued": removeQueueChip(event.text); break;
    // The share inbox is server-owned and not per-chat: every repaint is the
    // full list, so a claim on the phone clears the chip on the laptop too.
    case "shared": renderShares(event.items || []); break;
    case "cwd_queued": addCwdChip(event.path); break;
    case "cwd_dequeued": removeCwdChip(); break;
    case "token": onToken(event.text); break;
    case "delivery": onDelivery(event); break;
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
    case "redacted": addRedactedMsg(); break;
    case "rating": markRating(event.turn, event.rating, event.comment); break;
    case "ack": onAck(event.rid); break;
    case "error":
      // [ERROR-KIND-START]
      // An `error` says one of two unrelated things, and everything below the
      // guards is written for exactly one of them: YOUR TURN FAILED. That ends
      // the turn — close the answer, close the live trace, clear busy, offer
      // Retry, push a notification.
      //
      // The other kind is a REFUSAL of the request you just made, and it says
      // nothing whatsoever about the turn. Running them through the same path
      // is what made refusing to delete a message while the chat was working
      // tear down the RUNNING turn's card — taking Stop and Retry with it, and
      // stranding a task with no way to reach it. The refusal was right; it
      // just also destroyed the thing it was protecting.
      //
      // Told apart by the machine-readable code, never the prose: the server
      // stamps `refused` at the site that knows (WebServer._refuse), because a
      // turn failure goes through the bridge and a refusal goes down one
      // socket — a distinction the far end cannot re-derive. A refusal is a
      // toast: transient feedback about an action, like every other refused
      // action in this app.
      if (event.code === "no_such_session") { onSessionGone(event.name || ""); break; }
      if (event.code) { showToast(event.text); break; }
      // [ERROR-KIND-END]
      closeAnswer();
      finishTrace(true); // #48: a mid-turn error must close the live trace, not leave it stuck "Working…"
      turnStart = 0;
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
      // A turn that finished while you were WATCHING it is read. Entering a
      // chat stamps the seen map ([SEEN]), but the turn keeps writing to the
      // log after that, so its last-activity ends up newer than the stamp and
      // every chat you had just used came back marked unread the moment you
      // left it — which would have made the attention band the whole list.
      if (!replaying) markSeen(currentSession);
      // A finished turn is what the mirror is missing — but syncing right
      // away competed with the user's next action for the main thread and the
      // server (the chat on screen re-parses on every pass, its mtime always
      // moves). A calm debounce covers the multi-turn case; putting the app
      // away is covered separately by the sync-on-hidden in visibilitychange.
      if (!replaying) offlineSyncSoon(OFFLINE_SYNC_AFTER_DONE_MS);
      break;
    case "history": onHistory(event.messages); break;
    case "session_list": renderSessions(event); break;
    case "model_list": renderModels(event); break;
    case "model_changed": onModelChanged(event); break;
    case "cwd_changed": renderWorkspace(event); break;
    case "job_list": $("ws-jobs").textContent = event.text || "—"; break;
    case "file_list": onFileList(event); break;
    case "session_state": onSessionState(event); break;
    case "session_deleted": onSessionDeleted(event); break;
    case "session_changed": onSessionChanged(event); break;
    case "peek": onPeek(event); break;
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
  const known = recentSessions.find((s) => s.name === event.name);
  if (known) known.title = event.title;
}

// A chat you are NOT viewing changed state. Two kinds arrive here, and they are
// not the same news: `waiting` means it has stopped and cannot go on without
// you, `idle` that it finished on its own. Both belong in the count; only the
// second is worth a system notification, because an unattended hold already has
// its own push (`notify_hold`, server-side) and a hold you are around for is
// what the count and the toast are for.
// [ROSTER-START]
// The stream of "chat X is now …", for every chat, whatever this client is
// looking at (#204).
//
// It replaces a pull. The list of chats and what they were doing used to be
// refreshed only when this client ASKED — which it did on opening the rail —
// so between opens the roster decayed, and the four repairs before this one
// each widened when it re-asks rather than making the server tell it. What was
// never announced at all: a chat STARTING, an approval ANSWERED somewhere else
// (so a phone went on showing "Needs approval" for a card cleared on the
// laptop), and a chat renamed or deleted elsewhere.
//
// A row is applied whole and is idempotent, so a duplicate costs nothing and a
// missed one is repaired by the next row for that chat. `seq` is what turns
// "nothing changed" and "I missed something" into different observations: a
// gap asks for a snapshot rather than leaving a stale row on screen forever.
//
// THE TIMESTAMPS ARE STAMPED HERE, not carried. The server would have to parse
// a session log to know them, and a chat that just did something has just
// changed its file — so the parse cache is guaranteed to miss at exactly the
// moment a transition fires. Stamping on arrival is also the more honest
// clock: unread compares against THIS device's "I looked at it" map.
let rosterSeq = 0;
// Does this client hold every chat there is, or only the ones it has been told
// about? The pager rows a hello carries are the recency HEAD, not the archive,
// and deltas only describe chats that changed — so until a full list lands,
// the roster is a partial view and the rail has to ask for the rest.
let rosterComplete = false;

function rosterBaseline(seq) {
  // A hello. If the server's sequence is not the one we were following, deltas
  // happened while we were not listening — a reconnect, a server restart — and
  // they are simply GONE. Re-baselining without noticing is what would make
  // that invisible: the client would carry on believing rows it has no reason
  // to believe. So the mismatch marks the roster stale, and the next list
  // request repairs it.
  if (typeof seq !== "number") return;
  if (seq !== rosterSeq) rosterComplete = false;
  rosterSeq = seq;
}

function rosterSnapshotLanded(seq) {
  // A full, unfiltered list: everything there is, as of `seq`. Deltas already
  // folded into it must not read as a gap, and from here the stream alone is
  // enough — which is what lets opening the rail stop being a round trip.
  if (typeof seq === "number") rosterSeq = seq;
  rosterComplete = true;
}

function onSessionChanged(event) {
  const row = event.row || {};
  if (!row.name) return;
  if (typeof event.seq === "number") {
    // A gap means a delta was lost — a socket replaced mid-flight, an outbox
    // dropped on a session switch. Ask for the truth instead of applying this
    // row over rows that may already be wrong.
    const missed = event.seq > rosterSeq + 1;
    rosterSeq = event.seq;
    if (missed) {
      rosterComplete = false;
      requestSessions($("sessions-search").value || "");
    }
  }
  noteAttention(row);
  if (event.notice) rosterNotice(event.notice, row);
  if (railIsOpen()) renderSessionsFromCache();
}

// The heads-up half, kept apart from the data half on purpose: every client
// gets every row, but only a client that is NOT looking at the chat has any
// reason to be interrupted about it.
function rosterNotice(notice, row) {
  if (row.name === currentSession) return;
  const label = row.title
    ? `“${row.title.slice(0, 40)}”`
    : row.name.replace(/^session-|\.jsonl$/g, "").replace(/-\d{6}$/, "");
  if (notice === "held") {
    showToast(`${label}: waiting for your approval — swipe from the left edge to switch`);
    return;
  }
  showToast(`${label}: task finished — swipe from the left edge to switch back`);
  notify("aish — background task finished", row.title || row.name);
}

// The rail, repainted from what this client already knows — NO ROUND TRIP.
// The roster is the authoritative half of that paint ([ATTENTION]) and it has
// just been updated, so the answer is already here. Deliberately not
// `requestSessions`, which also ASKS the server: with the rail docked open
// that turned every delta into a snapshot request, which is the poll this
// plane exists to remove, only now triggered by its own events.
function renderSessionsFromCache() {
  renderOfflineSessions($("sessions-search").value || "");
}

// A server that predates the roster plane still sends the two old pushes.
// Kept so a new client against an old server degrades to the old behaviour
// rather than going silent.
function onSessionState(event) {
  noteAttention({ name: event.session, state: event.state, title: event.title });
  rosterNotice(event.state === "waiting" ? "held" : "finished", {
    name: event.session, title: event.title,
  });
  if (railIsOpen()) renderSessionsFromCache();
}
// [ROSTER-END]

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

// [VIEWCACHE-START]
// Rendered-transcript reuse: a session switch replays the full transcript and
// rebuilds the whole DOM (markdown + highlighting for every answer), which is
// what made swiping back and forth feel slow on long chats. When we LEAVE an
// idle view we stash its rendered nodes, and when a replay for that session
// arrives whose transcript is IDENTICAL to what those nodes were built from,
// we swap them back in instead of rebuilding. Identity is a fingerprint of
// the replay (event count + truncation + the last event): the transcript is
// append-only while a session stays open, so any change moves the count or
// the last event. Every mismatch falls back to a full rebuild — the cache can
// make a switch faster, never wronger.
const VIEW_CACHE_MAX = 4;
const viewCache = new Map(); // session name → {nodes, fp, renderedAnswers}
let viewFp = "";      // fingerprint of the replay the current view was built from
let viewDirty = true; // events arrived since that replay → a stash would be stale

// Event types that never touch the transcript DOM. Anything NOT listed marks
// the view dirty — over-dirtying only costs a rebuild, never a stale screen.
const VIEW_SAFE_EVENTS = new Set([
  "hello", "replay", "session_list", "model_list", "role", "ack",
  "session_renamed", "session_deleted", "cmd_history", "jobs", "files", "dirs",
  "console_started", "console_out", "console_exit", "console_error",
  "console_shared", "peek",
]);

function replayFp(event) {
  const events = event.events || [];
  const last = events[events.length - 1];
  // The last event is capped so a legacy one-blob `history` replay doesn't
  // put megabytes into the key; length + head disambiguates just as well.
  const tail = last ? JSON.stringify(last) : "";
  return `${event.truncated ? "t" : ""}${events.length}:${tail.length}:${tail.slice(0, 2000)}`;
}

// A view is only worth stashing when its DOM is a pure function of the replay
// it was built from: nothing arrived since (dirty), nothing is mid-flight
// (busy / pending approval cards / an un-acknowledged send bubble the server
// has never confirmed), and it wasn't painted from the mirror's truncated copy
// (offlineViewing).
function viewStashable(state) {
  return Boolean(
    state.name && state.fp && !state.dirty && !state.pendingCards &&
    !state.busy && !state.offlineViewing && !state.pendingSends
  );
}

// What to do when a replay lands, cheapest first:
//   "noop"    — the view ALREADY shows this exact transcript (same fingerprint,
//               nothing arrived since it was painted). The reconnect re-replay
//               — every phone unlock — and the second half of a prefetched
//               swipe land here: keep the DOM and the scroll position.
//   "reuse"   — a stash of this transcript exists: swap the rendered nodes in.
//   "rebuild" — anything else: render from the events. Always correct.
// The dirty gate protects every edge at once: any live event since the last
// paint (a card, a stream line, an injected turn) forces the rebuild path.
// An un-acknowledged send bubble ([PENDING-SEND]) fails "noop" for the same
// reason from the other side: the DOM holds a message the server has never
// confirmed, so it is NOT this replay's transcript however well the fingerprint
// matches — and a fingerprint cannot see it, because nothing about it came off
// the socket.
function replayLanding(state) {
  if (state.fp && state.fp === state.viewFp && !state.viewDirty && state.hasDom &&
      !state.pendingSends) {
    return "noop";
  }
  if (state.cachedFp && state.cachedFp === state.fp) return "reuse";
  return "rebuild";
}

// [SCROLLPOS-START]
// Where you were reading survives a rebuild: a reload after the phone put the
// app to sleep, the app-revision reload, a session switch and back. The "noop"
// landing already keeps the scroll by keeping the DOM; this is the other two
// landings, which throw the DOM away and used to always come back at the tail.
//
// A raw scrollTop would be a lie the moment any height differs, so what's stored
// is the SAME anchor the wrap toggle reflows around (`topVisibleAnchor`): the
// index of the child at the top of the viewport plus its on-screen offset. That
// index only means something if the rebuilt DOM is the one it was measured
// against, so it is stored with `transcriptFp()` — a fingerprint of the
// TRANSCRIPT ON SCREEN, deliberately not `viewFp`. viewFp is stamped by the last
// replay, and the flow this whole thing exists for (ask something, scroll up to
// read the answer, come back later) leaves it stale by definition — a live turn
// appends to the DOM without ever replaying, so keying on it would reject the
// restore in exactly the case that matters. Comparing rendered DOM to rendered
// DOM asks the right question: "is this the screen I left?"
//
// A mismatch deliberately falls through to the tail: something arrived while you
// were away, and the newest of it is what you came back for.
const SCROLL_KEY = "aish-scroll";
const SCROLL_MAX_KEPT = 24; // a handful of chats deep; the rest fall off oldest-first

// Cheap and stable across live-vs-replay rendering, which is the same hot/cold
// parity `reconstruct_events` already guarantees: same events → same children.
function transcriptFp() {
  const last = messagesEl.lastElementChild;
  return [
    messagesEl.childElementCount,
    last ? last.className : "",
    last ? last.textContent.length : 0,
  ].join(":");
}

function readScrollMemory() {
  try {
    return JSON.parse(localStorage.getItem(SCROLL_KEY) || "{}") || {};
  } catch {
    return {}; // unparseable or storage-denied (private mode) — just forget
  }
}

function rememberScrollPos() {
  if (!currentSession) return;
  const anchor = topVisibleAnchor();
  if (!anchor) return;
  const index = [...messagesEl.children].indexOf(anchor.el);
  if (index < 0) return;
  const mem = readScrollMemory();
  mem[currentSession] = {
    fp: transcriptFp(), index, offset: Math.round(anchor.offset), at: Date.now(),
  };
  const names = Object.keys(mem);
  if (names.length > SCROLL_MAX_KEPT) {
    names
      .sort((a, b) => (mem[a].at || 0) - (mem[b].at || 0))
      .slice(0, names.length - SCROLL_MAX_KEPT)
      .forEach((name) => delete mem[name]);
  }
  try {
    localStorage.setItem(SCROLL_KEY, JSON.stringify(mem));
  } catch {
    // storage full or denied: the position is a nicety, never a failure
  }
}

// True when the remembered position was applied — the caller falls back to the
// tail otherwise. Call this with the transcript fully built.
function restoreScrollPos() {
  const saved = readScrollMemory()[currentSession];
  if (!saved || saved.fp !== transcriptFp()) return false;
  const el = messagesEl.children[saved.index];
  if (!el) return false;
  restoreAnchor({ el, offset: saved.offset });
  return true;
}
// [SCROLLPOS-END]

function stashCurrentView() {
  const stashable = viewStashable({
    name: currentSession,
    fp: viewFp,
    dirty: viewDirty,
    pendingCards,
    busy: clientBusy,
    offlineViewing,
    pendingSends: pendingSends.length,
  });
  if (!stashable) return;
  viewCache.delete(currentSession); // re-insert = move to MRU end
  viewCache.set(currentSession, {
    nodes: [...messagesEl.children],
    fp: viewFp,
    renderedAnswers,
  });
  while (viewCache.size > VIEW_CACHE_MAX) {
    viewCache.delete(viewCache.keys().next().value);
  }
}
// [VIEWCACHE-END]

// [PREFETCH-START]
// Swipe-neighbor prefetch: after a view settles, quietly ask the server for
// the transcripts one page left and right (`peek` — a VIEW message: no claim,
// no hello, nothing recorded). A committed swipe then paints the prefetched
// events immediately instead of showing a blank parked page for a full round
// trip; the authoritative replay that follows lands on the SAME fingerprint
// and no-ops (see replayLanding), so nothing renders twice. A stale peek (the
// neighbor moved since) just fingerprint-misses into the normal rebuild.
const PREFETCH_MAX_AGE_MS = 90 * 1000;
const PREFETCH_KEEP = 4;
const prefetched = new Map(); // name → {events, truncated, ts}
let peekTimer = null;

function schedulePeeks() {
  clearTimeout(peekTimer);
  peekTimer = setTimeout(requestWarmPeeks, 900);
}

// WHICH chats are worth warming. The pager used to answer this with "the two
// pages either side of this one"; with the rail, the answer is simply the most
// recently used chats that aren't this one — which is exactly where a tap is
// most likely to land. Under recency ordering the chat you were just in sits at
// position one permanently, so bouncing between two chats stays instant, which
// is the case the old carousel was genuinely good at.
const PREFETCH_TARGETS = 2;

function prefetchTargets() {
  return recentSessions
    .filter((s) => s && s.name && s.name !== currentSession)
    .slice(-PREFETCH_TARGETS)          // hello.pager is oldest→newest
    .map((s) => s.name);
}

function requestWarmPeeks() {
  if (offlineMode || !ws || ws.readyState !== WebSocket.OPEN) return;
  for (const name of prefetchTargets()) {
    if (!prefetched.has(name)) send({ type: "peek", path: name });
  }
}

function onPeek(event) {
  if (!event.name) return;
  if (event.gone) {
    // Warming a chat the server no longer has: drop what we cached for it and
    // stay silent — no toast for a request the user never made.
    forgetSession(event.name);
    return;
  }
  prefetched.set(event.name, {
    events: event.events || [],
    truncated: !!event.truncated,
    ts: Date.now(),
  });
  while (prefetched.size > PREFETCH_KEEP) {
    prefetched.delete(prefetched.keys().next().value);
  }
}

// Single-use: consumed on the swipe that lands on it; the next hello re-peeks.
function freshPrefetch(name) {
  const pre = prefetched.get(name);
  if (!pre) return null;
  prefetched.delete(name);
  return Date.now() - pre.ts <= PREFETCH_MAX_AGE_MS ? pre : null;
}
// [PREFETCH-END]

function onHello(event) {
  // Server code changed since this page was built (or the page predates rev
  // stamping entirely) — reload; the replay mechanism restores the view.
  // Abandon this hello ONLY when a reload is actually coming: when the
  // throttle refuses (update loop guard), "stay put" must mean stay USABLE —
  // returning here anyway left the app connected but blank, with no replay
  // ever processed, until the next reconnect repeated the same abort.
  if (event.rev && event.rev !== PAGE_REV && reloadThrottled("rev")) return;
  // The interactive console is GLOBAL (#148 follow-up): it floats above whatever
  // chat is shown and is untouched by a session switch. A hello also means a
  // (re)connect. `#console` is a deep-link that survives a reload / server
  // restart (incl. the rev-reload above), so restore the overlay from it; on a
  // plain reconnect it is already open and we just re-attach (tmux redraws).
  if (consoleOpen) send({ type: "console_open" });
  else if (location.hash === "#console") openConsole();
  $("model-name").textContent = event.model;
  recentSessions = event.pager || [];
  cmdHistory = event.cmd_history || []; // personal command palette (#104)
  // Identity and everything coupled to it — the stash of the view we are
  // leaving, the fingerprint, the title, the remembered session, the URL, the
  // mirror's bookkeeping — belong to [SESSION-ENTER]. Below this line are the
  // things only a HELLO knows: the workspace, the busy/role state.
  enterSession(event.session, {
    source: "hello",
    title: event.title || "",
    stash: true, // the DOM on screen still belongs to the chat being left
  });
  // Every hello already carries the recency rows (they warm the swipe peeks),
  // and they now carry each chat's liveness too — so a boot, a reconnect and
  // every switch re-derive the attention count for free. Without this the badge
  // was whatever the last rail open computed, and after a reload it was EMPTY
  // until you opened the rail ([ATTENTION]).
  setAttentionRows(event.pager || []);
  rosterBaseline(event.roster_seq); // and whether we missed anything while away
  if (railIsOpen()) requestSessions($("sessions-search").value || ""); // docked: stay current
  currentLogPath = event.log_path || ""; // /session + "Copy log path" (#146)
  renderWorkspace(event);
  taskErrored = false; // fresh connected view — clear any stale red
  setBusy(event.busy);
  if (!event.busy) setStatus(null);
  // A fresh view starts with no known role: the server sends a `role` event
  // only when ANOTHER tab is already driving this session (#102). Until then,
  // hide the indicator — this tab is the presumed driver.
  setRolePill(false);
  updateEmptyHint();
  // A share almost always arrives with nothing connected, so hello — not the
  // broadcast — is how it is normally first seen (#213).
  renderShares(event.shares || []);
  hideBootLoader(); // connected and about to replay — drop the first-paint spinner
  schedulePeeks(); // warm the swipe neighbors once this view settles
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

// [REPLAY-LANDING-START]
// A replay is the ONE authoritative repaint: rendering is a pure function of the
// event array, so this is also where the view's fingerprint is (re)stamped. It
// owns viewFp/viewDirty together with enterSession — nothing else may claim "the
// screen now shows this transcript".
function onReplay(event) {
  // Whatever this replay decides below, the transcript is no longer waiting to
  // be painted for the first time ([PENDING-VIEW]).
  paintLanded();
  // The server's own account of this chat settles anything held for it
  // ([PENDING-SEND]) — BEFORE the landing is decided, because a `noop` landing
  // returns early and is just as authoritative about what arrived.
  adjudicateHeldSends(currentSession, event.events);
  // A repaint the server flags `seen` follows an edit made from a viewer's own
  // hands (removing an exchange, #202) and reaches only clients VIEWING this
  // chat, so what they are being handed is its current state. Without it the
  // removal — which counts as activity, so that every device's offline mirror
  // refetches the corrected transcript — would come back as an unread dot for
  // the person who just made it.
  if (event.seen) markSeen(currentSession);
  // [VIEWCACHE] Decide the landing BEFORE touching any transform (see
  // replayLanding for the three outcomes and why each is safe).
  const fp = replayFp(event);
  const landing = replayLanding({
    fp,
    viewFp,
    viewDirty,
    hasDom: messagesEl.childElementCount > 0,
    cachedFp: viewCache.get(currentSession)?.fp,
    pendingSends: pendingSends.length,
  });
  // [TURN-RESET] A replay is reachable mid-stream; closing the live turn is not
  // this function's to hand-roll, INCLUDING the case where the answer is that a
  // "noop" landing closes nothing.
  resetLiveTurn(landing);
  if (landing === "noop") {
    // The exact transcript on screen — the reconnect re-replay (every phone
    // unlock) and the warm paint a rail tap already made. Keep the DOM, the
    // reading position, and even a read-aloud in progress: rebuilding here is
    // what made every app-open jump to the bottom and re-render the world.
    viewFp = fp;
    return;
  }
  stopSpeaking(); // the active button is about to be detached with the DOM
  // Any un-acknowledged message bubble goes with the DOM it lives in — and its
  // text comes back to the composer rather than vanishing with it.
  clearPendingSends();
  messagesEl.replaceChildren();
  clearQueueChips(); // the whole queue area belongs to the chat being replaced; _show re-sends the new one's
  const cached = viewCache.get(currentSession);
  viewCache.delete(currentSession); // detached nodes are single-use either way
  if (landing === "reuse") {
    messagesEl.replaceChildren(...cached.nodes);
    renderedAnswers = cached.renderedAnswers;
  } else {
    if (event.truncated) addMsg("notice", "… earlier events trimmed …");
    replaying = true; // replayed history must not re-fire notifications
    try {
      for (const item of event.events) handle(item);
    } finally {
      replaying = false;
    }
  }
  // A fingerprint is a CLAIM that this DOM equals a server replay — which is
  // what lets a later replay land "noop" and keep it. A mirror paint has no such
  // claim: its events came from IndexedDB, are capped by `offline_events`, and
  // are only re-synced when the chat's activity stamp MOVES, so a chat nobody
  // has touched keeps whatever the mirror stored — from an older app, in an
  // older event SHAPE. That is not hypothetical: the delete chip rides a `turn`
  // field added to `user` events (#202), and `replayFp` (count + last event)
  // could not see the difference — the authoritative replay no-op'd and the
  // chip-less mirror DOM stayed on screen. Claiming nothing here forces the
  // rebuild that puts the server's version up. (`prefetch` is server data and
  // keeps its claim: that no-op is the point of the warm peek.)
  viewFp = offlineViewing ? "" : fp;
  viewDirty = false;
  // The reading position if this is the same transcript you left (a reload, a
  // switch and back); the tail if anything changed while you were away.
  if (!restoreScrollPos()) scrollToEnd(true);
  snapViewportSoon(); // session switches race keyboard dismissal with this rebuild (#8)
  setTimeout(() => reportViewport("after-replay"), 1200);
  // Every replay marks a fresh view (new chat, resume, reconnect) — on
  // desktop, land the cursor in the composer ready to type.
  if (FINE_POINTER && $("backdrop").hidden) input.focus();
}
// [REPLAY-LANDING-END]

// [ANSWERING-COLLAPSE-START]
// Entering the "Answering…" phase — the model stopped calling tools and is
// streaming its reply — collapses the still-open live timeline to its summary
// so the answer isn't buried under a tall list of steps (#168). Fires once, at
// the transition; a manual re-expand afterward is never fought (this doesn't
// run again for the same answer). Mirrors the approval-card collapse idiom
// (#65): only act on a live, open trace — and never on one the reader opened
// themselves, which is now the ONLY way an open live trace comes about.
function collapseTimelineForAnswering(t) {
  if (t && !t.userToggled && t.el.classList.contains("live") && t.el.classList.contains("open")) {
    t.el.classList.remove("open");
    return true;
  }
  return false;
}
// [ANSWERING-COLLAPSE-END]

function onToken(text) {
  // A replay replaced the transcript while this turn was streaming (see
  // resetLiveTurn). The rule: abandon drops the STALE LIVE tail, but REPLAYED
  // content is authoritative and re-opens the turn.
  //
  // The distinction is load-bearing, not a nicety. The server's hot transcript
  // records tokens (merging consecutive ones), so the ordinary same-session
  // reconnect — a phone unlock during a long answer — replays the partial answer
  // as a `token` event, and the loop thread makes that snapshot contiguous with
  // the live tail that follows. Rebuilding from it restores the bubble exactly;
  // dropping it would blank the answer until `done`, which for a long task is
  // minutes of missing content on the most common recovery path there is.
  // A live token is the other case: its bubble is gone, so painting it alone
  // would show a truncated answer AND (via sawAnswer) suppress the `done` render
  // that carries the whole text.
  if (answerAbandoned) {
    if (!replaying) return;
    answerAbandoned = false;
  }
  sawAnswer = true;
  // [ANSWER-OPEN-START]
  if (!answerEl) {
    answerEl = addMsg("answer md", "");
    answerText = "";
    answerStableLen = 0;
    answerStableNodes = 0;
    // The reply itself is the reading anchor: anchorAnswer scrolls only until
    // its top reaches the top of the screen, so the view rises while the answer
    // is still short and then locks with the answer owning the whole viewport.
    turnAnchorEl = answerEl;
    // The live "Thinking…" step is the last row on the timeline when the reply
    // starts streaming; relabel it so it reads as the answer landing, not more
    // thinking, and mark it so finishTrace finalizes it in place ("Answered")
    // instead of dropping the live thinking row.
    if (currentTrace && currentTrace.thinkingRow) {
      currentTrace.thinkingRow.titleEl.textContent = "Answering…";
      currentTrace.thinkingRow.isAnswer = true;
    }
    if (currentTrace) {
      currentTrace.answering = true;
      updateTraceHead(currentTrace);
    }
    // Entering "Answering…" collapses the open timeline to its summary (#168).
    collapseTimelineForAnswering(currentTrace);
    // Bring a LIVE reply on screen — the one you just asked for. A REPLAYED
    // token is a reconstruction of a turn you have already seen, and forcing
    // this would override the resting position onReplay is about to choose
    // (the reading position it restores, or the tail): a reload would land you
    // at the top of the last answer no matter where you had been reading. The
    // flag is read at SCHEDULE time — inside the callback the replay loop has
    // long since reset it.
    if (!replaying) requestAnimationFrame(() => anchorAnswer(true));
  }
  // [ANSWER-OPEN-END]
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
  anchorAnswer(); // rises to the anchor, then self-limits — see anchorAnswer
}

// The element that marks the START of the current turn's response — the answer
// bubble once it exists, else the user's own bubble.
let turnAnchorEl = null;

// Keep that anchor pinned to the TOP of the viewport and let the rest of the
// answer flow in below the fold — so you read from the beginning instead of the
// view jumping to the bottom and making you scroll back up.
//
// This is also the whole of the "scroll until it fills, then stop" rule, and it
// is a consequence of the clamp below rather than a mode: while the turn's
// content is shorter than a screen the target clamps to the very bottom, so the
// view rises as text arrives; the moment the anchor can actually reach the top
// the target stops moving, and since the anchor's own top never moves once
// content is only appended BELOW it, nothing scrolls again for the rest of the
// turn. No follow flag, no "stop chasing" branch.
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
// Up to 3 leading spaces are allowed (CommonMark), which is load-bearing for
// blocks nested in a list (#172): the list parser strips exactly the item's
// content column, so a fence the model indented by 4 under a `2. ` marker
// arrives here still carrying 1 space. Demanding column 0 made it leak as
// literal text. The same 0-3 allowance is applied to the other block markers
// below (heading, quote, table) for the same reason.
const FENCE_RE = /^( {0,3})(`{3,}|~{3,})([\w-]*)\s*$/;
function fenceOpen(line) {
  const m = line.match(FENCE_RE);
  return m ? { ch: m[2][0], len: m[2].length, lang: m[3], indent: m[1].length } : null;
}
function fenceCloses(line, fence) {
  const m = line.match(FENCE_RE);
  return Boolean(m) && m[3] === "" && m[2][0] === fence.ch && m[2].length >= fence.len;
}
// A fenced block's content loses the opening fence's own indentation (up to
// that many spaces) — otherwise a nested fence indents every code line.
function dedent(line, n) {
  let k = 0;
  while (k < n && line[k] === " ") k++;
  return line.slice(k);
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

// [ANSWER-CLOSE-START]
// The orderly end of a bubble: flush, decorate, release. resetLiveTurn is the
// DISORDERLY one (the transcript is being replaced under it) — between them they
// are the only writers of answerEl besides the token path that opens it.
//
// `interim` closes a NARRATION delivery (#212) instead of an answer. The tool
// row is the deliverable's row — copy, export, fork, regenerate, 👍/👎 — and
// every one of those names the turn's ANSWER: the fork ordinal counts final
// answers (`truncate_at_answer` defines them the same way), regenerate re-runs
// the prompt, and a rating must bind to the turn rather than fragment across
// however many times the model spoke on the way there. So an interim bubble
// gets no row at all; it is marked instead, and reads as progress.
function closeAnswer(interim) {
  // A finished answer (streaming ends, or something else interrupts the
  // block) gets its copy/read-aloud row; mid-stream re-renders would clobber it.
  if (answerEl) {
    renderAnswerNow(); // flush any tokens still waiting on the next frame
    highlightFences(answerEl); // the answer is settled now — safe to tokenize fences once
    if (interim) answerEl.classList.add("interim");
    else if (answerText.trim()) attachAnswerTools(answerEl, answerText, lastUserPrompt);
  }
  answerEl = null;
  answerText = "";
}
// [ANSWER-CLOSE-END]

// [DELIVERY-START]
// One thing said on the way to the answer (#212) is over: close its bubble so
// the next thing said gets its own, instead of a whole task arriving as one
// paragraph that grew for four minutes.
//
// The text rides the event for the same reason `done` carries `result`: the
// tokens may never have arrived. A bound turn cannot stream them at all (Verify
// buffers every token, and whether a turn is the ANSWER is only knowable once
// its tool calls arrive), and a mid-stream replay drops the stale live tail —
// in both cases `sawAnswer` is false and this is the only copy there is.
function onDelivery(event) {
  if (!sawAnswer && event.text) {
    // Complete and authoritative, so it paints even over an abandoned turn —
    // unlike a lone live token, which would be a truncated fragment. Clearing
    // the flag first is what lets it through onToken's own drop guard.
    answerAbandoned = false;
    onToken(event.text);
  }
  closeAnswer(true); // flushes whatever is still queued for the next frame
  // The turn goes on: the next delivery, and the answer itself, are fresh
  // bubbles. Both flags describe the bubble just closed, not the turn.
  sawAnswer = false;
  answerAbandoned = false;
}
// [DELIVERY-END]

function onDone(event) {
  // How long THIS answer took, for the readout under it — a wall-clock measure,
  // so it is meaningful only for a turn we watched run. A replayed turn now
  // carries a real origin (it is what the live clock counts from), and the time
  // since then is the age of the transcript, not the length of the turn.
  answerTiming = !replaying && turnStart ? (Date.now() - turnStart) / 1000 : 0;
  if (!sawAnswer && event.result) {
    const el = addMsg("answer md", "");
    el.replaceChildren(renderMarkdown(event.result));
    highlightFences(el);
    attachAnswerTools(el, event.result, lastUserPrompt);
  }
  closeAnswer();
  answerAbandoned = false; // this turn is over; the next one streams normally
  maybeSpeakReply(); // voice-in → voice-out: auto-read a reply to a dictated message (#97)
  finishTrace();
  turnStart = 0; // no turn is running; the next card must not inherit this clock
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
  turnStart = 0;
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
    const row = externalAnchor(source.url);
    row.className = "source-row";
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
  // A streaming thinking gist (unrecorded, live-only): stash it on the trace
  // so the header can say "Thinking: <gist>" while the model reasons.
  if (event.note && currentTrace) {
    currentTrace.liveGist = event.note;
    updateTraceHead(currentTrace);
  }
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

$("stop-btn").onclick = () => act({ type: "stop" }, { label: "the stop" });

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
  show_image: ["Fetched a picture", "web", "--blue"],
  recall: ["Recalled from memory", "knowledge", "--yellow"],
  read_docs: ["Read docs", "doc", "--dim"],
  read_file: ["read_file", "doc", "--dim"],
  read_skill: ["Read a skill", "knowledge", "--green"],
  write_file: ["write_file", "write", "--green"],
  edit_file: ["edit_file", "write", "--green"],
  remember: ["Saved to memory", "knowledge", "--yellow"],
  forget_memory: ["Forgot a memory", "knowledge", "--yellow"],
  create_rule: ["Wrote a rule", "knowledge", "--orange"],
  edit_rule: ["Changed a rule", "knowledge", "--orange"],
  retire_rule: ["Retired a rule", "knowledge", "--orange"],
};

// [TRACE-TAIL-START]
// The live trace is the TAIL of the turn: your prompt, then what the model
// wrote, then what it is doing right now. That ordering can't be chosen once at
// creation — the card exists BEFORE the answer bubble does (steps arrive first),
// and every later append (answer bubbles, terminal blocks, echoes, approval
// cards) lands after it. So the position is re-established instead: scrollToEnd
// is the funnel every content-adding path already calls, which is why the move
// lives there — one enforcement point covers appends not yet written.
function keepTraceLast() {
  const el = currentTrace && currentTrace.el;
  if (!el || !el.classList.contains("live")) return;
  if (el.parentNode === messagesEl && messagesEl.lastElementChild !== el) {
    messagesEl.appendChild(el);
    pinTrace(currentTrace); // a re-inserted node can lose the pane's scroll offset
  }
}

// The pinned card overlaps the floating jump-to-latest arrow (both live just
// above the composer), so the arrow is offset by the card's measured height.
// Its height is not a constant — the status line wraps up to three lines, and
// expanding the timeline changes it outright — hence an observer rather than a
// magic number. Guarded: the choreo sandboxes have no ResizeObserver, and the
// arrow's placement is cosmetic, so absent one it simply keeps its base offset.
function measurePinnedTrace(t) {
  const set = () => document.body.style.setProperty("--live-trace-h", `${t.el.offsetHeight}px`);
  set();
  if (typeof ResizeObserver !== "function") return;
  t.sizer = new ResizeObserver(set);
  t.sizer.observe(t.el);
}

function releasePinnedTrace(t) {
  if (t.sizer) { t.sizer.disconnect(); t.sizer = null; }
  document.body.style.removeProperty("--live-trace-h");
}
// [TRACE-TAIL-END]

// [TRACE-OPEN-START]
// The live trace comes into being HERE and nowhere else — one per turn, created
// lazily by the first step that needs it. Its disposal is [TRACE-CLOSE]'s; a
// third writer is how a trace from one chat ended up collecting another's steps.
function ensureTrace() {
  if (currentTrace) return currentTrace;
  const el = document.createElement("div");
  // Collapsed while running: the header IS the status line, and the card sits at
  // the tail of the transcript pinned above the composer — a tall timeline there
  // would eat the answer's reading space. Tap the header for the steps.
  el.className = "trace live";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "trace-head";
  // Order matters: the chevron follows the TEXT it discloses, and Stop owns the
  // trailing edge on its own. Two affordances at the same edge made the expand
  // tap land on Stop.
  head.innerHTML =
    `<span class="trace-status">${SPINNER}</span>` +
    `<span class="trace-headtext"><span class="trace-title">Working…</span>` +
    `<span class="trace-sub"></span></span>` +
    `<svg class="trace-chev" viewBox="0 0 24 24"><path d="M6 9.5l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>` +
    `<button type="button" class="trace-stop" aria-label="stop" title="Stop"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/></svg></button>`;
  const body = document.createElement("div");
  body.className = "trace-body";
  // Steps live in an inner content div so the timeline rail spans the FULL
  // (scrollable) content, not just the visible slice.
  body.innerHTML = '<div class="trace-inner"><div class="trace-rail"></div></div>';
  el.append(head, body);
  const t = {
    el, head, body, inner: body.querySelector(".trace-inner"),
    started: 0, secs: 0, tokensIn: 0, tokensOut: 0,
    pending: null, thinkingRow: null,
    // The live header counts the TURN, not this card: the card is created by the
    // turn's first step, which on a slow local model lands minutes after you hit
    // send. `turnStart` is that turn's origin (0 when no live turn is running, so
    // a replayed or stray step can't inherit a finished turn's clock); a replay
    // rebuilding a running turn arrives with no origin at all and back-dates this
    // from the steps it replays — see accountStepTime.
    startedAt: turnStart || Date.now(),
    // Whether that origin is the turn's real start or a guess starting now. A
    // replay reconstructs the guess from the steps; a known origin must not be
    // back-dated on top of that (it is already correct, and the sum would push
    // it into the past twice).
    originKnown: !!turnStart,
    timer: null,
    autoCollapsed: false, // collapsed by an approval card, to be restored after
    // Live-status DATA for the header line — strings are derived only in
    // traceStatusLine (the choreo tests run these handlers with the header
    // renderer stubbed out, so handlers must never build display text).
    turnSay: null,       // model preamble alongside this turn's tool calls
    turnGist: null,      // first line of the turn's thinking text
    liveGist: null,      // streaming thinking gist (status channel, unrecorded)
    running: [],         // in-flight tool_starts: {name, summary, command}
    waitingApproval: false, answering: false, stopping: false,
    userToggled: false,  // the reader took over the open/closed state
  };
  // The head toggles expand freely — even while the turn runs (#65). A manual
  // toggle is the user's choice, so clear any pending auto-restore so the
  // approval-resolved handler won't fight them by re-expanding.
  head.onclick = (e) => {
    if (e.target.closest(".trace-stop")) return;
    el.classList.toggle("open");
    t.autoCollapsed = false;
    t.userToggled = true;
  };
  head.querySelector(".trace-stop").onclick = (e) => {
    e.stopPropagation();
    // "Stopping…" says the task is coming down. Unreceipted it runs on behind
    // a header that has given up asking, with the control disabled — so the
    // claim is withdrawn and Stop becomes pressable again ([ACK-LEDGER]).
    const t = currentTrace;
    act({ type: "stop" }, { label: "the stop", lost: () => unmarkStopping(t) });
    markStopping(t); // immediate "Stopping…" feedback in the header
  };
  messagesEl.appendChild(el);
  // Deliberately NOT the scroll anchor: the anchor is what gets pinned to the
  // top of the screen, and the card now lives at the BOTTOM of the turn. The
  // answer claims it instead ([ANSWER-OPEN]).
  currentTrace = t;
  body.addEventListener("scroll", () => updateScrollHints(body));
  currentTrace.timer = setInterval(() => updateTraceHead(currentTrace), 1000);
  refreshStatusline(); // the trace header owns Stop now; hide the bottom bar
  measurePinnedTrace(t);
  scrollToEnd();
  return currentTrace;
}
// [TRACE-OPEN-END]

// The Stop button was pressed: reflect it in the header until `done` lands.
// State, not a one-shot DOM write — the 1s ticker used to repaint "Working…"
// right over the old direct title write.
function markStopping(t) {
  if (!t) return;
  t.stopping = true;
  t.el.classList.add("stopping");
  const btn = t.el.querySelector(".trace-stop");
  if (btn) btn.disabled = true;
  updateTraceHead(t);
}

// Withdraw that claim: the stop never reached the server, so the task is still
// running and the control that asks for it must work again ([ACK-LEDGER]).
function unmarkStopping(t) {
  if (!t) return;
  t.stopping = false;
  t.el.classList.remove("stopping");
  const btn = t.el.querySelector(".trace-stop");
  if (btn) btn.disabled = false;
  updateTraceHead(t);
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

// One step's duration, booked against the turn's two clocks: `secs` is the work
// the finished header reports, `startedAt` the wall-clock origin the live header
// counts from.
//
// The back-date is what makes a mid-turn REPLAY continue the clock instead of
// restarting it. A replay rebuilds the running turn's steps out of the log (every
// reconnect — each phone unlock — does this), and the log carries no origin for
// the turn: the replayed `user` event deliberately sets none, so the rebuilt card
// would count from the reconnect and read 0:03 on a turn ten minutes old. Each
// replayed step therefore pushes the origin back by its own duration,
// reconstructing where the turn actually began. Live steps never back-date —
// `startedAt` is already the real turn start, and parallel read-only tools would
// otherwise book their overlap twice.
function accountStepTime(t, secs) {
  if (!secs) return;
  t.secs += secs;
  if (replaying && !t.originKnown) t.startedAt -= secs * 1000;
}

// The turn's real start, off a REPLAYED `user` event (epoch seconds, stamped by
// the server when the turn began).
//
// This is the origin the live clock needs and the step-sum back-date above can
// only approximate: summing steps measures the WORK, and a turn is work plus
// everything between it — the step still in flight, an approval sitting unanswered,
// the answer streaming out — so the reconstruction always came back SHORT. That is
// why swiping to another chat and back wound the clock backwards by a minute
// instead of to zero: it was never a reset, it was a re-derivation from less.
//
// 0 means "unusable" — a transcript trimmed past the turn's own user event, a log
// written before the stamp existed, or a client clock behind the server's (a start
// in the future would count DOWN) — and that keeps the step-sum as the fallback.
function replayedTurnStart(event) {
  const ms = Number(event.ts) * 1000;
  if (!Number.isFinite(ms) || ms <= 0 || ms > Date.now()) return 0;
  return ms;
}

function traceStep(step) {
  const t = ensureTrace();
  if (step.kind === "thinking_start") {
    // A new model turn: the previous turn's words no longer describe it.
    t.turnSay = t.turnGist = t.liveGist = null;
    t.answering = false;
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
    // turn has tool calls), so this is the only place its token usage AND its
    // duration land. Unbooked, the finished header contradicted its own
    // timeline: a turn that thought for 3.6s, ran one tool for 2.2s and then
    // spent 23s writing the answer summarized itself as "Worked for 5.8s" with
    // an "Answered in 23s" row sitting right below it. Writing the answer is
    // the work the turn is usually mostly made of.
    accountStepTime(t, step.secs);
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
    accountStepTime(t, step.secs);
    if (step.tokens) { t.tokensIn += step.tokens[0] || 0; t.tokensOut += step.tokens[1] || 0; }
    // The model's own words for this turn (say = preamble, gist = thinking
    // text) — the header prefers these over the deterministic tool line.
    t.turnSay = step.say || null;
    t.turnGist = step.gist || null;
    t.liveGist = null; // the persisted gist supersedes the streamed one
    // A "thinking" step only fires when the turn ended in tool calls, so any
    // streamed text was preamble narration — not the answer.
    t.answering = false;
    if (t.thinkingRow) { // finalize the live row in place
      const ref = t.thinkingRow;
      clearStepTimer(t, ref);
      ref.row.classList.remove("running", "active-step");
      ref.badge.innerHTML = traceSvg("thinking", "var(--purple)");
      ref.titleEl.textContent = `Thought for ${fmtSecs(step.secs)}`;
      // History bonus: the row keeps the thinking gist as its subtitle —
      // must match the replay branch below or hot/cold rows diverge.
      if (step.gist && !ref.main.querySelector(".step-sub")) {
        const subEl = document.createElement("span");
        subEl.className = "step-sub";
        subEl.textContent = step.gist;
        ref.main.appendChild(subEl);
      }
      t.thinkingRow = null;
    } else {
      t.started += 1;
      traceRow(t, traceSvg("thinking", "var(--purple)"),
        `Thought for ${fmtSecs(step.secs)}`, step.gist || "");
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
  t.running.push({ name: step.name, summary: step.summary || "", command: step.command || "" });
  // The command + output + exit for run_command are drawn by the terminal
  // block that command_start builds once the command is approved and runs;
  // while the approval card is up the row is just the spinner.
  t.pending = { ...ref, name: step.name };
  updateTraceHead(t);
}

function toolFinish(t, step) {
  accountStepTime(t, step.secs);
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
  const runIdx = t.running.findIndex((r) => r.name === step.name);
  if (runIdx !== -1) t.running.splice(runIdx, 1);
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

// [TRACE-STATUS-START]
// The live header's one-liner: what is happening RIGHT NOW, so a collapsed
// trace still tells the story. Pure derivation over the data the step handlers
// stash on `t` — all display strings are built here and nowhere else.
const TOOL_STATUS = {
  run_command: (r) => "Running: " + (r.command || r.summary || "a command"),
  read_file: (r) => "Reading " + (r.summary || "a file"),
  write_file: (r) => "Writing " + (r.summary || "a file"),
  edit_file: (r) => "Editing " + (r.summary || "a file"),
  read_url: (r) => "Reading " + (r.summary || "a page"),
  web_search: (r) => "Searching: " + (r.summary || "…"),
  read_docs: (r) => "Reading docs: " + (r.summary || "…"),
  read_skill: (r) => "Reading skill: " + (r.summary || "…"),
  recall: (r) => "Recalling: " + (r.summary || "…"),
  remember: (r) => "Saving to memory: " + (r.summary || "…"),
  create_tool: () => "Creating a tool",
  import_skill: (r) => "Importing skill: " + (r.summary || "…"),
};

function toolStatusLine(r) {
  const build = TOOL_STATUS[r.name];
  if (build) return build(r);
  return "Running " + r.name + (r.summary ? ": " + r.summary : "");
}

function traceStatusLine(t) {
  if (t.stopping) return "Stopping…";
  if (t.waitingApproval) return "Waiting for approval…";
  if (t.answering) return "Answering…";
  if (t.running.length) {
    // Model words describe the whole turn; without them, name the newest
    // in-flight tool (read-only tools run in parallel — count the rest).
    const words = t.turnSay || t.turnGist;
    const line = words || toolStatusLine(t.running[t.running.length - 1]);
    const more = t.running.length - 1;
    return more > 0 ? `${line} · +${more} more` : line;
  }
  // The gist IS the status — no "Thinking:" label; the purple spinner and
  // the timeline row already say what phase this is.
  if (t.liveGist) return t.liveGist;
  if (t.thinkingRow) return "Thinking…";
  if (t.turnSay || t.turnGist) return t.turnSay || t.turnGist;
  return "Working…";
}
// [TRACE-STATUS-END]

function updateTraceHead(t) {
  const title = t.el.querySelector(".trace-title");
  const sub = t.el.querySelector(".trace-sub");
  const live = t.el.classList.contains("live");
  // Tokens ride the sub line, not a separate right-side chip — the status
  // line needs the full header width (a web-search query ellipsizes late,
  // not at a chip-squeezed midpoint).
  const parts = [];
  if (t.tokensIn) parts.push("↑" + fmtTokens(t.tokensIn));
  if (t.tokensOut) parts.push("↓" + fmtTokens(t.tokensOut));
  const tok = parts.join(" ");
  if (live) {
    title.textContent = traceStatusLine(t);
    const elapsed = Math.floor((Date.now() - t.startedAt) / 1000);
    // No step count while live — what is happening matters, not how many
    // steps it took so far.
    sub.textContent = mmss(elapsed) + (tok ? ` · ${tok}` : "");
    if (t.activeStartedAt) {
      const st = t.body.querySelector(".step.active-step .step-timer");
      if (st) st.textContent = `${Math.floor((Date.now() - t.activeStartedAt) / 1000)}s`;
    }
  } else {
    title.textContent = `Worked for ${fmtSecs(t.secs)}`;
    sub.textContent = `${t.started} step${t.started === 1 ? "" : "s"}` + (tok ? ` · ${tok}` : "");
  }
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

// [TRACE-CLOSE-START]
// …and it ends HERE, whatever ends it: a finished turn, an error, a stop, or the
// replay that replaced the transcript it was drawn into (resetLiveTurn calls
// this rather than nulling the variable, so the interval timer and every
// still-spinning row are finalized on every one of those paths).
function finishTrace(errored) {
  if (!currentTrace) return;
  const t = currentTrace;
  if (t.timer) { clearInterval(t.timer); t.timer = null; }
  releasePinnedTrace(t); // stops pinning, and gives the arrow its base offset back
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
// [TRACE-CLOSE-END]

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
  act({ type: "retry", text: prompt }, { label: "the retry" });
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

// [SYNTHETIC-START]
// A turn aish wrote itself (#171): the restart-recovery note, or the prompt an
// automation triggered a session with. It IS a real turn — it starts a task, so
// every turn side effect still runs — but the human never typed it, so it takes
// the quiet system-note row (the workspace marker's visual language) instead of
// a blue user bubble that would read as their own words.
const SYNTHETIC_LABELS = { resume: "Automatic resume", trigger: "Triggered request" };
// The label already says "automatic resume"; printing the marker again on the
// line below it is noise.
const SYNTHETIC_PREFIX_RE = /^\s*\[automatic resume\]\s*/i;

function addSystemMsg(kind, text) {
  const el = document.createElement("div");
  el.className = "msg system-note";
  const ico = document.createElement("span");
  ico.className = "sysnote-ico";
  ico.innerHTML = kind === "trigger" ? traceSvg("chat", "currentColor") : RERUN_SVG;
  const body = document.createElement("div");
  body.className = "sysnote-body";
  const label = document.createElement("div");
  label.className = "sysnote-label";
  label.textContent = SYNTHETIC_LABELS[kind] || "System";
  const detail = document.createElement("div");
  detail.className = "sysnote-text";
  detail.textContent = text.replace(SYNTHETIC_PREFIX_RE, "");
  body.append(label, detail);
  el.append(ico, body);
  messagesEl.appendChild(el);
  scrollToEnd();
  return el;
}
// [SYNTHETIC-END]

// [MSG-STAMP-START]
// When a turn happened (#200). Absolute, never relative: a rail row is
// re-rendered every time you open the list, so "2m" there stays true, but a
// transcript line is written once and read minutes or months later — "2m" would
// become a lie the moment you looked away. The date appears only when it is not
// today, which is the chat-app convention and keeps the common case to four
// characters.
function messageStamp(atSeconds) {
  const ms = (Number(atSeconds) || 0) * 1000;
  if (!ms) return "";
  const date = new Date(ms);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const today = dayStart(Date.now());
  const day = dayStart(ms);
  if (day >= today) return time;
  if (day >= today - 6 * DAY_MS) return `${date.toLocaleDateString([], { weekday: "short" })} ${time}`;
  return `${date.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

// It goes on the turn's OPENING message, once — not on both halves. A turn's
// two messages are seconds apart, so stamping the answer as well would print
// the same time twice for one exchange; and the answer's row already carries
// how LONG it took, which composes into a whole story: asked at 14:32, took 12s.
function stampTurn(tools, at) {
  const text = messageStamp(at);
  if (!text) return;
  const stamp = document.createElement("span");
  stamp.className = "msg-stamp";
  stamp.textContent = text;
  // PREPENDED, so the chips keep the trailing edge they have always had: the
  // row is right-packed, and appending would have shoved every familiar control
  // leftwards to make room for something you only read occasionally.
  tools.prepend(stamp);
}
// [MSG-STAMP-END]

// [REDACT-START]
// Deleting an exchange (#202). A chat had no eraser at all: the log is
// append-only and replayed whole, so a probe fired at the wrong chat, a message
// half-typed and sent by an autocorrect Return, or a secret pasted into the
// composer stayed there forever — the only tools were deleting the entire chat
// or hand-editing JSONL with the server stopped.
//
// The control lives on the PROMPT because the prompt is what a turn is named by,
// and it deletes the whole exchange: an answer repeats what it was asked, so
// taking away half of one is not a deletion.
//
// It asks BEFORE it deletes, in the shared confirmation modal ([CONFIRM]) —
// which is also where the reasoning lives for why an arm-then-confirm chip was
// not enough. The word is DELETE, not "remove": remove reads as "taken off this
// screen", and what happens here is that the text stops existing.
//
// The chip sits at the START of the row rather than its trailing edge: copy and
// reuse are tapped constantly and without thinking, so they keep the edge a
// thumb lands on by habit, and the destructive one does not.
const REDACT_LABEL = "delete this exchange";

function askDeleteTurn(turn) {
  askConfirm({
    title: "Delete this exchange?",
    // Concretely what goes, in the words the UI itself uses for them —
      // "everything it started" was true and told nobody anything.
    body:
      "Deletes your message, the thinking steps and commands it ran, and the " +
      "answer. Gone from this chat, from what the model remembers, and from " +
      "the offline copies on your devices. This cannot be undone.",
    verb: "Delete",
    action: () => act({ type: "redact", turn }, { label: "deleting that exchange" }),
  });
}

function redactChip(turn) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip redact-chip";
  btn.title = REDACT_LABEL;
  btn.setAttribute("aria-label", REDACT_LABEL);
  btn.append(trashIcon());
  btn.onclick = () => askDeleteTurn(turn);
  return btn;
}

// What is left where the exchange was. A chat must not silently lose a turn:
// the answer above it would read as a reply to nothing, and a deletion you
// can't see is indistinguishable from data quietly going missing.
//
// Deliberately UNDATED. The row sits between two timestamped turns, so its
// position already says when; a number on it reads as "deleted at", which is
// not what any stamp here could mean — the only time available is the deleted
// MESSAGE's, so one number would be claiming two different things.
function addRedactedMsg() {
  const el = document.createElement("div");
  el.className = "msg system-note redacted-note";
  const ico = document.createElement("span");
  ico.className = "sysnote-ico";
  ico.append(trashIcon());
  const body = document.createElement("div");
  body.className = "sysnote-body";
  const label = document.createElement("div");
  label.className = "sysnote-label";
  label.textContent = "Message deleted";
  body.appendChild(label);
  el.append(ico, body);
  messagesEl.appendChild(el);
  scrollToEnd();
  return el;
}
// [REDACT-END]

// What you attached, shown as what it is: a thumbnail for an image, a named
// chip for anything else. Tapping an image opens it full size through the same
// token-gated /file endpoint the transcript's inline images use — no second
// policy for where a local path may be loaded from.
function attachmentStrip(notes) {
  const strip = document.createElement("div");
  strip.className = "msg-attachments";
  for (const note of notes) {
    const src = note.kind === "image" ? imageSrc(note.path) : null;
    if (src) {
      const thumb = document.createElement("img");
      thumb.className = "msg-attachment-thumb";
      thumb.src = src;
      thumb.alt = note.name;
      thumb.title = note.name;
      // A file that has since been deleted must not leave a broken-image glyph
      // where a photo was: fall back to naming it, which is still true.
      thumb.onerror = () => thumb.replaceWith(attachmentChip(note));
      thumb.onclick = () => openPreview(src, note.name);
      strip.appendChild(thumb);
    } else {
      strip.appendChild(attachmentChip(note));
    }
  }
  return strip;
}

function attachmentChip(note) {
  const chip = document.createElement("span");
  chip.className = "msg-attachment-chip";
  // Shortened from the MIDDLE: the end of a file name is where it differs.
  chip.textContent = shortName(note.name);
  chip.title = note.path; // the full name and path stay one hover away
  return chip;
}

function addUserMsg(text, at, turn) {
  // The note lines are the model's business, not the reader's: the bubble shows
  // what was typed, and the attachments show as attachments.
  const { body, attachments: notes } = splitAttachmentNotes(text);
  const el = addMsg("user", body);
  if (notes.length) {
    el.appendChild(attachmentStrip(notes));
    if (!body) el.classList.add("attachments-only");
  }
  const tools = document.createElement("div");
  tools.className = "user-tools";
  // Copy and reuse hand back what was TYPED. Read from the split, not from the
  // rendered node — the node now carries chip text that was never in the prompt.
  const getText = () => body;
  // A turn id exists only for a turn the server has logged, so a live turn gets
  // its remove control on the next replay rather than a control that would name
  // nothing.
  currentTurnId = turn || "";
  if (turn) tools.append(redactChip(turn));
  tools.append(reuseChip(getText), copyChip(getText, "copy prompt"));
  stampTurn(tools, at);
  messagesEl.appendChild(tools);
  return el;
}

// [PENDING-SEND-START]
// A message you have sent is on screen BEFORE the server says so.
//
// The composer clears the instant the text is handed to the socket, but the
// blue bubble used to be drawn only when the server ECHOED the turn back. On a
// fast link those are the same moment and the distinction is invisible; on a
// slow one the message exists nowhere the user can see for seconds — the
// composer empty, the transcript unchanged — and a long message reads as simply
// lost. That is exactly how it was reported, and the reader was right to
// believe it: nothing on screen said otherwise.
//
// So the bubble is drawn on SEND, in a pending state, and RECONCILED when the
// server's own version arrives. The server's version wins, always: it carries
// the timestamp, the turn id the delete chip needs, and any attachment notes
// the server appended, none of which the client can know. A message the agent
// is too busy to start comes back as a `queued` event instead — same
// reconciliation, different resolution (the queue chip takes over).
//
// If the bubble's DOM is destroyed while still un-acknowledged — a reconnect
// rebuild, a chat switch, a socket that died — the TEXT is not lost. Never an
// automatic resend: the send may well have arrived, and a duplicated
// instruction to an agent that runs shell commands is a worse outcome than a
// message you have to send again by hand.
//
// BUT "the DOM is going away" IS NOT "the message did not arrive", and
// conflating the two is what shipped a duplicate. The text went straight back
// to the composer on every rebuild — and a rebuild is exactly what a RECONNECT
// does, whose very next act is to replay the transcript the server holds. So
// the recovery fired one moment before the definitive answer arrived, the
// answer said "I have your message", and the owner — looking at a toast that
// said it had not been confirmed, with the words back under the cursor — sent
// it again. Two identical instructions, one of them nobody asked for.
//
// So a detached send is HELD, not handed back, and the replay ADJUDICATES it:
// a transcript that contains the message proves it arrived, and only one that
// does not puts the words in the composer. Held text is persisted, so the
// window costs nothing if the tab dies; and if no replay ever comes to settle
// it (still offline), a deadline hands it back rather than holding forever —
// losing what you typed remains the one unacceptable outcome.
const pendingSends = []; // oldest first — {el, tools, status, text, session}

// Detached, unadjudicated sends: [{ text, session }]. Persisted under their own
// key rather than merged into the draft, because a draft is text you are
// WRITING and this is text you already pressed send on — only one of them may
// be silently dropped when it turns out to have arrived.
const HELD_KEY = "aish-held-sends";
let heldSends = [];
let heldTimer = null;

function loadHeldSends() {
  try {
    const raw = JSON.parse(localStorage.getItem(HELD_KEY));
    heldSends = Array.isArray(raw) ? raw.filter((h) => h && h.text) : [];
  } catch { heldSends = []; }
  // A tab that died holding them left the question open, not answered: this
  // one is about to connect and be replayed the same chat, which can still
  // settle them. Give that its chance — the deadline is what guarantees the
  // words come back if it never comes.
  if (heldSends.length) armHeldRelease();
}

function saveHeldSends() {
  try {
    if (heldSends.length) localStorage.setItem(HELD_KEY, JSON.stringify(heldSends));
    else localStorage.removeItem(HELD_KEY);
  } catch { /* private mode: held in memory only, same as the draft */ }
}

function addPendingSend(text) {
  const el = addMsg("user", text);
  el.classList.add("pending");
  const tools = document.createElement("div");
  tools.className = "user-tools pending-send";
  const status = document.createElement("span");
  status.className = "pending-send-status";
  status.textContent = "Sending…";
  tools.appendChild(status);
  messagesEl.appendChild(tools);
  pendingSends.push({ el, tools, status, text, session: currentSession });
  // SENDING IS SEEING. Your own message is output (it is a message in the
  // chat), so the seen stamp has to move past it or the chat you just typed
  // into flags itself unread the moment you leave — for a sentence you wrote
  // (#203). `markSeen` already runs on entering a chat and at `done`; this is
  // the third moment the log outruns the stamp, and it is the one that shows
  // while the turn is still running.
  markSeen(currentSession);
  armPendingSendWatch();
  scrollToEnd(true);
}

// The server has accounted for one of our sends. Matched by TEXT where it can
// be (two messages fired inside one round trip resolve in whatever order the
// server actually took them), falling back to the oldest — the server appends
// attachment notes, so an exact match is not always available.
function resolvePendingSend(text) {
  const bare = stripAttachmentNotes(text || "");
  let index = pendingSends.findIndex((item) => item.text === bare);
  if (index < 0) index = pendingSends.length ? 0 : -1;
  if (index < 0) return;
  const [item] = pendingSends.splice(index, 1);
  item.el.remove();
  item.tools.remove();
  if (!pendingSends.length) { clearTimeout(pendingSendTimer); pendingSendTimer = null; }
}

// Their DOM is going away while they are still un-acknowledged. The bubbles go
// with it; the TEXT is held, not handed back, until something says whether the
// server has it. Nothing is shown and nothing is said here — a rebuild is
// usually a reconnect, and a reconnect is about to answer the question.
function clearPendingSends() {
  if (!pendingSends.length) return;
  clearTimeout(pendingSendTimer);
  pendingSendTimer = null;
  for (const item of pendingSends.splice(0)) {
    item.el.remove();
    item.tools.remove();
    if (item.text) heldSends.push({ text: item.text, session: item.session });
  }
  saveHeldSends();
  armHeldRelease();
}

// A replay is the server's own account of a chat, so it settles every send held
// for that chat: present means it arrived (drop it silently — the transcript
// already shows it), absent means it did not (the words go back to the
// composer). Matched on EXACT text only, never the oldest-pending fallback
// `resolvePendingSend` may use live: a replay carries every message the chat
// ever had, and a positional match against those would "confirm" a send using
// something the user wrote last week.
function adjudicateHeldSends(session, events) {
  if (!heldSends.length) return;
  const arrived = new Set(
    (events || [])
      .filter((e) => e && e.type === "user" && typeof e.text === "string")
      .map((e) => stripAttachmentNotes(e.text))
  );
  const mine = (h) => !h.session || h.session === session;
  const unsent = heldSends.filter((h) => mine(h) && !arrived.has(h.text));
  heldSends = heldSends.filter((h) => !mine(h));
  saveHeldSends();
  returnToComposer(unsent.map((h) => h.text));
  if (!heldSends.length) { clearTimeout(heldTimer); heldTimer = null; }
}

// Nothing came to settle them — still offline, or the reconnect never landed.
// Holding forever would be losing what you typed by a slower route.
const HELD_RELEASE_MS = 12000;

function armHeldRelease() {
  clearTimeout(heldTimer);
  heldTimer = setTimeout(releaseHeldSends, HELD_RELEASE_MS);
}

function releaseHeldSends() {
  clearTimeout(heldTimer);
  heldTimer = null;
  const texts = heldSends.map((h) => h.text);
  heldSends = [];
  saveHeldSends();
  returnToComposer(texts);
}

// Prepended, so it is the next thing you would send. Skips anything already
// sitting there — a release racing an adjudication must not double the words.
function returnToComposer(texts) {
  const lost = (texts || []).filter((text) => text && !input.value.includes(text));
  if (!lost.length) return;
  input.value = [...lost, input.value].filter(Boolean).join("\n\n");
  saveDraft();
  resizeInput();
  showToast(lost.length > 1 ? "messages not confirmed — back in the composer"
                            : "message not confirmed — back in the composer");
}

// A socket that reports OPEN and is not: nothing will ever come back to resolve
// these, so say so rather than spinning "Sending…" indefinitely.
const PENDING_SEND_SLOW_MS = 8000;
let pendingSendTimer = null;

function armPendingSendWatch() {
  clearTimeout(pendingSendTimer);
  pendingSendTimer = setTimeout(() => {
    pendingSendTimer = null;
    if (pendingSends.length) markPendingSendsStalled();
  }, PENDING_SEND_SLOW_MS);
}

function markPendingSendsStalled() {
  for (const item of pendingSends) {
    if (item.status.dataset.stalled) continue;
    item.status.dataset.stalled = "1";
    item.status.textContent = "Still sending — ";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "pending-send-retry";
    retry.textContent = "reconnect";
    retry.onclick = () => reconnect();
    item.status.appendChild(retry);
  }
}
// [PENDING-SEND-END]

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
  keepTraceLast(); // whatever was just appended, the live status card stays the tail
  if (force || nearBottom()) messagesEl.scrollTop = messagesEl.scrollHeight;
  updateScrollButton();
  updateEmptyHint(); // every content-adding path funnels through here
}

// Is this chat still unused? Workspace notes (a UI /cd, a dir-trust marker) are
// system metadata, not turns — a chat is "fresh" after one. Named separately
// from updateEmptyHint because [SHARES] asks the same question for a different
// reason: whether opening ANOTHER new chat would just leave an empty one behind.
function transcriptIsEmpty() {
  return (
    messagesEl.childElementCount <= 4 &&
    [...messagesEl.children].every((c) => c.classList.contains("workspace-note"))
  );
}

// Empty-state welcome hero (#123): shown only while the transcript is empty.
function updateEmptyHint() {
  // A workspace-note (a UI /cd or dir-trust marker) is system metadata, not a
  // real turn — a fresh chat is still "empty" for onboarding after one, so the
  // welcome hero stays until the user actually sends a message (#135).
  // Fast path: this runs on EVERY content append (scrollToEnd funnels here),
  // and a populated transcript can only be "empty" if it holds nothing but
  // workspace notes — with more than a handful of children it never is, so
  // skip the per-append array spread over hundreds of nodes.
  $("welcome").hidden = !transcriptIsEmpty(); // brand hero on a fresh chat (#123)
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

// [VV-SETTLE-START]
if (window.visualViewport) {
  const onViewportChange = () => {
    // A live transcript touch freezes settling (see [TOUCH-FREEZE]): the
    // keyboard dismissal that touch just triggered would otherwise reflow and
    // auto-scroll the content mid-press, cancelling the long-press selection
    // the blur made possible. An established selection likewise must not be
    // yanked to the bottom by a later keyboard show/hide.
    if (viewportSettleAllowed()) {
      syncKeyboardInset();
      snapViewportHome();
      if (!selectionActive()) scrollToEnd();
    } else {
      trackViewportPan(); // frozen ≠ static: ride the browser's un-pan (see [TOUCH-FREEZE])
    }
    if (Date.now() - lastVvReport > 400) {
      lastVvReport = Date.now();
      reportViewport("vv-change"); // #8/#24 diagnostics at the moment it matters
    }
  };
  visualViewport.addEventListener("resize", onViewportChange);
  visualViewport.addEventListener("scroll", onViewportChange);
}
// [VV-SETTLE-END]

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

// [SCROLLPOS] Remembering the reading position walks the transcript's children,
// so it is debounced well clear of the scroll itself — the position that matters
// is where the scrolling STOPPED. pagehide/hidden catch a page torn down inside
// the debounce window, which on a phone is the common case (the reload after the
// app was put away is exactly that).
const SCROLL_POS_SAVE_MS = 300;
let scrollPosTimer = null;
messagesEl.addEventListener("scroll", () => {
  updateScrollButton();
  if (scrollPosTimer) clearTimeout(scrollPosTimer);
  scrollPosTimer = setTimeout(rememberScrollPos, SCROLL_POS_SAVE_MS);
}, { passive: true });
addEventListener("pagehide", rememberScrollPos);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) rememberScrollPos(); // iOS PWAs hide far more reliably than they pagehide
});
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
  // The legacy flat-transcript replay (a log too old to reconstruct) paints a
  // whole conversation, so it ends the live turn for the same reason a replay
  // does — via the owner, not by zeroing its own corner of the cluster.
  resetLiveTurn("rebuild");
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
      while (i < lines.length && !fenceCloses(lines[i], fence)) body.push(dedent(lines[i++], fence.indent));
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
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const h = document.createElement("h" + Math.min(heading[1].length + 1, 6));
      h.className = "md-h";
      h.appendChild(inlineMd(heading[2]));
      frag.appendChild(h);
      i++;
      continue;
    }
    if (/^(\s*)(?:[-*+]|\d+[.)])\s+/.test(line)) {
      flush();
      const opener = line.match(/^(\s*)(?:[-*+]|(\d+)[.)])\s+/);
      const baseIndent = opener[1].length;
      const ordered = opener[2] !== undefined;
      const list = document.createElement(ordered ? "ol" : "ul");
      // An ordered list that doesn't start at 1 keeps its true first number via
      // the start attribute, so a fragment picking up after a nested block never
      // silently renumbers from 1 (#166).
      if (ordered && parseInt(opener[2], 10) !== 1) list.setAttribute("start", opener[2]);
      const leadOf = (s) => s.match(/^\s*/)[0].length;
      while (i < lines.length) {
        const itemLine = lines[i];
        const m = itemLine.match(/^(\s*)(?:[-*+]|\d+[.)])(\s+)(.*)$/);
        if (!m || m[1].length !== baseIndent) break; // not an item at this level
        const contentCol = itemLine.length - m[3].length; // column the text starts at
        const parts = [m[3]];
        i++;
        // Absorb the item's continuation — blank lines plus lines indented under
        // the marker (a nested paragraph, code block, or sub-list) — instead of
        // breaking out of the list and restarting numbering (#166). Blank lines
        // are buffered and kept only when more indented content follows them.
        let pending = [];
        while (i < lines.length) {
          const cont = lines[i];
          if (cont.trim() === "") { pending.push(""); i++; continue; }
          if (leadOf(cont) <= baseIndent) break; // a sibling item or dedented text ends it
          parts.push(...pending, cont.slice(Math.min(contentCol, leadOf(cont))));
          pending = [];
          i++;
        }
        const li = document.createElement("li");
        if (parts.length === 1) {
          li.appendChild(inlineMd(parts[0])); // flat item — identical to before
        } else {
          li.appendChild(renderMarkdown(parts.join("\n"))); // nested blocks under the item
        }
        list.appendChild(li);
      }
      frag.appendChild(list);
      continue;
    }
    if (/^ {0,3}\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^ {0,3}\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      frag.appendChild(mdTable(lines, i));
      i += 2;
      while (i < lines.length && /^ {0,3}\|.*\|\s*$/.test(lines[i])) i++;
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flush();
      frag.appendChild(document.createElement("hr"));
      i++;
      continue;
    }
    if (/^ {0,3}>\s?/.test(line)) {
      flush();
      const quote = document.createElement("blockquote");
      const body = [];
      while (i < lines.length && /^ {0,3}>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^ {0,3}>\s?/, ""));
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
  for (let row = start + 2; row < lines.length && /^ {0,3}\|.*\|\s*$/.test(lines[row]); row++) {
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

// Every external http(s) link the transcript renders opens in the user's real
// browser, not an in-app webview (#165): target=_blank hands the URL to the OS,
// and rel="noopener noreferrer" severs the opener/referrer channel. Routing all
// transcript anchors through this one helper keeps the invariant structural.
// The in-app aish-*:// schemes (quick replies) render as buttons, never anchors,
// so they keep their own click handling and are never given target=_blank.
function externalAnchor(href) {
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

// [INLINEIMG-START]
// Remote-image fetch whitelist — keep in lockstep with CSP_IMG_HOSTS in
// server.py (content_security_policy). The CSP header already blocks other
// hosts at the browser layer, but only on pages served with the header;
// enforcing the same policy here makes it hold on EVERY render path
// (streaming, replay, offline replay, export preview) and degrade to a
// graceful non-embedded link instead of a CSP violation (#178 P1-12). A
// prompt-injected ![](https://attacker/x.png?<exfil>) must never fire a
// zero-click GET at render time.
const IMG_FETCH_HOSTS = ["img.youtube.com", "i.ytimg.com", "maps.googleapis.com"];

// Pure decision: may this <img src> be set (= fetched) at render time?
// data: carries its own bytes and same-origin paths (/file?…) stay on our
// server; a remote host must be on the whitelist. Unparseable → no fetch.
function imageFetchAllowed(src) {
  if (src.startsWith("data:")) return true;
  if (!/^https?:\/\//i.test(src)) return true;
  try {
    return IMG_FETCH_HOSTS.includes(new URL(src).hostname.toLowerCase());
  } catch {
    return false;
  }
}
// [INLINEIMG-END]

// [RENDERERR-START]
// The browser is the only place that knows an image did not render, and until
// #188 it kept that to itself: the fallback below wrote a small "unavailable"
// note into the DOM and told nobody, so the model's only feedback channel was
// the user typing "images don't show". These failures are reported back — logged
// as a trace step, and (for a live turn) handed to the model as a note on its
// next one, so it retries a different source instead of re-pasting a dead link.
//
// Debounced and batched: one answer with four broken pictures is ONE report and
// one note, not four. What is reported is the ORIGINAL target the model wrote,
// never the rewritten /file?…&token= URL — that would put the access token in
// the session log. Nothing is queued when the socket is down: a diagnostic that
// arrives after the conversation moved on is worse than none.
//
// LIVE ONLY (#201). A failure to render is a fact about the turn that WROTE the
// image, and that is the only turn anyone can act on it in: the model is handed
// the note solely on a live failure, so a replayed one was never anything but
// ledger noise. Reporting it on every render made it noise that compounded —
// re-reading an old chat re-reported the same dead links, each report appending
// to the log, which then read as fresh activity and marked the chat unread. One
// chat had accumulated 53 identical records, up to four per open, and could not
// be marked read by any amount of reading. A property of the transcript is not
// an event; only its arrival was.
const RENDER_ERROR_DEBOUNCE_MS = 1200;
const RENDER_ERROR_BATCH_MAX = 6;
const renderErrorBuffer = new Set(); // original targets awaiting one batched report
let renderErrorTimer = null;

// The `live` gate is HERE rather than at each call site so no future one can
// forget it — every path into a render failure funnels through this function.
function noteRenderError(target, live) {
  // The offline mirror legitimately serves transcripts whose images were never
  // synced; reporting that would blame the model for the network.
  if (offlineViewing) return;
  if (!live) return; // a replay re-reports what the live turn already said (#201)
  if (renderErrorBuffer.size >= RENDER_ERROR_BATCH_MAX && !renderErrorBuffer.has(target)) return;
  renderErrorBuffer.add(target);
  if (renderErrorTimer) clearTimeout(renderErrorTimer);
  renderErrorTimer = setTimeout(flushRenderErrors, RENDER_ERROR_DEBOUNCE_MS);
}

function flushRenderErrors() {
  renderErrorTimer = null;
  const items = [...renderErrorBuffer];
  renderErrorBuffer.clear();
  if (!items.length) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  // `live` stays on the wire — the server reads it to decide whether the model
  // gets a note — but nothing unlive reaches here any more.
  ws.send(JSON.stringify({ type: "render_error", what: "image", items, live: true }));
}
// [RENDERERR-END]

// Images (#9): ![alt](https://…) embeds a whitelisted web image (see
// IMG_FETCH_HOSTS above — any other remote host renders as a link, never a
// fetch); ![alt](/abs/path.png) is rewritten to the token-gated /file
// endpoint, which only serves image files inside the active session's
// roots. Any other scheme stays as the literal text. Tap opens the
// full-size image in a new tab.
// The src a markdown image target may actually be loaded from, or null when
// policy forbids it. Factored out of inlineImage so an embed POSTER resolves
// through the identical rules — a poster that skipped the whitelist would be
// the same zero-click fetch this policy exists to close.
function imageSrc(target) {
  if (/^https?:\/\//.test(target)) {
    return imageFetchAllowed(target) ? target : null;
  }
  if (target.startsWith("/")) {
    const params = new URLSearchParams({ path: target });
    if (token) params.set("token", token);
    return `/file?${params}`;
  }
  return null;
}

// The <img> itself, with the broken-file handling every image render shares.
// `onBroken` lets the caller decide what a failure looks like in its own layout.
function markdownImg(alt, src, target, live, onBroken) {
  const img = document.createElement("img");
  img.className = "md-img";
  img.loading = "lazy";
  img.alt = alt || target;
  // A missing file (deleted since, or another session's roots) renders as a
  // small broken-image note instead of the browser's default glyph.
  img.onerror = () => {
    onBroken();
    noteRenderError(target, live);
  };
  img.src = src;
  return img;
}

function inlineImage(alt, target) {
  // Whether this render is the live turn, captured now: onerror fires later, by
  // which time `replaying` says nothing about where this image came from.
  const live = !replaying && !offlineViewing;
  const src = imageSrc(target);
  if (src === null) {
    if (!/^https?:\/\//.test(target)) {
      return document.createTextNode(`![${alt}](${target})`);
    }
    // Same visual as the broken-image note; the anchor keeps the URL
    // reachable by an explicit user tap — only the zero-click fetch is out.
    // Reported on the LIVE turn (#188/#201): a hand-written remote image link
    // means the model skipped show_image, and that turn is the only one that
    // can still act on it. Re-reporting it on every later read of the chat
    // taught nobody and marked the chat unread — see [RENDERERR].
    noteRenderError(target, live);
    const broken = externalAnchor(target);
    broken.className = "img-link img-broken";
    broken.textContent = `🖼 ${alt || target} (not embedded)`;
    return broken;
  }
  // A LOCAL image (rewritten to /file) previews in place: opening a new tab to
  // look at a chart aish just drew means leaving an installed PWA for Safari.
  // A REMOTE one keeps its anchor — reaching the real URL by an explicit tap is
  // exactly what that link is for.
  const local = !/^https?:\/\//.test(target);
  const link = local ? document.createElement("span") : externalAnchor(src);
  link.className = "img-link";
  if (local) {
    link.onclick = () => openPreview(src, alt || target.split("/").pop());
  }
  link.appendChild(
    markdownImg(alt, src, target, live, () => {
      link.textContent = `🖼 ${alt || target} (unavailable)`;
      link.classList.add("img-broken");
    })
  );
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
    if (act({ type: "create_issue" }, {
      label: "filing that issue",
      lost: () => controls.classList.remove("spent"), // it can fire again — it never fired
    })) {
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
// `poster` (optional, from the [![img](src)](url) form) is a resolved image src
// shown INSTEAD of loading the frame immediately — see mapsCard.
function embedForLink(label, url, poster) {
  const yt = url.match(YOUTUBE_RE);
  if (yt) return youtubeEmbed(yt[1] || yt[2], label);
  const maps = url.match(MAPS_RE);
  if (maps) {
    const params = new URLSearchParams(maps[1]);
    const saddr = params.get("saddr");
    const daddr = params.get("daddr");
    if (saddr && daddr) {
      return mapsDirectionsEmbed(saddr, daddr, label, poster);
    }
    // "q" is the classic ?q= link param; "query" is what the standard
    // /maps/search/?api=1&query=... share links use instead.
    const q = params.get("q") || params.get("query");
    if (q) {
      return mapsEmbed(encodeURIComponent(q), label, poster);
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

const MAP_OPEN_SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7z" fill="currentColor"/><circle cx="12" cy="9" r="2.6" fill="var(--bg-elev2)"/></svg>';

// One map card, with or without a poster.
//
// WITHOUT a poster the frame loads immediately (the pre-existing behaviour for
// a bare maps link). WITH one — the [![static map](…)](maps link) form — the
// picture the tool already produced is painted instead, and the live frame is
// built on tap. That is not only cosmetic: the poster is same-origin and
// already stored, so it is what remains visible when the frame cannot load at
// all (offline, or Google blocked), where an eager iframe leaves a black box.
// Same interaction as the YouTube card above.
function mapsCard(frameSrc, label, poster) {
  const card = document.createElement("div");
  card.className = "embed embed-maps";
  const buildFrame = () => {
    const frame = document.createElement("iframe");
    frame.className = "embed-frame";
    frame.src = frameSrc;
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
    return frame;
  };

  if (!poster) {
    card.appendChild(buildFrame());
    return card;
  }

  card.classList.add("embed-poster");
  card.setAttribute("role", "button");
  card.tabIndex = 0;
  card.setAttribute("aria-label", `Open the interactive map: ${label}`);
  const img = document.createElement("img");
  img.className = "embed-thumb";
  img.loading = "lazy";
  img.alt = label;
  img.src = poster;
  card.appendChild(img);
  const badge = document.createElement("div");
  badge.className = "embed-play embed-open";
  badge.innerHTML = MAP_OPEN_SVG;
  card.appendChild(badge);

  const activate = () => {
    card.replaceChildren(buildFrame());
    card.classList.add("embed-active");
    card.classList.remove("embed-poster");
    card.removeAttribute("role");
    card.removeAttribute("tabindex");
    card.removeAttribute("aria-label");
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

function mapsEmbed(query, label, poster) {
  return mapsCard(`https://maps.google.com/maps?q=${query}&output=embed`, label, poster);
}

function mapsDirectionsEmbed(saddr, daddr, label, poster) {
  const src =
    `https://maps.google.com/maps?saddr=${encodeURIComponent(saddr)}` +
    `&daddr=${encodeURIComponent(daddr)}&output=embed`;
  return mapsCard(src, label, poster);
}

// [![alt](image)](url) — an image that is ALSO a link. Deliberately kept out of
// INLINE_RE: its plain-link branch would read a remote-hosted poster as a link
// labelled "![alt", and adding a branch there would renumber ten capture groups
// the whole renderer indexes by hand. inlineMd tries this first at each
// position instead.
const IMAGE_LINK_RE = /\[!\[([^\]\n]*)\]\(([^)\s]+)\)\]\((https?:\/\/[^)\s]+)\)/;

// An embeddable target becomes a poster-backed card; anything else stays a
// perfectly ordinary clickable picture.
function imageLink(alt, imageTarget, url) {
  const poster = imageSrc(imageTarget);
  if (poster !== null) {
    const embed = embedForLink(alt, url, poster);
    if (embed) return embed;
  }
  const link = externalAnchor(url);
  link.className = "img-link";
  if (poster === null) {
    link.textContent = alt || url;
    return link;
  }
  const live = !replaying && !offlineViewing;
  link.appendChild(
    markdownImg(alt, poster, imageTarget, live, () => {
      link.textContent = `🖼 ${alt || url} (unavailable)`;
      link.classList.add("img-broken");
    })
  );
  return link;
}

function inlineMd(text) {
  const frag = document.createDocumentFragment();
  // [no-chips] (#46) is the model's opt-out from the quick-reply safety net —
  // a directive, not content, so it never renders (code blocks skip inlineMd
  // and keep it literal). Stripping here covers streaming, replay, and reload.
  let rest = text.replace(/\[no-chips\]/gi, "");
  while (rest) {
    const inline = rest.match(INLINE_RE);
    const imageLinkMatch = rest.match(IMAGE_LINK_RE);
    // A tie goes to the image-link: starting at the same index it is the more
    // specific reading, and INLINE_RE would otherwise tear it into three nodes.
    const nested =
      imageLinkMatch && (!inline || imageLinkMatch.index <= inline.index);
    const match = nested ? imageLinkMatch : inline;
    if (!match) {
      frag.appendChild(document.createTextNode(rest));
      break;
    }
    if (match.index > 0) {
      frag.appendChild(document.createTextNode(rest.slice(0, match.index)));
    }
    if (nested) {
      frag.appendChild(imageLink(match[1], match[2], match[3]));
    } else if (match[1]) {
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
        const link = externalAnchor(match[6]);
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

function trashIcon() {
  return svgIcon("i-trash", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", { d: "M4.5 6.5h15" }));
    g.appendChild(make("path", { d: "M9.5 6.5V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v1.5" }));
    g.appendChild(make("path", { d: "M6.5 6.5 7.4 19a1.5 1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4l.9-12.5" }));
    g.appendChild(make("path", { d: "M10.5 10v6.5M13.5 10v6.5" }));
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

// [RATING]
// 👍/👎 on an answer, with an optional reason (#207). Two properties matter and
// both are deliberate:
//
//   The tap records IMMEDIATELY. A rating that waits for a comment is a rating
//   that mostly does not happen, and the count is the part the rules engine
//   needs — it is how "is he still correcting turns that passed every rule"
//   gets answered at all. The comment box opens after, and sending it writes a
//   second record for the same turn; the ledger takes the last.
//
//   Nothing here acts on the rating. It is evidence for the weekly pass and for
//   the owner — making it steer the running session would turn a feedback
//   control into a lever the model can be pointed at.
// What was typed for a turn, kept for THIS browser session only. Switching
// thumb, or withdrawing and changing your mind, must not cost you the sentence
// you already wrote — but a comment with no verdict attached is not a rating,
// so it is never written to the log until a thumb is on. Memory, not storage.
const ratingDrafts = new Map();

function ratingDraft(turn, el) {
  if (ratingDrafts.has(turn)) return ratingDrafts.get(turn);
  const said = el && el.querySelector(".rating-said");
  return said ? said.textContent : "";
}

function ratingChip(turn, kind) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `rating-chip rating-${kind}`;
  const label = kind === "up" ? "good answer" : "bad answer";
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.append(thumbIcon(kind));
  // Do not steal focus from an open reason box. Without this the tap blurs the
  // field first, blur commits and removes it, the row loses ~20px of height,
  // and the thumb the finger was aimed at has MOVED by the time the click
  // lands — so the first tap appears to do nothing but close the box. Toolbar
  // controls beside a text field conventionally decline focus for exactly this
  // reason. The commit still happens, below, in an order we choose.
  btn.onmousedown = (e) => e.preventDefault();
  btn.onclick = () => {
    // Commit anything typed BEFORE acting, so the reason belongs to the
    // verdict it was written under and the draft is up to date for a switch.
    commitOpenReason(ratingHost(btn));
    // Tapping the lit thumb withdraws it. An opinion you cannot take back is
    // one you hesitate to give, and the count this feeds is only worth having
    // if a mistap is cheap to undo. Withdrawal is a RECORD like any other —
    // `none` written after `down` — never a deletion, so the log stays
    // append-only and the ledger's last-wins already does the right thing.
    const host = ratingHost(btn);
    const draft = ratingDraft(turn, host);
    const previous = currentRating(turn); // to put back if the verdict never lands
    const next = btn.classList.contains("on") ? "none" : kind;
    if (next === "none") {
      // The words stay in memory — changing your mind back should not cost
      // you the sentence — but they are NOT written, because a comment with
      // no verdict attached is not a rating.
      act({ type: "rate", turn, rating: next },
        { label: "clearing that rating", lost: () => markRating(turn, previous) });
      markRating(turn, next);
      closeRatingComment(btn);
      showRatingComment(host, "");
    } else {
      // Selecting, or switching thumb. The reason carries over so the record
      // stays coherent — the latest one is always verdict + reason together —
      // and the box opens pre-filled so it can be edited to match.
      act({ type: "rate", turn, rating: kind, ...(draft ? { comment: draft } : {}) },
        { label: "that rating", lost: () => markRating(turn, previous) });
      markRating(turn, kind, draft);
      openRatingComment(btn, turn, kind, draft);
    }
  };
  return btn;
}

// One drawing, used both ways: thumbs-down is the same hand rotated half a
// turn about the icon's centre. Two hand-drawn paths would drift apart the
// first time either is nudged, and these two must read as exact opposites.
function thumbIcon(kind) {
  return svgIcon(`i-thumb i-thumb-${kind}`, (make, svg) => {
    const g = make("g", {
      fill: "none", stroke: "currentColor", "stroke-width": "1.7",
      "stroke-linecap": "round", "stroke-linejoin": "round",
      ...(kind === "down" ? { transform: "rotate(180 12 12)" } : {}),
    });
    // The cuff, and the hand: thumb up the left edge, four knuckles across.
    g.appendChild(make("rect", { x: "3", y: "11.2", width: "4", height: "9", rx: "1.2" }));
    g.appendChild(make("path", {
      d: "M7 20.2v-9l4.1-8a2 2 0 0 1 1.9 2.7l-1 4.1h5.2a2.1 2.1 0 0 1 2 2.6l-1.2 5.4"
         + "a2.2 2.2 0 0 1-2.1 1.7H7z",
    }));
    svg.appendChild(g);
  });
}

// The box goes on its OWN row, under the tools — not inside them. In the chip
// row it was a flex item competing with eight 40px controls, so on a phone it
// collapsed to nothing: the keyboard opened onto a field with no width, which
// reads exactly like a broken button.
// Commit an open reason box without waiting for blur, so a tap on another
// control performs its own action AND saves what was typed.
function commitOpenReason(host) {
  const note = host && host.querySelector(".rating-note");
  if (note && note.commitReason) note.commitReason();
}

function ratingHost(btn) {
  const tools = btn.parentElement;
  return (tools && tools.parentElement) || tools;
}

function closeRatingComment(btn) {
  const host = ratingHost(btn);
  const note = host && host.querySelector(".rating-note");
  if (note) note.remove();
}

function openRatingComment(btn, turn, kind, existing) {
  const host = ratingHost(btn);
  if (!host || host.querySelector(".rating-note")) return;
  const note = document.createElement("input");
  if (existing) note.value = existing;
  note.type = "text";
  note.className = "rating-note";
  note.placeholder = kind === "up" ? "what worked? (optional)" : "what was wrong? (optional)";
  // COMMITTED ON THE WAY OUT, however you leave — Return, tapping elsewhere,
  // dismissing the keyboard. Blur used to keep the text and send nothing, so a
  // comment typed and then tapped away from was silently discarded: the log
  // recorded the tap with `comment: ""` and the owner, reasonably, reported
  // that his comment had vanished. On a phone, tapping away IS how you finish
  // typing — especially now that the box scrolls itself to mid-screen, well
  // away from the keyboard's return key.
  let committed = false;
  const commit = () => {
    if (committed) return;
    committed = true;
    stopKeepingInView();
    const comment = note.value.trim();
    // Remembered either way, so withdrawing and reselecting restores it.
    if (comment) ratingDrafts.set(turn, comment);
    else ratingDrafts.delete(turn);
    if (comment) act({ type: "rate", turn, rating: kind, comment }, { label: "that reason" });
    note.remove();
    showRatingComment(ratingHost(btn), comment);
  };
  note.commitReason = commit;   // so another control can commit it deliberately
  note.onkeydown = (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    commit();
  };
  note.onblur = commit;
  host.appendChild(note);

  // Focusing is not enough on a phone. The keyboard opens AFTER focus, and
  // `syncKeyboardInset` then clamps the body to the visible strip — so a note
  // that was on screen when tapped ends up under the keyboard, with nothing
  // scrolling it back. That is the whole of "I tap the thumb, the keyboard
  // opens, and there is nowhere to type": the field exists, at full width,
  // just below the fold.
  //
  // So it is scrolled into view now AND again on each viewport change while it
  // holds focus, because the keyboard arrives, animates, and settles over
  // several events rather than one.
  const keepInView = () => note.scrollIntoView({ block: "center" });
  const stopKeepingInView = () => {
    if (!window.visualViewport) return;
    visualViewport.removeEventListener("resize", keepInView);
    visualViewport.removeEventListener("scroll", keepInView);
  };
  if (window.visualViewport) {
    visualViewport.addEventListener("resize", keepInView);
    visualViewport.addEventListener("scroll", keepInView);
  }
  note.focus();
  keepInView();
}

// Applied by turn id rather than by position: a rating can be written long
// after the turn it names, and on replay they all arrive at the end.
// What this device last PAINTED for a turn, remembered by the one function
// that paints it — so reverting an unreceipted verdict ([ACK-LEDGER]) does not
// have to read the answer back out of class names, which is a second authority
// on the same fact and drifts the first time the markup is nudged.
const ratingNow = new Map(); // turn -> "up" | "down" | "none"

function currentRating(turn) {
  return ratingNow.get(turn) || "none";
}

function markRating(turn, kind, comment) {
  ratingNow.set(turn, kind);
  const el = messagesEl.querySelector(`[data-turn="${CSS.escape(turn)}"]`);
  if (!el) return;
  const tools = el.querySelector(".msg-tools");
  if (!tools) return;
  tools.querySelectorAll(".rating-chip").forEach((chip) => {
    // `none` is a withdrawal: it lights nothing.
    chip.classList.toggle("on", chip.classList.contains(`rating-${kind}`));
  });
  // A replayed reason seeds the draft, so editing after a reload starts from
  // what was written rather than from an empty box.
  if (comment) ratingDrafts.set(turn, comment);
  showRatingComment(el, kind === "none" ? "" : comment);
}

// A reason is written to the log and replayed with the rating, so it must be
// READ BACK on render too — otherwise the owner reopens a chat, sees a lit
// thumb with no words, and cannot tell what he objected to. That is worse than
// no comment at all: it looks like the note was lost.
function showRatingComment(el, comment) {
  const existing = el.querySelector(".rating-said");
  if (existing) existing.remove();
  if (!comment) return;
  const said = document.createElement("button");
  said.type = "button";
  said.className = "rating-said";
  said.title = "edit your note";
  said.textContent = comment;
  // Tapping it reopens the box, pre-filled: the thumbs toggle the verdict, so
  // without this there is no way to correct a note short of withdrawing it.
  said.onclick = () => {
    const chip = el.querySelector(".rating-chip.on");
    if (!chip) return;
    const kind = chip.classList.contains("rating-up") ? "up" : "down";
    said.remove();
    openRatingComment(chip, el.dataset.turn, kind, comment);
  };
  el.appendChild(said);
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
// Content-Disposition header — the server names the file from the content.
function dispositionName(response, fallback) {
  const cd = response.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i);
  try { return (m && decodeURIComponent(m[1])) || fallback; } catch { return fallback; }
}

async function exportAnswerPdf(markdown, btn) {
  // No title is sent: the server has THIS session's model title its own answer
  // (falling back to the answer's lead sentence), and that names both the
  // document and the download. The prompt titled the request, not the document.
  const query = new URLSearchParams();
  if (currentSession) query.set("session", currentSession);
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

function exportChip(getText) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip";
  btn.title = "export answer to PDF";
  btn.setAttribute("aria-label", "export answer to PDF");
  btn.appendChild(pdfIcon());
  btn.onclick = () => exportAnswerPdf(getText(), btn);
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
    act({ type: "fork", after: ordinal }, { label: "the fork" });
  };
  return btn;
}

// [ANSWER-TOP-START]
// Jump to the START of this answer. A long reply runs off the top of the screen
// while you read it, and finding where it began means scrolling back past its
// own body — hunting for a boundary that looks like all the other text. The
// control belongs ON the answer because "this answer" is what it means; it sits
// at the FAR RIGHT of the tool row, apart from the left-hand cluster, because
// it is navigation rather than something done to the answer.
//
// The target is measured against the SCROLLER, not the window: #messages is a
// sibling below #topbar, not underneath it, so an element sitting at the
// scroller's own top is already clear of the header. Subtracting the bar's
// height on top of that (which reads plausibly, and was the first version)
// overshoots by exactly the header and lands you above the answer.
const ANSWER_TOP_GAP = 8; // breathing room above the first line
function scrollToAnswerTop(el) {
  const delta = el.getBoundingClientRect().top - messagesEl.getBoundingClientRect().top;
  const top = Math.max(0, messagesEl.scrollTop + delta - ANSWER_TOP_GAP);
  // Smooth where the platform honours it; `scrollTo` falls back to a jump when
  // it does not, so the button can never be a no-op.
  messagesEl.scrollTo({ top, behavior: "smooth" });
}

// The chip is worth showing only when the answer is TALLER than the transcript
// viewport. If it fits, then whenever you can see its tool row you can already
// see its first line — so the button would scroll nowhere, which reads as
// broken rather than as "nothing to do". Height-vs-viewport is the whole test:
// scroll position never enters it, because a fitting answer whose bottom you
// are looking at necessarily has its top on screen too.
//
// Watched rather than measured once: images, fonts and the reading-size stepper
// all change an answer's height after it is built. Guarded because the
// choreography sandboxes have no ResizeObserver.
const answerFitWatcher = typeof ResizeObserver === "function"
  ? new ResizeObserver((entries) => { for (const e of entries) syncAnswerTopChip(e.target); })
  : null;

function syncAnswerTopChip(answerEl) {
  const chip = answerEl.querySelector(".answer-top");
  if (!chip) return;
  // Compared against the bare viewport height: an answer exactly as tall as the
  // viewport HAS its first line on screen when you can see its last, so there is
  // nothing to jump to. ANSWER_TOP_GAP is breathing room for the scroll target,
  // not part of "does this fit" — folding it in here would keep the chip on a
  // fitting answer just to scroll it eight pixels, which is the "feels like it
  // didn't work" this exists to remove.
  chip.hidden = answerEl.offsetHeight <= messagesEl.clientHeight;
}

function answerTopChip(el) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip answer-top";
  btn.title = "jump to the start of this answer";
  btn.setAttribute("aria-label", "jump to the start of this answer");
  btn.appendChild(svgIcon("i-top", (make, svg) => {
    const g = make("g", { fill: "none", stroke: "currentColor", "stroke-width": "1.8",
      "stroke-linecap": "round", "stroke-linejoin": "round" });
    g.appendChild(make("path", { d: "M5 4.5h14" }));       // the line it goes to
    g.appendChild(make("path", { d: "M12 20V8.6" }));
    g.appendChild(make("path", { d: "M7.6 13 12 8.6l4.4 4.4" }));
    svg.appendChild(g);
  }));
  btn.onclick = () => scrollToAnswerTop(el);
  return btn;
}
// [ANSWER-TOP-END]

// Footer row under a finished answer: copy-as-markdown chip, plus the
// read-aloud player where speech synthesis exists.
// When the live turn began, and 0 whenever no live turn is running — the live
// trace card reads it as its clock's origin, so a stale value would date a fresh
// card from a turn that already ended. Set on a live `user` event, cleared at
// every way a turn ends (done / stopped / error). A replayed turn deliberately
// sets none: its origin is reconstructed from the steps (accountStepTime).
let turnStart = 0;
let answerTiming = 0;

// Each rendered final answer gets an ordinal so its Fork button can branch the
// conversation up to and including that answer. Reset whenever the transcript
// is rebuilt (replay/history), so it stays aligned with the log's answer order.
let renderedAnswers = 0;

// The turn id a rating names (#207) is minted for the USER message (#202's
// removal id), and the answer is rendered separately — so it is carried
// forward here, exactly as `prompt` and `answerTiming` already are, and
// populated by both the live and the history-replay paths.
let currentTurnId = "";

function attachAnswerTools(el, source, prompt) {
  const ordinal = ++renderedAnswers;
  const tools = document.createElement("div");
  tools.className = "msg-tools";
  // Order (#96): TTS first, then export/fork, Retry, and Copy last — same tight
  // spacing as before, just reordered so the two most-used actions (TTS, Copy)
  // bracket the row instead of sitting next to Retry.
  if (TTS_OK) tools.appendChild(buildTtsBox(el));
  tools.appendChild(exportChip(() => source));
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
  if (currentTurnId) {
    el.dataset.turn = currentTurnId;
    tools.appendChild(ratingChip(currentTurnId, "up"));
    tools.appendChild(ratingChip(currentTurnId, "down"));
  }
  tools.appendChild(copyChip(() => source, "copy answer"));
  // The trailing group: readouts first, then jump-to-top LAST, hard against the
  // right edge. Grouped rather than letting each element claim its own auto
  // margin — two auto margins split the slack and would park the readout
  // mid-row — and, more importantly, because the chip must sit in the SAME
  // place on every answer. A control you reach for constantly cannot shift
  // left and right depending on whether that particular answer happened to
  // record a duration. See [ANSWER-TOP].
  const end = document.createElement("span");
  end.className = "tools-end";
  if (answerTiming) {
    const timing = document.createElement("span");
    timing.className = "answer-timing";
    timing.textContent = fmtSecs(answerTiming);
    end.appendChild(timing);
    answerTiming = 0; // one readout per answer
  }
  end.appendChild(answerTopChip(el));
  tools.appendChild(end);
  el.appendChild(tools);
  // Measured only once the row is IN the answer: run before that and the height
  // read back is the answer without its own tool row — and, for a fresh element,
  // often no layout at all, so a short answer kept a chip that did nothing.
  syncAnswerTopChip(el);
  if (answerFitWatcher) answerFitWatcher.observe(el); // …and again as it grows
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
  if (currentTrace) {
    currentTrace.waitingApproval = true;
    updateTraceHead(currentTrace);
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
  const card = cards.get(id);
  const controls = card ? [...card.querySelectorAll("button, input, textarea")] : [];
  // Greying the card is a claim that the GATE has your answer, and the gate is
  // a worker thread parked on a slot that only this message fills. Unreceipted,
  // that claim strands the agent holding a command with a card that looks
  // answered and no way back to it — so the disabling is undone with it
  // ([ACK-LEDGER]). Nothing here re-sends: `bridge.answer` drops a duplicate,
  // but a second verdict the user did not give is not this layer's to invent.
  act({ type: "approval", id, action, ...extra }, {
    label: "your answer to the approval",
    lost: () => { for (const c of controls) c.disabled = false; },
  });
  for (const c of controls) c.disabled = true;
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
  // A rule is not a file, to the person approving it. Calling it one made the
  // owner think aish had bypassed its own rule tools and hand-written YAML —
  // he was deciding about a behaviour and the card showed him a text edit.
  const isRule = !!event.rule;
  htitle.textContent = isRule
    ? ({ Created: "New rule", Updated: "Rule change", Retired: "Retire rule" }[
        event.rule_verb
      ] || "Rule")
    : event.verb === "create" ? "Create file" : "Edit file";
  const hsub = document.createElement("span");
  hsub.className = isRule ? "card-hsub" : "card-hsub mono";
  hsub.textContent = isRule ? event.rule : relTarget(event.target);
  hsub.title = event.target; // full path on hover
  htext.append(htitle, hsub);
  head.append(ico, htext);
  if (!isRule) {
    // Line counts describe a text edit. For a rule they measure the wrong
    // thing entirely — nobody decides about a rule by how many lines it is.
    const added = document.createElement("span");
    added.className = "card-count add";
    added.textContent = `+${event.added}`;
    const removed = document.createElement("span");
    removed.className = "card-count del";
    removed.textContent = `−${event.removed}`;
    head.append(added, removed);
  }
  card.appendChild(head);

  // The compiled meaning, when the harness sent one (a rule write). It goes
  // ABOVE the diff because the diff is YAML the owner did not write, and what
  // he is agreeing to is the behaviour it describes.
  if (event.note) {
    const note = document.createElement("div");
    note.className = "card-note";
    note.textContent = event.note;
    card.appendChild(note);
  }

  const diff = renderDiff(event.diff || "");
  if (isRule) {
    // Folded away, not removed. The file is still the truth and anyone who
    // wants it is one tap from it — but it is the second thing, not the first.
    const fold = document.createElement("details");
    fold.className = "card-fold";
    const label = document.createElement("summary");
    label.textContent = "Show the file";
    fold.append(label, diff);
    card.appendChild(fold);
  } else {
    card.appendChild(diff);
  }

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
  if (pendingCards === 0 && currentTrace) {
    currentTrace.waitingApproval = false;
    updateTraceHead(currentTrace);
    if (currentTrace.autoCollapsed) {
      currentTrace.el.classList.add("open");
      currentTrace.autoCollapsed = false;
      pinTrace(currentTrace);
    }
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
loadHeldSends(); // anything a dead tab was holding ([PENDING-SEND])
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

// [ATTACHMENT-NOTES-START]
// The server appends a line per attachment to the text of the user's turn
// ("[image attached: cat.png — you can see it; file at /…/uploads/cat.png]").
// That line is written FOR THE MODEL — it is how a backend without native
// vision is told a file exists, and how one with vision is told it may look.
// It was also rendered verbatim in the blue bubble, so sending a photo showed
// the owner a sentence addressed to somebody else, ending in an absolute path.
//
// This is the ONE place that knows the note format. The prose is not a display
// string that leaked; it IS the record — it goes into the model conversation
// and into the session log, so `reconstruct_events` replays a cold session by
// handing back that same text. A structured field on the event would describe
// only turns logged after today and this parser would still be needed for
// every older one, which is two owners of one fact. So: parse once, here, and
// let both the live path and the replay path render from the result.
function parseAttachmentNote(line) {
  const native =
    /^\[(image|document) attached: (.+?) — you can (?:see|read) it; file at (.+)\]$/
      .exec(line.trim());
  if (native) return { kind: native[1], name: native[2], path: native[3] };
  const plain = /^\[attached file: (.+)\]$/.exec(line.trim());
  if (plain) {
    return { kind: "file", name: plain[1].split("/").pop() || plain[1], path: plain[1] };
  }
  return null;
}

// {body, attachments} — what the owner wrote, and what they attached.
function splitAttachmentNotes(text) {
  const body = [];
  const attachments = [];
  for (const line of String(text == null ? "" : text).split("\n")) {
    const note = parseAttachmentNote(line);
    if (note) attachments.push(note);
    else body.push(line);
  }
  return { body: body.join("\n").trim(), attachments };
}

function stripAttachmentNotes(text) {
  return splitAttachmentNotes(text).body;
}
// [ATTACHMENT-NOTES-END]

function rememberPrompt(text) {
  if (text && promptHistory[promptHistory.length - 1] !== text) promptHistory.push(text);
  if (promptHistory.length > 100) promptHistory.shift();
  historyIndex = null;
}

// How many lines the composer is actually rendering. Measured rather than
// compared against a pixel constant, because the reading-size stepper (#118)
// scales this font — a fixed threshold means "two lines" at one setting and
// "three" at another. Used to decide when the field takes a row of its own.
function composerLines() {
  const cs = getComputedStyle(input);
  const line = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.35 || 24;
  const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  return Math.max(1, Math.round((input.scrollHeight - pad) / line));
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.24)}px`;
  // The MOMENT it wraps to a second line, let the field take the full composer
  // width with the buttons tucked onto a row below (#97) — a narrow multi-line
  // box beside the buttons wastes the screen, and the in-field clear button had
  // nowhere sensible to sit in a two-line box that was still sharing its row.
  // Not in terminal mode.
  //
  // Sticky (#114): going full-width makes the box wider, so the SAME text wraps
  // to fewer lines and scrollHeight drops back under the threshold — a bare
  // scrollHeight>72 test then flip-flops tall/short on every keystroke. Once
  // tall, stay tall until the input is fully cleared (or terminal mode takes
  // over), so it only collapses back when you've emptied it.
  const composer = $("composer");
  const stayTall = composer.classList.contains("tall") && input.value !== "";
  composer.classList.toggle("tall", !cmdMode && (composerLines() > 1 || stayTall));
  // The in-field clear button (and the padding that keeps it off the text)
  // rides on the same signal, so it can never disagree with the box.
  composer.classList.toggle("has-text", input.value !== "");
  updateEmptyHint(); // draft state gates the empty-chat hint (#132)
}

// [COMPOSER-SLOT-START]
// Two composer affordances, split by WHAT THEY ACT ON — the principle worth
// keeping when the next one is added:
//
//   the FIELD owns its own content  → clearing is a control inside the field
//                                     (the platform's clear button, where
//                                     everyone already looks), shown only when
//                                     there is something to clear
//   the ROW owns actions on the message → pasting is a button beside attach,
//                                     dictate and send, and is ALWAYS there
//
// They were one shared slot first (paste when empty, clear when not) and that
// read as clever until it met the actual flow: you type a few words and THEN
// paste — "review this: <url>", "what is this?" + the text — and a shared slot
// hides paste at precisely that moment. Mutually exclusive in the UI is not the
// same as mutually exclusive in use.
//
// Reading the clipboard on an explicit tap is also the user gesture the
// permission model wants, which is why this is a button and not something
// automatic.
function clearComposer() {
  if (input.value === "") return;
  input.value = "";
  saveDraft();
  resizeInput();
  input.focus();
}

async function pasteIntoComposer() {
  // On a phone there is no Cmd+V, so this button is the ONLY way the clipboard
  // reaches the composer — which is why it has to handle a copied IMAGE too,
  // not just text. read() gives both; readText() below is the fallback for
  // browsers without it, and it can only ever see text.
  if (navigator.clipboard && navigator.clipboard.read) {
    try {
      let text = "";
      const files = [];
      for (const item of await navigator.clipboard.read()) {
        const imageType = (item.types || []).find((t) => t.startsWith("image/"));
        if (imageType) {
          const blob = await item.getType(imageType);
          files.push(new File([blob], "", { type: imageType }));
        } else if ((item.types || []).includes("text/plain")) {
          text += await (await item.getType("text/plain")).text();
        }
      }
      for (const file of files) await uploadFile(file);
      if (text) composerInsert(text); // same insertion path the @/slash triggers use
      if (!files.length && !text) showToast("clipboard is empty");
      return;
    } catch {
      // Refused, or a clipboard this browser will not describe. Fall through:
      // readText is a narrower permission and often still answers.
    }
  }
  // navigator.clipboard is unavailable on insecure origins and can be refused;
  // say so rather than appearing to do nothing. The keyboard shortcut and the
  // tap-hold menu both still work, so this is a convenience, never the only way.
  try {
    const text = await navigator.clipboard.readText();
    if (!text) { showToast("clipboard is empty"); return; }
    composerInsert(text); // same insertion path the @/slash triggers use
  } catch {
    showToast("can't read the clipboard here — use tap-and-hold to paste");
  }
}
// [COMPOSER-SLOT-END]

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
  ["/delete", "open a chat, then Delete chat in its title menu"],
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
// [ENTER-START]
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
      // Re-completing an already-exact command would be a no-op, so Enter falls
      // through instead: to the terminal-mode run below, or — in prose — to
      // nothing but closing the popup (#170: only the send button submits).
      const exact = (suggest.kind === "slash" || suggest.kind === "cmd")
        && chosen[0] === input.value.trim();
      if (e.key === "Tab" || !exact) {
        e.preventDefault();
        acceptSuggestion(chosen);
        return;
      }
      if (!cmdMode) { e.preventDefault(); hideSuggest(); return; }
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
  // Enter submits ONLY in terminal mode — that mode is a shell prompt and
  // running the command is the whole point of it (#100). Prose messages are
  // sent by the send button alone (#170): a newline is the common intent, and
  // autocorrect/IME/dictation all produce Returns that used to fire a
  // half-written message. Don't "fix" this asymmetry back into symmetry.
  if (e.key === "Enter" && !e.shiftKey && cmdMode) {
    e.preventDefault();
    submitInput();
  }
}

// iOS soft keyboards don't fire a reliable Enter keydown on a <textarea> — the
// return key arrives as a beforeinput/insertLineBreak. In terminal mode that
// key must RUN the command like a real shell, not drop a newline. Desktop is
// handled by the keydown above (which cancels this default), so no double-run.
// Prose is untouched here for the same reason as above — the newline stands.
function onInputBeforeInput(e) {
  if (cmdMode && e.inputType === "insertLineBreak") {
    e.preventDefault();
    submitCommand();
  }
}
// [ENTER-END]

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
  el.addEventListener("paste", onInputPaste);
}
attachInputListeners(input);
installFileDrop(window, document.body);

// The preview's own input. The ✕ is a plain button; everything else is
// pointer-driven ([PREVIEW-GESTURE]) — a click handler alongside it would fire
// a second time at the end of every drag and tap.
$("preview-close").onclick = closePreview;
$("preview").addEventListener("pointerdown", previewDown);
$("preview").addEventListener("pointermove", previewMove, { passive: false });
$("preview").addEventListener("pointerup", previewUp);
$("preview").addEventListener("pointercancel", previewUp);
$("preview").addEventListener("wheel", previewWheel, { passive: false });
// WebKit-only, and iOS is exactly where the pointer-pair pinch does not fire.
$("preview").addEventListener("gesturestart", previewGestureStart, { passive: false });
$("preview").addEventListener("gesturechange", previewGestureChange, { passive: false });
$("preview").addEventListener("gestureend", previewGestureEnd, { passive: false });

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
    // [PENDING-SEND] The bubble is drawn NOW, not when the server echoes the
    // turn back — the gap between the two is the whole defect. A `!` command is
    // deliberately exempt: its terminal block already carries the command, and
    // a bubble would be the duplicate #154 removed.
    if (!text.startsWith("!")) addPendingSend(text);
    if (pendingSpeak) { speakNextReply = true; pendingSpeak = false; } // dictated → speak reply
    maybeRequestNotifyPermission();
    input.value = "";
    localStorage.removeItem("aish-draft");
    resizeInput(); // #117: recompute height AND drop the .tall class now it's empty
    releaseSentShares(attachments); // shared items are spent when they are SENT
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
    case "/resume": case "/delete": openSessionRail(arg); return true;
    case "/new": case "/clear": return act({ type: "new" }, { label: "the new chat" });
    case "/fork": case "/branch": return act({ type: "fork" }, { label: "the fork" });
    case "/cd": return arg ? act({ type: "cd", path: arg }, { label: "the directory change" }) : (openDirSheet(), true);
    case "/add-dir": case "/dir-add":
      return arg ? act({ type: "add_dir", path: arg }, { label: "adding that directory" }) : (openSheet("workspace-sheet"), true);
    case "/learn": case "/feedback": {
      // Run as a task: the server swaps the text for the expanded prompt
      // (cli.parse_learn / parse_feedback) while the transcript shows what was
      // typed. Include attachments — /feedback WITH files uses the classic
      // upload flow, and without this they were silently dropped (#152).
      const sent = send({ type: "task", text, attachments: attachments.map((a) => a.path) });
      if (sent) {
        releaseSentShares(attachments);
        attachments = [];
        renderAttachments();
        scrollToEndSettled();
      }
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

// The one-time xterm load's own status text.
const CONSOLE_LOADING = "loading terminal…";

// Has the server's console_started — the only source of the REAL status label —
// landed for the open currently in progress? openConsole sends console_open
// FIRST and only then writes its provisional "attaching…"/"loading terminal…"
// text, so on a fast link the reply beats those writes and they clobber the real
// label. Nothing rewrites it afterwards (console_started fires once per open), so
// a placeholder then sat over a fully working terminal. Provisional text is
// therefore only ever written while this is false.
let consoleStartedSeen = false;

function setProvisionalConsoleStatus(text) {
  if (!consoleStartedSeen) setConsoleStatus(text);
}

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

// [XTERM-LAZY-START]
// xterm.js (~285 KB) is the terminal emulator — needed ONLY when the console
// opens, which most sessions never do. Keeping it off the boot path is the big
// half of the white-screen fix (#180): it no longer downloads, parses, or
// executes before the first chat can paint. These assets are loaded on demand
// the first time the console opens, stamped with the page rev so a device
// caches them immutably and a deploy busts them (the old static <script> tags
// were unversioned — a known stale-after-update gap).
function xtermAssetUrls(base, rev) {
  const v = rev ? `?v=${rev}` : "";
  return {
    css: `${base}vendor/xterm.css${v}`,
    js: [`${base}vendor/xterm.js${v}`, `${base}vendor/xterm-addon-fit.js${v}`],
  };
}

let xtermReady = null; // memoized: load at most once per page
function ensureXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (xtermReady) return xtermReady;
  const urls = xtermAssetUrls(BASE, PAGE_REV);
  if (!document.querySelector('link[data-xterm-css]')) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = urls.css;
    link.dataset.xtermCss = "1";
    document.head.appendChild(link);
  }
  const loadScript = (src) =>
    new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.appendChild(s);
    });
  // Sequential: the fit addon references the Terminal global from xterm.js.
  xtermReady = urls.js
    .reduce((chain, src) => chain.then(() => loadScript(src)), Promise.resolve())
    .catch((err) => {
      xtermReady = null; // let a later open retry a transient failure
      throw err;
    });
  return xtermReady;
}
// [XTERM-LAZY-END]

async function openConsole() {
  closeSheets();
  // Attach is requested LAST, once the terminal exists (see the send at the end
  // of this function). Sending console_open up here raced the server's
  // console_started reply — the ONLY source of the real cwd/command label — ahead
  // of consoleTerm being built; onConsoleStarted drops any event that lands before
  // the terminal exists, so the reply was lost and "attaching…" sat over a working
  // console forever. The lazy-xterm await widened that window. Building the
  // terminal before requesting attach closes the race at its root (the
  // consoleStartedSeen guard below is now just belt-and-suspenders). Here we only
  // CHECK the socket is live — no send yet.
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showToast("not connected — reconnecting…");
    return;
  }
  if (consoleOpen) return; // already showing (e.g. a reconnect reattach)
  // BELOW the early returns, deliberately: the flag's invariant is "false only
  // while an open is in progress", and a call that opens nothing starts no open.
  // Armed above, a no-op call (already open, or no socket) disarmed the guard
  // and never re-armed it — console_started fires once per open, so from then on
  // any provisional write could clobber a real label permanently (#181).
  consoleStartedSeen = false; // a new open: no real label for it yet
  if (location.hash !== "#console") history.replaceState(null, "", "#console"); // deep-link: survives reload/restart
  $("pty-overlay").hidden = false;
  // Freeze the page behind the overlay at its current scroll offset (iOS: a
  // position:fixed body is the only reliable lock; restored on close).
  consoleLockY = window.scrollY || 0;
  document.body.style.top = `-${consoleLockY}px`;
  document.body.classList.add("console-open");
  $("pty-share").hidden = true;
  setProvisionalConsoleStatus("attaching…");

  const screen = $("pty-screen");
  screen.textContent = "";
  // First open on this page loads the emulator (lazy — see ensureXterm). A
  // reopen resolves instantly. If it fails (offline mid-open), surface it and
  // leave the overlay recoverable rather than throwing into a half-open state.
  if (!window.Terminal || !window.FitAddon) {
    setProvisionalConsoleStatus(CONSOLE_LOADING);
    try { await ensureXterm(); }
    catch { setConsoleStatus("couldn't load the terminal — check your connection"); return; }
    if (!consoleOpen && $("pty-overlay").hidden) return; // closed while loading
    // Emulator ready: drop the loading text — unless console_started has since
    // landed with the real label, which the guard inside this helper honours.
    setProvisionalConsoleStatus("attaching…");
  }
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
  // Terminal is fully built and its handlers wired — NOW request attach. The
  // server's console_started/console_out replies can only arrive after this, so
  // consoleTerm is guaranteed present when onConsoleStarted runs and the real
  // label always lands (fixing the stuck "attaching…"). On a reconnect the
  // re-attach is sent from onHello instead, where the terminal already exists.
  send({ type: "console_open" });
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
  // First console open of this page load: if an unsent dictation survived the
  // reload (app update, PWA relaunch), bring the pad back with it — without
  // starting the mic. Once consumed, later opens leave the pad closed unless
  // you ask for it; the draft is still restored when you do open it.
  if (padRestorePending) {
    padRestorePending = false;
    if (padDraft()) openConsolePad(false);
  }
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
  consoleStartedSeen = true; // the real label has landed; stop writing placeholders
  // Header shows the console's identity (e.g. "tmux · aish-console"), NOT a cwd.
  // The server only knows the DEFAULT session's workspace, not the tmux pane's
  // real directory, so on a reattach the old cwd label read ~/aish while the
  // shell was elsewhere — and it went stale the moment you cd'd anyway. The
  // terminal's own prompt is the live, correct source of the cwd.
  setConsoleStatus(event.command);
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
  // Taking the chip away says the message will not be sent. Unreceipted that is
  // the most expensive lie in this file: the queue belongs to an agent that
  // runs shell commands, so a cancel that never landed means the thing you
  // withdrew RUNS, with nothing on screen still naming it. The chip goes back
  // ([ACK-LEDGER]) — and if the dequeue did land after all, the server's own
  // queue repaint drops it again on the next replay.
  const dequeue = () => act({ type: "dequeue", text }, {
    label: "cancelling that queued message",
    lost: () => addQueueChip(text),
  });
  chip.querySelector(".queue-remove").onclick = () => {
    dequeue();
    removeQueueChip(text);
  };
  // Edit: pull the message back into the composer to revise & resend (#14).
  chip.querySelector(".queue-edit").onclick = () => {
    dequeue();
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

// Every chip in the queue area belongs to ONE chat: it names a message that
// chat's agent is holding, and its Remove button dequeues from whatever session
// this client is viewing. So the whole area goes with the transcript.
//
// It used not to, and only for the message chips — the cwd card was already
// dropped here. A chip drawn while chat A was on screen therefore SURVIVED the
// switch to B and sat above B's composer, where "Remove" dequeued from B (a
// no-op) while A's message went on to run, and "Edit" pulled the text into B's
// composer while A still held its copy — one send away from running it twice,
// in two different chats. The server now re-sends the real queue on attach, so
// clearing here loses nothing: what comes back is the queue of the chat you
// actually landed in.
function clearQueueChips() {
  const list = $("queue-list");
  list.replaceChildren();
  list.hidden = true;
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
      // Same claim, and this one moves the APPROVAL ROOT when it applies.
      act({ type: "dequeue_cwd" }, {
        label: "cancelling that directory change",
        lost: () => addCwdChip(path),
      });
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

// [COMPOSER-FILES-START]
// Three ways a file reaches the composer, one destination: the ＋ picker, a
// PASTE, and a DROP. The picker was the only one for a long time, which on a
// desktop is the slowest possible way to hand over the screenshot you just
// took — the clipboard already holds it.
//
// What a file is CALLED is decided here, once, for all three. A pasted
// screenshot arrives as a Blob with an empty name (and Chrome's "image.png" is
// barely better), while /upload refuses an empty or dot-leading name — so a
// nameless paste would have failed with "invalid file name" and looked like the
// paste itself was unsupported.
function uploadName(file) {
  const given = (file.name || "").trim();
  if (given && !given.startsWith(".") && given !== "image.png") return given;
  const ext = ((file.type || "").split("/")[1] || "bin")
    .replace(/[^a-z0-9]/gi, "").slice(0, 5) || "bin";
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  return `pasted-${stamp}.${ext}`;
}

// The files on a clipboard or a drag, from either of the two APIs that carry
// them (`.files` everywhere current; `.items` for the Safari versions that only
// populate that one).
function transferFiles(data) {
  if (!data) return [];
  if (data.files && data.files.length) return Array.from(data.files);
  return Array.from(data.items || [])
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

// Paste is ADDITIVE and never calls preventDefault. Copying a chart out of a
// spreadsheet, or an image off a web page, puts BOTH an image and text on the
// clipboard; swallowing the event to take the image would silently drop the
// text the owner may well have been after. So the files become attachments and
// the browser's own text paste still runs — nothing on the clipboard is lost.
// (The composer is a <textarea>, where a file paste has no default behaviour of
// its own, so there is nothing to suppress.)
async function onInputPaste(event) {
  for (const file of transferFiles(event.clipboardData)) await uploadFile(file);
}
// [COMPOSER-FILES-END]

// [FILE-DROP-START]
// Dropping a file onto a web page NAVIGATES to it by default — the chat would
// simply disappear, replaced by the image, with the draft gone. So every
// dragover carrying files is prevented; that is what makes the drop ours.
// Bound on the window rather than the composer: aiming at a one-line box is a
// worse target than the whole conversation, and the highlight says where it
// will land either way.
function fileDrag(event) {
  return Array.from(event.dataTransfer ? event.dataTransfer.types || [] : [])
    .includes("Files");
}

function installFileDrop(target, body) {
  let depth = 0; // dragenter/leave fire per element crossed, not per window
  const show = (on) => body.classList.toggle("dropping", on);
  target.addEventListener("dragenter", (event) => {
    if (!fileDrag(event)) return;
    depth++;
    show(true);
  });
  target.addEventListener("dragover", (event) => {
    if (fileDrag(event)) event.preventDefault();
  });
  target.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) show(false);
  });
  target.addEventListener("drop", async (event) => {
    depth = 0;
    show(false);
    if (!fileDrag(event)) return;
    event.preventDefault();
    for (const file of transferFiles(event.dataTransfer)) await uploadFile(file);
  });
}
// [FILE-DROP-END]

async function uploadFile(file) {
  const query = new URLSearchParams({ name: uploadName(file) });
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
  // Name it from the path the SERVER chose: a second cat.png is stored as
  // cat-1.png, and a chip still reading "cat.png" would name the wrong file.
  attachments.push({ name: path.split("/").pop() || uploadName(file), path });
  renderAttachments();
}

// [SHARES-START]
// What the iPhone share sheet handed over. The server holds the inbox and is
// the only writer of it; this reconciles the composer against whatever the last
// `hello`/`shared` said and never keeps its own copy — two devices claiming
// from one inbox is the normal case (phone shares it, laptop uses it), and a
// local list would drift the moment the other one claimed something.
//
// A shared FILE is attached to your next message straight away. It used to be a
// chip you had to TAP first, on the theory that a share landing mid-sentence
// must not join a message you were already writing. That theory cost a real
// send: the strip sits in the composer's attachment zone, so it reads as
// already attached — you type a prompt, press send, and only the words go. The
// file was still sitting in the inbox, correctly, and completely uselessly.
//
// The safety property was never about the tap. It is that a share starts
// NOTHING: no session, no model call. That is enforced on the server and is
// untouched by attaching it here — you still write the prompt, you still press
// send, and the ✕ still takes it back out.
//
// Shared TEXT stays a chip, because there is no equivalent of ✕ for text that
// has appended itself to a half-written sentence.
//
// Consumed on SEND, not on attach: close the tab without sending and the item
// is still waiting next time, rather than silently spent.
let sharesPainted = false; // a later arrival is news; the first paint is not

function renderShares(items) {
  // The server's list is the truth, including "the other device used it".
  const live = new Set(items.map((item) => item.id));
  const kept = attachments.filter((a) => !a.share || live.has(a.share));
  let touched = kept.length !== attachments.length;
  attachments = kept;

  for (const item of items) {
    if (item.path && !attachments.some((a) => a.share === item.id)) {
      attachments.push({
        name: item.name || item.path.split("/").pop(),
        path: item.path,
        share: item.id, // what makes it releasable on send, and revocable above
      });
      touched = true;
      if (sharesPainted) {
        showToast(`${item.name} attached — shared from ${item.source || "your phone"}`);
      }
    }
    if (item.text) takeSharedText(item);
  }
  if (touched) renderAttachments();
  sharesPainted = true;
  openChatForFreshShares(items);
}

// Shared TEXT goes into the composer, appended at the END with a separator.
//
// Not at the cursor, and without stealing focus: this is not something the
// owner just asked for with a keystroke, it is an arrival, and inserting at the
// cursor could split a word while focusing would throw the keyboard up on a
// phone unasked. Appending cannot destroy anything — whatever was already there
// is still there, with the link after it.
//
// It is consumed IMMEDIATELY, unlike a file. The asymmetry is not an oversight:
// a file lives in an attachment chip that is NOT persisted, so it has to stay
// in the inbox until the message is actually sent or a reload would lose it,
// while text lands in the draft, which IS persisted. One principle — consume it
// once it is somewhere it cannot be lost — and two answers.
//
// The ledger is what keeps a repaint from appending the same link twice: `hello`
// repeats the inbox on every connect, and a `share_drop` that never lands leaves
// the item exactly where it was.
const textTaken = new Set();

function takeSharedText(item) {
  if (textTaken.has(item.id)) return;
  // Terminal mode's composer is a shell command line; a URL appended to it
  // would be nonsense. Leave the item in the inbox — the next repaint, or the
  // next time the app is opened, finds it again.
  if (cmdMode) return;
  textTaken.add(item.id);
  composerAppend(item.text);
  if (sharesPainted) {
    showToast(`link from ${item.source || "your phone"} added`);
  }
  dropShare(item.id);
}

function composerAppend(text) {
  // On its OWN LINE, not after a space: a shared link is somebody else's words
  // arriving in the middle of yours, and a line of its own is what makes the
  // two tellable apart at a glance in a long prompt. Trailing spaces are taken
  // with it, or the line you were writing keeps an invisible tail.
  const current = input.value.replace(/[ \t]+$/, "");
  const gap = !current || current.endsWith("\n") ? "" : "\n";
  input.value = current + gap + text;
  // The input event is what saves the draft, re-measures the box (a second line
  // makes it taller) and updates the suggestion popover — everything a
  // keystroke would have done.
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

// A share posted with `chat=new` wants its own conversation instead of landing
// on top of whatever was last open. It rides on the ITEM rather than on the
// launch URL because iOS will not open an installed web app at an address of
// your choosing — `webapp://…/?new` starts the app and drops the query — so
// `?new` ([OPEN-NEW]) can only ever work for a browser tab or a second Home
// Screen icon. This works however the app was opened.
//
// The attachment is NOT re-done afterwards: attachments deliberately survive a
// session switch, so whatever was just attached rides into the new chat. That
// also makes this failure-tolerant — if the new chat never arrives, the file is
// still attached to the chat you are in, which is the old behaviour and not a
// loss.
const freshHonoured = new Set(); // ids already acted on — a hello repeats them

function openChatForFreshShares(items) {
  const fresh = items.filter((item) => item.fresh && !freshHonoured.has(item.id));
  if (!fresh.length) return;
  // ALL of them are marked, and at most ONE chat is opened: sharing three
  // photos then opening aish means one new chat holding three, not three chats
  // holding one each and two of them empty.
  for (const item of fresh) freshHonoured.add(item.id);
  // Nothing to leave behind: opening a new chat from an unused one just leaves
  // an empty row in the rail and looks like the button misfired.
  if (transcriptIsEmpty()) return;
  requestNewChat();
}

// The server owns the inbox, so removal is a request, not a local splice. The
// repaint arrives as the `shared` broadcast that follows — including on the
// OTHER device, whose copy of this chip must go too.
function dropShare(id, lost) {
  act({ type: "share_drop", id }, { label: "clearing that shared item", lost });
}

// A message that has gone consumes the shared items it carried. Plain `send`,
// not `act`: if this is the request that goes missing, the item stays in the
// inbox and is offered again — which is precisely what "we don't know whether
// it was used" should look like. The opposite failure, quietly spending a
// share that never went anywhere, is the one with nothing on screen to notice.
function releaseSentShares(sent) {
  for (const attachment of sent) {
    if (attachment.share) send({ type: "share_drop", id: attachment.share });
  }
}
// [SHARES-END]

// Which attachments can be shown as a picture. Deliberately the same set the
// server will deliver natively (backends.IMAGE_SUFFIXES) — /file answers 415
// for anything else, and a chip that asked for one would flash a broken image.
const ATTACH_IMAGE_RE = /\.(png|jpe?g|gif|webp)$/i;

function renderAttachments() {
  const box = $("attachments");
  box.replaceChildren();
  box.hidden = !attachments.length;
  attachments.forEach((attachment, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    // A thumbnail, not just a filename — and it does two jobs. You can see
    // WHICH photo is about to go (a name like IMG_4021.jpg identifies nothing),
    // and the bytes are fetched NOW, while you are still typing, through the
    // same /file URL the sent bubble will use. That second job is why the photo
    // appears the instant you press send: on a phone over a tunnel a
    // multi-megabyte original took seconds to arrive when the bubble asked for
    // it for the first time, and there is no reason for that wait to land on
    // the one moment you are watching for a result.
    const src = ATTACH_IMAGE_RE.test(attachment.path || "") ? imageSrc(attachment.path) : null;
    if (src) {
      const thumb = document.createElement("img");
      thumb.className = "attach-thumb";
      thumb.src = src;
      thumb.alt = "";
      // A file that will not render must not leave a broken-image glyph in the
      // composer: drop back to the name, which is still true.
      thumb.onerror = () => thumb.remove();
      chip.appendChild(thumb);
    }
    const label = document.createElement("span");
    label.className = "attach-name";
    label.textContent = shortName(attachment.name);
    chip.appendChild(label);
    chip.title = attachment.name; // the full name is always one hover away
    if (src) {
      chip.classList.add("attach-openable");
      chip.onclick = (event) => {
        if (event.target.closest("button")) return; // ✕ is not "open it"
        openPreview(src, attachment.name);
      };
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "✕";
    remove.onclick = () => {
      const [gone] = attachments.splice(i, 1);
      // Taking a SHARED item out is a dismissal, and the server has to hear it:
      // otherwise the next repaint of the inbox puts it straight back, and the
      // ✕ reads as broken.
      if (gone && gone.share) dropShare(gone.share);
      renderAttachments();
    };
    chip.appendChild(remove);
    box.appendChild(chip);
  });
}

// [PREVIEW-START]
// Tap a picture, see the picture. It opens from three places — a composer
// attachment chip, an attachment on a sent message, and an inline image the
// model produced — and all three used to do something else or nothing: the
// composer chip was inert, and a thumbnail opened a NEW TAB, which from an
// installed PWA means leaving the app for Safari to look at your own photo.
//
// Deliberately NOT a dismiss-on-tap-the-image overlay: long-press on the image
// is how iOS offers Save Image, and a tap handler there makes that fiddly. The
// backdrop, the ✕ and Escape all close it.
function openPreview(src, name) {
  const box = $("preview");
  if (!box) return;
  $("preview-img").src = src;
  $("preview-img").alt = name || "";
  $("preview-name").textContent = name || "";
  box.hidden = false;
  // Every picture opens at fit. Inheriting the last one's zoom would show a
  // new photo already halfway into a corner ([PREVIEW-GESTURE]).
  previewReset();
}

function closePreview() {
  const box = $("preview");
  if (!box || box.hidden) return false;
  box.hidden = true;
  // Drop the bytes' claim on memory, and make sure a stale picture can never
  // flash when the next one is opened.
  $("preview-img").removeAttribute("src");
  previewReset();
  return true;
}

function previewIsOpen() {
  const box = $("preview");
  return !!box && !box.hidden;
}
// [PREVIEW-END]

// [PREVIEW-GESTURE-START]
// The gestures every photo viewer has, and which one without them is judged
// against: double-tap to zoom (and again to fit), drag to pan while zoomed,
// pinch to zoom by hand, and — at fit — a drag DOWN that carries the picture
// with your finger and lets go of it.
//
// The transform is modelled as pure functions over {scale, x, y} so the maths
// can be checked without a browser: the interesting failures here are not
// "does it move" but "does it move to the RIGHT place" — a zoom that does not
// keep the tapped detail under the finger, or a pan that lets the picture
// wander off into the black.
//
// Coordinates are relative to the IMG's own box (not the viewport), and the
// origin is its centre, which is where a CSS transform scales from.
const PREVIEW_ZOOM = 2.5;      // what a double-tap goes to; Photos-ish
const PREVIEW_MAX_ZOOM = 6;
const PREVIEW_DISMISS_PX = 100; // drag further than this at fit and it closes
const PREVIEW_TAP_MS = 300;     // two taps inside this are a double-tap
const PREVIEW_TAP_SLOP = 24;    // …and within this distance of each other

// The picture as actually drawn at scale 1. `object-fit: contain` letterboxes
// it, and panning must be bounded by the PICTURE rather than by the element —
// bounding by the element lets you drag the image off into the empty margin,
// which feels broken in a way that is hard to name.
function previewContent(view, natural) {
  if (!natural.w || !natural.h || !view.w || !view.h) return { w: view.w, h: view.h };
  const ratio = natural.w / natural.h;
  const w = Math.min(view.w, view.h * ratio);
  return { w, h: w / ratio };
}

// Keep the picture covering the screen: no offset that would show black where
// the picture could be. Below fit scale there is nothing to pan, so it centres.
function previewClamp(state, view, natural) {
  const content = previewContent(view, natural);
  const maxX = Math.max(0, (content.w * state.scale - view.w) / 2);
  const maxY = Math.max(0, (content.h * state.scale - view.h) / 2);
  return {
    scale: state.scale,
    x: Math.min(maxX, Math.max(-maxX, state.x)),
    y: Math.min(maxY, Math.max(-maxY, state.y)),
  };
}

// Scale about `point`, leaving whatever is under it exactly where it is. This
// is the difference between "zoom" and "zoom to the middle and hunt for the bit
// you wanted": double-tapping a face should enlarge THAT face.
function previewZoomAt(state, scale, point, view, natural) {
  scale = Math.min(PREVIEW_MAX_ZOOM, Math.max(1, scale));
  const k = scale / state.scale;
  return previewClamp({
    scale,
    x: (point.x - view.w / 2) * (1 - k) + state.x * k,
    y: (point.y - view.h / 2) * (1 - k) + state.y * k,
  }, view, natural);
}

// A double-tap toggles: zoomed in anywhere goes back to fit, which is what
// every viewer does and saves a pinch-out to escape.
function previewToggleZoom(state, point, view, natural) {
  const target = state.scale > 1.01 ? 1 : PREVIEW_ZOOM;
  return previewZoomAt(state, target, point, view, natural);
}

// A drag at fit scale is a dismissal in progress: the picture follows the
// finger and the background fades, so it is obvious what letting go will do.
// Once ZOOMED the same drag is a pan instead — a picture you are examining
// must not fall out of the window because you looked at its bottom edge.
function previewDrag(state, delta, view, natural) {
  if (state.scale > 1.01) {
    return {
      ...previewClamp({ scale: state.scale, x: state.x + delta.x, y: state.y + delta.y },
        view, natural),
      dismissing: 0,
    };
  }
  // Only downward carries the picture: sideways at fit does nothing (there is
  // no next photo to page to), and upward is left alone.
  const down = Math.max(0, delta.y);
  return { scale: 1, x: 0, y: down, dismissing: down / PREVIEW_DISMISS_PX };
}

function previewDragEnds(state) {
  return state.scale <= 1.01 && state.y >= PREVIEW_DISMISS_PX;
}

// Two fingers: scale by how much they spread, about the point between them.
function previewPinch(state, startState, ratio, point, view, natural) {
  return previewZoomAt(startState, startState.scale * ratio, point, view, natural);
}
// [PREVIEW-GESTURE-END]

// The DOM half of the preview gestures. Everything above is arithmetic; this
// is the part that has to know about fingers. Pointer events rather than touch
// events so a mouse gets the same behaviour for free (double-CLICK to zoom,
// drag to pan, wheel to scale) — and so two fingers are just two pointer ids
// instead of a second event family.
let previewState = { scale: 1, x: 0, y: 0 };
const previewPointers = new Map();
let previewDragFrom = null;   // {x, y, state, moved} while one finger is down
let previewPinchFrom = null;  // {dist, state} while two are
let previewLastTap = 0;
let previewLastTapAt = { x: 0, y: 0 };

// The element's LAYOUT box, in viewport coordinates — deliberately NOT
// getBoundingClientRect(), which reports the box AFTER the transform. Reading
// it there is the bug that let the picture escape: every clamp was measured
// against a "screen" that grew with the zoom, so at 6x the bounds were six
// times too generous and the photo could be flung right off, leaving a screen
// of black. It cannot be caught by the transform tests — they are handed a
// correct view — and it looks perfectly reasonable in the source.
//
// #preview is `position: fixed` and untransformed, so it is both a trustworthy
// origin and the image's offsetParent.
function previewBox() {
  const el = $("preview-img");
  const outerEl = $("preview");
  if (!el || !outerEl || !outerEl.getBoundingClientRect) return null;
  const outer = outerEl.getBoundingClientRect();
  const width = el.offsetWidth || outer.width;
  const height = el.offsetHeight || outer.height;
  return {
    view: { w: width, h: height },
    natural: { w: el.naturalWidth || 0, h: el.naturalHeight || 0 },
    rect: {
      left: outer.left + (el.offsetLeft || 0),
      top: outer.top + (el.offsetTop || 0),
      width,
      height,
    },
  };
}

function previewReset() {
  previewState = { scale: 1, x: 0, y: 0 };
  previewPointers.clear();
  previewDragFrom = previewPinchFrom = null;
  previewPaint(false);
}

// The one writer of what the preview looks like. `settle` animates — used when
// letting go, never while a finger is down, where the picture must track it
// exactly or the whole thing feels like it is lagging.
function previewPaint(settle, dismissing = 0) {
  const el = $("preview-img");
  const box = $("preview");
  if (!el || !box) return;
  if (el.classList) el.classList.toggle("settling", !!settle);
  if (el.style) {
    el.style.transform =
      `translate(${previewState.x}px, ${previewState.y}px) scale(${previewState.scale})`;
    // Fading the surround as it is dragged away is what says "let go and this
    // closes" without a word of instruction.
    el.style.opacity = String(Math.max(0.3, 1 - dismissing * 0.6));
  }
  if (box.style) box.style.background = `rgba(0, 0, 0, ${Math.max(0.35, 1 - dismissing * 0.7)})`;
}

function previewPoint(event, rect) {
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function previewDown(event) {
  if (event.target.closest && event.target.closest("#preview-bar")) return; // the ✕
  const box = previewBox();
  if (!box) return;
  previewPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  // Deliberately NO setPointerCapture. Capturing on the <img> looked like the
  // careful thing to do and silently broke every drag: the capture was lost a
  // frame later (a transformed capture target), the lostpointercapture came
  // with a pointercancel, and that ended the gesture — so exactly ONE move was
  // ever applied and a 220px drag registered as 18px. The overlay is
  // full-screen and the listeners are on it, so the pointer has nowhere to
  // escape to and capture buys nothing.
  if (previewPointers.size === 2) {
    const [a, b] = [...previewPointers.values()];
    previewPinchFrom = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, state: previewState };
    previewDragFrom = null;
    return;
  }
  previewDragFrom = { x: event.clientX, y: event.clientY, state: previewState, moved: false };
}

function previewMove(event) {
  const box = previewBox();
  if (!box || !previewPointers.has(event.pointerId)) return;
  previewPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  event.preventDefault();

  if (previewPointers.size >= 2 && previewPinchFrom) {
    const [a, b] = [...previewPointers.values()];
    const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    const mid = { x: (a.x + b.x) / 2 - box.rect.left, y: (a.y + b.y) / 2 - box.rect.top };
    previewState = previewPinch(
      previewState, previewPinchFrom.state, dist / previewPinchFrom.dist,
      mid, box.view, box.natural,
    );
    previewPaint(false);
    return;
  }
  if (!previewDragFrom) return;
  const delta = { x: event.clientX - previewDragFrom.x, y: event.clientY - previewDragFrom.y };
  if (Math.hypot(delta.x, delta.y) > 6) previewDragFrom.moved = true;
  const next = previewDrag(
    { ...previewDragFrom.state, x: previewDragFrom.state.x, y: previewDragFrom.state.y },
    delta, box.view, box.natural,
  );
  previewState = { scale: next.scale, x: next.x, y: next.y };
  previewPaint(false, next.dismissing || 0);
}

function previewUp(event) {
  const box = previewBox();
  previewPointers.delete(event.pointerId);
  if (previewPointers.size < 2) previewPinchFrom = null;
  if (!box || !previewDragFrom) return;
  const drag = previewDragFrom;
  previewDragFrom = null;
  if (previewPointers.size) return; // still pinching with the other finger

  if (drag.moved) {
    if (previewDragEnds(previewState)) { closePreview(); return; }
    // Not far enough: the picture goes back where it was, which is the answer
    // to "did that do anything?" — it visibly did not.
    previewState = previewClamp(previewState, box.view, box.natural);
    if (previewState.scale <= 1.01) previewState = { scale: 1, x: 0, y: 0 };
    previewPaint(true);
    return;
  }

  // A tap. Two in quick succession, close together, toggle the zoom.
  const now = Date.now();
  const near = Math.hypot(event.clientX - previewLastTapAt.x, event.clientY - previewLastTapAt.y);
  if (now - previewLastTap < PREVIEW_TAP_MS && near < PREVIEW_TAP_SLOP) {
    previewLastTap = 0;
    previewState = previewToggleZoom(
      previewState, previewPoint(event, box.rect), box.view, box.natural,
    );
    previewPaint(true);
    return;
  }
  previewLastTap = now;
  previewLastTapAt = { x: event.clientX, y: event.clientY };
  // A single tap on the SURROUND closes; on the picture it does nothing, so
  // long-press there still offers Save Image ([PREVIEW]).
  if (event.target && event.target.id !== "preview-img" && previewState.scale <= 1.01) {
    setTimeout(() => { if (previewLastTap === now) closePreview(); }, PREVIEW_TAP_MS);
  }
}

// iOS pinch. Safari does NOT hand a two-finger pinch over as two clean
// pointer streams — it claims the gesture as page zoom and reports it through
// WebKit's own gesturestart/gesturechange/gestureend, whose `scale` is the
// spread relative to the start of the gesture. So the pointer-pair path above
// (which is what Chrome and Android give) is not enough on the one device this
// feature exists for, and pinching there did nothing at all.
//
// preventDefault is half the point: without it Safari zooms the PAGE, leaving
// the app itself scaled up with no way back inside a standalone PWA.
let previewGestureFrom = null;

function previewGestureStart(event) {
  const box = previewBox();
  if (!box) return;
  event.preventDefault();
  previewGestureFrom = {
    state: previewState,
    point: previewPoint(event, box.rect),
  };
}

function previewGestureChange(event) {
  const box = previewBox();
  if (!box || !previewGestureFrom) return;
  event.preventDefault();
  previewState = previewPinch(
    previewState, previewGestureFrom.state, event.scale || 1,
    previewGestureFrom.point, box.view, box.natural,
  );
  previewPaint(false);
}

function previewGestureEnd(event) {
  if (event.preventDefault) event.preventDefault();
  previewGestureFrom = null;
  // A pinch that ended below fit springs back to it, the way letting go of an
  // over-pinched photo does everywhere else.
  const box = previewBox();
  if (box && previewState.scale <= 1.01) {
    previewState = { scale: 1, x: 0, y: 0 };
    previewPaint(true);
  }
}

function previewWheel(event) {
  const box = previewBox();
  if (!box) return;
  event.preventDefault();
  const scale = previewState.scale * (event.deltaY < 0 ? 1.15 : 1 / 1.15);
  previewState = previewZoomAt(
    previewState, scale, previewPoint(event, box.rect), box.view, box.natural,
  );
  previewPaint(false);
}

// A file name a chip cannot show in full, shortened from the MIDDLE. The end of
// a name is where it differs — IMG_4021 vs IMG_4022, "-final" vs "-final-2",
// and the extension — so a plain trailing ellipsis cuts off exactly what tells
// two files apart, which is what "the names are cut so I can't distinguish
// some of them" was about.
const NAME_MAX = 30;

function shortName(name, max = NAME_MAX) {
  name = String(name || "");
  if (name.length <= max) return name;
  const head = Math.ceil((max - 1) * 0.45);
  const tail = max - 1 - head;
  return `${name.slice(0, head)}…${name.slice(-tail)}`;
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
  if (dictateTarget === "pad") { resizePadInput(); savePadDraft(); } // no input event fires
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

// [PAD-DRAFT-START]
// Unsent pad text must survive the reloads the app performs on itself (a rev
// mismatch after an update) and PWA relaunches — losing a long spoken sentence
// to a restart is the same annoyance the history ring exists for. Mirrors the
// composer's `aish-draft`: saved on every change (dictation writes the value
// directly, which fires no input event, so renderDictation saves too), plus on
// pagehide, and cleared once the text is actually sent.
const PAD_DRAFT_KEY = "aish-pad-draft";
let padRestorePending = true; // consumed by the first openConsole of this page load

function savePadDraft() {
  const text = $("pad-input").value;
  try {
    if (text) localStorage.setItem(PAD_DRAFT_KEY, text);
    else localStorage.removeItem(PAD_DRAFT_KEY);
  } catch { /* storage full / private mode */ }
}

function padDraft() {
  try { return localStorage.getItem(PAD_DRAFT_KEY) || ""; } catch { return ""; }
}

function clearPadDraft() {
  try { localStorage.removeItem(PAD_DRAFT_KEY); } catch { /* nothing to clear */ }
}
// [PAD-DRAFT-END]

addEventListener("pagehide", savePadDraft);

function openConsolePad(dictate = true) {
  if (!consoleOpen) return;
  const pad = $("pty-pad");
  const el = $("pad-input");
  if (!pad.hidden) { el.focus(); return; }
  const draft = padDraft();
  if (draft && !el.value) el.value = draft; // survived a reload / relaunch
  pad.hidden = false;
  $("pad-history-list").hidden = true;
  setDictLang();
  resizePadInput();
  // Focus inside the opening gesture so iOS raises the keyboard immediately.
  el.focus();
  // Restoring a draft does NOT auto-start the mic: you may want to send or edit
  // what came back, and after a reload there is no gesture to unlock audio.
  if (dictate && SpeechRec) startDictation("pad"); // the pad exists to be spoken into
  else if (draft) showToast("restored your unsent dictation");
}

// Closing KEEPS the text (the draft is what you reopen into) — only sending it,
// or emptying the box yourself, discards it. Losing a spoken sentence to a
// mis-tapped ✕ would be the same failure this pad exists to prevent.
function closeConsolePad(discard = false) {
  if (dictating && dictateTarget === "pad") stopDictation();
  dictateTarget = "composer";
  dictHold = false;
  clearTimeout(padEditTimer);
  if (discard) clearPadDraft();
  else savePadDraft(); // before the box is emptied below
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

// [PAD-HISTORY-RENDER-START]
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
  // Oldest first, so the NEWEST sits at the bottom — nearest the textarea and
  // your thumb. Storage stays newest-first; only the rendering is reversed.
  for (const text of items.slice().reverse()) {
    const entry = document.createElement("button");
    entry.type = "button";
    entry.textContent = text.replace(/\s+/g, " ");
    entry.title = text;
    entry.onclick = () => {
      $("pad-input").value = text; // load, never auto-send — you may want to edit
      box.hidden = true;
      resizePadInput();
      savePadDraft();
      $("pad-input").focus();
    };
    box.appendChild(entry);
  }
  box.hidden = false;
  box.scrollTop = box.scrollHeight; // a long list opens showing the newest
}
// [PAD-HISTORY-RENDER-END]

// [PAD-SEND-START]
function padSend(withEnter = true) {
  const text = $("pad-input").value.replace(/\s+$/, "");
  if (!text) { showToast("nothing to send"); return; }
  padHistoryPush(text);
  consoleSend(withEnter ? `${text}\r` : text);
  closeConsolePad(true); // sent — discard the draft
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
  savePadDraft();
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
  // Enter is a NEWLINE — the pad composes multi-line text, and a stray Return
  // must never fire a command at the terminal. Only the Send button submits.
  // Esc closes without sending.
  if (e.key === "Escape") { e.preventDefault(); closeConsolePad(); }
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
  if (active && active.closest(".sheet, #session-rail")) active.blur();
  if (micListening) stopMic(); // don't leave the mic live after closing /mic
  stopComposerMenuTracking(); // drop the #103 viewport listeners with the menu
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  for (const menu of document.querySelectorAll(".popover-menu")) menu.hidden = true;
  closeSessionRail(); // the sessions rail is not a sheet, but Escape/backdrop close both
  $("backdrop").hidden = true;
  snapViewportSoon();
}
for (const b of document.querySelectorAll("[data-close]")) {
  b.onclick = closeSheets;
}
$("backdrop").onclick = closeSheets;

$("sessions-new").onclick = () => { act({ type: "new" }, { label: "the new chat" }); closeSessionRail(); };
// New chat is in TWO places on purpose: the rail's copy is in thumb reach but
// unreachable until you know the rail exists, and the header's is the one you
// find without being told. Same action, two discovery paths.
$("new-chip").onclick = () => requestNewChat();

// [ACTIVE-APPROVAL-CARD-START]
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
  // A confirmation modal is asking a question and owns Escape while it is up —
  // ahead of everything, since it can be raised over any of them, and Escape
  // must always answer "no" rather than dismiss something behind it (#202).
  // The preview sits over every other layer (it is opened FROM them), so it
  // takes Escape first — ahead even of the confirm modal, which cannot be
  // raised over it.
  if (e.key === "Escape" && closePreview()) { e.preventDefault(); return; }
  if (e.key === "Escape" && confirmIsOpen()) { e.preventDefault(); closeConfirm(); return; }
  // Esc leaves terminal mode / the PTY overlay first (#143); only if neither is
  // active does it fall through to dismissing an open sheet.
  if (e.key === "Escape" && escapeExit()) { e.preventDefault(); return; }
  if (e.key === "Escape" && (!$("backdrop").hidden || railIsOpen())) closeSheets();

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
      act({ type: "new" }, { label: "the new chat" });
      closeSheets();
      return;
    }
    if (key === "o" || (e.shiftKey && key === "p")) {
      e.preventDefault();
      toggleSessionRail();
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

// ---- the transcript's touch behaviour ------------------------------------
// The transcript owns NO horizontal gesture any more. Chats used to be switched
// by paging the transcript sideways through a stable per-device "working set"
// (#169); both are gone. The deck bet on spatial memory in a surface that never
// showed a map — you were asked to remember an order you could not see, a wrong
// guess cost a full page transition, and nothing ever taught you the right one —
// and the deck itself was a second, per-device source of truth about which chats
// matter, which had to be reconciled against the server's real session list
// forever (seeding, idle eviction, phantom pruning, fork anchoring). Switching
// now goes through the session rail: recency-ordered, server-derived, and
// visible while you choose. See [RAILSWIPE] below and the rail's own section.
//
// What survives here was never about paging: handing focus back so a long-press
// can select transcript text, and freezing viewport settling while it does.
// `currentSession` lives with its owner, [SESSION-ENTER].
let currentLogPath = ""; // absolute JSONL log path for this session, from hello (#146)
// The server's own recency list, oldest→newest, delivered on every hello. It
// used to be the pager's pages; it survives as the cheapest answer to "which
// chats are worth warming" (see prefetchTargets) and as the title source for a
// rename landing on a chat that isn't on screen.
let recentSessions = [];

// Text selection must win over every other gesture: dragging selection handles
// (or the drag right after a long-press) produces the same touch stream.
function selectionActive() {
  const selection = document.getSelection();
  return Boolean(selection && !selection.isCollapsed);
}

// [TOUCH-FREEZE-START]
// iOS WebKit will not establish a text selection while an editable element
// holds focus — the console's select mode blurs its textarea for exactly this
// reason (#148) — so a long-press on an answer while composing could never
// select it. Touching the transcript therefore hands focus back (dismissing
// the keyboard, the platform chat-app convention), and the viewport settling
// that dismissal triggers is FROZEN until the finger lifts: syncKeyboardInset's
// reflow, snapViewportHome's window scroll, and scrollToEnd would each move the
// text under the still-held finger, which cancels iOS's long-press gesture —
// the very selection the blur just made possible. The settle is run at
// touchend/touchcancel, when the gesture can no longer be disturbed.
let transcriptTouchDown = false;

function beginTranscriptTouch(activeEl, target) {
  transcriptTouchDown = true;
  const editable = activeEl &&
    (activeEl.tagName === "TEXTAREA" || activeEl.tagName === "INPUT" || activeEl.isContentEditable);
  // Never blur the element being touched — an approval card's own feedback
  // field lives INSIDE the transcript, and tapping into it must keep focus.
  if (editable && !activeEl.contains(target)) activeEl.blur();
}

function endTranscriptTouch(settle) {
  if (!transcriptTouchDown) return;
  transcriptTouchDown = false;
  settle(); // whatever viewport settling the touch suppressed, run it now
}

function viewportSettleAllowed() {
  return !transcriptTouchDown;
}

// The one movement the freeze cannot suppress by inaction: the keyboard
// dismissal makes iOS animate visualViewport.offsetTop back to 0, and a fixed
// body still pinned at the OLD offset slides DOWN the screen by exactly that
// much — losing the very text the finger is holding. Riding the pan (top
// follows offsetTop, nothing else) keeps the app's box glued to the visual
// viewport, so the content under the finger stays put; the kb-open toggle,
// the height, and every scroll stay frozen until the finger lifts.
function trackViewportPan() {
  if (!window.visualViewport) return;
  if (!document.body.classList.contains("kb-open")) return;
  document.body.style.top = `${Math.max(visualViewport.offsetTop, 0)}px`;
}
// [TOUCH-FREEZE-END]

// Some transcript children scroll sideways on their own (unwrapped command
// output, code blocks, tables). A pan that STARTS inside one is theirs — this
// is the same test the old pager used, and it is why swiping the whole
// transcript is safe: the only gesture it ever stole was inside these, and
// there it stands down. They also opt into `touch-action: pan-x pan-y`, so the
// browser scrolls them natively while we do nothing.
function scrollsHorizontally(node) {
  for (; node && node !== messagesEl; node = node.parentElement) {
    if (node.scrollWidth > node.clientWidth + 1) {
      const overflow = getComputedStyle(node).overflowX;
      if (overflow === "auto" || overflow === "scroll") return true;
    }
  }
  return false;
}

const railSwipe = {
  tracking: false, claimed: false, decided: false,
  startX: 0, startY: 0, startTime: 0, dx: 0, dy: 0, width: 1,
};

messagesEl.addEventListener("touchstart", (event) => {
  railSwipe.tracking = false;
  railSwipe.claimed = false;
  railSwipe.decided = false;
  if (event.touches.length !== 1) return;
  beginTranscriptTouch(document.activeElement, event.target);
  // The rail's opening gesture lives on the WHOLE transcript, not a 24px edge.
  // An edge-only drawer is the platform default, but this app is navigated by
  // it constantly and the edge is a fiddly target one-handed; the transcript is
  // the whole screen. What made the edge tempting — horizontally scrolling
  // children — is handled by standing down inside them, exactly as the old
  // pager did on this same surface for the same reason.
  if (railIsOpen() || !$("backdrop").hidden || consoleOpen) return;
  if (document.body.classList.contains("kb-open")) return;
  if (selectionActive() || scrollsHorizontally(event.target)) return;
  const touch = event.touches[0];
  railSwipe.tracking = true;
  railSwipe.startX = touch.clientX;
  railSwipe.startY = touch.clientY;
  railSwipe.startTime = event.timeStamp;
  railSwipe.dx = 0;
  railSwipe.dy = 0;
  railSwipe.width = messagesEl.clientWidth || innerWidth;
}, { passive: true });

messagesEl.addEventListener("touchmove", (event) => {
  if (!railSwipe.tracking || railSwipe.decided) return;
  const touch = event.touches[0];
  const dx = touch.clientX - railSwipe.startX;
  const dy = touch.clientY - railSwipe.startY;
  if (!railSwipe.claimed) {
    if (Math.abs(dx) < RAIL_AXIS_AT && Math.abs(dy) < RAIL_AXIS_AT) return;
    // Vertical-dominant, or a leftward drag (there is nothing to the left of
    // this view any more): stand down for the whole gesture rather than
    // half-tracking it.
    if (dx <= 0 || Math.abs(dx) < Math.abs(dy) * 1.4) { railSwipe.decided = true; return; }
    railSwipe.claimed = true;
    beginRailDrag();
  }
  railSwipe.dx = dx;
  railSwipe.dy = dy;
  // Passive, no preventDefault: `#messages` carries `touch-action: pan-y`, so
  // the browser already refuses to scroll a horizontally-started gesture. A
  // non-passive listener here would make every scrolled frame wait on the main
  // thread — the phone's biggest scroll-jank source.
  dragRailTo(Math.max(0, Math.min(dx, railSwipe.width)) / railSwipe.width);
}, { passive: true });

function endTranscriptGesture(event) {
  // scrollToEnd is deliberately NOT part of the deferred settle: yanking to the
  // bottom now would scroll a just-made selection out of view.
  endTranscriptTouch(() => { syncKeyboardInset(); snapViewportSoon(); });
  if (!railSwipe.tracking) return;
  railSwipe.tracking = false;
  if (!railSwipe.claimed) return;
  railSwipe.claimed = false;
  endRailDrag(railSwipeOpens({
    dx: railSwipe.dx,
    dy: railSwipe.dy,
    width: railSwipe.width,
    ms: (event ? event.timeStamp : railSwipe.startTime) - railSwipe.startTime,
    keyboardUp: false,
  }));
}
messagesEl.addEventListener("touchend", (event) => endTranscriptGesture(event));
messagesEl.addEventListener("touchcancel", (event) => endTranscriptGesture(event));

// ---- a chat that no longer exists ----------------------------------------
// [FORGET-SESSION-START]
// Everything this device was holding for a chat that is gone: a warm peek, a
// stashed DOM, a roster row — AND ITS CACHED COPY, which is the one that
// decides whether the row is still on screen. The rail paints from the mirror
// (opening it is a local act now, [ROSTER]), so a chat left in the mirror is a
// chat still IN THE LIST however cleanly the server deleted it, until some
// later sync happens to prune it — and never, if it was pinned.
//
// The SEEN STAMP STAYS, deliberately. It used to be dropped here, and that is
// what turned a stale row into an alarming one: unread is `output newer than
// this device's last look`, so forgetting the look made the leftover row
// UNREAD, which put the chat you had just deleted at the TOP of the list under
// "Needs you" and counted it on the badge. Learning that a chat is gone must
// never make it more prominent — and there is nothing to reclaim by forgetting,
// the map is capped (SEEN_MAX) and session names are never reused.
async function forgetSession(name) {
  if (!name) return;
  prefetched.delete(name);
  viewCache.delete(name);
  forgetAttention(name);
  await offlineForget(name);
}
// [FORGET-SESSION-END]

function onSessionGone(name) {
  forgetSession(name);
  showToast("that chat no longer exists");
  // We had already left for it ([PENDING-VIEW]) — don't leave the spinner
  // promising a transcript the server has just said does not exist.
  if (awaitingPaint === name) abandonPendingView("that chat no longer exists");
}

// Every client hears this now, not just the one that asked (#204): a chat
// deleted on the laptop used to sit on the phone's list until it refreshed,
// and tapping it opened a chat that no longer existed.
async function onSessionDeleted(event) {
  rosterBaseline(event.seq);
  showToast("session deleted");
  // Repaint only AFTER the copy is gone: the rail reads the mirror, so a
  // render racing the eviction paints the chat straight back onto the list.
  await forgetSession(event.name);
  if (railIsOpen()) renderSessionsFromCache();
}


// ---- opening the session rail by swiping the transcript ------------------
// The gesture lives on the WHOLE transcript, not a left edge. An edge-only
// drawer is the platform default and it is what this shipped as first, but this
// app is navigated between chats constantly and a 24px strip is a fiddly
// one-handed target — the complaint was immediate and correct. The thing that
// made the edge tempting is transcript children that scroll sideways
// (unwrapped command output, code blocks, tables), and the fix for those is
// the one the old swipe pager already used on this exact surface: stand down
// when the pan STARTS inside one. That is a solved problem, not a reason to
// shrink the target.
//
// The DECISION is a pure function so it can be unit-tested without a DOM; the
// wiring above only feeds it live geometry.
// [RAILSWIPE-START]
const RAIL_AXIS_AT = 12;      // px of travel before the gesture picks an axis
const RAIL_COMMIT_AT = 0.28;  // fraction of the transcript width that commits
const RAIL_FLICK_PX = 48;     // a short, fast drag commits on speed instead
const RAIL_FLICK_MS = 260;

function railSwipeOpens(g) {
  // g: { dx, dy, width, ms, keyboardUp }
  if (g.keyboardUp) return false;             // the composer owns gestures then
  if (g.dx <= 0) return false;                // leftward opens nothing
  if (Math.abs(g.dy) > g.dx * 0.8) return false;  // dominant horizontal only
  if (g.dx > (g.width || 0) * RAIL_COMMIT_AT) return true;
  return g.dx >= RAIL_FLICK_PX && g.ms <= RAIL_FLICK_MS; // flick
}
// [RAILSWIPE-END]

// sessions
$("back-chip").onclick = () => toggleSessionRail();
$("session-chip").onclick = () => openSessionMenu();
$("console-btn").onclick = () => toggleConsole(); // global Quake console (#148)

$("connbar").onclick = () => reconnect();
$("offlinebar").onclick = () => reconnect(); // same affordance, one bar (#165)
$("composer-slot").onclick = () => pasteIntoComposer();
$("input-clear").onclick = () => clearComposer();

// [CONFIRM-START]
// ONE modal for every irreversible action (#202). It replaced the two-tap
// arm-then-confirm guard everywhere it existed, for two reasons.
//
// An armed control communicates by CHANGING ITSELF, and on a phone that change
// happens under the finger covering it. The chat menu's version got away with
// it by rewriting its LABEL ("Delete chat" → "Confirm delete"); the
// transcript's delete chip is an icon, so "armed" was a 17px glyph turning red
// beneath a thumb — you tap, nothing seems to happen, you tap again, and it is
// gone. Functionally one tap.
//
// And the confirming tap landed on the SAME PIXEL as the arming one, so a
// double-tap habit or a "did that register?" retap committed the action.
//
// The modal fixes both, and adds what neither guard had: it says WHAT HAPPENS.
// A verb and a red button tell you what you are doing, never what it costs —
// that the log is unlinked, that the offline copies go too, that nothing brings
// it back. Consequences belong in the question, not in the user's memory.
//
// Shared on purpose: a second bespoke confirmation is how two dialogs end up
// disagreeing about how serious the same action is.
let confirmAction = null; // what the open modal does if confirmed

function askConfirm({ title, body, verb, action }) {
  confirmAction = action;
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  $("confirm-ok").textContent = verb || "Delete";
  $("confirm-modal").hidden = false;
  $("confirm-cancel").focus(); // the resting choice takes the keyboard, never the destructive one
}

function closeConfirm() {
  // Dismissing DROPS the pending action. An irreversible thing left
  // half-committed behind a closed dialog is exactly what this replaced.
  confirmAction = null;
  $("confirm-modal").hidden = true;
}

function runConfirmed() {
  const action = confirmAction;
  closeConfirm();
  if (action) action();
}

function confirmIsOpen() {
  return !$("confirm-modal").hidden;
}

$("confirm-ok").onclick = runConfirmed;
$("confirm-cancel").onclick = closeConfirm;
// Tapping the dim area outside the card cancels — the same as Cancel, never the
// action: a dismissal is not an answer.
$("confirm-modal").onclick = (e) => {
  if (e.target === $("confirm-modal")) closeConfirm();
};

// Deleting a chat is IRREVERSIBLE: the log file is unlinked, and the offline
// mirror drops server-deleted sessions on its next sync, so no copy survives.
// The name is captured when the question is ASKED, so an answer can never land
// on a chat the view moved to in between.
function askDeleteChat() {
  const name = currentSession;
  if (!name) return;
  askConfirm({
    title: "Delete this chat?",
    body:
      "Deletes the whole conversation — every message in it, and its log file " +
      "on the server. The copy on your devices is deleted at the next sync. " +
      "This cannot be undone.",
    verb: "Delete",
    // Answering "Delete" is not the same as the chat being deleted — the
    // request still has to arrive and be handled ([ACK-LEDGER]). Nothing to
    // undo here: the list is the claim, and it only drops the chat when the
    // server says it is gone ([FORGET-SESSION]).
    action: () => act({ type: "delete_session", name }, { label: "deleting that chat" }),
  });
}
// [CONFIRM-END]

// ---- session title menu -------------------------------------------------
// The tappable title opens a small menu of session actions (iOS Messages
// convention: settings live behind the title, not a floating overflow chip).
function openSessionMenu() {
  const menu = $("session-menu");
  $("wrap-state").textContent = document.body.classList.contains("wrap") ? "On" : "Off";
  refreshOfflinePinUi(); // async — the row shows "…" until the store answers
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
  // The header takes the new name at once, so it has to give it back if the
  // rename never lands — otherwise the chat answers to a name only this device
  // believes in, until some later repaint quietly disagrees.
  const previousTitle = $("session-title").textContent;
  if (currentSession) {
    act({ type: "rename_session", name: currentSession, title },
      { label: "the rename", lost: () => setTitle(previousTitle === "New chat" ? "" : previousTitle) });
  }
  setTitle(title); // optimistic; session_renamed reconfirms (and updates the drawer)
  closeSheets();
});
$("rename-cancel").onclick = () => closeSheets();

$("session-menu").addEventListener("click", (e) => {
  const item = e.target.closest(".menu-item");
  if (!item) return;
  // Deleting the current chat is unrecoverable, so it asks in the shared
  // confirmation modal ([CONFIRM]) — which also states what "delete" costs
  // here, something a red menu row never did.
  if (item.dataset.act === "delete") { closeSheets(); askDeleteChat(); return; }
  // Rename swaps the menu for an inline title field (keeps the backdrop) —
  // no blocking window.prompt, which would also trap automation.
  if (item.dataset.act === "rename") { openRenameBox(); return; }
  closeSheets(); // hides the menu + backdrop
  switch (item.dataset.act) {
    case "new": requestNewChat(); break;
    case "model": openModelSheet(""); break;
    case "cd": openDirSheet(); break;
    case "pin": toggleOfflinePin(); break;
    case "wrap": toggleWrap(); break;
    case "export": exportSessionPdf(); break;
    case "workspace": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
    case "copylog": copyLogPath(); break;
    case "reconnect": reconnect(); break;
  }
});

// ---- unread: has this chat moved since I last looked at it? --------------
// [SEEN-START]
// The list is organised by ATTENTION, not by provenance. Chats used to be split
// into Recent / Automated tabs — a partition by who STARTED the chat — and the
// design kept arguing with itself about it: attention counters had to be
// computed for BOTH tabs so the hidden one could still flag things, and search
// collapsed the split entirely because it lies about relevance. Both are the
// same complaint. Provenance is a property worth SEEING (it is a row glyph now),
// but it is not how anyone navigates: what you want to know is whether a chat
// wants you. An email-triggered session holding an approval wants you exactly as
// much as your own chat that finished a long task while you were away, and a
// three-week-old triggered chat about a receipt is just an old chat.
//
// "Unread" is per-DEVICE, because "I looked at it" is a fact about a screen, not
// about the server: the same chat can be read here and unread on the phone, and
// that is correct. The floor (`since`) is what keeps day one sane — without it
// the entire archive would arrive unread on a new device and the attention band,
// whose whole value is being short, would BE the list.
const SEEN_KEY = "aish-seen";
const SEEN_MAX = 300; // names kept; oldest views dropped first
let seenAt = {};      // name → ms this device last viewed it
let seenSince = 0;    // this device started tracking here; older activity is read

function saveSeen() {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify({ at: seenAt, since: seenSince })); }
  catch { /* private mode: unread degrades to in-memory only */ }
}

(function loadSeen() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY));
    if (raw && raw.at && raw.since) {
      seenAt = raw.at;
      seenSince = raw.since;
      return;
    }
  } catch { /* private mode / corrupt — fall through to a fresh floor */ }
  seenSince = Date.now();
  saveSeen();
})();

// The ONLY writer of the seen map. Called from [SESSION-ENTER], because "the
// view is now chat X" and "this device has looked at chat X" are the same event
// — putting it there is what keeps a socket hello, a mirror read and the boot
// paint from each having to remember it separately.
function markSeen(name) {
  if (!name) return;
  seenAt[name] = Date.now();
  const names = Object.keys(seenAt);
  if (names.length > SEEN_MAX) {
    names.sort((a, b) => seenAt[b] - seenAt[a]);
    for (const stale of names.slice(SEEN_MAX)) delete seenAt[stale];
  }
  saveSeen();
  refreshBadge(); // reading a chat drops it from the count HERE, not a round trip later
}

// PURE: the whole unread decision, testable with no DOM and no clock. `current`
// is part of it rather than a caller's afterthought — the chat on screen can
// never be unread, however much activity it produces while you watch it.
function sessionUnread(info, state) {
  if (!info || !info.name || info.name === state.current) return false;
  // OUTPUT, not activity (#203). `ts` answers "when did anything happen here",
  // which is the right stamp to ORDER by — a chat mid-turn really is the most
  // recent thing there is — and the wrong one to call unread by: every
  // thinking step a chat took moved it past this device's last look, so a chat
  // that was quietly working marked itself unread with nothing new to show.
  // `out` is the last thing that went into the CONVERSATION (`_is_output`).
  // Absent means the server or the mirror row predates the split, and then the
  // old stamp is still the best answer there is — never zero, which would read
  // as "nothing here is ever new".
  const at = (Number(info.out) || Number(info.ts) || 0) * 1000; // epoch SECONDS
  if (!at) return false;
  return at > Math.max(state.seen[info.name] || 0, state.since);
}
// [SEEN-END]

// SESSIONS_PARTITION_START
// One list, four bands, in the order you act on them:
//
//   Needs you  — held for approval, or OUTPUT you haven't seen. The only
//                band that is about you rather than about the chat, and the
//                reason the tabs are gone: it cuts ACROSS provenance.
//   Active now — running. Worth seeing, but it isn't asking for anything.
//   Pinned     — chats you said you care about, which recency would sink.
//   (rest)     — everything else, in date buckets.
//
// A session appears in exactly one band. Everything below is pure: it takes
// rows in and returns rows out, so the sectioning is unit-testable.

// The ONE "does this chat want me" predicate. The rail bands with it and the
// attention badge counts with it, so a chat can never sit in "Needs you"
// without being counted, or be counted without appearing there ([ATTENTION]).
//
// LIVENESS OUTRANKS THE STAMP. A chat that is WORKING is not asking for
// anything — it is `Active now`, and it will say so itself when it finishes or
// stops for you, both of which this predicate can already see. So a running
// chat is never `Needs you`, even when an EARLIER turn's output is genuinely
// unread: the row keeps its dot and says so, it just does not send you
// somewhere that has nothing for you yet.
//
// It used to have a second job. "Unread" was derived from last ACTIVITY, and a
// running turn is activity — every tool step moved the stamp — so a chat that
// was merely thinking kept re-crossing the unread line and landing here. That
// is fixed where it belonged, in `sessionUnread`, which now reads the last
// OUTPUT (#203). The guard below is no longer load-bearing against steps; it
// stands on its own meaning.
function needsYou(info, state) {
  if (info.state === "waiting") return true;   // stopped; it cannot go on without you
  if (info.state === "running") return false;  // working; not asking for anything
  return sessionUnread(info, state);
}

function partitionSessions(sessions, state) {
  const bands = { needsYou: [], active: [], pinned: [], rest: [] };
  const counts = { waiting: 0, running: 0, unread: 0 };
  for (const info of sessions) {
    const unread = sessionUnread(info, state);
    if (info.state === "waiting") counts.waiting++;
    if (info.state === "running") counts.running++;
    if (unread) counts.unread++;
    if (needsYou(info, state)) bands.needsYou.push(info);
    else if (info.state === "running") bands.active.push(info);
    else if (info.pinned) bands.pinned.push(info);
    else bands.rest.push(info);
  }
  return { bands, counts };
}
// SESSIONS_PARTITION_END

// ---- attention badge ----------------------------------------------------
// [ATTENTION-START]
// The durable count on the top-bar rail button: how many chats OTHER than the
// one on screen want you. It is the same question the rail's "Needs you" band
// answers, and that is exactly why it must not be answered twice.
//
// It used to be. The badge was written in two places — a `session_state` push
// ADDED a name, a `session_list` REPLACED the whole set — and a session_list is
// only ever requested when the rail is opened. So the badge's ground truth was
// refreshed by the act of opening the very list that would have shown you the
// answer, and between rail opens it drifted in both directions: reading a chat
// never decremented it (it counted chats you had already read), a background
// chat stopping for approval never incremented it (it missed the one thing that
// most literally needs you), and a reload started it EMPTY however many chats
// were waiting. Whichever way it was wrong, opening the rail refreshed it — so
// the number you acted on and the list you checked it against could never be
// caught disagreeing (#203).
//
// The shape that fixes it: the badge is a PURE FUNCTION of (rows, seen map,
// chat on screen), recomputed by its single owner whenever any of the three
// moves. Rows arrive from the server three ways — the session_list the rail
// requests, the recency rows every hello already carries (so a boot, a
// reconnect and a switch all re-derive it for free), and a pushed
// `session_state` for one chat. The seen map moves when you read a chat, which
// decrements the badge on the tap, with no round trip (L7).
//
// The MIRROR is deliberately not a source (L4): its rows carry a lagging `ts`
// and cannot see liveness at all (`state: ""`), so a cached list painting the
// rail must leave the badge alone rather than briefly claim a count it is not
// entitled to. The traffic runs the OTHER way instead — see `railRows`.
const attentionSessions = new Set();
let attentionRows = []; // last AUTHORITATIVE rows; the mirror's never land here

// Rows from the server, wholesale (a session_list, or a hello's recency list).
// Kept WHOLE, not reduced to the three fields the count reads: these are the
// best rows this device holds, and `railRows` renders them when the mirror
// cannot.
function setAttentionRows(rows) {
  attentionRows = (rows || []).filter((info) => info && info.name).map((info) => ({ ...info }));
  refreshBadge();
}

// One chat's state, pushed while you are elsewhere. Stamped with THIS device's
// clock deliberately: `sessionUnread` compares it against the seen map, which
// is this device's clock too, so a push cannot be read as already-seen by a
// server whose clock runs behind. The push carries a title, so a chat this
// device has never listed is still renderable when the count names it.
// One pushed row, applied WHOLE — a row, never a patch, so a duplicate costs
// nothing and a missed one is repaired by the next row for that chat.
function noteAttention(pushed) {
  const name = pushed && pushed.name;
  if (!name) return;
  const now = Date.now() / 1000;
  // A push announcing a chat has STOPPED — finished, or holding — is announcing
  // that something landed in it, so it moves the OUTPUT stamp too. Without
  // that, unread would go on comparing against whatever the last list said and
  // a background turn's answer would show no dot until the next one arrived
  // (#203). A `running` push says only that work started: `ts` moves, `out`
  // does not — the same distinction the stamps exist to draw.
  const stamps = pushed.state === "running" ? { ts: now } : { ts: now, out: now };
  // Empty is "no opinion", not "it is empty now": a row whose derivation had
  // nothing to say must not blank a preview or a title this device can still
  // show. Every field the server DOES have an opinion about wins.
  const known = { state: pushed.state || "" };
  for (const field of ["title", "snippet", "origin", "cwd"]) {
    if (pushed[field]) known[field] = pushed[field];
  }
  const row = attentionRows.find((info) => info.name === name);
  if (row) Object.assign(row, known, stamps);
  else attentionRows.push({ name, ...known, ...stamps });
  refreshBadge();
}

// THE COUNT'S OWN ROWS ARE ALWAYS RENDERABLE. This is the other half of the
// fix above, and without it the first half reads as a worse bug than the one it
// replaced (#203 follow-up).
//
// The rail paints from the offline mirror FIRST — that is what makes a swipe
// instant and what makes it work with no socket at all. But a mirror row cannot
// carry liveness (`state: ""`, by its own admission) and its `ts` is as old as
// the last sync. So a rail painted from the mirror alone has NO `Active now`
// band — it structurally cannot know a chat is running — and can miss the very
// chat the count is counting, whose stamp moved after the last sync. Once the
// count became authoritative and the list did not, the two visibly disagreed:
// the badge said 1 and the list you opened to answer it was empty.
//
// It is not a race you can win by waiting. On a phone the socket is usually
// still reconnecting when the rail opens, and `requestSessions` deliberately
// does not even ASK while it is down — so the mismatch lasts as long as the
// reconnect does.
//
// So the authoritative rows are laid OVER the cached ones for the two facts
// that decide a band, and any the mirror has never synced are appended rather
// than dropped: the count can never name a chat that is nowhere on screen. The
// mirror keeps everything the server row has no opinion about (snippet, cwd,
// pin). Search is exempt from the APPEND half only — ranked results are an
// answer to a question, and a row that does not match it is not an omission.
function railRows(cached, searching) {
  const live = new Map(attentionRows.map((info) => [info.name, info]));
  const merged = (cached || []).map((row) => {
    const known = live.get(row.name);
    if (!known) return row;
    live.delete(row.name);
    return {
      ...row,
      state: known.state || "",
      ts: Math.max(Number(known.ts) || 0, Number(row.ts) || 0),
      out: Math.max(Number(known.out) || 0, Number(row.out) || 0),
      // The server's preview is fresher than the mirror's whenever it has one:
      // it derives from the conversation this process is holding, so it moves
      // with the turn rather than with the last sync.
      title: known.title || row.title || "",
      snippet: known.snippet || row.snippet || "",
    };
  });
  if (searching) return merged;
  for (const known of live.values()) merged.push({ ...known, snippet: "", cwd: "" });
  // The bands below `Needs you` are date buckets, which only read as buckets
  // while the rows descend — an appended row would otherwise land under
  // whatever heading happened to be open when it was pushed on.
  merged.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  return merged;
}

// A chat that no longer exists must leave the count with it — the roster is
// the only thing that would otherwise go on naming it.
function forgetAttention(name) {
  if (!name) return;
  attentionRows = attentionRows.filter((info) => info.name !== name);
  refreshBadge();
}

function refreshBadge() {
  const state = { seen: seenAt, since: seenSince, current: currentSession };
  attentionSessions.clear();
  for (const info of attentionRows) {
    // The chat on screen is never counted, whatever its state: an approval it
    // is holding is a card in front of you, not somewhere else to go. This is
    // the same rule `sessionUnread` already applies, extended to liveness.
    if (info.name !== currentSession && needsYou(info, state)) {
      attentionSessions.add(info.name);
    }
  }
  const badge = $("back-badge");
  if (attentionSessions.size) {
    badge.textContent = String(attentionSessions.size);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}
// [ATTENTION-END]

let lastSessionEvent = null; // last session_list, so re-renders work offline

// The list is rendered from the local mirror FIRST and replaced by the server's
// answer when it arrives. Online that just means the rail paints instantly
// instead of after a round trip; offline it is the only source, and search
// keeps working because the ranking is a port of the server's own tiers
// (offlineRank) rather than a different, weaker matcher.
//
// What it paints is the mirror UNDER the best rows this device holds
// (`railRows`) — a mirror row cannot see liveness, so on its own it renders a
// rail with no `Active now` band and no sign of whatever the count is counting.
async function renderOfflineSessions(query) {
  const metas = await offlineList();
  const cached = offlineRank(metas || [], query || "").map((meta) => ({
    name: meta.name,
    title: meta.title,
    snippet: meta.snippet,
    ts: meta.ts,
    out: meta.out || 0, // last output as of the last sync (#203)
    state: "", // liveness is a server fact; a mirror can only lie about it
    cwd: "",
    origin: meta.origin,
    pinned: meta.pinned,
  }));
  // Not `metas.length`: a device that has connected but never finished a sync
  // still has rows worth painting, and they are the ones the count names.
  const sessions = railRows(cached, Boolean((query || "").trim()));
  if (!sessions.length) return false;
  renderSessions({
    type: "session_list",
    current: currentSession,
    fromCache: true,
    sessions,
  });
  return true;
}

// Paint the rail, and ask the server ONLY when asking would tell us something.
//
// This used to ask every time — on every rail open, drag, dock and hello — and
// that was the refresh mechanism: the list was only ever as current as the
// last time you opened it. With the roster published ([ROSTER]) the client is
// already current, so opening the rail is a local act and should cost no round
// trip (L7). Two things still genuinely need the server:
//
//   a SEARCH — a ranked answer over every message, which no local state holds;
//   an INCOMPLETE roster — a fresh connection knows only the recency head the
//     hello carried, and a reconnect knows it missed deltas it can never be
//     sent again. Both are `rosterComplete === false`, and both are repaired
//     by exactly one list.
//
// Take that guard away and the poll comes back; leave the roster's staleness
// implicit and this becomes a client that quietly shows old rows forever.
function requestSessions(query) {
  // The server's rows are decorated with pin state from the in-memory mirror,
  // so make sure it is current before the response lands — otherwise the
  // pinned band silently empties in the window after a reload.
  offlineRefreshMetaMap();
  renderOfflineSessions(query);
  const searching = Boolean((query || "").trim());
  if (rosterComplete && !searching) return;
  // Don't call send() while offline: its toast would fire on every keystroke
  // for a condition the offline bar already states.
  if (ws && ws.readyState === WebSocket.OPEN) send({ type: "sessions", query });
}

$("sessions-search").addEventListener(
  "input",
  debounce(() => requestSessions($("sessions-search").value), 150)
);

// ---- the session rail ----------------------------------------------------
// [RAIL-START]
// The sessions list is a LEFT SLIDE-OVER, not a full screen — and that is
// structural, not cosmetic. At a wide viewport the very same panel docks open
// as a permanent sidebar, so phone and desktop run ONE navigation model at two
// widths; a full-screen list would be a separate SCREEN, i.e. a second model
// that would have to be reconciled with the first later. The sliver of chat
// left visible behind it does three jobs a full screen cannot: it says "this is
// temporary, you are still in that chat", it gives a tap-to-dismiss target that
// isn't a back button, and it makes obvious that the transcript underneath is
// untouched.
//
// There is deliberately no drag-to-CLOSE on the list: a leftward drag on a row
// is already swipe-to-delete, and two recognizers on one surface is exactly the
// collision that cost #169 its per-row delete. Closing is the scrim, the button,
// or Escape.
const RAIL_DOCK_MIN = 900; // px of viewport width at which the rail docks open

function railDocked() { return innerWidth >= RAIL_DOCK_MIN; }
function railIsOpen() { return document.body.classList.contains("rail-open"); }
function railWidth() {
  return $("session-rail").offsetWidth || Math.min(innerWidth * 0.86, 380);
}

function openSessionRail(query = "") {
  // Whatever else owns the screen stands down first — the rail is not a sheet
  // and does not stack with one.
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  for (const menu of document.querySelectorAll(".popover-menu")) menu.hidden = true;
  $("backdrop").hidden = true;
  document.body.classList.remove("rail-dragging");
  document.body.classList.add("rail-open");
  clearRailDragStyles();
  $("sessions-search").value = query;
  // Auto-focus only where a hardware keyboard is likely: on touch devices
  // focusing would throw the on-screen keyboard over the list before the user
  // has even seen it — there, browsing is the common case and a tap on the
  // field opts into searching.
  if (FINE_POINTER) {
    // Focus only after layout settles: focusing synchronously lets iOS measure
    // the input at its pre-layout position and pan the whole layout absurdly
    // far to "reveal" it (#24). preventScroll stops the browser's own reveal.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        $("sessions-search").focus({ preventScroll: true });
        setTimeout(() => reportViewport("search-focused"), 600);
      })
    );
  }
  requestSessions(query);
}

function closeSessionRail() {
  if (railDocked()) return; // docked open is its resting state, not a mode
  const active = document.activeElement;
  if (active && active.closest("#session-rail")) active.blur();
  document.body.classList.remove("rail-open", "rail-dragging");
  clearRailDragStyles();
  snapViewportSoon();
}

function toggleSessionRail() {
  if (railIsOpen()) closeSessionRail();
  else openSessionRail("");
}

function clearRailDragStyles() {
  $("session-rail").style.transform = "";
  $("rail-scrim").style.opacity = "";
}

// The finger-following half of the edge gesture (see [RAILSWIPE]). The panel is
// laid out off-screen by CSS and dragged in with an inline transform; the class
// takes over again the moment the finger lifts, so no drag can leave the rail
// resting somewhere the stylesheet doesn't know about.
function beginRailDrag() {
  if (railDocked()) return;
  document.body.classList.add("rail-dragging");
  requestSessions($("sessions-search").value || "");
}

function dragRailTo(progress) {
  if (!document.body.classList.contains("rail-dragging")) return;
  $("session-rail").style.transform = `translateX(${-(1 - progress) * railWidth()}px)`;
  $("rail-scrim").style.opacity = String(progress);
}

function endRailDrag(open) {
  document.body.classList.remove("rail-dragging");
  clearRailDragStyles();
  if (open) openSessionRail("");
  else closeSessionRail();
}

// The scrim IS a dismiss target — the sliver of chat you can still see is what
// says the rail is temporary, and tapping it is what makes that true.
$("rail-scrim").onclick = () => closeSessionRail();

// …and so is a leftward drag on the panel itself: the exact inverse of the
// gesture that opened it, which is the first thing a hand reaches for to push
// something back off the screen. It is available because rows no longer own a
// horizontal gesture — the swipe-to-delete they used to carry meant this very
// drag deleted a chat instead, which is both surprising and the destructive
// reading of an ambiguous gesture.
(function attachRailCloseSwipe() {
  const rail = $("session-rail");
  let sx = 0, sy = 0, tracking = false, claimed = false;
  rail.addEventListener("touchstart", (event) => {
    tracking = false;
    if (event.touches.length !== 1 || railDocked() || !railIsOpen()) return;
    sx = event.touches[0].clientX;
    sy = event.touches[0].clientY;
    tracking = true;
    claimed = false;
  }, { passive: true });

  rail.addEventListener("touchmove", (event) => {
    if (!tracking) return;
    const dx = event.touches[0].clientX - sx;
    const dy = event.touches[0].clientY - sy;
    if (!claimed) {
      if (Math.abs(dx) < RAIL_AXIS_AT && Math.abs(dy) < RAIL_AXIS_AT) return;
      // The list scrolls vertically; a scroll must never drag the panel.
      if (dx >= 0 || Math.abs(dx) < Math.abs(dy) * 1.4) { tracking = false; return; }
      claimed = true;
      document.body.classList.add("rail-dragging");
    }
    // Dragging TOWARD closed: progress runs 1 → 0.
    dragRailTo(Math.max(0, Math.min(1, 1 + dx / railWidth())));
  }, { passive: true });

  const finish = (event) => {
    if (!tracking) return;
    tracking = false;
    if (!claimed) return;
    claimed = false;
    const touch = event.changedTouches[0];
    const dx = touch ? touch.clientX - sx : 0;
    // Past a third of the way back, let go of it; otherwise settle open again.
    endRailDrag(dx > -railWidth() * RAIL_COMMIT_AT);
  };
  rail.addEventListener("touchend", finish);
  rail.addEventListener("touchcancel", finish);
})();

// Docking is a viewport fact, so it is re-read on resize. Both directions
// matter: growing past the breakpoint must not leave a docked layout inset for
// a panel that isn't shown, and shrinking below it must not strand a rail that
// was never deliberately opened sitting over the chat.
addEventListener("resize", () => {
  if (railDocked()) {
    if (!railIsOpen()) {
      document.body.classList.add("rail-open");
      requestSessions($("sessions-search").value || "");
    }
    return;
  }
  document.body.classList.remove("rail-open", "rail-dragging");
  clearRailDragStyles();
});

// At a docked width the rail is open from the start. Only the CLASS is set
// here — filling it is left to the first hello, because this runs at module
// load, before the offline layer the list paints from is ready.
if (railDocked()) document.body.classList.add("rail-open");
// [RAIL-END]

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

// The row icon answers ONE question: what is the state of this chat? It used to
// answer two badly — a blue tick for "this is the chat you're in" (which the
// row's own highlight already says, and which read as "done"), and a provenance
// glyph only when nothing else was showing, so a screen of ordinary chats was a
// screen of identical grey icons. Now: attention states win, then provenance,
// then a plain chat mark — and "current" is not an icon at all.
const SESSION_ICONS = {
  waiting: `<svg viewBox="0 0 24 24"><path d="M12 3.5 21 19H3z" fill="none" stroke="var(--orange)" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v3.6M12 16.4v.1" stroke="var(--orange)" stroke-width="1.9" stroke-linecap="round"/></svg>`,
  chat: `<svg viewBox="0 0 24 24"><path d="M4 6.5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9l-4 3.2V16.5a2 2 0 0 1-1-1.7z" fill="none" stroke="var(--dim)" stroke-width="1.7" stroke-linejoin="round"/></svg>`,
  schedule: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="var(--purple, #a78bfa)" stroke-width="1.8"/><path d="M12 7.4V12l3 1.8" fill="none" stroke="var(--purple, #a78bfa)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  email: `<svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2.5" fill="none" stroke="var(--purple, #a78bfa)" stroke-width="1.8"/><path d="M4.5 8l7.5 5 7.5-5" fill="none" stroke="var(--purple, #a78bfa)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  webhook: `<svg viewBox="0 0 24 24"><path d="M13 4.5 6.5 13H12l-1 6.5L17.5 11H12z" fill="none" stroke="var(--purple, #a78bfa)" stroke-width="1.8" stroke-linejoin="round"/></svg>`,
};

const ORIGIN_LABELS = { email: "started by email", schedule: "scheduled", webhook: "started by a webhook" };

function sessionIcon(info) {
  const wrap = document.createElement("span");
  wrap.className = "row-icon";
  const auto = info.origin && info.origin !== "user" ? info.origin : "";
  if (info.state === "running") {
    wrap.className += " ic-running";
    wrap.innerHTML = '<span class="spin"></span>';
    wrap.title = "working";
  } else if (info.state === "waiting") {
    wrap.className += " ic-waiting";
    wrap.innerHTML = SESSION_ICONS.waiting;
    wrap.title = "needs your approval";
  } else if (auto) {
    // An unrecognised origin still reads as automation rather than as a chat.
    wrap.className += " ic-auto";
    wrap.innerHTML = SESSION_ICONS[auto] || SESSION_ICONS.webhook;
    wrap.title = ORIGIN_LABELS[auto] || `started by ${auto}`;
  } else {
    wrap.className += " ic-chat";
    wrap.innerHTML = SESSION_ICONS.chat;
  }
  return wrap;
}

function sessionRow(info, current, opts = {}) {
  const isCurrent = info.name === current;
  const row = document.createElement("button");
  row.className = "row session-row" + (isCurrent ? " current" : "");
  if (opts.unread) row.classList.add("unread");
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
  const stampLine = document.createElement("span");
  stampLine.className = "stamp-line";
  const stamp = document.createElement("span");
  stamp.className = "stamp";
  stamp.textContent = sessionStamp(info.ts);
  stampLine.append(stamp);
  // Pinned is ONE concept (#165's offline pin absorbed it): a pinned chat sits
  // in its own band at the top AND is never evicted from the offline mirror.
  // It goes BELOW the stamp, in the space the row's trash control used to take,
  // rather than beside it — stacked in the corner, the two of them were eating
  // the title's width for a mark that is decoration next to the title itself.
  right.append(stampLine);
  if (info.pinned) {
    const pin = document.createElement("span");
    pin.className = "row-pin";
    pin.title = "pinned — kept at the top and available offline";
    pin.innerHTML = PIN_SVG;
    right.append(pin);
  }
  // Unread is a BADGE on the status icon, not a column of its own. A leading
  // slot is the mail convention, but mail rows are wide and these are not: an
  // always-reserved 12px gutter on a phone-width panel spent real title space
  // on a state most rows do not have. Cornering it on the icon costs nothing,
  // is the notification-dot convention, and still reads at a glance.
  const icon = sessionIcon(info);
  if (opts.unread) {
    const dot = document.createElement("span");
    dot.className = "unread-dot";
    dot.setAttribute("aria-label", "unread");
    icon.appendChild(dot);
  }
  row.append(icon, body, right);
  return railRow(row, info);
}

// A pushpin standing UPRIGHT and solid — pinned. The tilted, hollow version of
// the same shape means "not pinned" and only appears in the chat menu, where
// both states exist; a row only ever shows the pinned one. Upright-vs-tilted is
// the distinction to keep: a pin lying at an angle reads as one not pushed in,
// which is why the first version (tilted AND blue) read backwards.
const PIN_SVG =
  '<svg viewBox="0 0 24 24"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2z" fill="currentColor"/></svg>';

// A rail row is just a row now. It used to ride over a red Delete button on a
// left-swipe, and that gesture had to go: leftward on this panel is the natural
// way to push it back off screen, and having the same drag mean "delete this
// chat" instead was both surprising and the more destructive reading of an
// ambiguous gesture. Deleting lives in the chat menu, which is where a rare,
// irreversible action belongs — and where it already was.
function railRow(row, info) {
  row.dataset.name = info.name; // so the "you are here" mark can move in place
  row.onclick = () => {
    resumeSession(info.name); // the socket if there is one, else the mirror
    closeSessionRail();
  };
  return row;
}

// Which chat you are in is a LOCAL fact — the client moves on the tap and the
// server's `current` only catches up a round trip later. On a docked rail that
// showed as the row you just tapped staying plain while the row you LEFT went
// on claiming to be current, which is the same "your tap wasn't heard" the
// transcript used to say. Repainted in place rather than by re-rendering the
// list: the list's CONTENT is still the server's to supply.
function refreshRailCurrent() {
  for (const row of document.querySelectorAll("#sessions-list .session-row")) {
    row.classList.toggle("current", row.dataset.name === currentSession);
  }
}

// ---- rendering the list --------------------------------------------------
function railSection(label, sessions, current, unreadState) {
  if (!sessions.length) return;
  const list = $("sessions-list");
  list.appendChild(sectionLabel(label));
  for (const info of sessions) {
    list.appendChild(sessionRow(info, current, { unread: sessionUnread(info, unreadState) }));
  }
}

function renderSessions(event) {
  lastSessionEvent = event;
  // Carry the mirror's own facts onto server-supplied rows: the pin lives on
  // this device, so the authoritative list only learns it from here.
  if (!event.fromCache) {
    for (const info of event.sessions) {
      const meta = offlineMeta.get(info.name);
      if (meta) info.pinned = meta.pinned;
    }
  }
  const list = $("sessions-list");
  list.replaceChildren();
  // "Which chat am I in" is the CLIENT's fact: it moves on the tap, while the
  // server's `current` reflects the last resume it has processed. Preferring
  // the server's here would undo refreshRailCurrent on the very next list that
  // arrives — and a list arrives right after every switch.
  const current = currentSession || event.current;
  const unreadState = { seen: seenAt, since: seenSince, current };
  const searching = Boolean($("sessions-search").value.trim());
  // The badge's ground truth, claimed only by an UNFILTERED SERVER list: a
  // cached list cannot see liveness at all and a ranked search result is a
  // subset of the chats there are, so either would silently under-count
  // ([ATTENTION]).
  if (!event.fromCache && !searching) {
    setAttentionRows(event.sessions);
    rosterSnapshotLanded(event.seq); // the stream alone suffices from here ([ROSTER])
  }
  // Ranked search results are ordered by relevance, so date/band grouping would
  // lie about why a row is where it is. Render a flat list.
  if (searching) {
    if (!event.sessions.length) { list.textContent = "no matching sessions"; return; }
    for (const info of event.sessions) {
      list.appendChild(sessionRow(info, current, { unread: sessionUnread(info, unreadState) }));
    }
    return;
  }
  if (!event.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "section-label";
    empty.textContent = "No chats yet";
    list.appendChild(empty);
    return;
  }
  const { bands } = partitionSessions(event.sessions, unreadState);
  railSection("Needs you", bands.needsYou, current, unreadState);
  railSection("Active now", bands.active, current, unreadState);
  railSection("Pinned", bands.pinned, current, unreadState);
  let lastGroup = null;
  for (const info of bands.rest) {
    const group = sessionGroup(info.ts);
    if (group !== lastGroup) {
      list.appendChild(sectionLabel(group));
      lastGroup = group;
    }
    list.appendChild(sessionRow(info, current, { unread: false }));
  }
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
      act({ type: "set_model", spec: model.name, save: $("model-save").checked },
        { label: "the model switch" });
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
  if (path) { act({ type: "add_dir", path }, { label: "adding that directory" }); $("root-input").value = ""; }
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
  // Closing the sheet reads as acceptance, and a cd moves the APPROVAL ROOT —
  // the one piece of state where a silent no-op is a safety question, not a
  // papercut. The chip still only moves on the server's own cwd_changed.
  act({ type: "cd", path }, { label: "the directory change" });
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
  act({ type: "cd", path: dirPath }, { label: "the directory change" });
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

// [BOOTLOADER-START]
// The first-paint spinner (index.html) is dismissed the moment the app has
// something real to show — whichever path gets there first: the socket's hello,
// the offline mirror's paint, or the token gate. Idempotent (each path may fire),
// and a safety timer clears it even if boot wedges, so it can never strand the
// user on a spinner.
function hideBootLoader() {
  const el = document.getElementById("boot-loader");
  if (!el || el.classList.contains("hide")) return;
  el.classList.add("hide");
  setTimeout(() => el.remove(), 300); // after the fade; removes it from the tree
}
setTimeout(hideBootLoader, 10000); // backstop: never trap behind the spinner
// [BOOTLOADER-END]

// Paint the last chat from the mirror, then open the socket. Not awaited: an
// IndexedDB hiccup must never delay the connection, and whichever finishes
// first is correct — a server replay overwrites the cached paint, and the
// cached paint checks serverPainted before touching the DOM.
offlineFirstPaint();
offlineRefreshMetaMap();
connect();
