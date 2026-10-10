// What the site refused, shown in the browser sheet ([BROWSER-VIEW-FAILURES]).
//
// Every byte of a failure's `said` is the SITE's reply. It is shown so the
// owner can read what the server actually answered, and it must never become
// markup in aish's own origin — a hostile reply would otherwise script the PWA.
//
// Run manually: node tests/js/test_browser_view_failures.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

function fakeBox() {
  const box = {
    hidden: true, children: [],
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); },
  };
  return box;
}

function render(failures) {
  const box = fakeBox();
  const sandbox = {
    $: (id) => (id === "bv-failures" ? box : null),
    document: {
      createElement: () => {
        const el = { className: "", textContent: "" };
        Object.defineProperty(el, "innerHTML", {
          set() { throw new Error("site text reached innerHTML"); },
        });
        return el;
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(),
      "// [BROWSER-VIEW-FAILURES-START]", "// [BROWSER-VIEW-FAILURES-END]")),
    sandbox,
  );
  sandbox.bvShowFailures(failures);
  return box;
}

{
  const hostile = '<img src=x onerror="alert(1)">';
  const box = render([{ method: "POST", where: "https://x.pl/login", status: 400, said: hostile }]);
  ok("a failure is shown", !box.hidden && box.children.length === 1);
  ok("the site's reply is shown as text, verbatim",
    box.children[0].textContent === `POST https://x.pl/login → 400 · ${hostile}`);
}

{
  const box = render([{ method: "GET", where: "https://x.pl/", status: 503, said: "" }]);
  ok("an empty reply says so rather than showing nothing",
    box.children[0].textContent.endsWith("(no reply text)"));
}

{
  const box = render([]);
  ok("no failures hides the box", box.hidden && box.children.length === 0);
}

report("browser view failures");
