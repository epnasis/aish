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
    checkAppVersion(); // server restarts are when the UI code changes
  };
  ws.onmessage = (raw) => handle(JSON.parse(raw.data));
  ws.onclose = (event) => {
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
      sawAnswer = false;
      turnStart = replaying ? 0 : Date.now(); // timing readout on the answer
      setBusy(true);
      if (!sessionTitled) setTitle(event.text.split("\n")[0]);
      rememberPrompt(stripAttachmentNotes(event.text));
      addUserMsg(event.text);
      // Your own message always comes into view, even if you were scrolled up.
      if (!replaying) scrollToEnd(true);
      break;
    case "queued":
      addQueueChip(event.text);
      break;
    case "token": onToken(event.text); break;
    case "echo":
      // The activity trace already shows a run_command's approval + result, so
      // drop the approver's redundant confirmation line while a trace is open.
      if (currentTrace && /^[✓✕] (auto-approved|session-allowed|always-allowed|blocked)/.test(event.text)) break;
      closeAnswer();
      addAnsiMsg("echo", event.text);
      break;
    case "stream": traceStream(event.text); break;
    case "step": traceStep(event); break;
    case "error":
      closeAnswer();
      addMsg("error", event.text);
      setBusy(false);
      setStatus(null);
      notify("aish — task failed", event.text);
      break;
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
  }
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

function onToken(text) {
  sawAnswer = true;
  if (!answerEl) {
    answerEl = addMsg("answer md", "");
    answerText = "";
    answerStableLen = 0;
    answerStableNodes = 0;
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
  scrollToEnd();
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
    if (answerText.trim()) attachAnswerTools(answerEl, answerText);
  }
  answerEl = null;
  answerText = "";
}

