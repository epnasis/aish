#!/usr/bin/env -S uv run python
"""Run the objective tracker over recorded chats, for the owner to read (#432).

    AISH_LOCAL_URL=http://10.99.0.2:8080/v1 uv run python scripts/measure_objective.py \\
        --goldens ~/.cache/aish-objective-goldens \\
        --model local:mlx-community/Qwen3.6-35B-A3B-8bit --model gemini:gemini-3.5-flash \\
        --out ~/.cache/aish-432

    … LOG.jsonl --at 6,12,18        one chat, boundaries given by hand
    … --dry-run                     compose the inputs and report their sizes only
    … --render A/results.json B/results.json   one side-by-side from runs made
                                    apart (e.g. one model per process, in parallel)

**It calls the backend directly and nothing else.** The logs are only read. No
chat is opened, nothing is sent to aish-web, nothing is written to any log and
no admission is checked or recorded: the results live in `--out`, which must be
outside the repository, because they carry the owner's own words and the
repository is public.

**As production runs it.** The tracker is called at EVERY task end up to the last
boundary, not only at the boundaries — each call is shown the statement the
previous call left and the owner's messages since, exactly as live emission
would (`objective.track`, the same function). The boundaries are where the
side-by-side shows the objective.

**No score.** The owner is the judge of whether a statement reads his goal right
(owner decision, epic #423). The output is `objectives.md`: per chat, per
boundary, the owner messages since the previous boundary, then the objective
each model had at that point, and the trail of how it got there. Tokens and
latency are recorded per call and summarised per model. `results.json` holds
every call.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import objective, roles  # noqa: E402

DEFAULT_OUT = Path.home() / ".cache" / "aish-432"
QUOTE_CHARS = 600  # how much of each owner message the side-by-side quotes


def chat_records(path: Path) -> list[dict]:
    """The log's records, minus any objective or tracker record it already
    holds — the run starts from nothing, as a chat did before this shipped."""
    out = []
    for record in objective.read_records(path):
        step = record.get("step") if record.get("kind") == "trace" else None
        if isinstance(step, dict) and step.get("kind") in ("objective", "role"):
            continue
        out.append(record)
    return out


def run_chat(
    path: Path, boundaries: list[int], model: str, charter: roles.Charter, dry_run: bool
) -> list[dict]:
    """Every task end up to the last boundary, chained. One entry per call."""
    records = chat_records(path)
    turns = sorted({i.turn for i in objective.owner_messages(records)})
    last = max(boundaries)
    generated: list[dict] = []
    calls: list[dict] = []
    for turn in [t for t in turns if t <= last]:
        history = records + [{"kind": "trace", "step": r} for r in generated]
        edge = objective.Boundary(path, 0, turn, model, None)
        base = objective.current(history)
        after = objective.accounted_to(history)
        shown, omitted = objective.select_messages(objective.owner_messages(records), after, turn)
        text = objective.compose_input(path.stem, turn, base, shown, omitted)
        if dry_run:
            calls.append({"turn": turn, "input_chars": len(text), "messages": len(shown)})
            continue
        started = time.perf_counter()
        out, _base = objective.track(
            edge, charter=charter, check_admission=False, records=records, history=history
        )
        wall = int((time.perf_counter() - started) * 1000)
        generated.extend(out)
        role = next((r for r in out if r.get("kind") == "role"), None)
        revision = next((r for r in out if r.get("kind") == "objective"), None)
        calls.append(
            {
                "turn": turn,
                "status": role.get("status") if role else None,
                "verdict": role.get("verdict") if role else None,
                "why": role.get("why") if role else None,
                "attempts": role.get("attempts") if role else 0,
                "ms": role.get("ms") if role else 0,
                "wall_ms": wall,
                "usage": role.get("usage") if role else {},
                "input_chars": role.get("input", {}).get("chars") if role else len(text),
                "messages": len(shown),
                "omitted": omitted,
                "revision": revision,
            }
        )
        print(
            f"  {path.stem[8:]} t{turn:>2} {model.split(':')[0]:<6} "
            f"{calls[-1]['status']}/{calls[-1]['verdict']} {wall} ms",
            flush=True,
        )
    return calls


def objective_at(calls: list[dict], boundary: int) -> dict | None:
    """The statement standing at a boundary: the newest revision at or before it."""
    found = None
    for call in calls:
        if call["turn"] <= boundary and call.get("revision"):
            found = call["revision"]
    return found


def quote(text: str) -> str:
    flat = " ".join(text.split())
    cut = flat[:QUOTE_CHARS] + (" […]" if len(flat) > QUOTE_CHARS else "")
    return cut.replace("|", "\\|")


def render(chats: list[dict], models: list[str]) -> str:
    lines = [
        "# The objective tracker on five recorded chats (#432)",
        "",
        f"Generated {datetime.datetime.now().isoformat(timespec='seconds')} by "
        "`scripts/measure_objective.py`. Each model ran the tracker at every task end, "
        "chained, as production does; each boundary below shows the objective standing "
        "there. **Nothing here is scored — you are the judge of whether each statement "
        "reads your goal right.**",
        "",
        "Models: " + ", ".join(f"`{m}`" for m in models) + ".",
        "",
    ]
    for chat in chats:
        lines += [f"## {chat['name']}", "", f"`{chat['log']}` — boundaries "
                  + ", ".join(str(b) for b in chat["boundaries"]), ""]
        previous = 0
        for boundary in chat["boundaries"]:
            lines += [f"### At turn {boundary}", "", "**Your messages since turn "
                      f"{previous}** (card comments are never shown to the tracker):", ""]
            said = [m for m in chat["messages"] if previous < m["turn"] <= boundary]
            lines += [f"- t{m['turn']}: {quote(m['text'])}" for m in said] or ["- (none)"]
            lines.append("")
            for model in models:
                revision = objective_at(chat["calls"][model], boundary)
                label = model.split(":", 1)[0] + " · " + model.split(":", 1)[-1].split("/")[-1]
                if revision is None:
                    lines.append(f"**{label}:** *no objective yet*")
                else:
                    lines.append(
                        f"**{label}** (revision at turn {revision['turn']}, "
                        f"{revision['change']}): {revision['statement']}"
                    )
                lines.append("")
            previous = boundary
        summary = "Every call (turn · verdict · change · statement)"
        lines += [f"<details><summary>{summary}</summary>", ""]
        for model in models:
            lines += [f"**{model}**", ""]
            for call in chat["calls"][model]:
                revision = call.get("revision")
                tail = (f" · {revision['change']} · {revision['statement']}" if revision
                        else (f" · {call['why']}" if call.get("why") else ""))
                lines.append(
                    f"- t{call['turn']} · {call['status']}/{call['verdict']} · "
                    f"{call['ms']} ms{tail}"
                )
            lines.append("")
        lines += ["</details>", ""]
    lines += [
        "## Cost per call", "",
        "| model | calls | ok | median ms | p90 ms | max ms | mean input tokens "
        "| mean output tokens | retried |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for model in models:
        calls = [c for chat in chats for c in chat["calls"][model]]
        ms = sorted(c["ms"] for c in calls if c.get("ms"))
        ins = [int((c.get("usage") or {}).get("input") or 0) for c in calls]
        outs = [int((c.get("usage") or {}).get("output") or 0) for c in calls]
        ok = sum(1 for c in calls if c.get("status") == "ok")
        retried = sum(1 for c in calls if (c.get("attempts") or 0) > 1)
        p90 = ms[int(len(ms) * 0.9)] if ms else 0
        lines.append(
            f"| `{model}` | {len(calls)} | {ok} | {int(statistics.median(ms)) if ms else 0} | "
            f"{p90} | {ms[-1] if ms else 0} | {int(statistics.mean(ins)) if ins else 0} | "
            f"{int(statistics.mean(outs)) if outs else 0} | {retried} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_runs(paths: list[Path], out: Path) -> int:
    """One side-by-side from several `results.json`, each with its own models,
    over the same chats."""
    runs = [json.loads(p.read_text()) for p in paths]
    chats = runs[0]["chats"]
    models = list(runs[0]["models"])
    for run in runs[1:]:
        models += run["models"]
        by_name = {c["name"]: c for c in run["chats"]}
        for chat in chats:
            chat["calls"].update(by_name[chat["name"]]["calls"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(
        {"charter": runs[0]["charter"], "models": models, "chats": chats},
        ensure_ascii=False, indent=1))
    (out / "objectives.md").write_text(render(chats, models))
    print(f"wrote {out / 'objectives.md'}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("logs", nargs="*", type=Path)
    parser.add_argument("--at", default="", help="comma-separated boundaries (one log)")
    parser.add_argument("--goldens", type=Path, help="a directory of golden-<chat>.json")
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--render", nargs="+", type=Path, default=[])
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    if repo in args.out.resolve().parents or args.out.resolve() == repo:
        print("--out must be outside the repository: the results carry the owner's words")
        return 2
    if args.render:
        return render_runs(args.render, args.out)
    jobs: list[tuple[Path, list[int]]] = []
    if args.goldens:
        for golden in sorted(args.goldens.glob("golden-session-*.json")):
            if ".v" in golden.name.removeprefix("golden-session-"):
                continue  # superseded copies (*.v1.json, *.v2.json)
            data = json.loads(golden.read_text())
            jobs.append((Path(data["log"]), sorted(int(b) for b in data["boundaries"])))
    for log in args.logs:
        jobs.append((log, sorted(int(b) for b in args.at.split(",") if b.strip())))
    if not jobs or not args.model:
        print("give --goldens or LOG --at, and at least one --model")
        return 2

    charter = roles.load_charters()[objective.TRACKER]
    chats = []
    for path, boundaries in jobs:
        records = chat_records(path)
        chat = {
            "name": path.stem,
            "log": str(path),
            "boundaries": boundaries,
            "messages": [i.as_json() for i in objective.owner_messages(records)],
            "calls": {},
        }
        for model in args.model:
            print(f"{path.stem} on {model}", flush=True)
            chat["calls"][model] = run_chat(path, boundaries, model, charter, args.dry_run)
        chats.append(chat)
    if args.dry_run:
        for chat in chats:
            for model, calls in chat["calls"].items():
                sizes = [c["input_chars"] for c in calls]
                print(f"{chat['name']} {model}: {len(calls)} calls, input chars "
                      f"{min(sizes)}–{max(sizes)}")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(
        {"charter": {"name": charter.name, "version": charter.version, "digest": charter.digest},
         "models": args.model, "chats": chats}, ensure_ascii=False, indent=1))
    (args.out / "objectives.md").write_text(render(chats, args.model))
    print(f"wrote {args.out / 'objectives.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
