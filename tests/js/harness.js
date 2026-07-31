// Shared choreography harness for the app.js Node checks (issue #181).
//
// WHY this exists. The frontend tests verify DECISIONS — given these inputs, is
// the answer right? Every one of the four shipped incidents in #181 had a
// correct decision function and a broken feature, because the defects live in
// the CHOREOGRAPHY: when a decision is consulted, what happens when a step is
// skipped, what happens when two sources race. Observing that needs a world you
// can stop, reorder and starve — so this world is HOSTILE BY DEFAULT:
//
//   - the page is HIDDEN unless a test asks for visible, so anything that
//     depends on a transition or a frame running simply does not happen;
//   - requestAnimationFrame callbacks are collected and never fired (a hidden
//     page runs no frames) unless a test drains them;
//   - timers are DATA — `{fn, ms}` — so a test decides which deadline fires and
//     in what order, and can assert that one was armed at all;
//   - the fake element tracks its COMPUTED transform separately from its inline
//     style, because "the DOM lies" (clearing an inline transform only starts a
//     transition, which a hidden page never runs) was itself one of the bugs;
//   - FakeWebSocket can go zombie: readyState OPEN, delivering nothing, its
//     close() observably doing nothing — the failure no event announces;
//   - sessionWorld() layers on what session identity needs: a URL really
//     rewritten by replaceState, a localStorage that can refuse to write
//     (private mode), and a recorder for every collaborator a hello touches.
//
// It is deliberately NOT retrofitted onto the existing tests: they are pins, and
// rewriting 30 files to share plumbing risks weakening them for zero coverage.
// New tests require this; old ones keep their own copies.
//
// This file is not a test — tests/test_frontend_js.py globs test_*.js only.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const APP_JS = path.join(__dirname, "..", "..", "aish", "static", "app.js");

/** The shipped app.js, read fresh. Tests run the REAL code, never a copy. */
function appSource() {
  return fs.readFileSync(APP_JS, "utf8");
}

/** Slice `src` from `start` to `end` (end exclusive), failing loudly if either
 *  marker moved — a silently-empty extraction is a vacuously passing test. */
function extract(src, start, end) {
  const from = src.indexOf(start);
  assert(from !== -1, `start marker not found: ${start}`);
  const to = src.indexOf(end, from);
  assert(to !== -1, `end marker not found: ${end}`);
  return src.slice(from, to);
}

/** A vm context surfaces top-level `var` and function declarations as sandbox
 *  properties, but NOT const/let (they land in the context's lexical scope,
 *  invisible to the test). Rewrite those column-zero declarations so extracted
 *  code is reachable — the code itself is untouched, so what runs is what ships.
 *
 *  Function declarations are deliberately left ALONE: rewriting them to
 *  `var f = function f` both loses hoisting and turns any `}` followed by an
 *  IIFE into a call on the assignment (`var f = function(){}(function g(){})()`
 *  — how loading a state block before its owner failed). */
function surface(code) {
  return code.replace(/(^|\n)(?:const|let) /g, "$1var ");
}

// ---- fake DOM -------------------------------------------------------------

/** An element just rich enough to read back what code did to it. */
function fakeElement(tag = "div") {
  const el = {
    tagName: tag,
    id: "",
    className: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    dataset: {},
    style: {},
    scrollTop: 0,
    children: [],
    // The real property render code branches on ("is anything on screen?").
    get childElementCount() { return this.children.length; },
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    replaceChildren(...nodes) { this.children = nodes; },
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) on = !this._set.has(c); on ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
  };
  return el;
}

/** The element that LIES. Writing `style.transform` with a transition declared
 *  only STARTS a transition; in a hostile (hidden) world nothing advances it, so
 *  the computed value keeps the old position while the inline style already
 *  reads the new one. `settle()` is what a visible page eventually does. */
function lyingElement(width = 1100) {
  const el = fakeElement("div");
  el._computed = "none";
  el.style = {
    _t: "", _tr: "",
    get transform() { return this._t; },
    set transform(v) {
      this._t = v;
      if (!this._tr || this._tr === "none") el._computed = v || "none";
    },
    get transition() { return this._tr; },
    set transition(v) { this._tr = v; },
  };
  el.clientWidth = width;
  el.offsetWidth = width;
  return el;
}

