/* aish web client: one WebSocket, dumb rendering.
 *
 * The server owns all state; on every (re)connect it sends hello + a full
 * transcript replay and this client just clears the DOM and re-renders.
 * Approval cards are keyed by request id so a later approval_resolved (live
 * or replayed) collapses them.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");

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
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  ws = new WebSocket(`${proto}//${location.host}/ws${query}`);
  ws.onopen = () => {
    backoff = 1000;
    $("connbar").hidden = true;
  };
  ws.onmessage = (raw) => handle(JSON.parse(raw.data));
  ws.onclose = (event) => {
    if (event.code === 4000) {
      showToast("another device connected — this tab is detached");
      return; // deliberate replacement: do not fight over the session
    }
    if (event.code === 4403) {
      showToast("wrong or missing token — add ?token=… to the URL");
      return;
    }
    $("connbar").hidden = false;
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  };
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
const cards = new Map(); // approval id -> card element

function handle(event) {
  switch (event.type) {
    case "hello": onHello(event); break;
    case "replay": onReplay(event); break;
    case "user": closeAnswer(); addMsg("user", event.text); break;
    case "token": onToken(event.text); break;
    case "echo": closeAnswer(); addMsg("echo", event.text); break;
    case "stream": addMsg("stream", event.text); break;
    case "error": closeAnswer(); addMsg("error", event.text); setStatus(null); break;
    case "status": onStatus(event); break;
    case "approval_request": onApprovalRequest(event); break;
    case "approval_resolved": onApprovalResolved(event); break;
    case "done": onDone(event); break;
    case "history": onHistory(event.messages); break;
    case "session_list": renderSessions(event.sessions); break;
    case "model_list": renderModels(event); break;
    case "model_changed": onModelChanged(event); break;
    case "cwd_changed": renderWorkspace(event); break;
    case "job_list": $("ws-jobs").textContent = event.text || "—"; break;
  }
}

function onHello(event) {
  $("model-chip").textContent = event.model;
  $("session-chip").textContent = event.session
    .replace(/^session-|\.jsonl$/g, "")
    .replace(/-\d{6}$/, ""); // drop the microseconds suffix
  renderWorkspace(event);
  if (!event.busy) setStatus(null);
}

function onReplay(event) {
  messagesEl.replaceChildren();
  cards.clear();
  answerEl = null;
  answerText = "";
  if (event.truncated) addMsg("notice", "… earlier events trimmed …");
  for (const item of event.events) handle(item);
  scrollToEnd(true);
}

function onToken(text) {
  if (!answerEl) {
    answerEl = addMsg("answer", "");
    answerText = "";
  }
  answerText += text;
  answerEl.textContent = answerText;
  scrollToEnd();
}

function closeAnswer() {
  answerEl = null;
  answerText = "";
}

function onDone(event) {
  if (!answerText.trim() && event.result) addMsg("answer", event.result);
  closeAnswer();
  setStatus(null);
}

function onStatus(event) {
  if (event.state === "idle") { setStatus(null); return; }
  let text = `${event.label || "working"}…`;
  if (event.tokens) text += ` · ↓ ${event.tokens >= 1000 ? (event.tokens / 1000).toFixed(1) + "k" : event.tokens} tokens`;
  setStatus(text);
}

function setStatus(text) {
  $("statusline").hidden = !text;
  $("status-text").textContent = text || "";
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

function nearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;
}

function scrollToEnd(force) {
  if (force || nearBottom()) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function onHistory(history) {
  addMsg("notice", `— resumed ${history.length} messages —`);
  for (const message of history) {
    const content = (message.content || "").trim();
    if (!content) continue;
    if (message.role === "user") addMsg("user", content);
    else if (message.role === "assistant") addMsg("answer", content);
    else {
      const lines = content.split("\n");
      const shown = lines.slice(0, 4).join("\n");
      addMsg("echo", lines.length > 4 ? `${shown}\n… (${lines.length - 4} more lines)` : shown);
    }
  }
  scrollToEnd(true);
}

// ---- approval cards ------------------------------------------------------
function onApprovalRequest(event) {
  closeAnswer();
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
  messagesEl.appendChild(card);
  scrollToEnd(true);
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
  for (const [label, cls, fn] of specs) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
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
    for (const b of card.querySelectorAll("button")) b.disabled = true;
  }
}

function buildCommandCard(card, event) {
  const parts = [document.createTextNode("▶ run command? ")];
  if (event.destructive) {
    const warn = document.createElement("span");
    warn.className = "destructive";
    warn.textContent = "⚠ destructive";
    parts.push(warn);
  }
  title(card, parts);
  const code = document.createElement("code");
  code.textContent = event.command;
  card.appendChild(code);
  const row = buttonRow(card, [
    ["Approve", "approve", () => answerCard(event.id, "approve")],
    ["Edit", "edit", () => showEditor()],
    ["Deny", "deny", () => answerCard(event.id, "deny")],
  ]);
  function showEditor() {
    row.hidden = true;
    const area = document.createElement("textarea");
    area.value = event.command;
    card.appendChild(area);
    const editRow = buttonRow(card, [
      ["Run edited", "approve", () =>
        answerCard(event.id, "edit", { command: area.value })],
      ["Cancel", "deny", () => { area.remove(); editRow.remove(); row.hidden = false; }],
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
  buttonRow(card, [
    ["Approve", "approve", () => answerCard(event.id, "approve")],
    ["Deny", "deny", () => answerCard(event.id, "deny")],
  ]);
}

function buildReadCard(card, event) {
  const label = event.reason === "outside"
    ? "▶ read file outside the project?"
    : "▶ read sensitive file? ⚠ may contain secrets";
  title(card, [document.createTextNode(label)]);
  const code = document.createElement("code");
  code.textContent = event.path;
  card.appendChild(code);
  buttonRow(card, [
    ["Approve", "approve", () => answerCard(event.id, "approve")],
    ["Deny", "deny", () => answerCard(event.id, "deny")],
  ]);
}

function onApprovalResolved(event) {
  const card = cards.get(event.id);
  if (!card) return;
  card.replaceChildren();
  card.className = "card resolved";
  const verdict = document.createElement("div");
  verdict.className = `verdict ${event.decision === "denied" ? "denied" : "approved"}`;
  verdict.textContent = `${event.decision}: ${(card.dataset.summary || "").slice(0, 120)}`;
  card.appendChild(verdict);
}

// ---- composer ------------------------------------------------------------
const input = $("input");

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  submitInput();
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitInput();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.3)}px`;
});

function submitInput() {
  const text = input.value.trim();
  if (!text) return;
  if (text.startsWith("/")) {
    input.value = "";
    input.style.height = "auto";
    handleSlash(text);
    return;
  }
  if (send({ type: "task", text })) {
    input.value = "";
    input.style.height = "auto";
  }
}

function handleSlash(text) {
  const [command, ...rest] = text.split(/\s+/);
  const arg = rest.join(" ");
  switch (command) {
    case "/model": openModelSheet(arg); break;
    case "/resume": openSessionsSheet(arg); break;
    case "/new": case "/clear": send({ type: "new" }); break;
    case "/cd": arg ? send({ type: "cd", path: arg }) : openSheet("workspace-sheet"); break;
    case "/add-dir": case "/dir-add":
      arg ? send({ type: "add_dir", path: arg }) : openSheet("workspace-sheet"); break;
    case "/jobs": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
    case "/help": openSheet("workspace-sheet"); break;
    default: showToast(`unknown command ${command}`);
  }
}

// ---- sheets --------------------------------------------------------------
function openSheet(id) {
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  $(id).hidden = false;
}
function closeSheets() {
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
}
for (const b of document.querySelectorAll("[data-close]")) {
  b.onclick = () => { $(b.dataset.close).hidden = true; };
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// sessions
$("session-chip").onclick = () => openSessionsSheet("");
$("new-chat").onclick = () => { send({ type: "new" }); closeSheets(); };
$("sessions-search").addEventListener(
  "input",
  debounce(() => send({ type: "sessions", query: $("sessions-search").value }), 150)
);

function openSessionsSheet(query) {
  openSheet("sessions-sheet");
  $("sessions-search").value = query;
  send({ type: "sessions", query });
}

function renderSessions(sessions) {
  const list = $("sessions-list");
  list.replaceChildren();
  if (!sessions.length) {
    list.textContent = "no matching sessions";
    return;
  }
  for (const info of sessions) {
    const row = document.createElement("button");
    row.className = "row";
    row.textContent = info.title;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${info.when} · ${info.count} msgs${info.model ? " · " + info.model : ""}`;
    row.appendChild(meta);
    row.onclick = () => { send({ type: "resume", path: info.name }); closeSheets(); };
    list.appendChild(row);
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
  $("model-list").textContent = "loading models…";
  send({ type: "models", query });
}

function renderModels(event) {
  const list = $("model-list");
  list.replaceChildren();
  for (const model of event.models) {
    const row = document.createElement("button");
    row.className = "row" + (model.name === event.current ? " current" : "");
    row.textContent = model.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = model.desc;
    row.appendChild(meta);
    row.onclick = () =>
      send({ type: "set_model", spec: model.name, save: $("model-save").checked });
    list.appendChild(row);
  }
}

function onModelChanged(event) {
  $("model-chip").textContent = event.model;
  closeSheets();
  showToast(event.saved ? `model: ${event.model} (saved as default)` : `model: ${event.model}`);
}

// workspace
$("menu-chip").onclick = () => { openSheet("workspace-sheet"); send({ type: "jobs" }); };
$("cd-go").onclick = () => {
  const path = $("cd-input").value.trim();
  if (path) { send({ type: "cd", path }); $("cd-input").value = ""; }
};
$("root-add").onclick = () => {
  const path = $("root-input").value.trim();
  if (path) { send({ type: "add_dir", path }); $("root-input").value = ""; }
};
$("jobs-refresh").onclick = () => send({ type: "jobs" });

function renderWorkspace(event) {
  if (event.cwd) $("ws-cwd").textContent = event.cwd;
  if (event.roots) $("ws-roots").textContent = event.roots.join("\n");
}

// toast
let toastTimer;
function showToast(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
}

connect();
