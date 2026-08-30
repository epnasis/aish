"""What aish spent, and what filled the context that made it cost that.

A pure scan over the session logs: no model call, no live process state, no
provider API. Same shape as `curate.scan_ledger`, and the same reason —
`docs/trace-contract.md` §7 makes a `scan_*` pass the sanctioned way to compute
over recorded evidence, as opposed to §0's prohibition on re-deriving behaviour
from source. Everything below is already in the log; nothing here needed a new
record kind. `docs/token-accounting.md`.

Three numbers, kept apart on purpose:

**Spend** — input + output tokens summed across calls. What you are billed and
rate-limited on.

**Context** — how big the conversation got. Summing prompt tokens across calls
DOUBLE-COUNTS the resent history, so spend reads as a far bigger number than the
context ever was; reporting one as the other would make every figure wrong in
the same direction.

**Residency** — chars x the number of calls they stayed in context. Because
every call resends the whole history, a page read early and never trimmed is
resent on every subsequent call, and that is what the rate limiter counts. It is
NOT cost: aish keeps the prefix stable precisely because providers cache it, so
on a caching backend a long-resident result is far cheaper than residency
suggests. Reported beside spend, never as spend.

And residency names the biggest resident, not the most wasteful one. A result
read at call 2 and still needed at call 50 is resident by necessity. Triage
metric, not blame metric.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence
from .explain import MISSING, PURGED, RECORDED, _records, origin_of, state_dir

# What a session whose model calls left no usage record is called. NEVER 0: a
# day of heavy claude-max use reading "0 tokens" is a confident lie, and the
# three-states discipline in docs/diagnostics.md exists for exactly this.
NOT_RECORDED = "not recorded"

# The window to measure a call against, from #330: the owner has ordered a
# machine to run inference locally and the realistic local context is ~60,000
# tokens, against a cloud window a hundred times larger. It is a DEFAULT and not
# a fact about any backend — `--window` overrides it — because a number invented
# here would otherwise be reported as though the log said it.
LOCAL_WINDOW_TOKENS = 60_000


@dataclass
class Call:
    """One model call's reported usage."""

    session: str
    ts: str
    number: int
    provider: str
    model: str
    input: int = 0
    output: int = 0
    cached: int = 0
    semantics: str = ""

    @property
    def day(self) -> str:
        return self.ts[:10]

    @property
    def total(self) -> int:
        return self.input + self.output


@dataclass
class Contributor:
    """One origin's share of a session's context.

    `chars` is what it put IN — a measured fact. `char_calls` is that multiplied
    by how long it stayed. Deliberately NOT converted to tokens: chars are
    measured, tokens here would be a model of one, and giving an estimate the
    provider's own unit invites a reader to sum the parts and contradict the
    call's reported total.
    """

    origin: str
    chars: int = 0
    items: int = 0
    char_calls: int = 0
    trimmed: int = 0  # results whose residency was ended by a trim


