/* aish web client: one WebSocket, dumb rendering.
 *
 * The server owns all state; on every (re)connect it sends hello + a full
 * transcript replay and this client just clears the DOM and re-renders.
 * Approval cards are keyed by request id so a later approval_resolved (live
 * or replayed) collapses them. Assistant answers render as markdown; command
 * output renders ANSI SGR colors. All text lands via textContent /
 * createTextNode — model output never reaches innerHTML.
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

// ---- websocket lifecycle -------------------------------------------------
let ws = null;
let backoff = 1000;
let reconnectTimer = null;

function connect() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  // Name the session this device was on: after a server restart the active
  // session is a fresh empty chat, and without this every reconnect (and
  // every rev-mismatch reload) would silently move the user there.
  const lastSession = localStorage.getItem("aish-session");
  if (lastSession) params.set("session", lastSession);
  const query = params.size ? `?${params}` : "";
  ws = new WebSocket(`${proto}//${location.host}${BASE}ws${query}`);
  ws.onopen = () => {
    backoff = 1000;
    $("connbar").hidden = true;
    connOk = true;
    updateDot();
    checkAppVersion(); // server restarts are when the UI code changes
  };
  ws.onmessage = (raw) => handle(JSON.parse(raw.data));
  ws.onclose = (event) => {
    connOk = false; // socket down — dot goes red until we reconnect
    updateDot();
    if (event.code === 4000) {
      showToast("another device connected — this tab is detached");
      return; // deliberate replacement: do not fight over the session
    }
    if (event.code === 4403) {
      // In-app entry: iOS home-screen apps launch without query params and
      // have storage isolated from Safari, so the URL trick can't help there.
      if (token) showToast("that token was rejected — check for typos");
      $("token-gate").hidden = false;
      $("token-input").focus();
      return;
    }
    $("connbar").hidden = false;
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  };
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
      setTimeout(() => location.reload(), 1000);
    }
  } catch { /* offline blip; next reconnect checks again */ }
}

function send(message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showToast("not connected");
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
    case "replay": onReplay(event); break;
    case "user":
      closeAnswer();
      finishTrace(); // close any trace from a prior turn before the new one
      removeQueueChip(event.text); // a queued message that just started running
      retireQuickReplies();
      retireRegen(); // a new turn supersedes the prior answer's regenerate

      sawAnswer = false;
      answerFilling = false;
      userCmdBlock = null; // a new turn supersedes any dangling ! command block
      taskErrored = false; // a new turn clears the prior error's red dot
      turnStart = replaying ? 0 : Date.now(); // timing readout on the answer
      setBusy(true);
      if (!sessionTitled) setTitle(event.text.split("\n")[0]);
      rememberPrompt(stripAttachmentNotes(event.text));
      lastUserPrompt = stripAttachmentNotes(event.text); // for error Retry
      turnAnchorEl = addUserMsg(event.text); // response-start anchor (until a trace supersedes it)
      // Your own message always comes into view, even if you were scrolled up.
      if (!replaying) scrollToEnd(true);
      break;
    case "queued":
      addQueueChip(event.text);
      break;
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
    case "done": onDone(event); break;
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
}

// The ?v= the server stamped into our own <script> tag — ground truth for
// which code revision this page actually runs (unlike any value learned at
// runtime, it can't be polluted by a stale-from-HTTP-cache load).
const PAGE_REV = (() => {
  const script = document.querySelector('script[src*="app.js"]');
  try { return new URL(script.src).searchParams.get("v"); } catch { return null; }
})();

