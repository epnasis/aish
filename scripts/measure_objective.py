#!/usr/bin/env -S uv run python
"""Run the distiller over recorded session logs and score it against goldens (#424).

    AISH_LOCAL_URL=http://10.99.0.2:8080/v1 uv run python scripts/measure_objective.py \\
        ~/.local/state/aish/session-….jsonl --at 6,12,18,34 \\
        --model local:mlx-community/Qwen3.6-35B-A3B-8bit --model gemini:gemini-3.8-flash \\
        --goldens ~/.cache/aish-objective-goldens

    … --at last                       one boundary: the log's last turn
    … --probe 18 --runs 20            the seed probe (below), on the first model
    … --from results.json             score or probe revisions an earlier run produced
    … --dry-run                       compose the inputs and report their sizes only

**It calls the backend directly and nothing else.** The log is only read. No chat
is opened, nothing is sent to aish-web, nothing is written to the log — the
revisions live in the results file. `docs/objective.md`, *Measurement*.

**Chained per model.** Each boundary gets ALL of the chat's material up to it
(the distiller rebuilds, #424) and the revision that model produced at the
previous boundary as `previous`, exactly as live emission would.

**Where results go.** `--out` (default `~/.cache/aish-objective-424/`), never the
repository: revisions and goldens carry the owner's own words, and this
repository is public.

**Goldens.** `--goldens DIR` holds `golden-<chat>.json` files (schema
`aish-objective-golden/v2`, documented in that directory's README), written
independently of this script and of the distiller; `*.v1.json` copies are
superseded and never read. `--at golden` runs at the golden's own boundaries.
Per boundary the score reports: key facts hit, each golden purpose found (a
distilled `why` quoted from one of the same messages, or overlapping it — the
README's accept-equivalent rule), each still-open task found not done, each
golden constraint carried, false claims fired (`review_only` ones apart),
false `done`s over open tasks, downgrades, coverage, tokens and latency. The
BAR — the owner's — is: coverage reached, every purpose, every open task and
every golden constraint present, and zero false `done`. Golden cites written
as `m@N` (a record index, from an earlier material) are mapped onto today's
refs before matching. Every rule is a substring or a cite match a person can
check, and hits and misses are listed by id; it decides nothing in the product.

**The seed probe** (#424 exit 3). Task N's stored request (its first model call,
from the per-chat `sent` store) with a probe question appended — "state the goals
and open tasks" — sent with no tools, `--runs` times with the boundary-N revision
inserted as a note just before the request's final owner message and `--runs`
times without. Each reply is scored by the string checks in `PROBE_CHECKS`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import backends, objective, roles, turns  # noqa: E402

DEFAULT_OUT = Path.home() / ".cache" / "aish-objective-424"
# A cloud reference answering 503 "high demand" is retried this many times,
# waiting BUSY_WAIT_S × the attempt number; the retries are counted per row.
BUSY_RETRIES = 6
BUSY_WAIT_S = 20

PROBE_QUESTION = (
    "Before you do anything else: state, in a few lines, the goals of this chat and "
    "the tasks that are still open. Do not call tools."
)

# Scored by string checks only, as the issue specifies. Each is a regex over the
# case-folded reply; the patterns are printed with the results so a reader can
# judge what a hit means.
PROBE_CHECKS: dict[str, str] = {
    "rain": r"rain|deszcz|opad",
    "day_before": r"day before|dzień przed|dzien przed|next[- ]day|24 ?(h|hours|godz)|jutr",
    "secrets": r"aish secret|secrets?\b|sekret",
    "skill": r"\bskill",
    "show_image": r"show_image|show image",
    "tomorrow_io": r"tomorrow\.io|tomorrow io|tomorrowio",
}


# ---------------------------------------------------------------- distilling


def distill_chain(
    path: Path, boundaries: list[int], model: str, charter: roles.Charter, dry_run: bool
) -> list[dict[str, Any]]:
    records = objective.read_records(path)
    items = objective.material(records)
    chat = path.stem
    base: dict | None = None
    out: list[dict[str, Any]] = []
    for revision_no, boundary in enumerate(boundaries, 1):
        text = objective.compose_input(chat, boundary, items, base)
        sent = json.loads(text)
        row: dict[str, Any] = {
            "model": model,
            "boundary": boundary,
            "base": base.get("revision") if base else None,
            "input_chars": len(text),
            "material_items": len(sent["material"]),
            "must_cover": len(sent["must_cover"]),
        }
        if dry_run:
            row["status"] = "dry-run"
            out.append(row)
            print(f"@{boundary:>3} input_chars={len(text)} items={row['material_items']} "
                  f"must_cover={row['must_cover']}", flush=True)
            continue
        started = time.perf_counter()
        busy_retries = 0
        while True:
            result = roles.run(
                charter,
                {"material": text},
                (),
                model_spec=model,
                check_admission=False,  # measuring is what admission would be decided on
            )
            # A provider saying it is overloaded is not an answer about the
            # distiller; retry it (measurement only — production records it).
            if (
                result.status == roles.Status.UNAVAILABLE
                and "503" in result.why
                and busy_retries < BUSY_RETRIES
            ):
                busy_retries += 1
                time.sleep(BUSY_WAIT_S * busy_retries)
                continue
            break
        row.update(
            status=result.status,
            why=result.why,
            attempts=result.attempts,
            ms=result.ms,
            wall_ms=int((time.perf_counter() - started) * 1000),
            busy_retries=busy_retries,
            usage=result.usage,
        )
        if result.status == roles.Status.OK and result.value is not None:
            value: objective.Revision = result.value
            row.update(
                change=value.change,
                covers_to_turn=value.covers_to_turn,
                not_goal_bearing=value.not_goal_bearing,
                downgrades=value.downgrades,
                goals=value.goals,
            )
            base = {
                "revision": revision_no,
                "turn": boundary,
                "covers_to_turn": value.covers_to_turn,
                "goals": value.goals,
                "origin": objective.ORIGIN_DISTILLER,
            }
        out.append(row)
        print(_summary(row), flush=True)
    return out


def _quote_line(label: str, quote: Any) -> str:
    if isinstance(quote, dict):
        cites = ",".join(x["ref"] for x in quote.get("cites") or ())
        return f"\n          {label}: {quote.get('text')!r} <{cites}>"
    return f"\n          {label}: {quote}"


def _summary(row: dict[str, Any]) -> str:
    usage = row.get("usage") or {}
    head = (
        f"[{row['model']}] @{row['boundary']:>3}  {row['status']:<11} "
        f"attempts={row.get('attempts')} ms={row.get('ms')} in={usage.get('input')} "
        f"out={usage.get('output')} input_chars={row['input_chars']}"
    )
    if row.get("why"):
        head += f"\n      why: {row['why'][:400]}"
    for goal in row.get("goals") or ():
        head += f"\n      [{goal['state']}] {goal['id']}: {goal['text']}"
        head += _quote_line("why", goal.get("why"))
        head += _quote_line("done_when", goal.get("done_when"))
        for c in goal.get("constraints") or ():
            head += f"\n          constraint: {c['text']!r}"
        for t in goal.get("tasks") or ():
            cites = ",".join(x["ref"] for x in t.get("cites") or ())
            head += f"\n          ({t['state']}) {t['id']}: {t['text']}  <{cites}>"
    if row.get("status") == roles.Status.OK:
        head += (
            f"\n      change={row.get('change')} covers_to_turn={row.get('covers_to_turn')} "
            f"not_goal_bearing={row.get('not_goal_bearing')} "
            f"downgrades={len(row.get('downgrades') or ())}"
        )
    return head


# ---------------------------------------------------------------- scoring


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _goal_texts(goals: list[dict]) -> list[str]:
    parts: list[str] = []
    for g in goals:
        parts.append(str(g.get("text") or ""))
        for key in ("why", "done_when"):
            if isinstance(g.get(key), dict):
                parts.append(str(g[key].get("text") or ""))
        parts += [str(c.get("text") or "") for c in g.get("constraints") or ()]
        parts += [str(t.get("text") or "") for t in g.get("tasks") or ()]
    return parts


def revision_words(goals: list[dict]) -> str:
    """Everything the revision says in its own words, case-folded."""
    return "\n".join(_goal_texts(goals)).casefold()


def ref_mapper(records: list[dict]) -> Any:
    """Golden cites were produced by an earlier `material()`, whose refs for a
    message with no id were its record index (`m@N`). Map those onto what the
    material produces now, so a cite names the same message either way."""

    def canon(ref: str) -> str:
        if ref.startswith("m@"):
            try:
                index = int(ref[2:])
                return objective._message_ref(index, records[index])
            except (ValueError, IndexError):
                return ref
        return ref

    return canon


def _refs(cites: Any, canon: Any) -> set[str]:
    return {canon(objective._bare_ref(c)) for c in cites or ()}


def _overlaps(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a in b or b in a)


def _hits(words: Any, item: dict) -> float:
    """The SHARE of a golden task's match words the item's text contains. A
    share, not a count: golden tasks carry match lists of different lengths,
    and a broad list must not out-vote a narrow one by size (review finding:
    one shared word with a three-word done-allowed task hid a real false done)."""
    words = [str(w).casefold() for w in words or ()]
    if not words:
        return 0.0
    text = str(item.get("text") or "").casefold()
    return sum(1 for w in words if w in text) / len(words)


def score(row: dict[str, Any], spec: dict[str, Any], canon: Any) -> dict[str, Any]:
    """One distilled revision against one golden boundary (schema v2, README in
    the goldens directory). Every rule is a case-folded substring or a cite
    match a person can check; hits and misses are listed by id."""
    goals = row.get("goals") or []
    blob = revision_words(goals)
    items = [item for g in goals for item in [g, *(g.get("tasks") or ())]]
    tasks = [t for g in goals for t in g.get("tasks") or ()]

    facts = spec.get("key_facts") or []
    fact_hits = [f["fact"] for f in facts if any(w.casefold() in blob for w in f["any_of"])]
    fact_miss = [f["fact"] for f in facts if f["fact"] not in fact_hits]

    # Purpose: a golden goal with a quoted why is found when a distilled why is
    # quoted from one of the same messages (accept-equivalent: any span of his
    # words there), or when the two quotes overlap.
    purposes, purpose_miss = 0, []
    for g in spec.get("goals") or ():
        wanted = [q for q in (g.get("why"), g.get("alternative_accepted")) if isinstance(q, dict)]
        if not wanted:
            continue
        purposes += 1
        found = any(
            isinstance(d.get("why"), dict)
            and (
                _refs(d["why"].get("cites"), canon) & _refs(w.get("cites"), canon)
                or _overlaps(d["why"].get("text") or "", w.get("text") or "")
            )
            for d in goals
            for w in wanted
        )
        if not found:
            purpose_miss.append(g["id"])

    # Pending tasks: a golden task the owner is still waiting on ("open") is
    # found when some distilled task hits its match words and is not done.
    open_tasks = [t for t in spec.get("tasks") or () if t.get("state") == "open"]
    pending_miss = [
        t["id"] for t in open_tasks
        if not any(
            any(w.casefold() in str(d.get("text") or "").casefold() for w in t.get("match") or ())
            and d.get("state") != "done"
            for d in tasks
        )
    ]
    # False done, disambiguated: match words overlap between golden tasks (a
    # finished "save the learnings" and an open "fix the memory bug" both say
    # "memory"), so a distilled `done` is charged to an open golden task only
    # when that task matches it STRICTLY better than every golden task whose
    # `accept` includes done. A tie is a candidate for a person, not a count.
    golden_tasks = spec.get("tasks") or []
    done_ok = [t for t in golden_tasks if "done" in (t.get("accept") or [t.get("expected")])]
    false_done, candidates = [], []
    for d in tasks:
        if d.get("state") != "done":
            continue
        best_ok = max((_hits(t.get("match"), d) for t in done_ok), default=0)
        for t in open_tasks:
            hits = _hits(t.get("match"), d)
            if hits and hits > best_ok:
                false_done.append(f"{t['id']} <- {d.get('id')}")
            elif hits:
                candidates.append(f"{t['id']} ~ {d.get('id')}")

    # Constraints: every golden constraint is found as an overlapping quote in
    # a distilled constraint, why or done_when (extras are never penalised).
    carried = [
        str(q.get("text") or "")
        for g in goals
        for q in [*(g.get("constraints") or ()), g.get("why"), g.get("done_when")]
        if isinstance(q, dict)
    ]
    wanted_constraints = [
        c for g in spec.get("goals") or () for c in g.get("constraints") or ()
    ]
    constraint_miss = [
        c["text"] for c in wanted_constraints
        if not any(_overlaps(c["text"], have) for have in carried)
    ]

    claims, review = [], []
    ok_goals = [
        g for g in spec.get("goals") or () if "done" in (g.get("accept") or [g.get("expected")])
    ]
    for claim in spec.get("false_claims") or ():
        level = claim.get("level")
        fired = False
        if level in ("task", "goal"):
            pool = tasks if level == "task" else goals
            for d in pool:
                text = str(d.get("text") or "").casefold()
                words = [w for w in claim.get("match") or () if w.casefold() in text]
                if not (
                    d.get("state") in (claim.get("forbidden_states") or ())
                    and words
                    and (
                        not claim.get("require_any")
                        or any(w.casefold() in text for w in claim["require_any"])
                    )
                ):
                    continue
                # The same disambiguation as false done: an item that matches a
                # golden item allowed in this state at least as well is that
                # item, not the claim.
                if level == "task" and d.get("state") == "done":
                    best_ok = max((_hits(t.get("match"), d) for t in done_ok), default=0)
                    if best_ok >= _hits(claim.get("match"), d):
                        candidates.append(f"claim '{claim['claim']}' ~ {d.get('id')}")
                        continue
                if level == "goal" and d.get("state") == "done" and any(
                    all(w.casefold() in str(g.get("text") or "").casefold() for w in words)
                    for g in ok_goals
                ):
                    candidates.append(f"claim '{claim['claim']}' ~ {d.get('id')}")
                    continue
                fired = True
        elif level == "text":
            fired = any(w.casefold() in blob for w in claim.get("absent") or ())
        if fired:
            (review if claim.get("review_only") else claims).append(claim["claim"])

    valid = row.get("status") == roles.Status.OK
    coverage = valid and row.get("covers_to_turn") == row["boundary"]
    usage = row.get("usage") or {}
    passed = (
        coverage
        and not purpose_miss
        and not pending_miss
        and not constraint_miss
        and not false_done
    )
    return {
        "model": row["model"],
        "boundary": row["boundary"],
        "valid": valid,
        "why_invalid": "" if valid else str(row.get("why") or row.get("status"))[:200],
        "facts_hit": fact_hits,
        "facts_missing": fact_miss,
        "facts_total": len(facts),
        "purposes_total": purposes,
        "purposes_missing": purpose_miss,
        "pending_total": len(open_tasks),
        "pending_missing": pending_miss,
        "constraints_total": len(wanted_constraints),
        "constraints_missing": constraint_miss,
        "false_claims": claims,
        "review_claims": review,
        "false_done": false_done,
        "candidates": candidates,
        "downgrades": len(row.get("downgrades") or ()),
        "coverage_reached": coverage,
        "not_goal_bearing": len(row.get("not_goal_bearing") or ()),
        "unstated": sum(
            1 for g in goals for k in ("why", "done_when") if g.get(k) == objective.UNSTATED
        ),
        "items": len(items),
        "attempts": row.get("attempts"),
        "ms": row.get("ms"),
        "tokens_in": usage.get("input"),
        "tokens_out": usage.get("output"),
        "input_chars": row.get("input_chars"),
        "passes_bar": passed,
    }


def load_golden(directory: Path, chat: str) -> dict[str, Any] | None:
    """The chat's golden: `golden-<chat>.json`, never a superseded `*.v1.json`."""
    path = directory / f"golden-{chat}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("chat") == chat else None


