// Node-only, dependency-free checks for aish's plan on the objective strip
// ([OBJECTIVE-STRIP] in app.js, #433).
//
// What is pinned here:
//   - the strip says the counts, and lists the OPEN tasks — the one being worked
//     on first — folding the rest into "+N more open" past PLAN_OPEN_SHOWN;
//   - done and dropped tasks are counted, never listed on the strip;
//   - each state reads in words, and a done says what it rests on;
//   - a pending replan request is said;
//   - the strip stays visible when there is a plan, even with no objective;
//   - dropping a task is asked in the shared modal, and both of his actions are
//     receipted `plan_action`s, never chat messages (the shipped source).
//
// Run manually: node tests/js/test_plan_strip.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function world() {
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "function objectiveStripHidden(state, emptyChat) {", "function renderPlanStrip(state) {")), sandbox);
  return sandbox;
}

function task(id, state, extra = {}) {
  return { id, title: `Task ${id}`, state, ...extra };
}

function countsOf(tasks) {
  const out = { pending: 0, doing: 0, done: 0, dropped_replan: 0, dropped_by_owner: 0 };
  for (const t of tasks) out[t.state] += 1;
  return out;
}

// ---- 1. counts and the open list --------------------------------------------
{
  const w = world();
  const tasks = [
    task("1", "done", { evidence: { ref: "t2.c3", quote: "curl -s a/health" } }),
    task("2", "pending"), task("3", "pending"), task("4", "doing"),
    task("5", "pending"), task("6", "dropped_replan"), task("7", "dropped_by_owner"),
  ];
  const copy = w.planCopy({ tasks, counts: countsOf(tasks) });
  ok("the summary counts done of the active tasks, and the dropped",
    copy.summary === "Plan · 1 of 5 done · 2 dropped");
  ok("the task being worked on comes first", copy.open[0].id === "4");
  ok("open tasks fold past the threshold",
    copy.open.length === 3 && copy.moreOpen === 1 && copy.open.map((t) => t.id).join() === "4,2,3");
  ok("done and dropped are never on the open list", !copy.open.some((t) => !t.open));
  ok("a done says what it rests on", copy.tasks[0].evidence === "rests on t2.c3: curl -s a/health");
  ok("states read in words",
    copy.tasks[5].word === "dropped when aish replanned" && copy.tasks[6].word === "dropped by you");
}

// ---- 2. few open tasks: nothing folds ----------------------------------------
{
  const w = world();
  const tasks = [task("1", "doing"), task("2", "pending")];
  const copy = w.planCopy({ tasks, counts: countsOf(tasks) });
  ok("two open tasks show whole", copy.moreOpen === 0 && copy.open.length === 2);
  ok("no dropped part when nothing was dropped", copy.summary === "Plan · 0 of 2 done");
}

// ---- 3. a replan request, and no plan at all --------------------------------
{
  const w = world();
  const tasks = [task("1", "pending")];
  const copy = w.planCopy({ tasks, counts: countsOf(tasks), replan_requested: true });
  ok("a pending replan request is said", copy.requested && /replan asked/.test(copy.summary));
  ok("no plan, no copy", w.planCopy(null) === null && w.planCopy({}) === null);
}

// ---- 4. the strip shows for a plan alone ------------------------------------
{
  const w = world();
  ok("a plan with no objective still shows the strip",
    !w.objectiveStripHidden({ objective: null, tracker: null, plan: { tasks: [] } }, true));
  ok("nothing at all in a fresh chat keeps it hidden",
    w.objectiveStripHidden({ objective: null, tracker: null, plan: null }, true));
}

// ---- 5. the wiring, in the shipped source -----------------------------------
{
  const block = extract(src, "// [OBJECTIVE-STRIP-START]", "// [OBJECTIVE-STRIP-END]");
  const drop = extract(src, "function askDropTask(task) {", "function askReplan() {");
  ok("a drop is asked in the shared modal, which says it is final",
    /askConfirm\(\{/.test(drop) && /cannot bring it back/.test(drop));
  ok("a drop is a receipted action naming the task, never a chat message",
    /act\(\{ type: "plan_action", name, action: "drop", task: task\.id \}/.test(drop) && !/type: "task"/.test(block));
  ok("a replan request is a receipted action",
    /act\(\{ type: "plan_action", name, action: "replan" \}/.test(block));
  ok("the event's plan reaches the state", /plan: event\.plan \|\| null/.test(block));
  ok("an optimistic objective edit keeps the plan", /plan: previous \? previous\.plan : null/.test(block));
  const slash = extract(src, "function handleSlash(text) {", "// ---- attachments");
  ok("/plan opens the plan", /case "\/plan": openPlanSheet\(\); return true;/.test(slash));
}

report("test_plan_strip");