// ---- controllable socket --------------------------------------------------

/** A WebSocket the test drives. Every interleaving #179 produced is reachable:
 *  deliver a message on a socket that should be dead, fire an onclose after its
 *  replacement is live, or hand out a zombie that accepts sends forever. */
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.closeCalls = 0;
    this.isZombie = false;
    this.onmessage = null;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this._listeners = {};
    this.constructor.created.push(this);
  }

  /** A fresh subclass with its own registry, so one scenario never counts
   *  another's sockets. `created` is read through this.constructor, so the
   *  subclass's own array wins. */
  static fresh() {
    const Socket = class extends FakeWebSocket {};
    Socket.created = [];
    return Socket;
  }

  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }

  /** The connection came up. */
  open() {
    this.readyState = FakeWebSocket.OPEN;
    if (this.onopen) this.onopen({ type: "open" });
    for (const fn of this._listeners.open || []) fn({ type: "open" });
    return this;
  }

  send(data) {
    this.sent.push(data);
    return true;
  }

  /** What the client actually asked for, parsed. */
  outbox() { return this.sent.map((s) => JSON.parse(s)); }

  /** Push a server event at whatever handler is still wired. Returns true if
   *  something received it — which is the whole question in the double-feed
   *  regression. */
  deliver(obj) {
    if (this.isZombie) return false; // a zombie accepts sends and answers nothing
    if (!this.onmessage) return false;
    this.onmessage({ data: JSON.stringify(obj) });
    return true;
  }

  close() {
    this.closeCalls += 1;
    if (this.isZombie) return; // the browser has not noticed it died
    this.readyState = FakeWebSocket.CLOSED;
    // Real close() fires onclose asynchronously; tests fire it explicitly (and
    // out of order, which is the point) via fireClose().
  }

  /** The socket announced its own death. Silently ignored once retired — which
   *  is exactly what stops a stray reconnect from being scheduled. */
  fireClose(event = { code: 1006 }) {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) { this.onclose(event); return true; }
    return false;
  }

  /** OPEN to everyone who asks, dead underneath: delivers nothing, and close()
   *  changes nothing observable. No event will ever announce this. */
  zombie() {
    this.readyState = FakeWebSocket.OPEN;
    this.isZombie = true;
    return this;
  }

  /** Fully neutralized — what retireSocket() promises. */
  get retired() {
    return this.onmessage === null && this.onopen === null
      && this.onclose === null && this.onerror === null;
  }

  /** Still able to feed the dispatcher. */
  get live() { return Boolean(this.onmessage); }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.created = [];

// ---- the world ------------------------------------------------------------

/**
 * A hostile sandbox plus the levers to un-hostile it one step at a time.
 *
 *   hostileWorld({ visible, width, globals })
 *
 * Returns an object over the vm context: `sandbox` (the globals the extracted
 * code sees and writes), `el` (the transcript element), `sockets` (every socket
 * constructed), and the levers below.
 */
