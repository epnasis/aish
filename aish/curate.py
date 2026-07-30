"""Retrieval self-curation: ledger + scripted judge loop (#185, #186).

The #183 audit was a hand-run reconstruction; this module makes it a loop.
Three layers, deliberately separated because they have different costs:

Layer 1 — the LEDGER (`scan_ledger`): pure code over the session logs, zero
model calls. Since #183 every preflight injection is logged as a `knowledge`
trace step carrying per-entry sim/rail and the selection mode, and the same
logs record what the task did next. The scan pairs each injection with its
task window (user message → next user message) and measures the two signals
no judge is needed for: ENGAGEMENT (the model read_skill'd an entry that was
injected for it) and MISSES (the model deliberately read_skill'd an entry
preflight did NOT inject — retrieval failed to surface it).

Layer 2 — the JUDGE LOOP (`run_curate`, console entry point `aish-curate`,
weekly via launchd): the ORCHESTRATION LIVES IN THIS SCRIPT, not in a model
session. v1 handed one agentic session a 12-suspect work queue and an 8B
local model could not hold it — 55 reads, zero actions (#186 A/B). Now the
script loops; each model interaction is ONE bounded decision with everything
it needs in the prompt (identity, body, stats, evidence) and a forced
verdict format: repair | pin | disable | skip. No tools, no session, no
server — `backends.make_chat` is called directly, so the model is a pure
judge: text in, one verdict out. The ACTION ENVELOPE IS CODE, not prompt
obedience: `parse_verdict` accepts only the four verbs, `update_entry_meta`
rewrites frontmatter only (body preserved byte-for-byte), and deletion has
no code path at all. Everything is reversible via the knowledge git backup.
Every judged entry lands in `curation-actions.jsonl`, which also drives the
cooldown: an entry acted on (or skipped) recently is not re-judged, making a
retried launchd job idempotent without any server-side dedup.

Layer 3 — the DUPLICATE PASS: cross-entry judgment doesn't fit a one-entry
prompt, so it is its own bounded sub-pass. Candidate pairs are proposed
DETERMINISTICALLY (identity-line embedding similarity via SemanticIndex,
difflib ratio fallback — same thresholds as save_memory's near-duplicate
gate) and the judge answers one pairwise question: merge or distinct. A
merge disables the loser and optionally repairs the survivor — still inside
the same envelope, still no deletion.

Privacy (#186): evidence excerpts quote what the owner typed, so the judge
DEFAULTS TO THE LOCAL MODEL and everything stays on this machine — no
prompt ever leaves it unless --model names a cloud spec explicitly. This
box's ceiling is 8B-class (16 GB minus resident VMs; a 14B loaded here
crashed the host).

Deliberately NOT here: automatic threshold retuning. The ledger informs;
`PREFLIGHT_MIN_SIM` changes by human decision — a self-tuning floor fed by
noisy proxies is a silent-regression machine.

Testability: the judge is a `judge=` callable seam (prompt → text), the
duplicate scorer a `scores=` seam, the notifier a `notify_fn=` seam, and the
state dir and clock are injectable — no model, no network, no Keychain.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import skills
from .embeddings import entry_text

# The judge reads evidence excerpts of what the owner typed, so it must run
# on a model meeting the strictest privacy bar among its sources: a LOCAL
# one. Cloud specs are for explicit experiments only (--model /
# AISH_CURATE_MODEL). qwen3:8b is also this host's memory ceiling.
DEFAULT_MODEL = "qwen3:8b"

LEDGER_DAYS = 14  # how far back the scan reads
MIN_INJECTIONS = 4  # fewer than this is not evidence of dead weight
MISS_MIN = 2  # deliberate reads of a never-injected entry before it's a miss
EVIDENCE_PER_ENTRY = 3  # recent example tasks quoted per suspect
EVIDENCE_PROMPT_CHARS = 120  # per quoted task prompt
SUSPECTS_MAX = 12  # entries judged per pass — small on purpose, weekly cadence

JUDGE_BODY_CHARS = 2500  # entry body quoted to the judge (identity is short)
JUDGE_NUM_CTX = 8192  # one bounded decision needs no more
ACTION_COOLDOWN_DAYS = 10  # judged (incl. skipped) entries rest between passes
PAIRS_MAX = 6  # duplicate pairs judged per pass
ACTIONS_FILE = "curation-actions.jsonl"

VERDICTS = ("repair", "pin", "disable", "skip")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


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


# ---------------------------------------------------------------------------
# Layer 2: the judge loop — one entry, one bounded decision, envelope in code.


@dataclass
class Verdict:
    action: str  # one of VERDICTS
    reason: str = ""
    description: str = ""
    keywords: str = ""


def judge_prompt(entry, stat: EntryStats | None, category: str) -> str:
    """Everything the judge needs about ONE entry, plus a forced answer
    format with an example — imperative phrasing because small local models
    ignore anything softer."""
    body = entry.body.strip()
    if len(body) > JUDGE_BODY_CHARS:
        body = body[:JUDGE_BODY_CHARS] + "…"
    lines = [
        "You are a strict knowledge-base curator. Judge ONE saved entry and "
        "answer ONLY in the exact format shown at the end.",
        "",
        f'THE ENTRY ({entry.kind} "{entry.name}"):',
        f"description: {entry.description}",
        f"keywords: {', '.join(entry.keywords) if entry.keywords else '(none)'}",
        f"pinned: {'yes' if entry.pinned else 'no'}",
        "body:",
        body or "(empty)",
        "",
    ]
    if stat is not None and category == "dead-weight":
        sim = f"{stat.median_sim:.2f}" if stat.median_sim is not None else "n/a"
        lines += [
            "RETRIEVAL EVIDENCE (last two weeks): this entry was auto-injected "
            f"into {stat.injections} tasks and NEVER used ({stat.rails} entered "
            f"via keyword match, median similarity {sim}). Tasks it was "
            "injected into:",
        ]
        for ts, prompt, s in stat.evidence:
            tag = f" (sim {s:.2f})" if s is not None else ""
            lines.append(f'- {ts[:16]}{tag}: "{prompt}"')
    elif stat is not None:
        lines += [
            "RETRIEVAL EVIDENCE (last two weeks): the assistant deliberately "
            f"looked this entry up {stat.miss_reads} times, but retrieval "
            f"surfaced it only {stat.injections} times — its description/"
            "keywords fail to match the tasks that need it.",
        ]
    lines += [
        "",
        "DECIDE exactly one action:",
        "- repair — the entry is useful but its description/keywords are "
        "generic or wrong, so retrieval mis-fires; you MUST then provide a "
        "corrected description and 3-6 DISTINCTIVE keywords (never generic "
        "words like 'code', 'change', 'file').",
        "- pin — it is a standing always/never behavior rule that must apply "
        "to every task regardless of topic.",
        "- disable — stale, redundant, or noise; reversible retirement.",
        "- skip — the entry is fine as-is, or the evidence is insufficient.",
        "",
        "Answer EXACTLY in this format, nothing after it:",
        "VERDICT: <repair|pin|disable|skip>",
        "REASON: <one sentence>",
        "DESCRIPTION: <only for repair>",
        "KEYWORDS: <comma-separated, only for repair>",
        "",
        "Example:",
        "VERDICT: repair",
        "REASON: Generic keywords make it fire on unrelated tasks.",
        "DESCRIPTION: Use the trippy CLI for hotel and villa searches with "
        "live prices.",
        "KEYWORDS: hotel, villa, trippy, accommodation",
    ]
    return "\n".join(lines)


def _fields(text: str) -> dict[str, str]:
    """KEY: value lines from a judge reply, thinking spans stripped, keys
    casefolded. Later duplicates win (models sometimes restate)."""
    out: dict[str, str] = {}
    for line in _THINK_RE.sub("", text).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().isalpha():
            out[key.strip().casefold()] = value.strip()
    return out


def parse_verdict(text: str) -> Verdict | None:
    """The ONLY door into the action envelope: anything but the four verbs —
    or a repair without a replacement description — is a parse failure, and
    the caller retries once then records an unparseable skip."""
    fields = _fields(text)
    action = fields.get("verdict", "").casefold().strip(" .")
    if action not in VERDICTS:
        return None
    verdict = Verdict(
        action=action,
        reason=fields.get("reason", ""),
        description=fields.get("description", ""),
        keywords=fields.get("keywords", ""),
    )
    if action == "repair" and not verdict.description:
        return None
    return verdict


def update_entry_meta(
    path: Path,
    *,
    description: str | None = None,
    keywords: str | None = None,
    pinned: bool | None = None,
    disabled: bool | None = None,
) -> None:
    """Frontmatter-only rewrite: description/keywords/pinned/status may
    change, every other frontmatter line and the ENTIRE body are preserved
    byte-for-byte. This is the whole mutation surface of the judge loop —
    bodies and deletion have no code path here, which is what makes the
    envelope enforcement code rather than prompt obedience."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not match:
        raise ValueError(f"no frontmatter in {path}")
    body = text[match.end():]
    keep: list[str] = []
    for line in match.group(1).splitlines():
        key = line.partition(":")[0].strip().casefold()
        if key == "description" and description is not None:
            continue
        if key == "keywords" and keywords is not None:
            continue
        if key == "pinned" and pinned is not None:
            continue
        if key == "status" and disabled is not None:
            continue
        keep.append(line)
    front = keep
    if description is not None:
        front.insert(1, f"description: {' '.join(description.split())}")
    if keywords is not None:
        seen: set[str] = set()
        words = []
        for word in (w.strip() for w in keywords.split(",")):
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                words.append(word)
        if words:
            front.append(f"keywords: {', '.join(words[: skills.KEYWORDS_MAX])}")
    if pinned:
        front.append("pinned: yes")
    if disabled:
        front.append("status: disabled")
    path.write_text("---\n" + "\n".join(front) + "\n---\n" + body, encoding="utf-8")