@dataclass
class SessionUsage:
    name: str
    path: Path
    provider: str = ""
    model: str = ""
    origin: str = "user"
    title: str = ""
    calls: list[Call] = field(default_factory=list)
    # Raw `role` records (#297), kept as written. `roles.scan_counters` turns
    # them into counters; this module only carries them, so the two readers
    # cannot disagree about what a role call was.
    role_calls: list[dict] = field(default_factory=list)
    # Raw `vocab` records (#322), likewise kept as written.
    vocab_calls: list[dict] = field(default_factory=list)
    contributors: list[Contributor] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    trims: int = 0
    unattributed_trims: int = 0
    rewritten: bool = False
    saw_model_calls: bool = False
    # Whether this log's message records carry the `model_call` stamp (#262).
    # Without it, when a message entered the context can only be inferred from
    # the order lines happen to sit in the file, and residency degrades to
    # "chars x every call in the session" — a number that LOOKS like attribution
    # and is arithmetic on an assumption. A log written before the stamp says so
    # instead of showing it.
    stamped: bool = False
    # The PER-CALL floor: what every call of this chat carried besides the
    # conversation — the system text (`brief.system`) and the tool menu
    # (`brief.tools`, whose bytes are in the evidence store).
    #
    # **Only the menu half is fixed.** The system text is COMPOSED PER TASK: the
    # prompt, aish's own usage notes, the live knowledge index, and the rules
    # and preloaded skills that task selected. Calling the whole thing a
    # standing prompt would aim someone cutting for a 60k window at the one
    # component that is not the problem. The brief sizes the parts and does not
    # split them, and this reader may not split them either — that would mean
    # parsing inside a recorded digest, which is re-derivation.
    #
    # Taken from the LAST brief in the file, i.e. what was in force at the end.
    # States are separate from sizes: a log with no brief and a chat handed no
    # tools are different facts (docs/diagnostics.md, three states).
    system_chars: int = 0
    system_state: str = MISSING
    menu_chars: int = 0
    menu_state: str = MISSING
    tool_count: int = 0

    @property
    def fixed_chars(self) -> int:
        return self.system_chars + self.menu_chars

    @property
    def fixed_measured(self) -> bool:
        return self.system_state == RECORDED and self.menu_state == RECORDED

    @property
    def recorded(self) -> bool:
        """False when this session made model calls that reported no usage —
        claude-max drives the Claude Agent SDK's own loop and records no input
        tokens at all. Its spend is unknown, which is a different fact from
        zero and must never be shown as zero."""
        return bool(self.calls) or not self.saw_model_calls

    @property
    def spend(self) -> int:
        return sum(call.total for call in self.calls)

    @property
    def peak_context(self) -> int:
        return max((call.input for call in self.calls), default=0)


def scan_session(path: Path) -> SessionUsage:
    """One log file → its usage. Never raises on content: a torn line is skipped
    the way every other reader of these files skips it."""
    session = SessionUsage(name=path.stem, path=path)
    # Messages still in context, oldest first per origin, as
    # origin -> [[first_call_present, chars, closed_at_or_None], ...]
    live: dict[str, list[list]] = defaultdict(list)
    buckets: dict[str, Contributor] = {}
    call_number = 0
    last_call = 0

    def bucket(origin: str) -> Contributor:
        return buckets.setdefault(origin, Contributor(origin=origin))

    for record in _records(path):
        kind = record.get("kind")
        if kind == "model":
            spec = str(record.get("model") or "")
            provider, _, name = spec.partition(":")
            session.provider, session.model = (provider, name) if name else ("ollama", spec)
        elif kind == "task_start":
            session.origin = str(record.get("origin") or session.origin)
        elif kind == "title":
            session.title = str(record.get("title") or "")
        elif kind == "redaction" or record.get("redacted"):
            # The log is not the billing truth: a redaction removes records for
            # calls that were still billed. Web Retry no longer belongs in this
            # sentence — `supersede_last_turn` MARKS the discarded turn instead
            # of deleting it (#339), so its calls are still here to be counted
            # and nothing sets this flag for them.
            session.rewritten = True
        elif kind == "message":
            if "model_call" in record:
                session.stamped = True
            stamp = int(record.get("model_call") or 0)
            chars = len(str(record.get("content") or ""))
            origin = origin_of(record)
            entry = [stamp + 1, chars, None]
            live[origin].append(entry)
            item = bucket(origin)
            item.chars += chars
            item.items += 1
        elif kind == "trace":
            step = record.get("step") or {}
            skind = step.get("kind")
            if skind == "reasoning":
                call_number = int(step.get("model_call") or call_number + 1)
                last_call = max(last_call, call_number)
                session.calls.append(_call_from(session, record, step, call_number))
                session.saw_model_calls = True
            elif skind == "model_error":
                session.saw_model_calls = True
                session.failures.append(dict(step))
            elif skind == "trim":
                session.trims += 1
                session.unattributed_trims += int(step.get("stubbed_truncated") or 0)
                _close_stubbed(live, buckets, step, last_call)
            elif skind == "brief":
                _fixed_floor(session, step, path)
            elif skind == "role":
                # Per-charter attribution (#297). It cannot travel through the
                # governor — `reserve_for_call` takes a provider:model key and
                # nothing else — so it lives in the role's own record and is
                # read here. Kept OUT of `session.calls`, deliberately: those
                # are the acting loop's calls, and folding a role's spend into
                # them would make every existing per-model figure move for a
                # reason no reader could see. It is reported beside them.
                session.role_calls.append(dict(step))
            elif skind == "vocab":
                # #322, carried the same way and for the same reason: this
                # module holds the raw records and `vocab.scan_counters` is the
                # single reading of them, so `aish usage`'s pointer and
                # `aish vocab`'s table can never disagree about what a
                # consultation was.
                session.vocab_calls.append(dict(step))

    if session.stamped:
        for origin, entries in live.items():
            item = bucket(origin)
            for first, chars, closed in entries:
                end = last_call if closed is None else closed
                item.char_calls += chars * max(0, end - first + 1)
    session.contributors = sorted(
        buckets.values(), key=lambda c: (c.char_calls, c.chars), reverse=True
    )
    return session


