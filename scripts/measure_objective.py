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

**Goldens.** `--goldens DIR` holds one JSON file per chat, written independently
of this script and of the distiller. The file whose `"chat"` equals the log's
name is used. Per boundary (a string key, the turn):

    {"chat": "session-…",
     "boundaries": {"6": {
        "key_facts": [{"id": "rain", "any": ["deszcz", "rain", "opad"]}, …],
        "forbidden": [{"id": "reality-done", "any": ["…"]}, …],
        "not_done":  [{"id": "vs-reality", "any": ["rzeczywist", "reality"]}, …]}}}

A bare string in any list is read as `{"id": s, "any": [s]}`. Scoring is by
case-folded substring over the revision's own words — goal texts, `why`,
`done_when`, constraints and task texts:

- **key facts present** — an entry is present when any of its strings appears;
- **forbidden claims** — an entry is hit when any of its strings appears;
- **false done** — a goal or task in state `done` whose text contains any string
  of a `not_done` entry;
- **downgrades** — the unsupported `done`s code turned into `unknown`;
- **coverage reached** — the revision validated (so every must-cover owner text
  was cited or listed as not goal-bearing) and `covers_to_turn` is the boundary;
- tokens and latency, from the role result (summed over attempts).

Substring scoring is a proxy a person must be able to check, so every hit and
miss is printed with the entry id. It decides nothing about the product.

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
        result = roles.run(
            charter,
            {"material": text},
            (),
            model_spec=model,
            check_admission=False,  # measuring is what admission would be decided on
        )
        row.update(
            status=result.status,
            why=result.why,
            attempts=result.attempts,
            ms=result.ms,
            wall_ms=int((time.perf_counter() - started) * 1000),
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


def _entries(raw: Any) -> list[dict[str, Any]]:
    out = []
    for entry in raw or ():
        if isinstance(entry, str):
            out.append({"id": entry, "any": [entry]})
        elif isinstance(entry, dict):
            words = entry.get("any") or entry.get("match") or []
            out.append({"id": str(entry.get("id") or words[:1]), "any": [str(w) for w in words]})
    return out


def revision_words(goals: list[dict]) -> str:
    """Everything the revision says in its own words, case-folded."""
    parts: list[str] = []
    for g in goals:
        parts.append(str(g.get("text") or ""))
        for key in ("why", "done_when"):
            if isinstance(g.get(key), dict):
                parts.append(str(g[key].get("text") or ""))
        parts += [str(c.get("text") or "") for c in g.get("constraints") or ()]
        parts += [str(t.get("text") or "") for t in g.get("tasks") or ()]
    return "\n".join(parts).casefold()


def _hit(entry: dict[str, Any], blob: str) -> bool:
    return any(w.casefold() in blob for w in entry["any"])


def score(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    goals = row.get("goals") or []
    blob = revision_words(goals)
    facts = _entries(spec.get("key_facts"))
    forbidden = _entries(spec.get("forbidden"))
    not_done = _entries(spec.get("not_done"))
    done_texts = [
        str(item.get("text") or "").casefold()
        for g in goals
        for item in [g, *(g.get("tasks") or ())]
        if item.get("state") == "done"
    ]
    usage = row.get("usage") or {}
    return {
        "model": row["model"],
        "boundary": row["boundary"],
        "valid": row.get("status") == roles.Status.OK,
        "facts_present": [e["id"] for e in facts if _hit(e, blob)],
        "facts_missing": [e["id"] for e in facts if not _hit(e, blob)],
        "facts_total": len(facts),
        "forbidden_hits": [e["id"] for e in forbidden if _hit(e, blob)],
        "false_done": [e["id"] for e in not_done if any(_hit(e, t) for t in done_texts)],
        "downgrades": len(row.get("downgrades") or ()),
        "coverage_reached": row.get("status") == roles.Status.OK
        and row.get("covers_to_turn") == row["boundary"],
        "not_goal_bearing": len(row.get("not_goal_bearing") or ()),
        "unstated": sum(
            1 for g in goals for k in ("why", "done_when") if g.get(k) == objective.UNSTATED
        ),
        "attempts": row.get("attempts"),
        "ms": row.get("ms"),
        "tokens_in": usage.get("input"),
        "tokens_out": usage.get("output"),
        "input_chars": row.get("input_chars"),
    }


def load_golden(directory: Path, chat: str) -> dict[str, Any] | None:
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("chat") == chat:
            return data
    return None


def score_table(scores: list[dict[str, Any]]) -> str:
    lines = [
        "model | turn | valid | facts | forbidden | false_done | downgrades | coverage | "
        "unstated | attempts | ms | tokens in/out"
    ]
    for s in scores:
        lines.append(
            f"{s['model']} | {s['boundary']} | {s['valid']} | "
            f"{len(s['facts_present'])}/{s['facts_total']} | {len(s['forbidden_hits'])} | "
            f"{len(s['false_done'])} | {s['downgrades']} | {s['coverage_reached']} | "
            f"{s['unstated']} | {s['attempts']} | {s['ms']} | "
            f"{s['tokens_in']}/{s['tokens_out']}"
        )
        if s["facts_missing"]:
            lines.append(f"    missing facts: {s['facts_missing']}")
        if s["forbidden_hits"]:
            lines.append(f"    forbidden: {s['forbidden_hits']}")
        if s["false_done"]:
            lines.append(f"    false done: {s['false_done']}")
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
    for arm, base in arms.items():
        results = []
        for n in range(runs):
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
        out["arms"][arm] = results
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
    parser.add_argument("--at", default="last", help="comma-separated turns, or 'last'")
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
    if args.at == "last":
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
            report["scores"] = [
                score(row, specs[str(row["boundary"])])
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