def load_recent_actions(state_dir, now: datetime) -> dict[str, str]:
    """name -> last action within the cooldown window. Skips count too:
    re-judging the same 'fine as-is' entry weekly is the loop's version of
    thrash, and the ledger's stats lag reality by up to LEDGER_DAYS."""
    path = Path(state_dir) / ACTIONS_FILE
    floor = (now - timedelta(days=ACTION_COOLDOWN_DAYS)).isoformat()
    recent: dict[str, str] = {}
    if not path.is_file():
        return recent
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", "") >= floor and rec.get("name"):
            recent[rec["name"]] = rec.get("action", "")
    return recent


def log_action(state_dir, record: dict) -> None:
    path = Path(state_dir) / ACTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Layer 3: the duplicate pass — deterministic pairing, one pairwise question.


def dup_candidates(entries: list, scores=None) -> list[tuple]:
    """(entry_a, entry_b, similarity) pairs likely to be duplicates, best
    first. Pairing is DETERMINISTIC — identity-line embeddings when `scores`
    (SemanticIndex.scores) works, difflib ratio otherwise — reusing the
    save_memory near-duplicate thresholds. The judge only ever answers
    merge-or-distinct on a proposed pair; it never goes looking."""
    pairs: list[tuple] = []
    if scores is not None:
        for i, entry in enumerate(entries):
            sims = scores(entry_text(entry), entries)
            if sims is None:
                scores = None  # embeddings down: fall through to difflib
                break
            for other in entries[i + 1 :]:
                sim = sims.get(id(other), 0.0)
                if sim >= skills.DEDUP_MIN_SIM:
                    pairs.append((entry, other, sim))
    if scores is None:
        for i, entry in enumerate(entries):
            text_a = entry_text(entry).casefold()
            for other in entries[i + 1 :]:
                ratio = difflib.SequenceMatcher(
                    None, text_a, entry_text(other).casefold()
                ).ratio()
                if ratio >= skills.DEDUP_LEXICAL_RATIO:
                    pairs.append((entry, other, ratio))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:PAIRS_MAX]


