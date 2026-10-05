#!/usr/bin/env python3
"""Does the wording of aish's own notes decide whether the model thinks the
USER said them? Measured by resending recorded requests, changing one message.

The owner's local model wrote "the user wants me to add quick-reply chips"
after an `[aish: …]` answer-check note in most of the reasonings that followed
one (contract §3.17). Four hypotheses could explain it, and only an experiment
that varies one thing at a time tells them apart:

    A  the note as aish sends it today
    B  aish's voice: the note says it is from aish and not the user, a rule's
       description is quoted as the owner's rule, and the "withheld" sentence
       no longer opens with "The user"
    D  A, inside <system-reminder>…</system-reminder>
    C  B, inside <system-reminder>…</system-reminder>

Each case is a model call the log recorded (`sent`, contract §3.12) whose last
message is an `[aish: …]` note. Its request is rebuilt byte for byte from the
per-chat store, the note is rewritten per arm, and the result is sent to the
same local server with the recorded options — the one change is the note.

Stdlib only, so it runs under the system python3 (uv's interpreter is refused
LAN sockets on the owner's machine). Reads the owner's state tree; writes
mined cases and results OUTSIDE the repo (`docs/replay.md`: nothing mined
from his sessions lives in this public repo).

    measure_note_voice.py mine  --out CASES.jsonl [--sessions 200]
    measure_note_voice.py run   CASES.jsonl --out RUN_DIR [--samples 2] [--url …]
    measure_note_voice.py judge RUN_DIR      # blind file for a reader: no arm
    measure_note_voice.py tally RUN_DIR [--labels LABELS.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("AISH_STATE_DIR") or Path.home() / ".local/state/aish")
RULES = Path(os.environ.get("AISH_CONFIG_HOME") or Path.home() / ".config/aish") / "rules"
NOTE = "[aish: "
ARMS = ("A", "B", "C", "D")
MAX_TOKENS = 1500  # enough for the reasoning's opening and the answer's start
REASONING_HEAD = 900

# B's wording. Applied to the note as it was rendered, so the experiment and
# a later code change can be pinned to produce the same text.
VOICE_OPENER = (
    "[aish: this note is from aish, the program you run inside — not from the user. "
)
WITHHELD_TODAY = "The user has not seen that answer — it was withheld."
WITHHELD_VOICE = "That answer was withheld: it was not shown to the user."
RULE_NAME = re.compile(r"The rule '([^']+)'")

# What the reasoning says about who asked. A heuristic for a first look; the
# blind read is the measurement.
SAYS_USER = re.compile(
    r"\bthe user (?:is |has |wants|asks|asked|pointed|points|says|said|reminds|requires)"
    r"|użytkownik (?:chce|prosi|wskazuje|zwraca|przypomina)",
    re.I,
)
SAYS_AISH = re.compile(
    r"\b(?:aish|the system|the harness|the rule check|system reminder|system note)\b", re.I
)


# --------------------------------------------------------------------- mine


def _records(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _blob(chat: str, digest: str) -> str | None:
    path = STATE / "turns" / chat / digest
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def mine(sessions: int, model_prefix: str) -> list[dict]:
    """Every recorded call on the local model whose LAST message is a note."""
    logs = sorted(STATE.glob("session-2026*.jsonl"), reverse=True)[:sessions]
    cases = []
    for log in logs:
        chat = log.stem
        model = ""
        reasoning: dict[tuple, dict] = {}
        sent: list[dict] = []
        for r in _records(log):
            if r.get("kind") == "model":
                model = str(r.get("model") or "")
            step = r.get("step") or {}
            if r.get("kind") != "trace" or r.get("superseded"):
                continue
            if step.get("kind") == "sent" and model.startswith(model_prefix):
                sent.append(step)
            elif step.get("kind") == "reasoning":
                reasoning[(step.get("turn"), step.get("model_call"))] = step
        for step in sent:
            entries = step.get("messages") or []
            if not entries or entries[-1].get("role") != "user":
                continue
            last = _blob(chat, entries[-1]["digest"])
            if last is None:
                continue
            note = str(json.loads(last).get("content") or "")
            if not note.startswith(NOTE) or any(e.get("media") for e in entries):
                continue
            live = reasoning.get((step.get("turn"), step.get("model_call")), {})
            cases.append({
                "chat": chat, "turn": step.get("turn"), "model_call": step.get("model_call"),
                "note": note, "live_reasoning": str(live.get("text") or "")[:REASONING_HEAD],
                "live_said": str(live.get("said") or "")[:400],
            })
    return cases


# ---------------------------------------------------------------------- arms


def _rule_description(name: str) -> str | None:
    path = RULES / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^description:\s*(.+)$", text, re.M)
    return match.group(1).strip().strip("'\"") if match else None


def voice(note: str) -> tuple[str, list[str]]:
    """Arm B's rewrite of one note, and which of its three parts applied."""
    applied = []
    body = note[len(NOTE):] if note.startswith(NOTE) else note
    text = VOICE_OPENER + body
    applied.append("opener")
    for name in dict.fromkeys(RULE_NAME.findall(note)):
        description = _rule_description(name)
        if description and f"{description}\n" in text:
            text = text.replace(
                f"{description}\n", f"The owner wrote this rule as: «{description}»\n", 1
            )
            applied.append("quoted")
    if WITHHELD_TODAY in text:
        text = text.replace(WITHHELD_TODAY, WITHHELD_VOICE)
        applied.append("withheld")
    return text, applied


