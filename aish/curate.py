"""Retrieval self-curation: ledger + weekly curation pass (#185).

The #183 audit was a hand-run reconstruction; this module makes it a loop.
Two layers, deliberately separated because they have different costs:

Layer 1 — the LEDGER (`scan_ledger`): pure code over the session logs, zero
model calls. Since #183 every preflight injection is logged as a `knowledge`
trace step carrying per-entry sim/rail and the selection mode, and the same
logs record what the task did next. The scan pairs each injection with its
task window (user message → next user message) and measures the two signals
no judge is needed for: ENGAGEMENT (the model read_skill'd an entry that was
injected for it) and MISSES (the model deliberately read_skill'd an entry
preflight did NOT inject — retrieval failed to surface it).

Layer 2 — the CURATION PASS (`run_curate`, console entry point
`aish-curate`, scheduled via launchd like aish-email-poll): computes the
ledger, and when there is something actionable, POSTs a curation task to
/trigger (origin="schedule"). ALL evidence rides in the prompt — a non-user
session cannot roam the past-session archive (#178 P0-2), and that
containment is the point, not a limitation. The triggered session's action
vocabulary is BOUNDED and reversible: repair descriptions/keywords, pin
standing rules, and disable entries via remember(disabled=true) — all
auto-approved memory writes, all recoverable from the knowledge git backup.
Deletion and skill-FILE edits are only ever PROPOSED (they hold as approval
cards — draft-and-hold, same as any automated session's mutations).

Deliberately NOT here: automatic threshold retuning. The ledger informs;
`PREFLIGHT_MIN_SIM` changes by human decision — a self-tuning floor fed by
noisy proxies is a silent-regression machine.

Testability mirrors email_poll.py: the HTTP POST is a parameter seam, the
state dir and clock are injectable, and the ledger is pure parsing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .email_poll import PostResult, http_post, read_token, trigger

# The curation prompt aggregates task excerpts from EVERY recent session —
# including chats that ran purely on the local model because their content
# was private — so it must run on a model meeting the strictest privacy bar
# among its sources: a LOCAL one. Cloud specs are for explicit experiments
# (--model / AISH_CURATE_MODEL); the server refuses with 503 when the
# requested model can't be built, never falling back to its cloud default.
DEFAULT_MODEL = "qwen3:8b"

LEDGER_DAYS = 14  # how far back the scan reads
MIN_INJECTIONS = 4  # fewer than this is not evidence of dead weight
MISS_MIN = 2  # deliberate reads of a never-injected entry before it's a miss
EVIDENCE_PER_ENTRY = 3  # recent example tasks quoted per suspect
EVIDENCE_PROMPT_CHARS = 120  # per quoted task prompt
REPORT_MAX_CHARS = 9000  # the whole ledger section of the prompt
SUSPECTS_MAX = 12  # entries judged per pass — small on purpose, weekly cadence


def log(msg: str) -> None:
    print(f"[curate] {msg}", file=sys.stderr, flush=True)


@dataclass
class EntryStats:
    """Per-entry ledger row, accumulated across task windows."""

    name: str
    kind: str = ""
    injections: int = 0
    reads: int = 0  # read_skill of this entry AFTER it was injected — engagement
    miss_reads: int = 0  # read_skill of this entry when it was NOT injected
    sims: list[float] = field(default_factory=list)
    rails: int = 0  # injections that entered on a name/keyword rail
    evidence: list[tuple[str, str, float | None]] = field(default_factory=list)
    # (ts, prompt head, sim) — newest kept, EVIDENCE_PER_ENTRY per entry

    @property
    def median_sim(self) -> float | None:
        if not self.sims:
            return None
        ordered = sorted(self.sims)
        return ordered[len(ordered) // 2]


@dataclass
class Ledger:
    entries: dict[str, EntryStats] = field(default_factory=dict)
    tasks: int = 0
    tasks_with_injection: int = 0
    lexical_tasks: int = 0  # selection ran without embeddings (fallback visible)

    def stat(self, name: str, kind: str = "") -> EntryStats:
        row = self.entries.setdefault(name, EntryStats(name))
        if kind and not row.kind:
            row.kind = kind
        return row


def _windows(path: Path):
    """Yield (ts, prompt, injected_items, read_skill_names) task windows from
    one session log. The `knowledge` trace step is emitted just BEFORE its
    user message is appended (agent.run_task order), so a pending injection
    attaches to the NEXT user record — the same pairing the #183 audit used."""
    pending: list[dict] | None = None
    current: dict | None = None
    for line in path.open(encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind = rec.get("kind")
        if kind == "trace":
            step = rec.get("step", {})
            sk = step.get("kind")
            if sk == "knowledge":
                pending = step.get("items", [])
            elif sk == "tool" and current is not None:
                if step.get("name") == "read_skill":
                    current["reads"].append(str(step.get("summary", "")).strip())
        elif kind == "message" and rec.get("role") == "user":
            if current is not None:
                yield current
            current = {
                "ts": str(rec.get("ts", "")),
                "prompt": str(rec.get("content", ""))[:500],
                "injected": pending or [],
                "reads": [],
            }
            pending = None
    if current is not None:
        yield current


def scan_ledger(state_dir, days: int = LEDGER_DAYS, now: datetime | None = None) -> Ledger:
    """Aggregate every task window in the recent logs into per-entry stats.
    Session files are date-named (session-YYYYMMDD-…), so the age cut is a
    filename comparison — no file is opened outside the window."""
    now = now or datetime.now()
    floor = f"session-{(now - timedelta(days=days)):%Y%m%d}"
    ledger = Ledger()
    for path in sorted(Path(state_dir).glob("session-*.jsonl")):
        if path.name < floor:
            continue
        try:
            for window in _windows(path):
                ledger.tasks += 1
                injected_names = set()
                lexical = False
                for item in window["injected"]:
                    name = str(item.get("label") or item.get("name") or "")
                    if not name:
                        continue
                    injected_names.add(name)
                    row = ledger.stat(name, str(item.get("kind", "")))
                    row.injections += 1
                    sim = item.get("sim")
                    if isinstance(sim, (int, float)):
                        row.sims.append(float(sim))
                    else:
                        lexical = lexical or "score" in item
                    if item.get("rail") or "score" in item:
                        row.rails += 1
                    if len(row.evidence) < EVIDENCE_PER_ENTRY:
                        row.evidence.append(
                            (
                                window["ts"],
                                " ".join(window["prompt"].split())[:EVIDENCE_PROMPT_CHARS],
                                float(sim) if isinstance(sim, (int, float)) else None,
                            )
                        )
                if injected_names:
                    ledger.tasks_with_injection += 1
                    if lexical:
                        ledger.lexical_tasks += 1
                for read in window["reads"]:
                    if not read:
                        continue
                    row = ledger.stat(read, "skill")
                    if read in injected_names:
                        row.reads += 1
                    else:
                        row.miss_reads += 1
        except OSError:
            continue
    return ledger


def dead_weight(ledger: Ledger) -> list[EntryStats]:
    """Entries injected repeatedly with zero engagement, worst first. Skills
    have a hard engagement signal (read_skill); memories cannot (a fact
    influences an answer invisibly), so for them frequency + low similarity
    is the flag and the judge makes the call."""
    rows = [
        r
        for r in ledger.entries.values()
        if r.injections >= MIN_INJECTIONS and r.reads == 0
    ]
    rows.sort(key=lambda r: (-r.injections, r.median_sim or 0.0))
    return rows[:SUSPECTS_MAX]


def missing(ledger: Ledger) -> list[EntryStats]:
    """Entries the model deliberately reached for that preflight never (or
    rarely) surfaced — their identity lines need repair, not the threshold."""
    rows = [
        r
        for r in ledger.entries.values()
        if r.miss_reads >= MISS_MIN and r.miss_reads > r.injections
    ]
    rows.sort(key=lambda r: -r.miss_reads)
    return rows[:SUSPECTS_MAX]


IDENTITY_CHARS = 220  # per-suspect identity line quoted in the report
GONE_NOTE = "no longer active — already retired or deleted; SKIP it"


def corpus_identities(cwd: str = "") -> dict[str, str]:
    """Current identity line per ACTIVE entry — the no-shell rule's other
    half (#185 follow-up): the session must never need to list or read
    knowledge files, so the code composing its prompt reads them instead.
    Runs in the aish-curate process (the owner's own launchd job), where
    filesystem access is attended-equivalent by definition."""
    from . import skills

    identities: dict[str, str] = {}
    for entry in skills.load_entries(cwd or str(Path.home()), None):
        line = entry.description
        if entry.keywords:
            line += f" [keywords: {', '.join(entry.keywords)}]"
        if entry.pinned:
            line += " (pinned)"
        identities[entry.name] = line[:IDENTITY_CHARS]
    return identities


def render_report(ledger: Ledger, identities: dict[str, str] | None = None) -> str:
    """The ledger as compact prompt text; empty string when there is nothing
    actionable (the caller then skips the trigger entirely). `identities`
    (name → current description/keywords line) rides along per suspect so
    the session judges from complete information; a suspect absent from it
    was already retired, and saying so stops the model acting on ghosts."""
    dead = dead_weight(ledger)
    missed = missing(ledger)
    if not dead and not missed:
        return ""
    identities = identities or {}

    def identity_line(name: str) -> str:
        return f"    now: {identities.get(name, GONE_NOTE)}"

    lines = [
        f"Ledger window: {ledger.tasks} tasks, {ledger.tasks_with_injection} "
        f"with injections, {ledger.lexical_tasks} on lexical fallback.",
    ]
    if dead:
        lines.append("\nDEAD WEIGHT — injected repeatedly, never engaged:")
        for r in dead:
            sim = f"median sim {r.median_sim:.2f}" if r.median_sim is not None else "no sims"
            lines.append(
                f"- {r.name} ({r.kind or 'entry'}): {r.injections} injections, "
                f"{r.rails} via rail, {sim}"
            )
            lines.append(identity_line(r.name))
            for ts, prompt, s in r.evidence:
                tag = f" (sim {s:.2f})" if s is not None else ""
                lines.append(f"    e.g. {ts[:16]}{tag}: {prompt!r}")
    if missed:
        lines.append("\nMISSED — deliberately read but not surfaced by preflight:")
        for r in missed:
            lines.append(
                f"- {r.name}: read {r.miss_reads}x without injection "
                f"(injected only {r.injections}x)"
            )
            lines.append(identity_line(r.name))
    text = "\n".join(lines)
    if len(text) > REPORT_MAX_CHARS:
        text = text[:REPORT_MAX_CHARS] + "\n…(report truncated)"
    return text


def build_prompt(report: str) -> str:
    return (
        "You are the weekly KNOWLEDGE CURATION pass — an automated session, "
        "no human watching. The retrieval ledger below was measured from the "
        "last two weeks of session logs: which saved skills/memories were "
        "injected, at what similarity, and whether they were ever actually "
        "used.\n\n"
        f"{report}\n\n"
        "For EACH dead-weight entry, in order:\n"
        "1. Inspect it first. Each suspect's 'now:' line IS its current "
        "description/keywords — call recall(name=<entry>) only when you also "
        "need the body. An entry marked 'no longer active' is already retired: "
        "SKIP it, never recreate it. NEVER act on the stats line alone.\n"
        "2. Then choose ONE action:\n"
        "   - REPAIR: if the entry is useful but its description/keywords are "
        "generic (that is WHY it fires on unrelated tasks), rewrite them via "
        "remember(name=<entry>, note=<corrected description>, keywords=<few "
        "DISTINCTIVE triggers>) — keep keywords specific, never generic words "
        "like 'code' or 'change'.\n"
        "   - PIN: if it is a standing behavior rule that should apply to "
        "every task (an 'always/never do X'), remember(name=<entry>, "
        "note=<its description>, pinned=true).\n"
        "   - DISABLE: if it is stale, redundant, or noise, "
        "remember(name=<entry>, note=<its description>, disabled=true). "
        "This is reversible and the correct retirement path.\n"
        "For each MISSED entry: repair its description/keywords so retrieval "
        "finds it (add the words tasks actually used, per the ledger).\n\n"
        "HARD RULES for this session:\n"
        "- You MUST NOT call forget_memory — deletion is proposed in your "
        "summary only, never executed here.\n"
        "- You MUST NOT use run_command or any shell: everything you need is "
        "reachable via recall/read_skill, and a shell command in this "
        "unattended session STALLS it on an approval card until the owner "
        "returns.\n"
        "- Skill FILES (write_file/edit_file on skills) will be HELD for "
        "approval; only propose such an edit when it clearly matters.\n"
        "- Change at most one thing per entry; when unsure, do nothing and "
        "say why.\n\n"
        "Finish with a compact summary: entries repaired / pinned / disabled "
        "/ left alone, and any proposals needing the owner."
    )


def run_curate(
    *,
    post: Callable[..., PostResult] = http_post,
    env: Mapping[str, str] | None = None,
    token: str | None = None,
    state_dir=None,
    now: datetime | None = None,
    dry_run: bool = False,
    model: str | None = None,
) -> int:
    """One curation pass: scan, and trigger only when actionable. The ISO-week
    dedup key — suffixed with the model slug — means a retried launchd job
    (or an overlapping manual run) cannot double-open a session within the
    same week, while an explicit model experiment gets its own session
    instead of deduping into the scheduled one."""
    env = os.environ if env is None else env
    now = now or datetime.now()
    if state_dir is None:
        state_dir = Path.home() / ".local" / "state" / "aish"
    if model is None:
        model = env.get("AISH_CURATE_MODEL", "").strip() or DEFAULT_MODEL
    ledger = scan_ledger(state_dir, now=now)
    report = render_report(ledger, corpus_identities())
    if not report:
        log(f"nothing to curate ({ledger.tasks} tasks scanned)")
        return 0
    prompt = build_prompt(report)
    if dry_run:
        print(prompt)
        return 0
    if token is None:
        token = read_token()
    if not token:
        log("no token (Keychain aish/AISH_WEB_TOKEN or env) — refusing to run")
        return 2
    base = env.get("AISH_WEB_URL", "http://192.168.10.20:8787")
    week = f"{now:%G-W%V}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", model).strip("-").lower()
    ok = trigger(
        base,
        token,
        prompt,
        meta={"dedup_key": f"curate-{week}-{slug}"},
        title=f"Knowledge curation {week}",
        post=post,
        origin="schedule",
        model=model,
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="aish retrieval self-curation pass (#185)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the curation prompt instead of triggering a session",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "model spec for the curation session (default: $AISH_CURATE_MODEL "
            f"or {DEFAULT_MODEL}; keep it LOCAL — the prompt aggregates "
            "excerpts from private sessions)"
        ),
    )
    args = parser.parse_args()
    return run_curate(dry_run=args.dry_run, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
