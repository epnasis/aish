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
    case "echo": closeAnswer(); addAnsiMsg("echo", event.text); break;
    case "stream": addAnsiMsg("stream", event.text); break;
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
    case "file_list": onFileList(event); break;
    case "session_state": onSessionState(event); break;
  }
}

function onSessionState(event) {
  const short = event.session.replace(/^session-|\.jsonl$/g, "").replace(/-\d{6}$/, "");
  showToast(`session ${short}: task finished — tap the session title to open it`);
  if (!$("sessions-sheet").hidden) {
    send({ type: "sessions", query: $("sessions-search").value });
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
    answerEl = addMsg("answer md", "");
    answerText = "";
  }
  answerText += text;
  answerEl.replaceChildren(renderMarkdown(answerText));
  scrollToEnd();
}

function closeAnswer() {
  answerEl = null;
  answerText = "";
}

function onDone(event) {
  if (!answerText.trim() && event.result) {
    addMsg("answer md", "").replaceChildren(renderMarkdown(event.result));
  }
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

function addAnsiMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.appendChild(ansiFragment(text));
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
    else if (message.role === "assistant") {
      addMsg("answer md", "").replaceChildren(renderMarkdown(content));
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
      frag.appendChild(pre);
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
  for (let row = start + 2; row < lines.length && /^\|.*\|\s*$/.test(lines[row]); row++) {
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
  return wrap;
}

const INLINE_RE = new RegExp(
  "(`[^`]+`)" +
  "|(\\*\\*[^*]+\\*\\*|__[^_]+__)" +
  "|(\\*[^*\\s][^*]*\\*)" +
  "|(~~[^~]+~~)" +
  "|\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)"
);

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
  const prefixes = (event.prefixes || []).join(", ");
  const row = buttonRow(card, [
    ["Approve", "approve", () => answerCard(event.id, "approve")],
    ["Allow this session", "session",
      () => answerCard(event.id, "approve_session"),
      prefixes ? `auto-approve "${prefixes}" until the server restarts` : ""],
    ["Edit", "edit", () => showEditor()],
    ["Deny", "deny", () => answerCard(event.id, "deny")],
  ]);
  row.classList.add("grid2");
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

// ---- composer + autocomplete ---------------------------------------------
const input = $("input");

const SLASH_COMMANDS = [
  ["/model", "switch model — opens the searchable picker"],
  ["/resume", "search & resume an earlier session"],
  ["/new", "fresh conversation in a new session"],
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
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitInput();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.3)}px`;
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
    input.value = "";
    input.style.height = "auto";
    handleSlash(text);
    return;
  }
  if (!text && !attachments.length) return;
  if (attachments.length) {
    const listing = attachments.map((a) => `[attached file: ${a.path}]`).join("\n");
    text = text ? `${text}\n\n${listing}` : listing;
  }
  if (send({ type: "task", text })) {
    input.value = "";
    input.style.height = "auto";
    attachments = [];
    renderAttachments();
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
    case "/resume": openSessionsSheet(arg); break;
    case "/new": case "/clear": send({ type: "new" }); break;
    case "/cd": arg ? send({ type: "cd", path: arg }) : openSheet("workspace-sheet"); break;
    case "/add-dir": case "/dir-add":
      arg ? send({ type: "add_dir", path: arg }) : openSheet("workspace-sheet"); break;
    case "/jobs": openSheet("workspace-sheet"); send({ type: "jobs" }); break;
    case "/help": openSheet("workspace-sheet"); break;
    case "/quit": case "/exit": showToast("just close the tab — sessions persist"); break;
    default: showToast(`unknown command ${command}`);
  }
}

// ---- attachments ---------------------------------------------------------
let attachments = []; // {name, path}

$("attach").onclick = () => $("file-input").click();

$("file-input").addEventListener("change", async () => {
  for (const file of $("file-input").files) await uploadFile(file);
  $("file-input").value = "";
});

async function uploadFile(file) {
  const query = new URLSearchParams({ name: file.name });
  if (token) query.set("token", token);
  let response;
  try {
    response = await fetch(`/upload?${query}`, { method: "POST", body: file });
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
$("new-chip").onclick = () => send({ type: "new" });
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

const STATE_BADGES = {
  running: ["● running", "st-running"],
  waiting: ["● needs approval", "st-waiting"],
  idle: ["○ open", "st-open"],
};
const STATE_ORDER = { waiting: 0, running: 1, idle: 2, "": 3 };

function renderSessions(sessions) {
  const list = $("sessions-list");
  list.replaceChildren();
  if (!sessions.length) {
    list.textContent = "no matching sessions";
    return;
  }
  // Open sessions surface first (needs-approval, then running), keeping the
  // server's ranking within each group.
  const sorted = [...sessions].sort(
    (a, b) => STATE_ORDER[a.state || ""] - STATE_ORDER[b.state || ""]
  );
  for (const info of sorted) {
    const row = document.createElement("button");
    row.className = "row";
    if (info.state) {
      const [label, cls] = STATE_BADGES[info.state] || [];
      const badge = document.createElement("span");
      badge.className = `badge ${cls}`;
      badge.textContent = label;
      row.appendChild(badge);
    }
    row.appendChild(document.createTextNode(info.title));
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
