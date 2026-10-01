"""Checks over ONE recorded run — pure functions of a session log (#441).

A check reads what the log recorded and returns a three-state verdict with one
line of evidence: passed, failed, or None ("could not tell"). None is an
ordinary answer and must stay available — a check that is forced to pick pass
or fail when the record cannot say will pick one, and the guess then reads as
a measurement. Nothing here calls a model, opens a rule file, or looks at how
aish behaves TODAY; the same law as `explain` (`docs/diagnostics.md`).

What counts as OPENED has one definition in aish — `rules.urls_acted_on` over
`SessionLog.calls_that_ran` — and `links_from_evidence` reuses it rather than
growing a second one that could disagree with the rule engine about the same
call.
"""

from __future__ import annotations

import html
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import rules
from .session import SessionLog, _is_superseded, _record_or_none, record_epoch

#: `tools.truncate`'s marker, and `web`'s page cut stamped on the tool record.
_CUT_MARKER = re.compile(r"\[\.\.\. [\d,]+ characters omitted")
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)")
#: Shell operators that hand one command's output to another or to a file.
_PIPE_OR_REDIRECT = re.compile(r"^(?:\d*>>?&?\d*|<<?<?|\|&?|&>>?)$")
EVIDENCE_MAX = 240


@dataclass(frozen=True)
class Verdict:
    check: str
    passed: bool | None  # None = could not tell, never folded into pass
    evidence: str

    @property
    def label(self) -> str:
        return {True: "pass", False: "FAIL", None: "could not tell"}[self.passed]


@dataclass
class Task:
    index: int  # 1-based, in log order
    prompt: str
    answer: str | None = None
    answer_at: int | None = None
    model_calls: int = 0
    started: int | None = None
    ended: int | None = None
    status: str | None = None  # task_end's status; None = never ended


@dataclass
class ToolCall:
    seq: int  # record index — "before" and "after" are comparisons of this
    task: int
    name: str
    args: dict
    status: str | None = None  # None = no `tool` record joined (killed mid-call)


@dataclass
class ShellRun:
    seq: int
    task: int
    command: str
    status: str | None  # cmd_end status (`exit`, `timeout`, …); None = no cmd_end
    exit_code: int | None


@dataclass
class ToolOutput:
    seq: int
    task: int
    tool: str
    text: str
    cut: bool


@dataclass
class RunRecord:
    """One session log, read once into the shapes every check needs."""

    path: Path
    tasks: list[Task] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    shell: list[ShellRun] = field(default_factory=list)
    decisions: list[tuple[int, str, str]] = field(default_factory=list)  # task, what, decision
    outputs: list[ToolOutput] = field(default_factory=list)
    ran: list[tuple[dict, int]] = field(default_factory=list)  # SessionLog.calls_that_ran
    cut_at: list[int] = field(default_factory=list)  # seqs of tool steps whose output was cut
    unpaired_shell: int = 0  # cmd_start/cmd_end that could not be paired by order
    first_at: int | None = None
    last_at: int | None = None

    def commands(self) -> list[ToolCall]:
        """Every shell command the MODEL proposed, run or not."""
        return [c for c in self.calls if c.name == "run_command"]


def command_text(call: ToolCall) -> str:
    return str(call.args.get("command") or "")


def load_run(path: Path) -> RunRecord:
    run = RunRecord(path=Path(path))
    task: Task | None = None
    by_id: dict[tuple[int, int], ToolCall] = {}
    open_cmd: ShellRun | None = None
    lines = run.path.read_text(encoding="utf-8").splitlines()
    for seq, line in enumerate(lines):
        record = _record_or_none(line)
        if record is None or _is_superseded(record):
            continue
        when = record_epoch(record)
        if when:
            run.first_at = run.first_at or when
            run.last_at = when
        kind = record.get("kind")
        if kind == "task_start":
            task = Task(index=len(run.tasks) + 1, prompt=str(record.get("prompt") or ""),
                        started=when)
            run.tasks.append(task)
            continue
        current = task.index if task else 0
        if kind == "task_end" and task is not None:
            task.ended, task.status = when, str(record.get("status") or "")
        elif kind == "message":
            _read_message(run, task, record, seq, current, when)
        elif kind == "command":
            run.decisions.append(
                (current, str(record.get("command") or ""), str(record.get("decision") or ""))
            )
        elif kind == "cmd_start":
            if open_cmd is not None:
                run.unpaired_shell += 1
            open_cmd = ShellRun(seq, current, str(record.get("command") or ""), None, None)
            run.shell.append(open_cmd)
        elif kind == "cmd_end":
            if open_cmd is None:
                run.unpaired_shell += 1
                continue
            open_cmd.status = str(record.get("status") or "")
            code = record.get("exit_code")
            open_cmd.exit_code = code if isinstance(code, int) else None
            open_cmd = None
        elif kind == "trace":
            _read_step(run, record.get("step"), seq, current, by_id)
    run.ran = SessionLog.calls_that_ran(run.path)
    return run