function onHello(event) {
  // Server code changed since this page was built (or the page predates rev
  // stamping entirely) — reload; the replay mechanism restores the view.
  if (event.rev && event.rev !== PAGE_REV) { location.reload(); return; }
  $("model-name").textContent = event.model;
  setTitle(event.title);
  pagerSessions = event.pager || [];
  currentSession = event.session;
  localStorage.setItem("aish-session", event.session); // reconnects return here
  renderWorkspace(event);
  taskErrored = false; // fresh connected view — clear any stale red
  setBusy(event.busy);
  if (!event.busy) setStatus(null);
  updateEmptyHint();
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

// Offset where the stable prefix ends: just past the last blank line that is
// outside a code fence and already followed by another line (a trailing
// blank may still grow into a paragraph continuation). Blocks never span a
// blank line except fenced code, so splitting here renders identically to a
// full parse.
function stableBoundary(text) {
  const lines = text.split("\n");
  let boundary = 0;
  let pos = 0;
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (inFence) {
      if (/^```\s*$/.test(line)) inFence = false;
    } else if (/^```(\w*)\s*$/.test(line)) {
      inFence = true;
    } else if (line.trim() === "" && i < lines.length - 1) {
      boundary = pos + line.length + 1;
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
    attachAnswerTools(el, event.result, lastUserPrompt);
  }
  closeAnswer();
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
    const body = term.querySelector(".term-out");
    if (body.childNodes.length) body.appendChild(document.createTextNode("\n"));
    body.appendChild(ansiFragment(text));
    term.classList.add("has-output");
    scrollToEnd();
    if (currentTrace) pinTrace(currentTrace);
    return;
  }
  addStreamLine(text);
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
  // A pure-answer turn leaves no steps — drop the empty trace box entirely.
  if (!t.body.querySelector(".step")) { t.el.remove(); refreshStatusline(); return; }
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

// Re-run a prompt even if the previous turn didn't finish: first stop/reconcile
// whatever is (or only looks) still running — `stop` is a graceful no-op when
// nothing runs (#48) and cancels a live/stuck task otherwise — then send it.
// Safe in every state: idle, busy, or wedged after a disruption.
function rerunPrompt(prompt) {
  if (!prompt) return;
  send({ type: "stop" });
  send({ type: "task", text: prompt });
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
function addUserMsg(text) {
  const el = addMsg("user", text);
  makeRecallable(el);
  const tools = document.createElement("div");
  tools.className = "user-tools";
  tools.appendChild(copyChip(() => stripAttachmentNotes(el.textContent), "copy prompt"));
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

// Empty-state education (#33): a fresh chat with earlier sessions to go back
// to points at the two resume affordances (swipe pager, history button).
function updateEmptyHint() {
  const hasOthers = pagerSessions.some((s) => s.name !== currentSession);
  $("empty-hint").hidden = !hasOthers || messagesEl.children.length > 0;
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
  try { send({ type: "client_debug", text }); } catch { /* socket down — skip */ }
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

function updateScrollButton() {
  $("scroll-down").hidden = nearBottom();
  const top = messagesEl.scrollTop;
  if (top < 120 || top > lastScrollTop) scrollingToTop = false;
  if (top < lastScrollTop && top > messagesEl.clientHeight && !scrollingToTop) {
    $("scroll-top").hidden = false;
  } else if (top > lastScrollTop || top < 120) {
    $("scroll-top").hidden = true;
  }
  lastScrollTop = top;
}

messagesEl.addEventListener("scroll", updateScrollButton, { passive: true });

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

// ---- markdown rendering --------------------------------------------------
function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = text.split("\n");
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
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++; // closing fence (or EOF while streaming)
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (fence[1]) code.dataset.lang = fence[1];
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

const INLINE_RE = new RegExp(
  "(`[^`]+`)" +
  "|(\\*\\*[^*]+\\*\\*|__[^_]+__)" +
  "|(\\*[^*\\s][^*]*\\*)" +
  "|(~~[^~]+~~)" +
  "|\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)" +
  "|\\[([^\\]]+)\\]\\(aish-reply:\\/\\/([^)]*)\\)" +
  "|!\\[([^\\]]*)\\]\\(([^)\\s]+)\\)"
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
  btn.textContent = label;
  let reply = (payload || "").trim();
  try { reply = decodeURIComponent(reply) || reply; } catch { /* keep raw */ }
  if (!reply) reply = label;
  btn.onclick = () => {
    if (input.value.trim() && input.value.trim() !== reply) {
      showToast("clear the input first to use a quick reply");
      return;
    }
    input.value = reply;
    input.setSelectionRange(reply.length, reply.length);
    resizeInput();
    input.focus();
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

// Rich embeds (#50): whitelisted YouTube / Google Maps links become inline
// cards in the WEB transcript only — the CLI keeps plain markdown links.
// Security: only strictly-matched ids/queries ever reach an iframe src, and
// the value is decoded then re-encoded with encodeURIComponent, so raw
// model/page text is never interpolated into a frame URL. Frames are
// sandboxed, given no referrer, and share no origin with aish.
const YOUTUBE_RE =
  /^https?:\/\/(?:www\.)?(?:youtube\.com\/watch\?(?:[^#]*&)?v=([a-zA-Z0-9_-]{11})|youtu\.be\/([a-zA-Z0-9_-]{11}))(?:[#&?/]|$)/;
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
  } catch {
    showToast("export failed — is the server reachable?");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Whole-session export (final answers only) — a plain GET the browser turns
// into a download via Content-Disposition, so an anchor click is enough.
function exportSessionPdf() {
  if (!currentSession) return;
  const query = new URLSearchParams({ session: currentSession });
  if (token) query.set("token", token);
  const a = document.createElement("a");
  a.href = `${BASE}export/session?${query}`;
  a.download = "aish-session.pdf";
  document.body.appendChild(a);
  a.click();
  a.remove();
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
  tools.appendChild(copyChip(() => source, "copy answer"));
  tools.appendChild(exportChip(() => source, () => prompt || ""));
  tools.appendChild(forkChip(ordinal));
  if (TTS_OK) tools.appendChild(buildTtsBox(el));
  // Regenerate: only the newest answer keeps it, so retire the previous one.
  retireRegen();
  if (lastUserPrompt) {
    const regen = document.createElement("button");
    regen.type = "button";
    regen.className = "regen-chip";
    regen.title = "regenerate";
    regen.setAttribute("aria-label", "regenerate answer");
    regen.innerHTML = RERUN_SVG;
    regen.onclick = () => rerunPrompt(lastUserPrompt);
    tools.appendChild(regen);
    lastRegenBtn = regen;
  }
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
}

function startPlayback(box, el) {
  stopSpeaking();
  const text = speakableText(el);
  if (!text) return;
  player.box = box;
  player.chunks = chunkParagraphs(text);
  player.lang = speechLang(text);
  box.classList.add("active");
  box.querySelector(".t-rate").textContent = rateLabel();
  speakChunk(0);
}

function speakChunk(index) {
  player.seq += 1;
  const seq = player.seq;
  speechSynthesis.cancel();
  player.index = index;
  player.paused = false;
  player.box.classList.remove("paused");
  const utterance = new SpeechSynthesisUtterance(player.chunks[index]);
  utterance.lang = player.lang;
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
}

function togglePause() {
  if (player.paused) {
    speechSynthesis.resume();
    player.paused = false;
  } else {
    speechSynthesis.pause();
    player.paused = true;
  }
  player.box.classList.toggle("paused", player.paused);
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
}

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
  } else {
    card.dataset.summary = `read ${event.path}`;
    buildReadCard(card, event);
  }
  cards.set(event.id, card);
  pendingCards += 1;
  refreshStatusline();
  messagesEl.appendChild(card);
  scrollToEnd(true);
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
}

// #13/#34: optional feedback typed straight into the approval card. The text
// rides along with WHICHEVER button is pressed — on Deny it explains the
// refusal, on any approval it reaches the model as guidance for this and
// future actions. Typing feedback implies no verdict: Enter just dismisses
// the keyboard, the user still picks a button.
function feedbackField() {
  // A full-width multi-line box (design 2c): room to type an actual note, not a
  // cramped single line. Enter inserts a newline; the verdict still comes from a
  // button press, so nothing here submits.
  const ta = document.createElement("textarea");
  ta.className = "feedback";
  ta.rows = 2;
  ta.placeholder = "Optional comment";
  ta.autocomplete = "off";
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
  // A command approval is an "attention needed" card: always the orange accent
  // (border + icon + subtitle), per the design — gray reads as "safe/neutral",
  // which an approval prompt never is. `destructive` only sharpens the icon and
  // subtitle wording; write/diff cards use the blue accent instead.
  card.classList.add("approval-card", "danger");
  const destructive = Boolean(event.destructive);
  const head = document.createElement("div");
  head.className = "card-head danger";
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
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "cmd-icon";
  editBtn.title = "Edit the command before running";
  editBtn.appendChild(pencilIcon());
  editBtn.onclick = () => toggleEdit();
  box.append(dollar, code, editBtn, copyChip(() => code.textContent, "copy command"));
  card.appendChild(box);

  function toggleEdit() {
    if (code.isContentEditable) {
      code.contentEditable = "false";
      code.classList.remove("editing");
      editBtn.classList.remove("active");
      return;
    }
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

// The card header shows the file; a full absolute path is noise. Prefer the
// path relative to the working directory (design shows `config/http.py`), else
// the last couple of segments.
function relTarget(target) {
  const cwd = currentCwd || "";
  if (cwd && target.startsWith(cwd + "/")) return target.slice(cwd.length + 1);
  const parts = target.split("/").filter(Boolean);
  return parts.length > 2 ? parts.slice(-2).join("/") : target;
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
const input = $("input");

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
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.3)}px`;
}

function makeRecallable(bubble) {
  // Touch path for prompt recall (no arrow keys on phone keyboards): tap
  // one of your bubbles to put its text back in the composer. Only fills
  // an empty composer so a stray tap can't clobber a draft.
  bubble.title = "tap to reuse this prompt";
  bubble.addEventListener("click", () => {
    const text = stripAttachmentNotes(bubble.textContent);
    if (!text) return;
    if (input.value.trim() && input.value.trim() !== text) {
      showToast("clear the input first to reuse this prompt");
      return;
    }
    input.value = text;
    const end = text.length;
    input.setSelectionRange(end, end);
    resizeInput();
    input.focus();
  });
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
  ["/cd", "change working directory (re-anchors approval root)"],
  ["/add-dir", "allow auto-approved work in another tree"],
  ["/jobs", "list background jobs"],
  ["/help", "about aish web"],
];

const suggest = { items: [], index: 0, kind: null, fragment: "" };

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  submitInput();
});

input.addEventListener("keydown", (e) => {
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
      // Enter on an exactly-typed command submits instead of re-completing.
      if (e.key === "Tab" || !(suggest.kind === "slash" && chosen[0] === input.value.trim())) {
        e.preventDefault();
        acceptSuggestion(chosen);
        return;
      }
    }
    if (e.key === "Escape") {
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
});

input.addEventListener("input", () => {
  historyIndex = null; // typing leaves history-recall mode
  saveDraft();
  resizeInput();
  updateSuggest();
});

function atFragment(text) {
  const at = text.lastIndexOf("@");
  if (at < 0 || (at > 0 && !/\s/.test(text[at - 1]))) return null;
  const fragment = text.slice(at + 1);
  return /\s/.test(fragment) ? null : fragment;
}

const requestFiles = debounce((query) => send({ type: "files", query }), 120);

function updateSuggest() {
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

function onFileList(event) {
  if (suggest.kind !== "file" || event.query !== suggest.fragment) return;
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
  if (suggest.kind === "slash") {
    input.value = value + " ";
  } else {
    const pos = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, pos);
    const at = before.lastIndexOf("@");
    const inserted = value.endsWith("/") ? value : value + " ";
    input.value = before.slice(0, at + 1) + inserted + input.value.slice(pos);
    const caret = at + 1 + inserted.length;
    input.setSelectionRange(caret, caret);
  }
  hideSuggest();
  input.focus();
  if (suggest.kind !== "slash") updateSuggest();
}

function submitInput() {
  hideSuggest();
  let text = input.value.trim();
  if (text.startsWith("/")) {
    rememberPrompt(text); // slash commands never echo back as user events
    input.value = "";
    localStorage.removeItem("aish-draft");
    input.style.height = "auto";
    handleSlash(text);
    return;
  }
  if (!text && !attachments.length) return;
  // The server decides per-backend whether attachments go to the model
  // natively (vision) or as path notes for the gated tools.
  if (send({ type: "task", text, attachments: attachments.map((a) => a.path) })) {
    maybeRequestNotifyPermission();
    input.value = "";
    localStorage.removeItem("aish-draft");
    input.style.height = "auto";
    attachments = [];
    renderAttachments();
    scrollToEndSettled();
  }
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
      return;
    }
  }
  switch (command) {
    case "/model": openModelSheet(arg); break;
    case "/resume": case "/delete": openSessionsSheet(arg); break;
    case "/new": case "/clear": send({ type: "new" }); break;
    case "/fork": case "/branch": send({ type: "fork" }); break;
    case "/cd": arg ? send({ type: "cd", path: arg }) : openDirSheet(); break;
    case "/add-dir": case "/dir-add":
      arg ? send({ type: "add_dir", path: arg }) : openSheet("workspace-sheet"); break;
    case "/learn":
      // Runs as a task: the server swaps the text for the distillation
      // prompt (cli.parse_learn) while the transcript shows what was typed.
      if (send({ type: "task", text })) scrollToEndSettled();
      break;
    case "/jobs": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
    case "/help": openSheet("workspace-sheet"); break;
    case "/quit": case "/exit": showToast("just close the tab — sessions persist"); break;
    case "/debug": reportViewport("manual"); showToast("viewport state sent to server log"); break;
    default: showToast(`unknown command ${command}`);
  }
}

// ---- attachments ---------------------------------------------------------
let attachments = []; // {name, path}

// The + button opens the composer actions popover (attach / reference / slash
// / photo); it sits above the button, iOS-style.
$("attach").onclick = () => {
  const menu = $("composer-actions");
  if (!menu.hidden) { closeSheets(); return; }
  menu.style.visibility = "hidden";
  menu.hidden = false;
  const anchor = $("attach").getBoundingClientRect();
  menu.style.left = `${anchor.left}px`;
  menu.style.top = `${anchor.top - menu.offsetHeight - 6}px`;
  menu.style.visibility = "";
  $("backdrop").hidden = false;
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
  }
});

// Insert a trigger char and fire the input flow (mention / slash suggestions).
function composerInsert(ch) {
  input.focus();
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.setRangeText(ch, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

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
function closeSheets() {
  // Blur a focused sheet input before hiding it: merely hiding leaves iOS to
  // dismiss the keyboard on its own schedule, and the layout-viewport pan it
  // caused can then settle without any visualViewport event (#8).
  const active = document.activeElement;
  if (active && active.closest(".sheet, .screen")) active.blur();
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

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && (!$("backdrop").hidden || !$("sessions-sheet").hidden)) closeSheets();
  // Cmd/Ctrl+Shift+O = new chat, Cmd/Ctrl+Shift+P = search sessions
  // (ChatGPT / command-palette conventions).
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
    e.preventDefault();
    send({ type: "new" });
    closeSheets();
  }
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "p") {
    e.preventDefault();
    openSessionsSheet("");
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
// by last interaction (hello.pager), with Safari's direction semantics:
// swipe right = back = older chat, swipe left = forward = newer — or a
// brand-new chat once past the newest. Touches near the screen edges are
// left to Safari's back/forward gesture, and pans starting inside
// horizontally scrollable output stay scrolls.
let pagerSessions = []; // [{name, title}] oldest→newest, from hello
let currentSession = null;
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

function sessionNeighbor(direction) {
  const index = pagerSessions.findIndex((s) => s.name === currentSession);
  return index < 0 ? null : pagerSessions[index + direction] || null;
}

// Safari semantics: back (swipe right, -1) = older chat, forward (swipe
// left, +1) = newer — and one page past the newest is a fresh chat, gated
// on the current one having content so empties never stack up.
const NEW_CHAT_TARGET = { fresh: true, title: "New chat" };

function swipeTarget(direction) {
  const neighbor = sessionNeighbor(direction);
  if (neighbor) return neighbor;
  return direction === 1 && sessionTitled ? NEW_CHAT_TARGET : null;
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
$("empty-hint").onclick = () => openSessionsSheet("");
$("new-chip").onclick = () => send({ type: "new" });

// ---- session title menu -------------------------------------------------
// The tappable title opens a small menu of session actions (iOS Messages
// convention: settings live behind the title, not a floating overflow chip).
function openSessionMenu() {
  const menu = $("session-menu");
  const del = menu.querySelector('[data-act="delete"]');
  if (del) resetDeleteChat(del); // never open still armed from a prior dismissal
  $("wrap-state").textContent = document.body.classList.contains("wrap") ? "On" : "Off";
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
  closeSheets(); // hides the menu + backdrop
  switch (item.dataset.act) {
    case "new": send({ type: "new" }); break;
    case "model": openModelSheet(""); break;
    case "cd": openDirSheet(); break;
    case "wrap": toggleWrap(); break;
    case "export": exportSessionPdf(); break;
    case "workspace": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
  }
});

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
$("sessions-search").addEventListener(
  "input",
  debounce(() => send({ type: "sessions", query: $("sessions-search").value }), 150)
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
  send({ type: "sessions", query });
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
    dx = e.clientX - startX;
    if (Math.abs(dx) > 6) row.classList.add("dragging");
    set(Math.min(0, Math.max(-100, (open ? -88 : 0) + dx)));
  });
  const finish = () => {
    if (startX === null) return;
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
    send({ type: "resume", path: info.name });
    closeSheets();
  };
  return wrap;
}

function renderSessions(event) {
  const list = $("sessions-list");
  list.replaceChildren();
  if (!event.sessions.length) {
    list.textContent = "no matching sessions";
    return;
  }
  // Ranked search results are ordered by relevance, so date/status grouping
  // would lie — render a flat list there. While browsing, running/waiting
  // sessions surface under "Active now"; the rest keep date headers.
  const grouped = !$("sessions-search").value.trim();
  if (!grouped) {
    for (const info of event.sessions) list.appendChild(sessionRow(info, event.current));
    return;
  }
  const active = event.sessions.filter((s) => s.state === "running" || s.state === "waiting");
  const rest = event.sessions.filter((s) => !(s.state === "running" || s.state === "waiting"));
  if (active.length) {
    list.appendChild(sectionLabel("Active now"));
    for (const info of active) list.appendChild(sessionRow(info, event.current));
  }
  let lastGroup = null;
  for (const info of rest) {
    const group = sessionGroup(info.ts);
    if (group !== lastGroup) {
      list.appendChild(sectionLabel(group));
      lastGroup = group;
    }
    list.appendChild(sessionRow(info, event.current));
  }
}

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
  try { return JSON.parse(localStorage.getItem(RECENT_MODELS_KEY)) || []; }
  catch { return []; }
}
function rememberModel(name) {
  const list = [name, ...recentModels().filter((n) => n !== name)].slice(0, 5);
  localStorage.setItem(RECENT_MODELS_KEY, JSON.stringify(list));
}

function renderModels(event) {
  const list = $("model-list");
  list.replaceChildren();
  const modelRow = (model) => {
    const row = document.createElement("button");
    row.className = "row" + (model.name === event.current ? " current" : "");
    row.textContent = model.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = model.desc;
    row.appendChild(meta);
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
  try { return JSON.parse(localStorage.getItem(RECENT_DIRS_KEY)) || []; }
  catch { return []; }
}
function rememberDir(path) {
  const list = [path, ...recentDirs().filter((p) => p !== path)].slice(0, 6);
  localStorage.setItem(RECENT_DIRS_KEY, JSON.stringify(list));
}

let dirPath = "";       // directory the picker is browsing
let dirEntries = [];    // its subdirectory names
let dirSearchTimer = null;

async function dirsFetch(url, params) {
  if (token) params.set("token", token);
  const response = await fetch(`${BASE}${url.replace(/^\//, "")}?${params}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.status);
  return body;
}

async function browseDir(path) {
  let body;
  try {
    body = await dirsFetch("/dirs", new URLSearchParams({ path }));
  } catch (err) {
    showToast(`can't open: ${err.message}`);
    return;
  }
  dirPath = body.path;
  dirEntries = body.dirs;
  $("dir-search").value = "";
  $("dir-sheet").classList.add("browsing");
  renderDirList();
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
const DIR_CHEVRON = '<svg class="dir-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" fill="none" stroke="var(--sep2)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function dirRow(label, meta, onTap, kind = "folder") {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "row dir-row" + (kind === "up" ? " up" : "");
  row.innerHTML = kind === "up" ? DIR_ICON_UP : DIR_ICON_FOLDER;
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
  row.onclick = onTap;
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

function renderDirList(deepResults = null) {
  $("dir-current").textContent = abbreviatePath(dirPath);
  $("dir-use-label").textContent = `Set working directory to “${baseName(dirPath)}”`;
  renderDirCrumb();
  const list = $("dir-list");
  list.replaceChildren();
  const raw = $("dir-search").value.trim();
  const query = raw.toLowerCase();

  if (!query) list.appendChild(dirRow("Recent folders", null, showDirRecents, "up"));

  // Escape hatch: a typed absolute (or ~) path jumps straight there —
  // the rare case the browse/search flow doesn't cover.
  if (raw.startsWith("/") || raw.startsWith("~")) {
    const target = raw.startsWith("~") ? homeDir + raw.slice(1) : raw;
    list.appendChild(dirRow(`Go to ${raw}`, null, () => browseDir(target), "up"));
  }

  if (!query) list.appendChild(sectionLabel("Folders"));
  if (dirPath !== "/") {
    list.appendChild(
      dirRow("..", null, () => browseDir(dirPath.replace(/\/[^/]+$/, "") || "/"), "up")
    );
  }
  const visible = dirEntries.filter(
    (n) => !n.startsWith(".") && (!query || n.toLowerCase().includes(query))
  );
  for (const name of visible) {
    list.appendChild(
      dirRow(name, null, () => browseDir(dirPath === "/" ? `/${name}` : `${dirPath}/${name}`))
    );
  }
  if (deepResults && deepResults.length) {
    list.appendChild(sectionLabel("Everywhere"));
    for (const p of deepResults) {
      list.appendChild(dirRow(abbreviatePath(p), null, () => browseDir(p), "recent"));
    }
  }
}

function openDirSheet() {
  openSheet("dir-sheet");
  dirPath = currentCwd || homeDir || "/";
  showDirRecents(); // step 1: recents + "Choose another folder…"
}

$("cwd-chip").onclick = () => openDirSheet();
$("dir-use").onclick = () => {
  if (!dirPath) return;
  rememberDir(dirPath);
  send({ type: "cd", path: dirPath });
  closeSheets();
};
$("dir-search").addEventListener("input", () => {
  renderDirList();
  clearTimeout(dirSearchTimer);
  const query = $("dir-search").value.trim();
  if (query.length < 2) return;
  dirSearchTimer = setTimeout(async () => {
    try {
      const body = await dirsFetch(
        "/dirs/search", new URLSearchParams({ q: query, base: homeDir || dirPath })
      );
      if ($("dir-search").value.trim() === query) renderDirList(body.results);
    } catch { /* deep search is best-effort */ }
  }, 250);
});

// toast
let toastTimer;
function showToast(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
}

$("token-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("token-input").value.trim();
  if (!value) return;
  localStorage.setItem("aish-token", value);
  location.reload(); // reconnect with the new token from a clean slate
});

connect();