_MENU_SIZES: dict[str, int | None] = {}


def _fixed_floor(session: SessionUsage, step: dict, path: Path) -> None:
    """What this chat carried on every call besides the conversation (#330).

    The system parts are sized by the brief itself. The tool menu is not in the
    log at all — the brief records its digest and the bytes live once in the
    evidence store — so this is a lookup of recorded bytes, cached per digest
    because one menu serves hundreds of sessions. A digest whose bytes are gone
    is `purged`, never 0.

    Only the MENU is constant across tasks; see the note on the fields.
    """
    system = step.get("system")
    if system is not None:
        session.system_chars = sum(int(p.get("chars") or 0) for p in system)
        session.system_state = RECORDED
    menu = step.get("tools") or {}
    digest = str(menu.get("digest") or "")
    session.tool_count = int(menu.get("count") or session.tool_count)
    if not digest:
        return
    if digest not in _MENU_SIZES:
        blob = evidence.get(digest, path.parent)
        _MENU_SIZES[digest] = None if blob is None else len(blob)
    size = _MENU_SIZES[digest]
    session.menu_chars = size or 0
    session.menu_state = PURGED if size is None else RECORDED


def _call_from(session: SessionUsage, record: dict, step: dict, number: int) -> Call:
    tokens = step.get("tokens") or [0, 0]
    detail = step.get("usage") or {}
    return Call(
        session=session.name,
        ts=str(record.get("ts") or ""),
        number=number,
        provider=session.provider,
        model=session.model,
        # The detail is authoritative where it exists: `tokens[0]` means three
        # different things across the three backends, and only `semantics` says
        # which. Falling back to it is honest for logs written before the split
        # was kept, and those are exactly the logs whose units are ambiguous.
        input=int(detail.get("input") or (tokens[0] if tokens else 0)),
        output=int(detail.get("output") or (tokens[1] if len(tokens) > 1 else 0)),
        cached=int(detail.get("cached") or detail.get("cache_read") or 0),
        semantics=str(detail.get("semantics") or ""),
    )


def _close_stubbed(live, buckets, step: dict, at_call: int) -> None:
    """A trim ends a result's residency. Oldest-first, matching the policy the
    trimmer actually uses, and per TOOL NAME rather than per instance:
    `stubbed[].at` indexes the live message list, which does not map 1:1 to log
    order across resume, redact or rewind. Attribution by name survives that;
    per-instance identity does not, and claiming it would be a false precision.
    """
    for stub in step.get("stubbed") or []:
        origin = str(stub.get("tool") or "tool")
        for entry in live.get(origin, []):
            if entry[2] is None:
                entry[2] = at_call
                buckets.setdefault(origin, Contributor(origin=origin)).trimmed += 1
                break


def scan(root: os.PathLike | str | None = None, days: int | None = None) -> list[SessionUsage]:
    """Every session log, newest last. One unreadable file must not take the
    whole report down with it — the containment rule a single corrupt session
    log taught the web server."""
    directory = Path(root) if root is not None else state_dir()
    if not directory.is_dir():
        return []
    out: list[SessionUsage] = []
    for path in sorted(directory.glob("session-*.jsonl")):
        if days is not None and not _within(path.stem, days):
            continue
        try:
            out.append(scan_session(path))
        except OSError:
            continue
    return out