def pair_prompt(a, b, sim: float) -> str:
    return "\n".join(
        [
            "You are a strict knowledge-base curator. Two saved entries look "
            f"like duplicates (similarity {sim:.2f}). Decide whether they say "
            "the same thing.",
            "",
            f'ENTRY A ({a.kind} "{a.name}"): {a.description}',
            f"  keywords: {', '.join(a.keywords) if a.keywords else '(none)'}",
            f'ENTRY B ({b.kind} "{b.name}"): {b.description}',
            f"  keywords: {', '.join(b.keywords) if b.keywords else '(none)'}",
            "",
            "If they express the SAME rule or fact, answer merge and name the "
            "better-written one to keep (the other is retired, reversibly). "
            "If they cover genuinely different things, answer distinct.",
            "",
            "Answer EXACTLY in this format, nothing after it:",
            "VERDICT: <merge|distinct>",
            f"KEEP: <{a.name}|{b.name}, only for merge>",
            "REASON: <one sentence>",
        ]
    )


def parse_pair_verdict(text: str, a, b) -> tuple[str, object | None]:
    """("merge", survivor_entry) | ("distinct", None) | ("invalid", None).
    A merge naming neither entry is invalid — the envelope never guesses."""
    fields = _fields(text)
    action = fields.get("verdict", "").casefold().strip(" .")
    if action == "distinct":
        return "distinct", None
    if action == "merge":
        keep = fields.get("keep", "").strip()
        for entry in (a, b):
            if entry.name == keep:
                return "merge", entry
        return "invalid", None
    return "invalid", None


