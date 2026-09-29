// Node-only, dependency-free checks for the objective strip ([OBJECTIVE-STRIP]
// in app.js, #432).
//
// What is pinned here:
//   - WHOSE words the strip shows is always said (L8): a tracker statement is
//     labelled as aish's reading, only an owner edit as his;
//   - "no objective yet" is sayable, and says what was OBSERVED about the
//     tracker — it has not looked, it looked and found none, it could not
//     tell, or it could not run and why — never a guessed cause;
//   - the trail reads newest first, with what became of each statement;
//   - the strip stays out of a fresh, empty chat but appears once there is
//     anything to say;
//   - an event for another chat never paints this one, and a switch clears it
//     (asserted against the shipped functions and the shipped dispatcher).
//
// Run manually: node tests/js/test_objective_strip.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function world(globals = {}) {
  const sandbox = { renders: [], ...globals };
  sandbox.renderObjective = (state) => sandbox.renders.push(state);
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "const OBJECTIVE_LEFT", "function renderObjective(state) {")), sandbox);
  vm.runInContext(surface(extract(src, "function onObjective(event) {", "function openObjectiveSheet() {")), sandbox);
  vm.runInContext("var objectiveState = null;", sandbox);
  return sandbox;
}

// ---- 1. whose words ---------------------------------------------------------
{
  const w = world();
  const read = w.objectiveCopy({ objective: { statement: "Know if it rains tomorrow", origin: "tracker" } });
  ok("a tracker statement is labelled as aish's reading", read.label === "aish reads your goal as:");
  ok("…and the sheet says it is not his words", /not your words/.test(read.whose));
  ok("the statement itself is shown", read.text === "Know if it rains tomorrow" && !read.empty);
  const own = w.objectiveCopy({ objective: { statement: "Mine", origin: "owner" } });
  ok("only his own edit is shown as his", own.label === "Your objective:" && /your own words/i.test(own.whose));
}

// ---- 2. none yet, and what was observed ------------------------------------
{
  const w = world();
  const never = w.objectiveCopy({ objective: null, tracker: null });
  ok("none yet is a statement the strip can make", never.empty && never.text === "none yet");
  ok("…and says aish has not looked yet", /has not read this chat/.test(never.whose));
  const none = w.objectiveCopy({ tracker: { turn: 3, status: "ok", verdict: "unchanged" } });
  ok("it looked and found none", /After turn 3/.test(none.whose) && /found no objective/.test(none.whose));
  const unsure = w.objectiveCopy({ tracker: { turn: 4, status: "ok", verdict: "unknown" } });
  ok("it looked and could not tell", /could not tell/.test(unsure.whose));
  const blocked = w.objectiveCopy({ tracker: { turn: 2, status: "unadmitted", why: "no admission recorded" } });
  ok("it could not run, and the recorded reason is quoted, not guessed",
    /did not produce one \(unadmitted: no admission recorded\)/.test(blocked.whose));
}

{
  const w = world();
  const dropped = w.objectiveCopy({ tracker: { turn: 5, status: "ok", verdict: "revised",
    discarded: "the chat was rewritten after this task ended" } });
  ok("a discarded reading says it was discarded, never 'found none'",
    /reading was discarded: the chat was rewritten/.test(dropped.whose) && !/found no/.test(dropped.whose));
}

// ---- 3. the trail -----------------------------------------------------------
{
  const w = world();
  const rows = w.objectiveTrail({
    trail: [
      { revision: 1, turn: 2, origin: "tracker", statement: "first", left: "evolved" },
      { revision: 2, turn: 5, origin: "owner", statement: "his", left: "pivoted" },
    ],
  });
  ok("newest first", rows.map((r) => r.text).join(",") === "his,first");
  ok("each says who wrote it and what became of it",
    rows[0].meta === "turn 5 · you · then pivoted" && rows[1].meta === "turn 2 · aish · then evolved");
  ok("an unknown change is said as unknown", /how is unknown/.test(
    w.objectiveTrail({ trail: [{ turn: 1, origin: "tracker", statement: "x", left: "unknown" }] })[0].meta));
  ok("no revision, no trail", w.objectiveTrail(null).length === 0);
}

// ---- 4. when the strip shows ------------------------------------------------
{
  const w = world();
  ok("nothing known: hidden", w.objectiveStripHidden(null, false));
  ok("a fresh empty chat keeps its welcome", w.objectiveStripHidden({ objective: null, tracker: null }, true));
  ok("a chat with turns says none yet", !w.objectiveStripHidden({ objective: null, tracker: null }, false));
  ok("a statement always shows", !w.objectiveStripHidden({ objective: { statement: "x" }, tracker: null }, true));
}

// ---- 5. an event for another chat never paints this one ---------------------
{
  const w = world({ currentSession: "a.jsonl" });
  w.onObjective({ type: "objective", name: "b.jsonl", objective: { statement: "theirs" }, tracker: null });
  ok("another chat's event is dropped", w.renders.length === 0);
  w.onObjective({ type: "objective", name: "a.jsonl", objective: { statement: "ours" }, tracker: null });
  ok("this chat's event paints", w.renders.length === 1 && w.renders[0].objective.statement === "ours");
  w.clearObjective();
  ok("clearing forgets it", w.renders.length === 2 && w.renders[1] === null);
}

// ---- 6. the wiring, in the shipped source ----------------------------------
{
  const handle = extract(src, "function handle(event) {", "function onSessionRenamed(event) {");
  ok("the dispatcher routes `objective` to its owner",
    /case "objective": onObjective\(event\); break;/.test(handle));
  const enter = extract(src, "// [SESSION-ENTER-START]", "// [SESSION-ENTER-END]");
  ok("a chat switch clears the strip", /if \(name !== currentSession\) clearObjective\(\);/.test(enter));
  const slash = extract(src, "function handleSlash(text) {", "// ---- attachments");
  ok("/objective opens the sheet and /objective edit the editor",
    /case "\/objective":/.test(slash) && /openObjectiveEditor\(\)/.test(slash) && /openObjectiveSheet\(\)/.test(slash));
  const safe = extract(src, "const VIEW_SAFE_EVENTS", "]);");
  ok("an objective event never dirties the transcript view", /"objective"/.test(safe));
  const edit = extract(src, '$("objective-form").addEventListener', "// [OBJECTIVE-STRIP-END]");
  ok("an edit is an action with a receipt, never a chat message",
    /act\(\{ type: "set_objective", name, statement \}/.test(edit) && !/type: "task"/.test(edit));
}

report("test_objective_strip");
