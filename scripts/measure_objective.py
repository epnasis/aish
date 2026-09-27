#!/usr/bin/env -S uv run python
"""Run the distiller over a recorded session log, at given task boundaries (#424).

    AISH_LOCAL_URL=http://10.99.0.2:8080/v1 uv run python scripts/measure_objective.py \\
        ~/.local/state/aish/session-….jsonl --at 6,12,18,34 \\
        --model local:mlx-community/Qwen3.6-35B-A3B-8bit \\
        --golden ~/.cache/aish-objective-424/golden-….json

    … --at last                       one boundary: the log's last turn
    … --probe 18 --runs 20            the seed probe (below), after distilling
    … --probe 18 --from results.json  the probe on a revision an earlier run produced
                                      (no distilling at all)
    … --dry-run                       compose the inputs, call no model

**It calls the backend directly and nothing else.** The log is only read. No chat
is opened, nothing is sent to aish-web, nothing is written to the log — the
revisions live in the results file. `docs/objective.md`, *Measurement*.

**Chained.** Each boundary's base is the revision the previous boundary produced,
exactly as live emission would have it; a boundary whose distill fails leaves the
base where it was, as live emission does.

**Where results go.** `--out` (default `~/.cache/aish-objective-424/`), never the
repository: the revisions and the golden file carry the owner's own words, and
this repository is public.

**The golden comparison is a REPORT, not a verdict.** A golden file lists, per
boundary, the tasks and goals a person derived from the log, each with `state`
(`done`, `open` — must not be done — or `either`) and `match` keywords. A distilled
task whose text contains a keyword of an `open` golden task and whose state is
`done` is printed as a CANDIDATE violation for a person to read. Keyword matching
is fuzzy by construction; it narrows what to read and decides nothing.

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
        row: dict[str, Any] = {
            "boundary": boundary,
            "base": base.get("revision") if base else None,
            "input_chars": len(text),
            "new_items": len(json.loads(text)["new"]),
        }
        if dry_run:
            row["status"] = "dry-run"
            out.append(row)
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
            revision = {
                "revision": revision_no,
                "covers_to_turn": value.covers_to_turn,
                "goals": value.goals,
                "origin": objective.ORIGIN_DISTILLER,
            }
            row.update(
                change=value.change,
                covers_to_turn=value.covers_to_turn,
                uncited=value.uncited,
                goals=value.goals,
            )
            base = revision
        out.append(row)
        print(_summary(row), flush=True)
    return out


def _summary(row: dict[str, Any]) -> str:
    usage = row.get("usage") or {}
    head = (
        f"@{row['boundary']:>3}  {row['status']:<11} attempts={row.get('attempts')} "
        f"ms={row.get('ms')} in={usage.get('input')} out={usage.get('output')} "
        f"input_chars={row['input_chars']} new_items={row['new_items']}"
    )
    if row.get("why"):
        head += f"\n      why: {row['why'][:400]}"
    for goal in row.get("goals") or ():
        head += f"\n      [{goal['state']}] {goal['id']}: {goal['text']}"
        for c in goal.get("constraints") or ():
            head += f"\n          constraint: {c['text']!r}"
        for t in goal.get("tasks") or ():
            cites = ",".join(x["ref"] for x in t.get("cites") or ())
            head += f"\n          ({t['state']}) {t['id']}: {t['text']}  <{cites}>"
    if row.get("status") == roles.Status.OK:
        head += (
            f"\n      change={row.get('change')} covers_to_turn={row.get('covers_to_turn')} "
            f"uncited={row.get('uncited')}"
        )
    return head


# ---------------------------------------------------------------- golden


def compare_golden(rows: list[dict[str, Any]], golden: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate violations: a distilled `done` over a golden `open` task."""
    report = []
    for row in rows:
        spec = (golden.get("boundaries") or {}).get(str(row["boundary"]))
        if not spec or not row.get("goals"):
            continue
        tasks = [(t, g) for g in row["goals"] for t in g.get("tasks") or ()]
        for want in spec.get("tasks") or ():
            words = [w.casefold() for w in want.get("match") or ()]
            hits = [
                (t, g) for t, g in tasks if any(w in t["text"].casefold() for w in words)
            ]
            entry = {
                "boundary": row["boundary"],
                "golden": want["id"],
                "golden_state": want["state"],
                "matched": [f"({t['state']}) {t['id']}: {t['text']}" for t, _ in hits],
            }
            entry["candidate_violation"] = want["state"] == "open" and any(
                t["state"] == "done" for t, _ in hits
            )
            report.append(entry)
    return report


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
    parser.add_argument("--model", default="")
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--probe", type=int, default=0, help="turn to run the seed probe at")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--probe-max-tokens", type=int, default=600)
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local" / "state" / "aish")
    parser.add_argument("--from", dest="from_results", type=Path,
                        help="take the probe's revision from an earlier results file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    charter = roles.load_charters()[objective.DISTILLER]
    if args.at == "last":
        items = objective.material(objective.read_records(args.log))
        boundaries = [max((i.turn for i in items), default=0)]
    else:
        boundaries = [int(x) for x in args.at.split(",") if x.strip()]
    if not args.model and not args.dry_run:
        print("no --model; nothing to measure against")
        return 2

    if args.from_results:
        rows = json.loads(args.from_results.read_text())["rows"]
        print(f"{args.log.name}: revisions from {args.from_results.name}, no distilling")
    else:
        print(f"{args.log.name}: distiller v{charter.version} at {boundaries} on {args.model}")
        rows = distill_chain(args.log, boundaries, args.model, charter, args.dry_run)
    report: dict[str, Any] = {
        "log": str(args.log),
        "model": args.model,
        "charter_version": charter.version,
        "charter_digest": charter.digest,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }
    if args.from_results:
        report["from"] = str(args.from_results)
    if args.golden:
        golden = json.loads(args.golden.read_text())
        report["golden"] = compare_golden(rows, golden)
        print("\ngolden comparison (a report for a person, not a verdict):")
        for entry in report["golden"]:
            flag = "CANDIDATE VIOLATION" if entry["candidate_violation"] else "ok"
            print(f"  @{entry['boundary']} {entry['golden']} [{entry['golden_state']}] "
                  f"{flag}  matched: {entry['matched'] or 'nothing'}")
    if args.probe and not args.dry_run:
        at = next((r for r in rows if r["boundary"] == args.probe and r.get("goals")), None)
        if at is None:
            print(f"\nno valid revision at turn {args.probe}; the seed probe needs one")
        else:
            revision = {"goals": at["goals"]}
            report["probe"] = probe(args.log, args.probe, revision, args.model, args.runs,
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