function hostileWorld({ visible = false, width = 1100, globals = {} } = {}) {
  const src = appSource();
  const timers = [];
  const frames = [];
  const listeners = {};
  const el = lyingElement(width);
  const Socket = FakeWebSocket.fresh();

  const document_ = {
    visibilityState: visible ? "visible" : "hidden",
    get hidden() { return this.visibilityState !== "visible"; },
    createElement: (tag) => fakeElement(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    head: fakeElement("head"),
    body: fakeElement("body"),
  };

  const sandbox = {
    messagesEl: el,
    document: document_,
    WebSocket: Socket,
    getComputedStyle: () => ({ transform: el._computed }),
    // Timers are data. Nothing fires unless a test says so, which is how a
    // deadline that was never armed becomes a visible failure.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
    setInterval: (fn, ms) => { timers.push({ fn, ms, repeating: true }); return timers.length; },
    clearInterval: (id) => { if (id) timers[id - 1] = null; },
    // A hidden page runs no frames. Collected, never drained by default.
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: (id) => { if (id) frames[id - 1] = null; },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    console,
    JSON,
    Date,
    Math,
    Promise,
    URLSearchParams,
    Set,
    Map,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  Object.assign(sandbox, globals);
  vm.createContext(sandbox);

  const world = {
    src,
    sandbox,
    el,
    timers,
    frames,
    listeners,
    WebSocket: Socket,
    sockets: Socket.created,

    /** Evaluate raw code in the world. */
    run(code) { vm.runInContext(code, sandbox); return world; },

    /** Extract a marked block from app.js and evaluate it here. */
    load(start, end) { return world.run(surface(extract(src, start, end))); },

    /** Timers still armed (cleared ones are holes). */
    armed(pred = () => true) { return timers.filter((t) => t && pred(t)); },

    /** Fire every armed timer matching `pred`, clearing it first so the
     *  callback may re-arm. Returns how many fired. */
    fire(pred = () => true) {
      let fired = 0;
      timers.forEach((t, i) => {
        if (!t || !pred(t)) return;
        timers[i] = null;
        fired += 1;
        t.fn();
      });
      return fired;
    },

    /** Fire exactly one armed timer, failing if there is none to fire. */
    fireOne(pred = () => true) {
      const i = timers.findIndex((t) => t && pred(t));
      assert(i !== -1, "expected an armed timer, found none");
      const t = timers[i];
      timers[i] = null;
      t.fn();
      return t;
    },

    /** Let the page paint (opt-in: the hostile default never does). */
    drainFrames() {
      const queued = frames.splice(0, frames.length);
      for (const fn of queued) if (fn) fn();
      return queued.length;
    },

    /** A transition reaching its end state — the thing a hidden page withholds. */
    settle() { el._computed = el.style.transform || "none"; return world; },

    /** Move the page in or out of view; `dispatch` decides whether the
     *  registered visibilitychange listeners actually hear about it. */
    setVisible(v) { document_.visibilityState = v ? "visible" : "hidden"; return world; },

    /** Invoke listeners registered through document/window addEventListener. */
    dispatch(type, event = { type }) {
      const fns = listeners[type] || [];
      for (const fn of fns) fn(event);
      return fns.length;
    },
  };
  return world;
}

// ---- the session world ----------------------------------------------------

/** A localStorage that can REFUSE to write. Private mode is not an exotic
 *  environment — it is the one where an unguarded setItem turns a transition
 *  into a half-finished one, so it has to be a lever, not a footnote. */
function fakeStorage({ throws = false } = {}) {
  const map = new Map();
  return {
    map,
    throwOnSet: throws,
    getItem(key) { return map.has(key) ? map.get(key) : null; },
    setItem(key, value) {
      if (this.throwOnSet) throw new Error("QuotaExceededError (private mode)");
      map.set(key, String(value));
    },
    removeItem(key) { map.delete(key); },
  };
}

/**
 * The wider world session identity needs: a URL that really is rewritten by
 * history.replaceState, a storage that can throw, an element per id, and a
 * recorder for every collaborator a hello touches.
 *
 *   sessionWorld({ visible, storageThrows, globals })
 *
 * [SESSION-ENTER] is loaded for you (it is the owner under test in every
 * scenario); load whatever else a scenario drives — onHello, commitPage,
 * offlineFirstPaint — with `w.load(...)`, and pass real
 * implementations through `globals` where a recorder would hide the answer.
 * Anything loaded later shadows a recorder of the same name, which is how a
 * scenario opts into the REAL ensureCurrentInDeck.
 */
function sessionWorld({ visible = true, storageThrows = false, globals = {} } = {}) {
  const calls = [];
  const seen = { title: undefined, replays: [], busy: null, booted: false };
  const storage = fakeStorage({ throws: storageThrows });
  const elements = new Map();
  let current = new URL("https://aish.test/");

  const spy = (name, fn) => (...args) => {
    calls.push({ name, args });
    return fn ? fn(...args) : undefined;
  };
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, fakeElement("div"));
    return elements.get(id);
  };

  const location_ = {
    get href() { return current.href; },
    get search() { return current.search; },
    get hash() { return current.hash; },
    get pathname() { return current.pathname; },
    protocol: "https:",
    host: "aish.test",
  };
  const history_ = {
    replaceState(_state, _title, next) { current = new URL(next, current); },
  };

  const w = hostileWorld({
    visible,
    globals: {
      URL,
      URLSearchParams,
      localStorage: storage,
      location: location_,
      history: history_,
      $,
      publicSession: (name) => (name || "").replace(/\.jsonl$/, ""),
      storeSession: (id) => (!id || id.endsWith(".jsonl") ? id : `${id}.jsonl`),
      // view-cache state the owner reads and writes
      viewFp: "",
      viewDirty: true,
      offlineViewing: false,
      serverPainted: false,
      pendingCards: 0,
      clientBusy: false,
      renderedAnswers: 0,
      // Un-acknowledged send bubbles ([PENDING-SEND]): the view-cache decisions
      // read the count, and the replay paths reconcile them. Stubbed here —
      // test_pending_send.js drives the real block.
      pendingSends: [],
      resolvePendingSend() {},
      clearPendingSends() {},
      clearQueueChips() {},
      paintLanded() {},
      // app state a hello walks over
      recentSessions: [],
      cmdHistory: [],
      currentLogPath: "",
      taskErrored: false,
      consoleOpen: false,
      offlineMode: false,
      PAGE_REV: "rev1",
      FIRST_PAINT_GRACE_MS: 250,
      // collaborators: recorded, so a scenario can assert that the rest of a
      // transition still happened after the interesting step failed
      stashCurrentView: spy("stashCurrentView"),
      setTitle: spy("setTitle", (text) => { seen.title = text; }),
      offlineTouch: spy("offlineTouch"),
      refreshOfflinePinUi: spy("refreshOfflinePinUi"),
      refreshRailCurrent: spy("refreshRailCurrent"),
      onReplay: spy("onReplay", (event) => { seen.replays.push(event); }),
      hideBootLoader: spy("hideBootLoader", () => { seen.booted = true; }),
      setBusy: spy("setBusy", (busy) => { seen.busy = busy; }),
      setStatus: spy("setStatus"),
      setRolePill: spy("setRolePill"),
      updateEmptyHint: spy("updateEmptyHint"),
      renderWorkspace: spy("renderWorkspace"),
      schedulePeeks: spy("schedulePeeks"),
      markSeen: spy("markSeen"),
      setAttentionRows: spy("setAttentionRows"),
      requestSessions: spy("requestSessions"),
      railIsOpen: spy("railIsOpen", () => false),
      openConsole: spy("openConsole"),
      reloadThrottled: spy("reloadThrottled"),
      showToast: spy("showToast"),
      send: spy("send", () => true),
      offlineLoad: spy("offlineLoad", () => Promise.resolve(null)),
      offlineList: spy("offlineList", () => Promise.resolve([])),
      freshPrefetch: spy("freshPrefetch", () => null),
      ...globals,
    },
  });

  w.load("// [SESSION-ENTER-START]", "// [SESSION-ENTER-END]");

  w.calls = calls;
  w.seen = seen;
  w.storage = storage;
  w.$ = $;
  w.called = (name) => calls.filter((c) => c.name === name).length;
  w.lastCall = (name) => [...calls].reverse().find((c) => c.name === name);
  w.href = () => current.href;
  w.urlSession = () => current.searchParams.get("session");
  w.remembered = () => storage.getItem("aish-session");
  return w;
}

// ---- reporting ------------------------------------------------------------

/** The usual counter: `const { ok, report } = checks();` */
function checks() {
  let count = 0;
  return {
    ok(label, cond) { assert(cond, label); count += 1; },
    report(name) { console.log(`${name}: ${count} ok — all checks passed`); },
    get count() { return count; },
  };
}

/**
 * Guard against a vacuous pass. A choreography scenario often asserts inside a
 * queued callback; if a harness change lets the process exit before that runs,
 * the file must FAIL rather than silently assert nothing.
 *
 *   const done = expectReached("the async landing checks never ran");
 *   ... done();
 */
function expectReached(label) {
  const state = { reached: false };
  process.on("exit", (code) => {
    if (code === 0) assert(state.reached, label);
  });
  return () => { state.reached = true; };
}

module.exports = {
  APP_JS,
  appSource,
  extract,
  surface,
  fakeElement,
  lyingElement,
  fakeStorage,
  FakeWebSocket,
  hostileWorld,
  sessionWorld,
  checks,
  expectReached,
};