def score_table(scores: list[dict[str, Any]]) -> str:
    lines = [
        "model | turn | valid | facts | purpose | pending | constraints | false_claims | "
        "false_done | downgrades | coverage | ngb | unstated | attempts | ms | tokens in/out | BAR"
    ]
    for s in scores:
        lines.append(
            f"{s['model']} | {s['boundary']} | {s['valid']} | "
            f"{len(s['facts_hit'])}/{s['facts_total']} | "
            f"{s['purposes_total'] - len(s['purposes_missing'])}/{s['purposes_total']} | "
            f"{s['pending_total'] - len(s['pending_missing'])}/{s['pending_total']} | "
            f"{s['constraints_total'] - len(s['constraints_missing'])}/{s['constraints_total']} | "
            f"{len(s['false_claims'])} | {len(s['false_done'])} | {s['downgrades']} | "
            f"{s['coverage_reached']} | {s['not_goal_bearing']} | {s['unstated']} | "
            f"{s['attempts']} | {s['ms']} | {s['tokens_in']}/{s['tokens_out']} | "
            f"{'PASS' if s['passes_bar'] else 'fail'}"
        )
        for key in ("why_invalid", "facts_missing", "purposes_missing", "pending_missing",
                    "constraints_missing", "false_claims", "review_claims", "false_done",
                    "candidates"):
            if s.get(key):
                lines.append(f"    {key}: {s[key]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the seed probe


def stored_request(path: Path, turn: int, state_dir: Path) -> tuple[list[dict], dict]:
    """Task `turn`'s first model call, as sent: its messages and options."""
    for record in objective.read_records(path):
        step = record.get("step") or {}
        if (
            record.get("kind") == "trace"
            and not record.get("superseded")
            and step.get("kind") == "sent"
            and step.get("turn") == turn
            and step.get("model_call") == 1
        ):
            messages = []
            for entry in step["messages"]:
                blob = turns.get(entry["digest"], state_dir, path.stem)
                if blob is None:
                    raise SystemExit(f"message {entry['at']} of turn {turn} is not in the store")
                messages.append(json.loads(blob))
            return messages, step.get("options") or {}
    raise SystemExit(f"no live `sent` record for turn {turn} model call 1")


def render(revision: dict) -> str:
    """The revision as plain text for the probe. A measurement rendering only —
    how the Objective reaches the model for real is #426's to decide."""
    lines = ["[aish: the Objective of this chat, as recorded so far]"]
    for goal in revision["goals"]:
        lines.append(f"- goal ({goal['state']}): {goal['text']}")
        for key in ("why", "done_when"):
            if isinstance(goal.get(key), dict):
                lines.append(f"    {key} (his words): {goal[key]['text']}")
        for c in goal.get("constraints") or ():
            lines.append(f"    constraint (his words): {c['text']}")
        for t in goal.get("tasks") or ():
            lines.append(f"    task ({t['state']}): {t['text']}")
    return "\n".join(lines)


def probe(
    path: Path, turn: int, revision: dict, model: str, runs: int, state_dir: Path,
    max_tokens: int,
) -> dict[str, Any]:
    messages, _options = stored_request(path, turn, state_dir)
    last_owner = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    note = {"role": "user", "content": render(revision)}
    arms = {
        "with": messages[:last_owner] + [note] + messages[last_owner:],
        "without": list(messages),
    }
    # The stored messages are the WIRE messages (already converted by the
    # adapter), so they go to the server as they are, through the same client
    # the local backend builds — never back through the adapter, which would
    # convert them a second time. No tools, thinking off, both arms alike.
    provider, name = backends.parse_model(model)
    if provider != backends.LOCAL:
        raise SystemExit("the seed probe replays a stored local request; --model must be local:")
    client = backends._local_client()
    out: dict[str, Any] = {"turn": turn, "runs": runs, "checks": PROBE_CHECKS, "arms": {},
                           "max_tokens": max_tokens, "thinking": False}
    out["arms"] = {arm: [] for arm in arms}
    for n in range(runs):
        # Interleaved, so neither arm runs on a warmer server than the other.
        for arm, base in arms.items():
            results = out["arms"][arm]
            request = base + [{"role": "user", "content": PROBE_QUESTION}]
            started = time.perf_counter()
            usage: dict[str, Any] = {}
            try:
                response = client.chat.completions.create(
                    model=name, messages=request, max_tokens=max_tokens,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                text = str(response.choices[0].message.content or "")
                if response.usage is not None:
                    usage = {"input": response.usage.prompt_tokens,
                             "output": response.usage.completion_tokens}
                error = ""
            except Exception as exc:  # noqa: BLE001 — a failed run is a recorded run
                text, error = "", f"{type(exc).__name__}: {exc}"
            ms = int((time.perf_counter() - started) * 1000)
            hits = {k: bool(re.search(p, text.casefold())) for k, p in PROBE_CHECKS.items()}
            results.append({"n": n, "ms": ms, "hits": hits, "score": sum(hits.values()),
                            "error": error, "usage": usage, "reply": text})
            print(f"  probe {arm:<7} #{n:<2} score={sum(hits.values())} ms={ms} "
                  f"{'ERR ' + error if error else ''}", flush=True)
    return out


def probe_table(result: dict[str, Any]) -> str:
    lines = [f"seed probe, turn {result['turn']}, {result['runs']} runs per arm"]
    for arm, runs in result["arms"].items():
        ok = [r for r in runs if not r["error"]]
        rates = {k: sum(r["hits"][k] for r in ok) for k in PROBE_CHECKS}
        mean = sum(r["score"] for r in ok) / len(ok) if ok else 0.0
        lines.append(
            f"  {arm:<7} ok={len(ok)}/{len(runs)} mean_score={mean:.2f}/6 "
            + " ".join(f"{k}={v}/{len(ok)}" for k, v in rates.items())
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--at", default="last",
                        help="comma-separated turns, 'last', or 'golden' (its boundaries)")
    parser.add_argument("--model", action="append", default=[],
                        help="repeatable; the first is the one probed")
    parser.add_argument("--goldens", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--probe", type=int, default=0, help="turn to run the seed probe at")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--probe-max-tokens", type=int, default=600)
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local" / "state" / "aish")
    parser.add_argument("--from", dest="from_results", type=Path,
                        help="use the revisions an earlier results file holds")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    charter = roles.load_charters()[objective.DISTILLER]
    if args.at == "golden":
        golden_spec = load_golden(args.goldens, args.log.stem) if args.goldens else None
        if golden_spec is None:
            print("--at golden needs --goldens holding this chat's golden")
            return 2
        boundaries = sorted(int(k) for k in golden_spec.get("boundaries") or {})
    elif args.at == "last":
        items = objective.material(objective.read_records(args.log))
        boundaries = [max((i.turn for i in items), default=0)]
    else:
        boundaries = [int(x) for x in args.at.split(",") if x.strip()]
    if not args.model and not args.dry_run and not args.from_results:
        print("no --model; nothing to measure against")
        return 2

    rows: list[dict[str, Any]] = []
    if args.from_results:
        rows = json.loads(args.from_results.read_text())["rows"]
        print(f"{args.log.name}: revisions from {args.from_results.name}, no distilling")
    else:
        for model in args.model or [""]:
            print(f"{args.log.name}: distiller v{charter.version} at {boundaries} on {model}")
            rows += distill_chain(args.log, boundaries, model, charter, args.dry_run)
    report: dict[str, Any] = {
        "log": str(args.log),
        "models": args.model,
        "charter_version": charter.version,
        "charter_digest": charter.digest,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }
    if args.from_results:
        report["from"] = str(args.from_results)
    if args.goldens and not args.dry_run:
        golden = load_golden(args.goldens, args.log.stem)
        if golden is None:
            print(f"\nno golden for {args.log.stem} in {args.goldens}")
        else:
            specs = golden.get("boundaries") or {}
            canon = ref_mapper(objective.read_records(args.log))
            report["scores"] = [
                score(row, specs[str(row["boundary"])], canon)
                for row in rows
                if str(row["boundary"]) in specs
            ]
            print("\n" + score_table(report["scores"]))
    if args.probe and not args.dry_run and args.model:
        probed = args.model[0]
        at = next((r for r in rows if r["boundary"] == args.probe and r.get("goals")
                   and r.get("model", probed) == probed), None)
        if at is None:
            print(f"\nno valid revision at turn {args.probe}; the seed probe needs one")
        else:
            revision = {"goals": at["goals"]}
            report["probe"] = probe(args.log, args.probe, revision, probed, args.runs,
                                    args.state_dir, args.probe_max_tokens)
            print("\n" + probe_table(report["probe"]))

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = args.out / f"{args.log.stem}-{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nresults: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