def _within(stem: str, days: int) -> bool:
    """`session-YYYYMMDD-...` — the date is in the NAME, so a whole file can be
    skipped without opening it. That is what keeps `aish usage --days 7` from
    parsing a year of logs to discard them."""
    import datetime

    parts = stem.split("-")
    if len(parts) < 2 or len(parts[1]) != 8 or not parts[1].isdigit():
        return True  # unnameable: include it rather than silently drop it
    try:
        when = datetime.date(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:]))
    except ValueError:
        return True
    return (datetime.date.today() - when).days < days


def calls_of(sessions: list[SessionUsage]) -> list[Call]:
    return [call for session in sessions for call in session.calls]


def by_day(calls: list[Call]) -> dict[str, list[Call]]:
    grouped: dict[str, list[Call]] = defaultdict(list)
    for call in calls:
        grouped[call.day].append(call)
    return dict(sorted(grouped.items()))


def by_week(calls: list[Call]) -> dict[str, list[Call]]:
    import datetime

    grouped: dict[str, list[Call]] = defaultdict(list)
    for call in calls:
        try:
            when = datetime.date.fromisoformat(call.day)
        except ValueError:
            continue
        monday = when - datetime.timedelta(days=when.weekday())
        grouped[f"w/c {monday.isoformat()}"].append(call)
    return dict(sorted(grouped.items()))


def by_model(calls: list[Call]) -> dict[str, list[Call]]:
    grouped: dict[str, list[Call]] = defaultdict(list)
    for call in calls:
        grouped[f"{call.provider}:{call.model}" if call.provider else call.model].append(call)
    return dict(sorted(grouped.items(), key=lambda kv: -sum(c.total for c in kv[1])))


def json_report(sessions: list[SessionUsage]) -> str:
    """The same figures, for anything that is not a terminal."""
    return json.dumps(
        {
            "sessions": [
                {
                    "name": s.name,
                    "model": f"{s.provider}:{s.model}" if s.provider else s.model,
                    "origin": s.origin,
                    "calls": len(s.calls),
                    "input": sum(c.input for c in s.calls),
                    "output": sum(c.output for c in s.calls),
                    "cached": sum(c.cached for c in s.calls),
                    "peak_context": s.peak_context,
                    "recorded": s.recorded,
                    "rewritten": s.rewritten,
                    "failures": len(s.failures),
                    "residency_recorded": s.stamped,
                    # Absent rather than 0 when no brief was written: a consumer
                    # must not read "never recorded" as "cost nothing".
                    **({"system_chars": s.system_chars}
                       if s.system_state == RECORDED else {}),
                    **({"menu_chars": s.menu_chars, "tools": s.tool_count}
                       if s.menu_state == RECORDED else {}),
                    "fixed_floor_state": (
                        RECORDED if s.fixed_measured
                        else PURGED if s.menu_state == PURGED else MISSING
                    ),
                    "contributors": [
                        {
                            "origin": c.origin,
                            "chars": c.chars,
                            "items": c.items,
                            # Absent rather than 0 when the log cannot support
                            # it: a consumer must not read "not measurable" as
                            # "nothing was resident".
                            **({"char_calls": c.char_calls} if s.stamped else {}),
                            "trimmed": c.trimmed,
                        }
                        for c in s.contributors
                    ],
                }
                for s in sessions
            ],
            # Per-charter, across the whole window rather than per session: a
            # role's fire rate is a property of the charter, and a per-session
            # count of two calls says nothing about it (#297, contract §7).
            "roles": {
                name: {
                    "calls": c.calls,
                    "by_status": c.by_status,
                    "retries": c.retries,
                    "input": c.input_tokens,
                    "output": c.output_tokens,
                    "input_chars": c.input_chars,
                    "ms_p50": c.ms_p50,
                    "flags": c.flags,
                }
                for name, c in sorted(_role_counters(sessions).items())
            },
        },
        indent=2,
    )