def wrap(note: str) -> str:
    return f"<system-reminder>\n{note}\n</system-reminder>"


def arm_text(note: str, arm: str) -> tuple[str, list[str]]:
    if arm == "A":
        return note, []
    if arm == "D":
        return wrap(note), ["wrapper"]
    spoken, applied = voice(note)
    if arm == "B":
        return spoken, applied
    return wrap(spoken), [*applied, "wrapper"]


# ----------------------------------------------------------------------- run


def request_for(case: dict) -> dict | None:
    """The recorded request, rebuilt from the per-chat store, as the HTTP body
    the OpenAI-compatible server receives (`extra_body` flattened, as the SDK
    does)."""
    log = STATE / f"{case['chat']}.jsonl"
    for r in _records(log):
        step = r.get("step") or {}
        if (r.get("kind") == "trace" and step.get("kind") == "sent"
                and step.get("turn") == case["turn"]
                and step.get("model_call") == case["model_call"]):
            messages = []
            for entry in step.get("messages") or []:
                blob = _blob(case["chat"], entry["digest"])
                if blob is None:
                    return None
                messages.append(json.loads(blob))
            body: dict = {}
            options = dict(step.get("options") or {})
            extra = options.pop("extra_body", {}) or {}
            body.update(options)
            body["messages"] = messages
            if step.get("tools"):
                tools = _blob(case["chat"], step["tools"]["digest"])
                if tools is None:
                    return None
                body["tools"] = json.loads(tools)
            body.update(extra)
            body["max_tokens"] = MAX_TOKENS
            body["stream"] = False
            return body
    return None


def call(url: str, body: dict, timeout: float = 900) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def split(reply: dict) -> tuple[str, str]:
    message = (reply.get("choices") or [{}])[0].get("message") or {}
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    content = str(message.get("content") or "")
    if not reasoning and "</think>" in content:
        reasoning, _, content = content.partition("</think>")
        reasoning = reasoning.replace("<think>", "")
    return reasoning.strip(), content.strip()


def run(cases: list[dict], out: Path, samples: int, url: str, seed: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    done = (
        {(r["case"], r["arm"], r["sample"]) for r in _records(results)}
        if results.exists() else set()
    )
    rng = random.Random(seed)
    for index, case in enumerate(cases):
        body = request_for(case)
        if body is None:
            print(f"case {index}: request not in the store, skipped", file=sys.stderr)
            continue
        # Arms interleaved per case, in a shuffled order, so the server's
        # prefix cache and any drift land on every arm alike.
        jobs = [(arm, n) for n in range(samples) for arm in ARMS]
        rng.shuffle(jobs)
        for arm, n in jobs:
            if (index, arm, n) in done:
                continue
            text, applied = arm_text(case["note"], arm)
            sent = json.loads(json.dumps(body))
            sent["messages"][-1]["content"] = text
            started = time.time()
            try:
                reasoning, content = split(call(url, sent))
                error = ""
            except Exception as exc:  # noqa: BLE001 — recorded, the batch goes on
                reasoning, content, error = "", "", repr(exc)
            row = {"case": index, "arm": arm, "sample": n, "applied": applied,
                   "secs": round(time.time() - started, 1), "error": error,
                   "reasoning": reasoning[:REASONING_HEAD], "content": content[:600]}
            with results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"case {index} arm {arm} #{n}: {row['secs']}s {error[:80]}", flush=True)


