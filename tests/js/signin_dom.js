// A minimal DOM, just large enough to run the SHIPPED `SIGNIN_FORM_JS`.
//
// Why this exists: which control the sign-in replay presses is decided by
// JavaScript that runs IN THE PAGE, so a Python fake that returns a canned
// `{submit: true}` pins the wiring and says nothing at all about the decision.
// eon.pl was exactly that gap — the shipped snippet returned `submit: false`
// on a real login form for a year and every test agreed with it, because no
// test ever ran the snippet.
//
// Same rule as `tests/js/harness.js`: the REAL source is evaluated here, never
// a hand-copied duplicate. It is handed in on stdin by `tests/test_signin.py`,
// which reads it straight off `aish.browser`.
//
// It is deliberately small. It implements only what the snippet touches, and
// only the selector shapes the snippet writes: `*`, `input`, `button`,
// `input[type=password]`, `input[type=submit]` and the comma-joined pair. A
// selector it cannot parse throws rather than quietly matching nothing — a
// silent empty match is the failure mode this whole file exists to catch.

'use strict';

const BOX = {width: 120, height: 32};
const NOTHING = {width: 0, height: 0};

function parseSelector(selector) {
  return selector.split(',').map((part) => {
    const trimmed = part.trim();
    const match = /^(\*|[a-zA-Z]+)(?:\[([a-zA-Z-]+)=([^\]]+)\])?$/.exec(trimmed);
    if (!match) throw new Error(`the fake DOM cannot parse '${trimmed}'`);
    const [, tag, attr, rawValue] = match;
    const value = rawValue === undefined
      ? undefined
      : rawValue.replace(/^['"]|['"]$/g, '');
    return {tag: tag === '*' ? null : tag.toUpperCase(), attr, value};
  });
}

class El {
  constructor(spec, doc) {
    this.tagName = String(spec.tag || 'div').toUpperCase();
    this.attrs = Object.assign({}, spec.attrs || {});
    this.label = spec.label || '';
    this.hidden = !!spec.hidden;
    this.children = [];
    this.parentElement = null;
    this.ownerDocument = doc;
    // Nothing here renders into a shadow root; the field exists because
    // DEEP_JS reads it on every element it walks past.
    this.shadowRoot = null;
    for (const child of spec.children || []) {
      const el = new El(child, doc);
      el.parentElement = this;
      this.children.push(el);
    }
  }

  get disabled() {
    return 'disabled' in this.attrs;
  }

  // The resolved absolute address, which is what a real HTMLFormElement.action
  // returns — and an action-less form answers with the document's own address.
  get action() {
    const declared = this.attrs.action;
    return new URL(declared === undefined ? '' : declared, this.ownerDocument.baseURI)
      .href;
  }

  get form() {
    let node = this.parentElement;
    while (node && node.tagName !== 'FORM') node = node.parentElement;
    return node || null;
  }

  getAttribute(name) {
    return name in this.attrs ? this.attrs[name] : null;
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  getBoundingClientRect() {
    return this.hidden ? NOTHING : BOX;
  }

  checkVisibility() {
    return !this.hidden;
  }

  getRootNode() {
    return this.ownerDocument;
  }

  descendants() {
    const out = [];
    for (const child of this.children) {
      out.push(child, ...child.descendants());
    }
    return out;
  }

  querySelectorAll(selector) {
    const parts = parseSelector(selector);
    return this.descendants().filter((el) => parts.some((part) => (
      (part.tag === null || part.tag === el.tagName)
      && (part.attr === undefined || el.getAttribute(part.attr) === part.value)
    )));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

// Build the page, run the shipped snippet over it, and report both what it
// RETURNED and where its tags actually landed. The tag is the load-bearing
// half: `_sign_in_on` presses `[data-aish-signin='submit']` and never reads
// the returned flag.
function run({js, origin, base, dom}) {
  const doc = {baseURI: base || `${origin}/login`};
  const root = new El({tag: 'body', children: [dom]}, doc);
  doc.body = root;
  doc.querySelectorAll = (selector) => root.querySelectorAll(selector);
  doc.querySelector = (selector) => root.querySelector(selector);

  const scope = {
    document: doc,
    location: {origin, href: doc.baseURI},
    URL,
    CSS: {escape: (value) => value},
    console,
  };
  const evaluate = new Function(
    ...Object.keys(scope), `"use strict"; return (${js});`,
  )(...Object.values(scope));

  const result = evaluate(origin);
  const tagged = {};
  for (const el of root.descendants()) {
    const role = el.getAttribute('data-aish-signin');
    if (role) tagged[role] = {tag: el.tagName, type: el.getAttribute('type'), label: el.label};
  }
  return {result, tagged};
}

let stdin = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { stdin += chunk; });
process.stdin.on('end', () => {
  process.stdout.write(JSON.stringify(run(JSON.parse(stdin))));
});
