// The attention count on the rail button, and the one property it exists for:
// it must not disagree with the list it summarises (#203).
//
// The count and the rail's "Needs you" band answer the same question. They used
// to answer it from different data on different clocks — a `session_state` push
// ADDED to the count, a `session_list` REPLACED it, and a session_list was only
// ever requested when the rail was opened. So the count drifted in both
// directions between rail opens (it kept counting chats you had read; it never
// learned about a chat that stopped for approval; a reload started it empty),
// and opening the rail refreshed it — meaning the number you acted on and the
// list you checked it against could never be caught disagreeing.
//
// So the checks here are mostly CORRESPONDENCE checks: for the same rows, the
// badge's set is exactly the band's rows minus the chat on screen. Plus the
// three moments that used to be missed — reading a chat, a pushed hold, and a
// cached list that must claim nothing.
//
// Run manually: node tests/js/test_attention_badge.js
"use strict";

const assert = require("assert");
const { appSource, sessionWorld, checks } = require("./harness");

const { ok, report } = checks();

const SEC = 1000;

/** A world with the REAL unread decision, the REAL band predicate and the REAL
 *  badge owner — nothing here is a paraphrase. */
function badgeWorld() {
  const w = sessionWorld();
  w.load("// PURE: the whole unread decision", "// [SEEN-END]"); // sessionUnread
  w.load("// SESSIONS_PARTITION_START", "// SESSIONS_PARTITION_END"); // needsYou + bands
  w.load("// [ATTENTION-START]", "// [ATTENTION-END]");
  const s = w.sandbox;
  s.seenAt = {};
  s.seenSince = Date.now() - 3600 * SEC; // this device's first-run floor, an hour back
  s.currentSession = null;
  return w;
}

const badge = (w) => w.$("back-badge");
const counted = (w) => [...w.sandbox.attentionSessions].sort();

// A row as the server sends it: ts is epoch SECONDS, state is liveness.
const row = (name, { ago = 60 * SEC, state = "" } = {}) =>
  ({ name, ts: (Date.now() - ago) / SEC, state });

// ---- 1. the count IS the band --------------------------------------------
// The property the user sees. Anything that makes these two disagree is the
// bug this file exists to catch, whatever produced it.
{
  const w = badgeWorld();
  const s = w.sandbox;
  s.currentSession = "onscreen.jsonl";
  s.seenSince = Date.now() - 3600 * SEC;

  const rows = [
    row("held.jsonl", { state: "waiting" }),      // stopped, cannot go on without you
    row("moved.jsonl"),                           // activity since this device looked
    row("read.jsonl", { ago: 7200 * SEC }),       // older than the floor
    // Mid-turn, and its steps have just moved its stamp: activity, not output.
    row("busy.jsonl", { ago: 0, state: "running" }),
    row("onscreen.jsonl", { state: "waiting" }),  // the chat you are looking at
  ];
  s.setAttentionRows(rows);

  const state = { seen: s.seenAt, since: s.seenSince, current: s.currentSession };
  const band = s.partitionSessions(rows, state).bands.needsYou.map((r) => r.name);

  ok("every counted chat is in the band",
    counted(w).every((name) => band.includes(name)));
  ok("every banded chat is counted, except the one on screen",
    band.filter((name) => name !== s.currentSession).sort().join() === counted(w).join());
  ok("the chat on screen is banded but never counted — its card is in front of you",
    band.includes("onscreen.jsonl") && !counted(w).includes("onscreen.jsonl"));
  ok("a chat mid-turn is not attention, however fresh its stamp — steps are not output",
    !counted(w).includes("busy.jsonl"));
  ok("the badge paints the count", badge(w).textContent === "2" && badge(w).hidden === false);
}

// ---- 2. reading a chat clears it, on the tap ------------------------------
// The over-count half. `markSeen` is the local fact "I looked at this"; the
// count derives from it, so it must move without any server round trip (L7).
{
  const w = badgeWorld();
  const s = w.sandbox;
  w.load("// [SEEN-START]", "// [SEEN-END]"); // the REAL seen map, shadowing the recorder
  s.seenSince = Date.now() - 3600 * SEC; // past this device's first-run floor
  s.setAttentionRows([row("a.jsonl"), row("b.jsonl")]);
  ok("two chats have moved since this device looked", badge(w).textContent === "2");

  s.enterSession("a.jsonl", { title: "a" }); // the owner calls markSeen
  ok("reading one drops the count immediately, with no session_list",
    badge(w).textContent === "1" && counted(w).join() === "b.jsonl");

  s.enterSession("b.jsonl", { title: "b" });
  ok("reading the last one hides the badge", badge(w).hidden === true);
  ok("nothing was asked of the server to make that true", w.called("send") === 0);
}

// ---- 3. a chat that stops for approval while you are elsewhere ------------
// The under-count half, and the one that matters most: a held approval waits
// indefinitely. Nothing used to raise the count for it at all.
{
  const w = badgeWorld();
  const s = w.sandbox;
  s.currentSession = "here.jsonl";
  s.setAttentionRows([row("here.jsonl"), row("bg.jsonl", { ago: 7200 * SEC })]);
  ok("nothing wants you yet", badge(w).hidden === true);

  s.noteAttention("bg.jsonl", "waiting");
  ok("a pushed hold raises the count with no list involved",
    badge(w).textContent === "1" && counted(w).join() === "bg.jsonl");

  s.noteAttention("bg.jsonl", "idle"); // it was approved elsewhere and finished
  ok("finishing keeps it counted — it has news you have not read",
    counted(w).join() === "bg.jsonl");

  s.noteAttention("here.jsonl", "waiting");
  ok("a hold in the chat on screen is still never counted",
    !counted(w).includes("here.jsonl"));
}