def _role_counters(sessions: list[SessionUsage]):
    from . import roles as roles_mod

    return roles_mod.scan_counters(
        {"step": step} for session in sessions for step in session.role_calls
    )


# ------------------------------------------------------------------ rendering

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def human(count: int) -> str:
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(count) >= limit:
            return f"{count / limit:.1f}{suffix}"
    return str(count)


def _totals(calls: list[Call]) -> tuple[int, int, int, int]:
    return (
        len(calls),
        sum(c.input for c in calls),
        sum(c.output for c in calls),
        sum(c.cached for c in calls),
    )


def _rows(title: str, grouped: dict[str, list[Call]]) -> list[str]:
    out = [f"{BOLD}{title}{RESET}", f"  {'':<26} {'calls':>7} {'in':>9} {'out':>8} {'cached':>8}"]
    for label, calls in grouped.items():
        n, tin, tout, cached = _totals(calls)
        shown = human(cached) if cached else f"{DIM}—{RESET}"
        out.append(f"  {label:<26} {n:>7} {human(tin):>9} {human(tout):>8} {shown:>8}")
    return out


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _trend_rows(
    sessions: list[SessionUsage], grouped: dict[str, list[Call]], window: int
) -> list[str]:
    """The same figures over time, against the window that will actually exist.

    #330's constraint: inference is moving local and a realistic local context
    is ~60,000 tokens. The totals above cannot answer "would this have fit" — a
    period of small calls and a period of three enormous ones sum alike. So this
    counts CALLS over the window and reports the distribution rather than a
    mean, because the tail is the thing that breaks.

    **It takes the SAME grouping the tables above were built from.** Bucketing
    sessions by their own start date instead put 745 calls under a day the row
    directly above called 490 — two tables, one period label, two answers, with
    nothing on screen to say they were counting different things.

    It lives HERE and not in `aish explain` deliberately. `explain` reads one
    turn out of one file; a trend is a scan across sessions, which is what this
    module already is. Building it in both would give the owner two places to
    read the same fact, and they would disagree the first time one changed.
    """
    if not grouped:
        return []
    floors = {s.name: s.fixed_chars for s in sessions if s.fixed_measured}
    out = [
        "",
        f"{BOLD}against a {window:,}-token window{RESET} "
        f"{DIM}(#330 — what a local model will have){RESET}",
        f"  {'':<26} {'calls':>7} {'over':>10} {'median in':>10} {'p95 in':>9} "
        f"{'sys+menu':>9}",
    ]
    for label, group in grouped.items():
        inputs = [c.input for c in group]
        over = sum(1 for value in inputs if value > window)
        share = f"{over} ({over / len(inputs) * 100:.0f}%)" if inputs else "—"
        # The floor of the chats these calls belong to — measured where a brief
        # was written, and simply absent where none was.
        measured = [floors[c.session] for c in group if c.session in floors]
        fixed = human(_percentile(measured, 0.5)) if measured else f"{DIM}—{RESET}"
        out.append(
            f"  {label:<26} {len(inputs):>7} {share:>10} "
            f"{human(_percentile(inputs, 0.5)):>10} {human(_percentile(inputs, 0.95)):>9} "
            f"{fixed:>9}"
        )
    unmeasured = sum(1 for s in sessions if s.calls and not s.fixed_measured)
    out.append(
        f"{DIM}  sys+menu = chars carried on EVERY call besides the conversation. The MENU"
        f" half is{RESET}"
    )
    out.append(
        f"{DIM}  the same on every task; the system half is composed per task — the prompt,"
        f" aish's own{RESET}"
    )
    out.append(
        f"{DIM}  usage notes, the knowledge index, and the rules and skills that task"
        f" selected.{RESET}"
    )
    out.append(
        f"{DIM}  Chars are measured; the window is in tokens, and the two are not added —"
        f" `aish explain{RESET}"
    )
    out.append(f"{DIM}  <chat> <turn>` gives one turn's own ratio.{RESET}")
    if unmeasured:
        out.append(
            f"{DIM}  {unmeasured} chat(s) recorded no brief, so their sys+menu is "
            f"{NOT_RECORDED} rather than 0{RESET}"
        )
    return out