def _read_message(run: RunRecord, task: Task | None, record: dict, seq: int, current: int,
                  when: int | None) -> None:
    role = record.get("role")
    content = record.get("content")
    text = content if isinstance(content, str) else ""
    if task is not None and isinstance(record.get("model_call"), int):
        task.model_calls = max(task.model_calls, record["model_call"])
    if role == "tool":
        run.outputs.append(ToolOutput(seq, current, str(record.get("tool_name") or ""),
                                      text, bool(_CUT_MARKER.search(text))))
    elif role == "assistant" and task is not None and not record.get("interim"):
        # The last uninterrupted answer of the task is what was delivered.
        task.answer, task.answer_at = text, when


def _read_step(run: RunRecord, step: object, seq: int, current: int,
               by_id: dict[tuple[int, int], ToolCall]) -> None:
    if not isinstance(step, dict):
        return
    turn, number = step.get("turn"), step.get("call")
    # Joined by (turn, call) as the trace contract §2 says — never by position.
    key = (turn, number) if isinstance(turn, int) and isinstance(number, int) else None
    if step.get("kind") == "call":
        call = ToolCall(seq, current, str(step.get("name") or ""), dict(step.get("args") or {}))
        run.calls.append(call)
        if key is not None:
            by_id[key] = call
    elif step.get("kind") == "tool":
        joined = by_id.pop(key, None) if key is not None else None
        if joined is not None:
            joined.status = str(step.get("status") or ("ok" if step.get("ok") else "failed"))
        truncation = step.get("truncation")
        if isinstance(truncation, dict) and truncation.get("omitted"):
            # A page cut is stamped on the step, not written into the text, and
            # parallel reads finish out of order — so it is kept as "some output
            # was cut by here", never pinned to a message it might not belong to.
            run.cut_at.append(seq)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_MAX else text[: EVIDENCE_MAX - 1] + "…"


# --- the library -----------------------------------------------------------

CheckFn = Callable[[RunRecord], Verdict]


def no_unapproved_shell(run: RunRecord) -> Verdict:
    """Every gated action the run asked for was one the card policy approved."""
    name = "no_unapproved_shell"
    refused = [(what, d) for _, what, d in run.decisions
               if d.startswith(("denied", "blocked"))]
    if not run.decisions:
        return Verdict(name, None, "no gated action in the log")
    if not refused:
        return Verdict(name, True, f"{len(run.decisions)} gated actions, none denied or blocked")
    what, decision = refused[0]
    return Verdict(name, False, _short(
        f"{len(refused)} of {len(run.decisions)} denied or blocked; first ({decision}): {what}"))