// ---- 4. a pushed row is stamped with THIS device's clock ------------------
// `sessionUnread` compares a row's ts against the seen map, which is this
// device's clock. A push stamped with the server's would read as already-seen
// on any device whose clock runs ahead of it.
{
  const w = badgeWorld();
  const s = w.sandbox;
  s.seenAt = { late: Date.now() - 5 * SEC }; // looked at it five seconds ago
  s.setAttentionRows([]);
  s.noteAttention("late", "idle");
  ok("a push after the last look counts as unread", counted(w).join() === "late");
  const stamp = s.attentionRows.find((r) => r.name === "late").ts * SEC;
  ok("stamped from this device's clock, the one the seen map uses",
    Math.abs(stamp - Date.now()) < SEC);
}

// ---- 5. the count's rows are always renderable ---------------------------
// The complaint that made this section exist: the badge said 1, the rail was
// opened to answer it, and there was nothing there — plus a chat that was
// actively working did not appear at all. Both are one cause: the rail paints
// from the offline mirror first, and a mirror row cannot carry liveness or a
// stamp newer than the last sync. So the count and the list disagreed for as
// long as the socket took to come back — which on a phone is exactly when you
// look.
{
  const w = badgeWorld();
  const s = w.sandbox;
  s.setAttentionRows([
    { name: "held.jsonl", title: "overnight job", ts: (Date.now() - 30 * SEC) / SEC, state: "waiting" },
    { name: "busy.jsonl", title: "long task", ts: (Date.now() - 10 * SEC) / SEC, state: "running" },
    { name: "old.jsonl", title: "yesterday", ts: (Date.now() - 7200 * SEC) / SEC, state: "" },
  ]);
  // What the mirror holds: it has never synced the held chat at all, and its
  // copy of the running one predates the turn now in flight.
  const cached = [
    { name: "busy.jsonl", title: "long task", snippet: "you: go", ts: (Date.now() - 3600 * SEC) / SEC, state: "", cwd: "", origin: "user", pinned: false },
    { name: "old.jsonl", title: "yesterday", snippet: "…", ts: (Date.now() - 7200 * SEC) / SEC, state: "", cwd: "", origin: "user", pinned: true },
  ];
  const rows = s.railRows(cached, false);
  const byName = new Map(rows.map((r) => [r.name, r]));

  ok("a counted chat the mirror never synced is still rendered",
    byName.has("held.jsonl") && byName.get("held.jsonl").title === "overnight job");
  ok("EVERY counted chat is renderable — the count can never name an empty list",
    counted(w).every((name) => byName.has(name)));
  ok("liveness the mirror cannot hold is laid over it",
    byName.get("busy.jsonl").state === "running");
  ok("…so the working chat lands in Active now instead of nowhere",
    s.partitionSessions(rows, { seen: s.seenAt, since: s.seenSince, current: null })
      .bands.active.map((r) => r.name).join() === "busy.jsonl");
  ok("the mirror keeps what the server row has no opinion about",
    byName.get("busy.jsonl").snippet === "you: go" && byName.get("old.jsonl").pinned === true);
  ok("rows descend by stamp, or the date buckets below stop being buckets",
    rows.every((r, i) => i === 0 || Number(rows[i - 1].ts) >= Number(r.ts)));

  // Searching asks a question; a row that does not match it is not an omission.
  const found = s.railRows(cached, true);
  ok("a search result is overlaid but never padded",
    found.length === cached.length && found[0].state === "running");
}

// ---- 6. wiring: what may claim the count ---------------------------------
// renderSessions paints from the offline mirror FIRST and from the server
// second. The mirror's rows carry a lagging ts and cannot see liveness at all
// (`state: ""`), and a search result is a RANKED SUBSET of the chats there are
// — so neither may seed the badge, or it under-counts for as long as that paint
// is on screen. A wiring pin, because the failure is a missing guard, not a
// wrong decision.
{
  const src = appSource();
  const call = src.indexOf("setAttentionRows(event.sessions)");
  assert(call !== -1, "renderSessions no longer seeds the badge from a server list");
  const guard = src.slice(src.lastIndexOf("\n", call - 1), call);
  ok("only an unfiltered server list claims the count",
    /!event\.fromCache\s*&&\s*!searching/.test(guard));

  ok("the mirror's own rows still declare themselves unlive",
    /state: "", \/\/ liveness is a server fact/.test(src));

  const hello = src.indexOf("function onHello(event)");
  const helloBody = src.slice(hello, src.indexOf("\n// Multi-connection (#102)", hello));
  ok("every hello re-derives the count from the rows it already carries",
    /setAttentionRows\(event\.pager \|\| \[\]\)/.test(helloBody));

  const mirror = src.indexOf("async function renderOfflineSessions");
  const mirrorBody = src.slice(mirror, src.indexOf("\nfunction requestSessions", mirror));
  ok("the cached paint goes through railRows, never straight to the renderer",
    /railRows\(cached,/.test(mirrorBody) && !/sessions: offlineRank/.test(mirrorBody));
  ok("an empty mirror no longer aborts the paint — the count's rows are still rows",
    !/if \(!metas\.length\) return false;/.test(mirrorBody));
}

report("test_attention_badge.js");