def render(
    sessions: list[SessionUsage],
    period: str = "day",
    window: int = LOCAL_WINDOW_TOKENS,
) -> str:
    """The summary. Spend and context are reported separately and never added:
    summing prompt tokens across calls double-counts the resent history."""
    calls = calls_of(sessions)
    if not sessions:
        return "no session logs found"
    lines = [
        f"{BOLD}token usage{RESET} · {len(sessions)} session"
        f"{'' if len(sessions) == 1 else 's'} · {len(calls)} recorded model calls",
        "",
    ]
    grouped = by_week(calls) if period == "week" else by_day(calls)
    lines += _rows(f"by {period}", grouped) + [""]
    lines += _rows("by model", by_model(calls)) + [""]
    n, tin, tout, cached = _totals(calls)
    lines.append(
        f"  {BOLD}spend{RESET} {human(tin)} in + {human(tout)} out"
        + (f", of which {human(cached)} served from cache" if cached else "")
    )
    peak = max((s.peak_context for s in sessions), default=0)
    lines.append(
        f"  {BOLD}context{RESET} peaked at {peak:,} tokens in one call"
        f" {DIM}(spend counts each resend; context does not){RESET}"
    )
    lines += _trend_rows(sessions, grouped, window)
    lines += _role_rows(sessions)
    lines += [""] + _caveats(sessions)
    lines.append(f"{DIM}  measures RECORDED usage — not a billing statement{RESET}")
    return "\n".join(lines)


def _role_rows(sessions: list[SessionUsage]) -> list[str]:
    """What the isolated roles spent, and what they actually did (#297).

    The counters #295 P4 makes the admission price for a scored answer, in the
    one place the owner already looks at spend. Reported BESIDE the acting
    model's figures rather than inside them: a role's tokens are real money on
    the same key, but folding them in would silently move every per-model
    number that existed before roles did.

    It says what was RECORDED. A charter with no calls does not appear at all —
    which is not the same as a charter that behaved well, and the absence of a
    section is the honest rendering of "nothing was asked".
    """
    counters = _role_counters(sessions)
    if not counters:
        return []
    lines = ["", f"  {BOLD}isolated roles{RESET}"]
    for name, c in sorted(counters.items()):
        answered = c.examined
        other = {k: v for k, v in c.by_status.items() if k != "ok"}
        lines.append(
            f"    {name:<18} {c.calls:>5} call{'' if c.calls == 1 else 's'}"
            f" · {answered} answered"
            + (f" · {', '.join(f'{v} {k}' for k, v in sorted(other.items()))}" if other else "")
            + (f" · {c.retries} retried" if c.retries else "")
        )
        if c.input_tokens or c.output_tokens:
            per = c.input_tokens // c.calls if c.calls else 0
            lines.append(
                f"      {DIM}spend{RESET} {human(c.input_tokens)} in + "
                f"{human(c.output_tokens)} out {DIM}(~{per} in per call){RESET}"
                + (f" {DIM}· {c.ms_p50} ms median{RESET}" if c.ms else "")
            )
        for field_name, tally in sorted(c.flags.items()):
            counted = ", ".join(f"{v} {k}" for k, v in sorted(tally.items()))
            lines.append(f"      {DIM}{field_name}{RESET} {counted}")
    return lines