function onDone(event) {
  answerTiming = turnStart ? (Date.now() - turnStart) / 1000 : 0;
  if (!sawAnswer && event.result) {
    const el = addMsg("answer md", "");
    el.replaceChildren(renderMarkdown(event.result));
    attachAnswerTools(el, event.result);
  }
  closeAnswer();
  finishTrace();
  if (event.sources && event.sources.length) addSources(event.sources);
  setBusy(false);
  setStatus(null);
  notify("aish — answer ready", event.result);
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

function setBusy(busy) {
  clientBusy = busy;
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
  body.innerHTML = '<div class="trace-rail"></div>';
  el.append(head, body);
  // The head toggles expand ONLY when finished; a live trace stays open.
  head.onclick = (e) => {
    if (e.target.closest(".trace-stop")) return;
    if (!el.classList.contains("live")) el.classList.toggle("open");
  };
  head.querySelector(".trace-stop").onclick = (e) => { e.stopPropagation(); send({ type: "stop" }); };
  messagesEl.appendChild(el);
  currentTrace = {
    el, head, body, started: 0, secs: 0, tokensIn: 0, tokensOut: 0,
    pending: null, thinkingRow: null, startedAt: Date.now(), timer: null,
  };
  currentTrace.timer = setInterval(() => updateTraceHead(currentTrace), 1000);
  refreshStatusline(); // the trace header owns Stop now; hide the bottom bar
  scrollToEnd();
  return currentTrace;
}

// Keep the newest step visible inside the height-capped live steps pane.
function pinTrace(t) {
  if (t.el.classList.contains("live")) {
    requestAnimationFrame(() => { t.body.scrollTop = t.body.scrollHeight; });
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
  t.body.appendChild(row);
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
    // The turn was a plain answer — drop the active thinking row.
    if (t.thinkingRow) { t.thinkingRow.row.remove(); t.thinkingRow = null; t.started -= 1; }
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
    const { main } = traceRow(
      t, traceSvg("knowledge", "var(--yellow)"), "Recalled from memory",
      `${items.length} item${items.length === 1 ? "" : "s"} from past work`
    );
    if (items.length) {
      const chips = document.createElement("div");
      chips.className = "know-chips";
      for (const it of items) {
        const chip = document.createElement("span");
        chip.className = "know-chip";
        chip.textContent = it.label || "";
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

function toolStart(t, step) {
  t.started += 1;
  const [title, iconKey] = TOOL_META[step.name] || [step.name, "dot", "--dim"];
  const ref = traceRow(t, SPINNER, title, step.name === "run_command" ? "" : step.summary);
  ref.row.classList.add("running", "active-step");
  startStepTimer(t, ref);
  if (step.name === "run_command" && step.command) {
    const cmd = document.createElement("div");
    cmd.className = "step-cmd mono";
    cmd.textContent = step.command;
    ref.main.appendChild(cmd);
    const out = document.createElement("div");
    out.className = "step-output";
    ref.main.appendChild(out);
    ref.output = out;
  }
  t.pending = { ...ref, name: step.name };
}

function toolFinish(t, step) {
  t.secs += step.secs || 0;
  const meta = TOOL_META[step.name] || [step.name, "dot", "--dim"];
  const denied = step.decision === "denied" || step.decision === "blocked" || step.decision === "rejected";
  let ref = t.pending && t.pending.name === step.name ? t.pending : null;
  if (!ref) {
    // No matching start (e.g. replay ordering): synthesize a completed row.
    t.started += 1;
    ref = traceRow(t, "", meta[0], step.name === "run_command" ? "" : step.summary);
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
  const iconName = denied ? "denied" : step.name === "run_command" ? "command"
    : !step.ok ? "denied" : meta[1];
  const color = denied || !step.ok ? "var(--red)" : `var(${meta[2]})`;
  ref.badge.innerHTML = traceSvg(iconName, color);
  // status tag on the title
  const tag = document.createElement("span");
  tag.className = "step-tag " + (denied || !step.ok ? "bad" : "ok");
  tag.textContent = denied
    ? (step.decision === "blocked" ? "Blocked" : "Denied")
    : !step.ok ? "Error"
    : step.name === "run_command" ? `${ref.manual ? "Approved" : "Auto-approved"} · ${fmtSecs(step.secs)}`
    : fmtSecs(step.secs);
  ref.titleEl.appendChild(tag);
  // The user's approval note, shown back on the step (#3).
  if (step.comment) {
    const note = document.createElement("span");
    note.className = "step-sub step-note";
    note.textContent = `“${step.comment}”`;
    ref.main.appendChild(note);
  }
  if (denied) {
    if (ref.row.querySelector(".step-cmd")) ref.row.querySelector(".step-cmd").classList.add("struck");
    // Why it was skipped/blocked (denial comment, gate reason) — #5, #12.
    if (step.output) {
      const why = document.createElement("span");
      why.className = "step-sub";
      why.textContent = step.output;
      ref.main.appendChild(why);
    }
  }
  // command output block (finalize streamed output, or render from the step)
  if (step.name === "run_command" && step.output && !denied) {
    let out = ref.output;
    if (!out) { out = document.createElement("div"); out.className = "step-output"; ref.main.appendChild(out); }
    const streamed = out.dataset.streamed && out.querySelector(".out-box");
    if (streamed) finalizeOutBox(streamed, splitExit(step.output)[1]);
    else renderStepOutput(out, step.output);
  }
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
  box.querySelector(".out-expand").onclick = () => { box.classList.toggle("expanded"); labelExpand(box); };
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
}

// Peel a trailing "[exit code: N]" into the header label.
function splitExit(text) {
  const m = text.match(/\n?\[exit code: (-?\d+)\]\s*$/);
  return m ? [text.slice(0, m.index), `stdout · exit ${m[1]}`] : [text, "stdout"];
}

function renderStepOutput(container, text) {
  container.replaceChildren();
  const [bodyText, label] = splitExit(text);
  const box = outBox(false);
  box.querySelector(".out-body").appendChild(ansiFragment(bodyText));
  container.appendChild(box);
  finalizeOutBox(box, label);
}

function renderErrorBox(container, text) {
  container.replaceChildren();
  const box = outBox(true);
  box.querySelector(".out-body").appendChild(ansiFragment(text));
  container.appendChild(box);
  finalizeOutBox(box, "error");
}

function traceStream(text) {
  // While a run_command step is live, its output streams into the trace row;
  // otherwise (a user-run !command, no active trace) it renders inline.
  if (currentTrace && currentTrace.pending && currentTrace.pending.output) {
    const out = currentTrace.pending.output;
    out.dataset.streamed = "1";
    let box = out.querySelector(".out-box");
    if (!box) { out.replaceChildren(); box = outBox(false); out.appendChild(box); }
    const body = box.querySelector(".out-body");
    if (body.childNodes.length) body.appendChild(document.createTextNode("\n"));
    body.appendChild(ansiFragment(text));
    scrollToEnd();
    pinTrace(currentTrace);
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

function finishTrace() {
  if (!currentTrace) return;
  const t = currentTrace;
  if (t.timer) { clearInterval(t.timer); t.timer = null; }
  if (t.thinkingRow) { t.thinkingRow.row.remove(); t.thinkingRow = null; }
  t.pending = null;
  currentTrace = null;
  // A pure-answer turn leaves no steps — drop the empty trace box entirely.
  if (!t.body.querySelector(".step")) { t.el.remove(); refreshStatusline(); return; }
  t.el.classList.remove("live");
  t.el.classList.remove("open"); // collapse to the summary; tap to expand
  t.el.querySelector(".trace-status").innerHTML = traceSvg("check", "var(--green)");
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
  addMsg("notice", `— resumed ${history.length} messages —`);
  for (const message of history) {
    const content = (message.content || "").trim();
    if (!content) continue;
    if (message.role === "user") {
      retireQuickReplies();
      addUserMsg(content);
    }
    else if (message.role === "assistant") {
      const el = addMsg("answer md", "");
      el.replaceChildren(renderMarkdown(content));
      attachAnswerTools(el, content);
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
      holder.append(copyChip(() => code.textContent, "copy code"), pre);
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

function inlineMd(text) {
  const frag = document.createDocumentFragment();
  let rest = text;
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
      const link = document.createElement("a");
      link.href = match[6];
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.appendChild(inlineMd(match[5]));
      frag.appendChild(link);
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

// Footer row under a finished answer: copy-as-markdown chip, plus the
// read-aloud player where speech synthesis exists.
let turnStart = 0;
let answerTiming = 0;

function attachAnswerTools(el, source) {
  const tools = document.createElement("div");
  tools.className = "msg-tools";
  tools.appendChild(copyChip(() => source, "copy answer"));
  if (TTS_OK) tools.appendChild(buildTtsBox(el));
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
  const input = document.createElement("input");
  input.type = "text";
  input.className = "feedback";
  input.placeholder = "Optional comment";
  input.enterKeyHint = "done";
  input.autocomplete = "off";
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      input.blur();
    }
  });
  return input;
}

function feedbackExtra(input) {
  const comment = input.value.trim();
  return comment ? { comment } : {};
}

const CARD_TRIANGLE = '<svg viewBox="0 0 24 24"><path d="M12 3.5 21 19H3z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v3.5M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>';
const CARD_SHIELD = '<svg viewBox="0 0 24 24"><path d="M12 3.5l7 2.5v5c0 4.2-2.9 7.5-7 9-4.1-1.5-7-4.8-7-9V6z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>';

function optRow(title, sub, cls, fn) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "opt " + cls;
  const t = document.createElement("span");
  t.className = "opt-title";
  t.textContent = title;
  b.appendChild(t);
  if (sub) {
    const s = document.createElement("span");
    s.className = "opt-sub";
    s.textContent = sub;
    b.appendChild(s);
  }
  b.onclick = fn;
  return b;
}

function buildCommandCard(card, event) {
  card.classList.add("approval-card");
  const destructive = Boolean(event.destructive);
  const head = document.createElement("div");
  head.className = "card-head" + (destructive ? " danger" : "");
  head.innerHTML =
    `<span class="card-ico">${destructive ? CARD_TRIANGLE : CARD_SHIELD}</span>` +
    `<span class="card-htext"><span class="card-htitle">Approval needed</span>` +
    `<span class="card-hsub"></span></span>`;
  head.querySelector(".card-hsub").textContent =
    destructive ? "Destructive — review before running" : "Runs a shell command";
  card.appendChild(head);

  // $ command box, with edit + copy
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
  editBtn.onclick = () => showEditor();
  box.append(dollar, code, editBtn, copyChip(() => event.command, "copy command"));
  card.appendChild(box);

  // where it runs
  const where = document.createElement("div");
  where.className = "card-where mono";
  where.textContent = `runs in ${abbreviatePath(currentCwd || "")}`;
  card.appendChild(where);

  const escapes = event.escapes || [];
  if (escapes.length) card.appendChild(escapeNote(escapes));
  const prefixes = (event.prefixes || []).join(", ");

  const feedback = feedbackField();
  card.appendChild(feedback);

  const opts = document.createElement("div");
  opts.className = "opts";
  opts.appendChild(optRow("Approve", "", "primary",
    () => answerCard(event.id, "approve", feedbackExtra(feedback))));
  if (prefixes) {
    opts.appendChild(optRow("Allow this session",
      `auto-approve “${prefixes}” until the server restarts`, "",
      () => answerCard(event.id, "approve_session", feedbackExtra(feedback))));
    opts.appendChild(optRow("Always allow",
      `save “${prefixes}” to the allowlist — persists across sessions`, "",
      () => answerCard(event.id, "approve_always", feedbackExtra(feedback))));
  }
  if (escapes.length) {
    opts.appendChild(optRow("Trust directory",
      `auto-approve anything in ${escapes.join(", ")}`, "",
      () => answerCard(event.id, "approve_trust", feedbackExtra(feedback))));
  }
  opts.appendChild(optRow("Deny", "", "deny",
    () => answerCard(event.id, "deny", feedbackExtra(feedback))));
  card.appendChild(opts);

  function showEditor() {
    opts.hidden = true;
    editBtn.hidden = true;
    const area = document.createElement("textarea");
    area.value = event.command;
    card.appendChild(area);
    const editRow = buttonRow(card, [
      ["Run edited", "approve", () =>
        answerCard(event.id, "edit", { command: area.value, ...feedbackExtra(feedback) })],
      ["Cancel", "edit", () => {
        area.remove(); editRow.remove(); opts.hidden = false; editBtn.hidden = false;
      }],
    ]);
    area.focus();
  }
}

function buildWriteCard(card, event) {
  title(card, [document.createTextNode(
    `▶ ${event.verb} file? ${event.target} (+${event.added} −${event.removed})`
  )]);
  const diff = document.createElement("div");
  diff.className = "diff";
  for (const line of (event.diff || "").split("\n")) {
    const el = document.createElement("div");
    if (line.startsWith("+++") || line.startsWith("---")) el.className = "head";
    else if (line.startsWith("+")) el.className = "add";
    else if (line.startsWith("-")) el.className = "del";
    else if (line.startsWith("@@")) el.className = "hunk";
    else el.className = "ctx";
    el.textContent = line || " ";
    diff.appendChild(el);
  }
  card.appendChild(diff);
  const feedback = feedbackField();
  card.appendChild(feedback);
  buttonRow(card, [
    ["Approve", "approve", () => answerCard(event.id, "approve", feedbackExtra(feedback))],
    ["Deny", "deny", () => answerCard(event.id, "deny", feedbackExtra(feedback))],
  ]);
}

// What "Allow this session" / "Always allow" would actually allowlist: the
// derived command prefix(es), not the full command line.
function prefixNote(prefixes) {
  const note = document.createElement("div");
  note.className = "prefix-note";
  note.textContent = `“Allow” buttons save the rule: ${prefixes}`;
  return note;
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
  const label = event.reason === "outside"
    ? "▶ read file outside the project?"
    : "▶ read sensitive file? ⚠ may contain secrets";
  title(card, [document.createTextNode(label)]);
  const code = document.createElement("code");
  code.textContent = event.path;
  card.appendChild(code);
  const escapes = event.escapes || [];
  const specs = [["Approve", "approve", () => answerCard(event.id, "approve")]];
  if (escapes.length) {
    specs.push(["Trust directory", "session",
      () => answerCard(event.id, "approve_trust"),
      `add ${escapes.join(", ")} to the session roots until the session closes`]);
  }
  specs.push(["Deny", "deny", () => answerCard(event.id, "deny")]);
  buttonRow(card, specs);
}

function onApprovalResolved(event) {
  const card = cards.get(event.id);
  if (!card) return;
  pendingCards = Math.max(0, pendingCards - 1);
  refreshStatusline();
  card.replaceChildren();
  card.className = "card resolved";
  const verdict = document.createElement("div");
  verdict.className = `verdict ${event.decision === "denied" ? "denied" : "approved"}`;
  verdict.textContent = `${event.decision}: ${(card.dataset.summary || "").slice(0, 120)}`;
  card.appendChild(verdict);
  if (event.comment) {
    const note = document.createElement("div");
    note.className = "verdict-comment";
    note.textContent = `“${event.comment}”`;
    card.appendChild(note);
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

const SLASH_ALL = SLASH_COMMANDS.map(([cmd]) => cmd).concat(["/clear", "/dir-add", "/quit", "/exit"]);

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

$("session-menu").addEventListener("click", (e) => {
  const item = e.target.closest(".menu-item");
  if (!item) return;
  closeSheets(); // hides the menu + backdrop
  switch (item.dataset.act) {
    case "new": send({ type: "new" }); break;
    case "model": openModelSheet(""); break;
    case "cd": openDirSheet(); break;
    case "wrap": toggleWrap(); break;
    case "workspace": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
  }
});

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
  if (now - ms < 8 * 3600 * 1000) return "Last 8 hours";
  const today = dayStart(now);
  const day = dayStart(ms);
  if (day >= today) return "Today";
  if (day >= today - DAY_MS) return "Yesterday";
  if (day >= today - 7 * DAY_MS) return "Previous 7 days";
  return "Older";
}

function sessionStamp(ts) {
  const date = new Date(ts * 1000);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = dayStart(Date.now());
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
  renderDirList();
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

  // Escape hatch: a typed absolute (or ~) path jumps straight there —
  // the rare case the browse/search flow doesn't cover.
  if (raw.startsWith("/") || raw.startsWith("~")) {
    const target = raw.startsWith("~") ? homeDir + raw.slice(1) : raw;
    list.appendChild(dirRow(`Go to ${raw}`, null, () => browseDir(target), "up"));
  }

  if (!query) {
    const recents = recentDirs().filter((p) => p !== dirPath);
    if (recents.length) {
      list.appendChild(sectionLabel("Recent"));
      for (const p of recents) {
        list.appendChild(dirRow(abbreviatePath(p), null, () => browseDir(p), "recent"));
      }
      list.appendChild(sectionLabel("Folders"));
    }
  }
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
  browseDir(currentCwd || homeDir || "/");
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
