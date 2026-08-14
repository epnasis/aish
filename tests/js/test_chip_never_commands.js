// A chip is a MESSAGE, never a command ([CHIP-NEVER-COMMANDS], #221).
//
// Chip labels and payloads come from MODEL output, and a chip sends on ONE
// TAP. Under prompt injection that output is attacker-controlled, so routing a
// chip through handleSlash handed a hostile fetched page a privileged local
// action: it could render a friendly "Sign in to continue" button that opened
// aish's OWN remote-browser login sheet at a credential-harvesting URL. That
// sheet is deliberately built to feel trustworthy, which is precisely the
// trust the attack borrowed.
//
// Run manually: node tests/js/test_chip_never_commands.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

/** Run the REAL submitInput with everything it touches stubbed, and report
 *  which path the text took. */
function submit(text, options) {
  const seen = { slash: null, task: null };
  const sandbox = {
    hideSuggest() {},
    dictating: false,
    stopDictation() {},
    cmdMode: false,
    submitCommand() {},
    input: { value: text },
    handleSlash: (t) => { seen.slash = t; return true; },
    rememberPrompt() {},
    resizeInput() {},
    localStorage: { removeItem() {} },
    attachments: [],
    // Everything past the slash branch: reaching here means "sent as a message".
    sendTask: (t) => { seen.task = t; },
  };
  vm.createContext(sandbox);
  const src = extract(
    appSource(),
    "// [CHIP-NEVER-COMMANDS-START]",
    "// [CHIP-NEVER-COMMANDS-END]"
  ) + "\n  seen_task_marker = text;\n}\n";
  sandbox.seen_task_marker = null;
  vm.runInContext(surface(src), sandbox);
  sandbox.submitInput(options);
  return { slash: seen.slash, message: sandbox.seen_task_marker };
}

// Typed by the human: still a command. The feature must not regress.
{
  const r = submit("/browser https://allegro.pl", undefined);
  ok("a TYPED slash command still runs as a command", r.slash === "/browser https://allegro.pl");
}

// The attack: a model-authored chip carrying a slash command.
{
  const r = submit("/browser https://phish.example", { fromChip: true });
  ok("a CHIP carrying /browser never reaches handleSlash", r.slash === null);
  ok("…and is treated as ordinary message text instead",
    r.message === "/browser https://phish.example");
}

// Other privileged commands are equally off-limits from a chip.
for (const command of ["/cd /etc", "/add-dir /", "/new", "/feedback"]) {
  const r = submit(command, { fromChip: true });
  ok(`a chip carrying ${command} does not execute it`, r.slash === null);
}

// An ordinary chip reply is unaffected.
{
  const r = submit("yes please", { fromChip: true });
  ok("an ordinary chip reply is still sent as a message", r.message === "yes please");
}

report("a chip is a message, never a command");