def _caveats(sessions: list[SessionUsage]) -> list[str]:
    """Everything the number is NOT. A report that omits these is confidently
    wrong in a direction the reader cannot see."""
    out = []
    unrecorded = [s for s in sessions if not s.recorded]
    if unrecorded:
        out.append(
            f"  ⚠ {len(unrecorded)} session{'' if len(unrecorded) == 1 else 's'} made model "
            f"calls that reported no usage — shown as {NOT_RECORDED}, never as 0"
        )
    rewritten = [s for s in sessions if s.rewritten]
    if rewritten:
        out.append(
            f"  ⚠ {len(rewritten)} session{'' if len(rewritten) == 1 else 's'} had records "
            "removed by Retry or redaction — those calls were billed and are not counted here"
        )
    failures = sum(len(s.failures) for s in sessions)
    if failures:
        out.append(
            f"  ⚠ {failures} model call{'' if failures == 1 else 's'} failed "
            "— see `aish explain`"
        )
    out.append(
        f"{DIM}  calls outside a task (session retitling, curate) are not counted{RESET}"
    )
    # The word-list pointer (#322), here and nowhere else in this report. It is
    # the one place he already looks, and it is SILENT unless something is
    # anomalous — a row that appears on every ordinary browse is a row he learns
    # to skip, which would cost more than no counter at all. It names the lists
    # and points at `aish vocab`; it never prints the table.
    from . import vocab as vocab_mod

    if line := vocab_mod.summary_line(vocab_mod.scan_counters(
        {"step": step} for s in sessions for step in s.vocab_calls
    )):
        out.append(f"  ⚑ {line}")
    return out


def render_session(session: SessionUsage) -> str:
    """The drill-down: what filled this chat's context."""
    model = f"{session.provider}:{session.model}" if session.provider else session.model
    head = f"{BOLD}{session.name}{RESET}"
    if session.title:
        head += f"  {DIM}{session.title}{RESET}"
    lines = [head, f"  {model} · {session.origin} · {len(session.calls)} recorded calls"]
    if not session.recorded:
        lines.append(f"  spend {BOLD}{NOT_RECORDED}{RESET} — this backend reports no usage")
    else:
        _, tin, tout, cached = _totals(session.calls)
        lines.append(
            f"  spend {human(tin)} in + {human(tout)} out"
            + (f" ({human(cached)} cached)" if cached else "")
            + f" · context peaked at {session.peak_context:,} tokens"
        )
    if session.fixed_measured:
        lines.append(
            f"  on every call {session.system_chars:,} chars of system text "
            f"{DIM}(composed per task){RESET} + {session.menu_chars:,} of tool menu "
            f"({session.tool_count} tools) {DIM}(the same on every task){RESET}"
        )
    elif session.calls:
        lines.append(
            f"  on every call {BOLD}{NOT_RECORDED}{RESET} — this log wrote no brief"
            if session.system_state != RECORDED
            else f"  on every call {session.system_chars:,} chars of system text; the tool "
                 f"menu's bytes are {BOLD}purged{RESET}"
        )
    lines += ["", f"{BOLD}what filled the context{RESET}"]
    # The resident column is dropped rather than filled with "not recorded" on
    # every row: an absent column reads as "this log cannot say", a column of
    # repeated apologies reads as noise.
    tail = f" {'resident':>10} {'trimmed':>8}" if session.stamped else ""
    lines.append(f"  {'':<22} {'items':>6} {'chars':>9}{tail}")
    for item in session.contributors[:15]:
        row = f"  {item.origin:<22} {item.items:>6} {human(item.chars):>9}"
        if session.stamped:
            row += f" {human(item.char_calls):>10} {item.trimmed or '—':>8}"
        lines.append(row)
    if not session.stamped:
        return "\n".join(
            lines
            + [
                "",
                f"{DIM}  This log predates the per-call stamp on message records, so WHEN each",
                "  result entered the context is not recorded and residency cannot be computed.",
                f"  `chars` is measured and stands.{RESET}",
            ]
        )
    lines += [
        "",
        f"{DIM}  resident = chars x calls they stayed in context. Every call resends the whole",
        "  history, so that is what the RATE LIMIT counts — it is not cost, because providers",
        "  cache the stable prefix. It names the biggest resident, not the most wasteful one:",
        f"  a page read early and still needed at the end is resident by necessity.{RESET}",
    ]
    if session.unattributed_trims:
        lines.append(
            f"{DIM}  {session.unattributed_trims} trimmed result(s) could not be attributed "
            f"(the trim record caps its list), so residency above is an upper bound.{RESET}"
        )
    return "\n".join(lines)