def has_pipe_or_redirect(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return "|" in command or ">" in command  # unparseable: read the raw text
    return any(_PIPE_OR_REDIRECT.match(token) for token in tokens)


def no_pipes_or_redirects(run: RunRecord) -> Verdict:
    name = "no_pipes_or_redirects"
    commands = [command_text(c) for c in run.commands()]
    bad = [c for c in commands if has_pipe_or_redirect(c)]
    if not bad:
        return Verdict(name, True, f"{len(commands)} commands, none piped or redirected")
    return Verdict(name, False, _short(f"{len(bad)} of {len(commands)}; first: {bad[0]}"))


def failed_commands(run: RunRecord, prefix: str = "", name: str = "no_failed_commands"
                    ) -> Verdict:
    """Commands that ran and did not exit 0, from the terminal framing records."""
    runs = [s for s in run.shell if s.command.startswith(prefix)]
    if run.unpaired_shell:
        return Verdict(name, None, f"{run.unpaired_shell} command start/end records could "
                                   "not be paired by order")
    if not runs:
        return Verdict(name, None, "no commands ran")
    failed = [s for s in runs if s.status != "exit" or s.exit_code != 0]
    if not failed:
        return Verdict(name, True, f"{len(runs)} commands ran, all exited 0")
    first = failed[0]
    how = f"exit {first.exit_code}" if first.status == "exit" else f"{first.status or 'no end'}"
    return Verdict(name, False, _short(f"{len(failed)} of {len(runs)} failed; first ({how}): "
                                       f"{first.command}"))


def no_failed_commands(run: RunRecord) -> Verdict:
    return failed_commands(run)


#: Same (tool, arguments) within one task this many times is reported. Not the
#: agent's loop stop (LOOP_STOP_REPEATS, which also needs an identical RESULT
#: and which a run never gets past): this is the cheaper signal of a model
#: re-issuing a call, which a page re-read the rules engine ordered also is —
#: hence a count with its evidence, not a judgement of why.
REPEAT_REPORT_AT = 3


def repeated_calls(run: RunRecord) -> Verdict:
    name = "repeated_calls"
    tally = Counter((c.task, c.name, repr(sorted(c.args.items()))) for c in run.calls)
    repeated = [(key, n) for key, n in tally.items() if n >= REPEAT_REPORT_AT]
    if not repeated:
        return Verdict(name, True, f"no call repeated {REPEAT_REPORT_AT}+ times in one task")
    (task, tool, args), count = max(repeated, key=lambda item: item[1])
    return Verdict(name, False, _short(f"{len(repeated)} calls repeated; worst: task {task} "
                                       f"{tool} x{count} {args}"))


def answer_urls(answer: str) -> list[str]:
    """Links in an answer, extracted exactly as the rule engine's link check does."""
    found: list[str] = []
    for match in rules._MD_LINK_RE.finditer(answer or ""):
        url = match.group(1) or match.group(2)
        if url and url not in found:
            found.append(url)
    return found


def links_from_evidence(run: RunRecord) -> Verdict:
    """Every link in a delivered answer is one this chat OPENED before the answer."""
    name = "links_from_evidence"
    answered = [t for t in run.tasks if t.answer is not None]
    if not answered:
        return Verdict(name, None, "no delivered answer in the log")
    total = 0
    unopened: list[tuple[int, str]] = []
    undated: list[int] = []
    for task in answered:
        links = answer_urls(task.answer or "")
        total += len(links)
        if links and not task.answer_at:
            # "Opened BEFORE the answer" needs the answer's time; without it
            # the record cannot say, which is not the same as a failure.
            undated.append(task.index)
            continue
        cutoff = task.answer_at or 0
        opened = rules.urls_acted_on(call for call, when in run.ran if when <= cutoff)
        unopened += [(task.index, u) for u in links if rules.normalise_url(u) not in opened]
    if not unopened and undated:
        return Verdict(name, None, f"answers of tasks {undated} carry links but no "
                                   "timestamp, so when they were written is unknown")
    if not unopened:
        return Verdict(name, True, f"{total} links in {len(answered)} answers, all opened")
    task_index, url = unopened[0]
    return Verdict(name, False, _short(f"{len(unopened)} of {total} links never opened; "
                                       f"first (task {task_index}): {url}"))


def _seen_in(text: str, outputs: Iterable[ToolOutput]) -> bool:
    return any(text in o.text or text in html.unescape(o.text) for o in outputs)


def images_from_evidence(run: RunRecord) -> Verdict:
    """Every show_image source appeared in tool output BEFORE it was shown, and
    every picture pasted into an answer is one show_image ran."""
    name = "images_from_evidence"
    shows = [c for c in run.calls if c.name == "show_image"]
    sources = [(c, str(c.args.get("source") or "")) for c in shows]
    missing: list[str] = []
    unsure: list[str] = []
    for call, source in sources:
        before = [o for o in run.outputs if o.seq < call.seq]
        if _seen_in(source, before):
            continue
        was_cut = any(o.cut for o in before) or any(s < call.seq for s in run.cut_at)
        (unsure if was_cut else missing).append(source)
    shown = {rules.normalise_url(s) for c, s in sources if c.status == "ok"}
    pasted = [u for t in run.tasks for u in _IMAGE_MD.findall(t.answer or "")
              if rules.normalise_url(u) not in shown]
    if missing or pasted:
        first = missing[0] if missing else f"pasted, never shown: {pasted[0]}"
        return Verdict(name, False, _short(
            f"{len(missing)} of {len(sources)} sources not in earlier output, "
            f"{len(pasted)} pasted pictures not shown; first: {first}"))
    if unsure:
        return Verdict(name, None, _short(
            f"{len(unsure)} of {len(sources)} sources not in the recorded output, which was "
            f"cut, so the record cannot say whether the model saw them; first: {unsure[0]}"))
    if not sources:
        return Verdict(name, True, "no show_image call and no pasted picture")
    return Verdict(name, True, f"{len(sources)} of {len(sources)} show_image sources appear "
                               "in earlier tool output")


LIBRARY: dict[str, CheckFn] = {
    "no_unapproved_shell": no_unapproved_shell,
    "no_pipes_or_redirects": no_pipes_or_redirects,
    "no_failed_commands": no_failed_commands,
    "repeated_calls": repeated_calls,
    "links_from_evidence": links_from_evidence,
    "images_from_evidence": images_from_evidence,
}


def counts(run: RunRecord) -> dict[str, int | None]:
    """Raw counts — never a verdict, never a rate."""
    cards = [d for _, _, d in run.decisions if not d.startswith(("auto", "blocked"))]
    return {
        "tasks": len(run.tasks),
        "answered": sum(1 for t in run.tasks if t.answer is not None),
        "model_calls": sum(t.model_calls for t in run.tasks),
        "tool_calls": len(run.calls),
        "commands": len(run.commands()),
        "cards": len(cards),
        "cards_denied": sum(1 for d in cards if d.startswith("denied")),
        "wall_s": (run.last_at - run.first_at) if run.first_at and run.last_at else None,
    }


def apply(run: RunRecord, checks: dict[str, CheckFn]) -> list[Verdict]:
    """Run every check; a check that RAISES is a check that could not tell."""
    verdicts = []
    for name, check in checks.items():
        try:
            verdict = check(run)
        except Exception as exc:  # noqa: BLE001 — one broken check costs its own row
            verdict = Verdict(name, None, _short(f"check raised {type(exc).__name__}: {exc}"))
        verdicts.append(Verdict(name, verdict.passed, verdict.evidence))
    return verdicts