# ------------------------------------------------------------- judge, tally


def judge_file(out: Path, seed: int) -> None:
    """The blind file: model output only, shuffled, under opaque ids. The key
    stays beside it; the reader never sees an arm or the note."""
    rows = [r for r in _records(out / "results.jsonl") if not r["error"]]
    rng = random.Random(seed)
    rng.shuffle(rows)
    key, blind = {}, []
    for i, row in enumerate(rows):
        item = f"x{i:04d}"
        key[item] = {"case": row["case"], "arm": row["arm"], "sample": row["sample"]}
        blind.append({"id": item, "reasoning": row["reasoning"], "answer": row["content"][:400]})
    (out / "blind.jsonl").write_text(
        "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in blind), encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key), encoding="utf-8")
    print(f"{len(blind)} items -> {out / 'blind.jsonl'}")


def tally(out: Path, labels_path: Path | None) -> None:
    rows = list(_records(out / "results.jsonl"))
    table: dict[str, dict[str, int]] = {arm: {} for arm in ARMS}

    def bump(arm: str, key: str) -> None:
        table[arm][key] = table[arm].get(key, 0) + 1

    for row in rows:
        arm = row["arm"]
        bump(arm, "runs")
        if row["error"]:
            bump(arm, "errors")
            continue
        head = row["reasoning"][:600]
        bump(arm, "regex:user" if SAYS_USER.search(head)
             else "regex:aish" if SAYS_AISH.search(head) else "regex:neither")
    if labels_path is not None:
        key = json.loads((out / "key.json").read_text(encoding="utf-8"))
        for label in _records(labels_path):
            entry = key.get(label.get("id"))
            if entry:
                bump(entry["arm"], f"judge:{label.get('attribution')}")
                if label.get("answer_addresses_note"):
                    bump(entry["arm"], "judge:answer_addresses_note")
    for arm in ARMS:
        cells = ", ".join(f"{k} {v}" for k, v in sorted(table[arm].items()))
        print(f"{arm}: {cells}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--sessions", type=int, default=200)
    m.add_argument("--model", default="local:")
    r = sub.add_parser("run")
    r.add_argument("cases", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--samples", type=int, default=2)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--seed", type=int, default=441)
    r.add_argument("--url", default=os.environ.get("AISH_LOCAL_URL", ""),
                   help="the local model server (default: $AISH_LOCAL_URL)")
    j = sub.add_parser("judge")
    j.add_argument("out", type=Path)
    j.add_argument("--seed", type=int, default=7)
    t = sub.add_parser("tally")
    t.add_argument("out", type=Path)
    t.add_argument("--labels", type=Path)
    args = parser.parse_args()
    out = getattr(args, "out", None)
    repo = Path(__file__).resolve().parent.parent
    if out is not None and repo in Path(out).resolve().parents:
        # Mined cases and model replies carry his words: never in this repo.
        sys.exit(f"refusing to write inside the repo ({repo}): use the private trees")
    if args.cmd == "mine":
        cases = mine(args.sessions, args.model)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
        print(f"{len(cases)} cases -> {args.out}")
    elif args.cmd == "run":
        if not args.url:
            sys.exit("no model server: pass --url or set AISH_LOCAL_URL")
        cases = list(_records(args.cases))
        if args.limit:
            cases = cases[: args.limit]
        run(cases, args.out, args.samples, args.url, args.seed)
    elif args.cmd == "judge":
        judge_file(args.out, args.seed)
    else:
        tally(args.out, args.labels)


if __name__ == "__main__":
    main()