# ---------------------------------------------------------------------------


def _make_judge(model: str):
    """A prompt→text callable over the exact ollama chat convention every
    backend is adapted to. No tools, no streaming, small context — the judge
    is stateless and each call is independent."""
    from . import backends

    chat, _provider, name = backends.make_chat(model)

    def ask(prompt: str) -> str:
        response = chat(
            model=name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            options={"num_ctx": JUDGE_NUM_CTX},
            think=False,
        )
        message = getattr(response, "message", None)
        if message is None and isinstance(response, dict):
            message = response.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return str(content or "")

    return ask


RETRY_NUDGE = (
    "\n\nYour previous reply did not match the required format. Reply again "
    "with ONLY the format lines, starting with 'VERDICT:'."
)


def _judged(judge, prompt: str, parse):
    """One judged decision with a single format retry; None if both fail."""
    verdict = parse(judge(prompt))
    if verdict is None:
        verdict = parse(judge(prompt + RETRY_NUDGE))
    return verdict


def run_curate(
    *,
    judge: Callable[[str], str] | None = None,
    scores=None,
    notify_fn: Callable[[str, str], object] | None = None,
    env: Mapping[str, str] | None = None,
    state_dir=None,
    now: datetime | None = None,
    dry_run: bool = False,
    model: str | None = None,
) -> int:
    """One curation pass, orchestrated HERE: suspects and duplicate pairs are
    computed deterministically, each gets one bounded judge call, verdicts
    are applied through the code envelope, and everything lands in the
    action log (which is also the cooldown that makes retries idempotent)."""
    env = os.environ if env is None else env
    now = now or datetime.now()
    if state_dir is None:
        state_dir = Path.home() / ".local" / "state" / "aish"
    if model is None:
        model = env.get("AISH_CURATE_MODEL", "").strip() or DEFAULT_MODEL

    ledger = scan_ledger(state_dir, now=now)
    entries = {
        e.name: e
        for e in skills.load_entries(str(Path.home()), None)
        if e.path is not None  # legacy lesson lines have no file to update
    }
    recent = load_recent_actions(state_dir, now)

    suspects = [(s, "dead-weight") for s in dead_weight(ledger)]
    suspects += [(s, "missed") for s in missing(ledger)]
    suspects = [
        (s, category)
        for s, category in suspects
        if s.name in entries and s.name not in recent
    ][:SUSPECTS_MAX]

    if scores is None and not dry_run:
        try:
            from .embeddings import SemanticIndex

            scores = SemanticIndex(state_dir).scores
        except Exception:  # noqa: BLE001 — embeddings are an upgrade, never a dependency
            scores = None
    active = [e for e in entries.values()]
    pairs = [
        (a, b, sim)
        for a, b, sim in dup_candidates(active, scores)
        if a.name not in recent and b.name not in recent
    ]

    if dry_run:
        print(f"suspects ({len(suspects)}):")
        for s, category in suspects:
            print(f"  {s.name} [{category}] injections={s.injections} misses={s.miss_reads}")
        print(f"duplicate candidates ({len(pairs)}):")
        for a, b, sim in pairs:
            print(f"  {a.name} <-> {b.name} ({sim:.2f})")
        return 0
    if not suspects and not pairs:
        log(f"nothing to curate ({ledger.tasks} tasks scanned)")
        return 0

    if judge is None:
        try:
            judge = _make_judge(model)
        except Exception as exc:  # noqa: BLE001 — backend build failed: config error
            log(f"cannot build judge model {model!r}: {exc}")
            return 2

    counts = {"repair": 0, "pin": 0, "disable": 0, "skip": 0, "unparseable": 0, "merge": 0}
    acted_this_run: set[str] = set()

    for stat, category in suspects:
        entry = entries[stat.name]
        assert entry.path is not None  # entries were filtered to file-backed
        verdict = _judged(judge, judge_prompt(entry, stat, category), parse_verdict)
        if verdict is None:
            counts["unparseable"] += 1
            record = {"action": "skip", "reason": "unparseable judge reply"}
        else:
            counts[verdict.action] += 1
            record = {"action": verdict.action, "reason": verdict.reason}
            try:
                if verdict.action == "repair":
                    update_entry_meta(
                        entry.path,
                        description=verdict.description,
                        keywords=verdict.keywords or None,
                    )
                elif verdict.action == "pin":
                    update_entry_meta(entry.path, pinned=True)
                elif verdict.action == "disable":
                    update_entry_meta(entry.path, disabled=True)
            except (OSError, ValueError) as exc:
                log(f"apply failed for {entry.name}: {exc}")
                record = {"action": "skip", "reason": f"apply failed: {exc}"}
        acted_this_run.add(entry.name)
        log_action(
            state_dir,
            {"ts": now.isoformat(), "name": entry.name, "category": category,
             "model": model, **record},
        )
        log(f"{entry.name}: {record['action']} — {record['reason'][:80]}")

    for a, b, sim in pairs:
        if a.name in acted_this_run or b.name in acted_this_run:
            continue
        action, survivor = _judged(
            judge, pair_prompt(a, b, sim), lambda t, a=a, b=b: (
                v if (v := parse_pair_verdict(t, a, b))[0] != "invalid" else None
            ),
        ) or ("invalid", None)
        if action == "merge" and survivor is not None:
            loser = b if survivor is a else a
            assert loser.path is not None  # entries were filtered to file-backed
            try:
                update_entry_meta(loser.path, disabled=True)
                counts["merge"] += 1
                outcome = f"merged into {survivor.name}"
            except (OSError, ValueError) as exc:
                log(f"merge apply failed for {loser.name}: {exc}")
                outcome = f"merge failed: {exc}"
            log_action(
                state_dir,
                {"ts": now.isoformat(), "name": loser.name, "category": "duplicate",
                 "model": model, "action": "disable", "reason": outcome},
            )
            acted_this_run.update((a.name, b.name))
            log(f"{loser.name}: {outcome}")
        else:
            for entry in (a, b):
                log_action(
                    state_dir,
                    {"ts": now.isoformat(), "name": entry.name, "category": "duplicate",
                     "model": model, "action": "skip",
                     "reason": "judged distinct" if action == "distinct" else "unparseable"},
                )

    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    log(f"pass complete: {summary or 'no decisions'}")
    if notify_fn is None:
        from . import notify

        if notify.configured():
            notify_fn = notify.pushover
    if notify_fn is not None and any(counts.values()):
        notify_fn(
            "aish knowledge curation",
            f"Week {now:%G-W%V}: {summary}. All changes reversible "
            "(knowledge git; disabled entries can be re-enabled).",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="aish retrieval self-curation: scripted judge loop (#185/#186)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list suspects and duplicate candidates; no model calls, no writes",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "judge model spec (default: $AISH_CURATE_MODEL or "
            f"{DEFAULT_MODEL}; keep it LOCAL — prompts quote excerpts from "
            "private sessions)"
        ),
    )
    args = parser.parse_args()
    return run_curate(dry_run=args.dry_run, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
