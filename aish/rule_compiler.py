"""Owner's plain language → rule field values (#205 §4).

The acting model **never learns the rule grammar**. Its job is to pass through
what the owner asked for, which models are reliable at; the grammar lives in
one place, versioned with the code, so changing the vocabulary does not require
every model on every backend to relearn it.

Isolated, not in the session. The same argument the engine already makes from
the other direction: *a narrow question with minimal evidence is something a
small local model can answer well, and the same question buried in a 40k-token
transcript is not.* A model mid-conversation about YouTube videos is context-
switching into a grammar it half-remembers.

Precision about why isolation is used here: this is **generation, not a
verdict**. The isolation invariant exists to stop a judge ratifying the actor's
own justification, and a compiler only proposes. Isolation is used because it is
more accurate. What makes it *safe* is that code validates the output and the
owner approves it.

The output space is field values against a closed schema — never a document.
The model cannot emit YAML, cannot invent a key, and cannot write an obligation
in prose, because none of those are things it is asked for.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import rules

# Rounds of compile → lint → feed the error back. Bounded for the same reason
# every other loop here is: a compiler that retries forever turns an
# inexpressible request into a hang instead of an answer.
MAX_ROUNDS = 4

NUM_CTX = 8192

# The refusal is a sentence for a person, and an uncapped one arrives as a wall
# of text in an approval-adjacent message.
CANNOT_CHARS = 400

# The owner's own words, quoted back into the issue. Bounded because a whole
# pasted document would drown the two facts that make the report useful.
REQUEST_CHARS = 300


@dataclass
class Compiled:
    """Either `fields` (ready for `rules.render`) or `problem` — never both.

    `dropped` is the third state, and it exists because refusing outright was
    wrong. Five of the six rules the compiler could not write were requests
    where ONE clause is inexpressible and the rest is fine, and the owner's
    ruling is that partial is acceptable **but never silent**: say what will be
    enforced, say what will not, and let him decide. So `dropped` travels
    ALONGSIDE fields, is shown on the approval card, and never quietly
    disappears — which is the difference between honest partial compilation and
    the approximation the "do not approximate" law was written against.
    """

    fields: dict = field(default_factory=dict)
    problem: str = ""
    dropped: str = ""
    rounds: int = 0

    def __bool__(self) -> bool:
        return bool(self.fields) and not self.problem


def _vocabulary() -> str:
    """The grammar, generated FROM the code rather than restated in a prompt.

    A prompt that lists the verbs by hand is a second copy of the vocabulary,
    and the first thing that happens to a second copy is that it drifts. This
    one cannot: adding a verb or a detector changes what the compiler is told,
    in the same commit.
    """
    verbs = [
        "- answer_from: <tool name>, or the word 'material' meaning what the owner "
        "handed over (a link, an attachment, a typed path). Everything else is "
        "then refused for that answer.",
        "- never_use: a list of tool names that must not run.",
        "- must_first: one tool or skill that must have RUN before the answer is "
        f"delivered — or the literal word '{rules.FIRST_ANSWER}', meaning the "
        "assistant must say something to the person BEFORE it runs anything at "
        "all. Use that for \"answer me before you go off and do things\".",
        "- answer_must_include / answer_must_not_include: what has to be (or not "
        "be) in the finished answer. THREE forms: something the reader would SEE "
        "(" + ", ".join(sorted(rules.ANSWER_KINDS)) + "), or `{any_of: [picture, "
        "video]}` when either will do; `{pattern: <regex>}` for anything literal "
        "about the wording; or `{like: [3-5 example sentences], in: "
        + " | ".join(rules.WHERE_VALUES) + "}` when it is about TONE or MEANING "
        "rather than exact words — that one is matched by meaning, so it is how "
        "\"no flattery\", \"no apologies\", \"never promise\" get expressed. "
        "NEVER a bare plain phrase: a check nothing can evaluate is a promise "
        "nothing keeps.",
        "- ask_me_first: true — the owner decides this one, every time it comes "
        "up. Needs when_subject 'action', because it holds ONE kind of call. "
        "Use it when the point is that HE chooses, not that the assistant "
        "should choose differently.",
    ]
    subjects = [
        "- prompt: what the owner typed, plus any attachments. Give ONE of: "
        "when_has (" + ", ".join(sorted(rules.CONTAINS_DETECTORS)) + ") when the "
        "condition is about what the message CARRIES; when_like, a list of "
        "3-5 example messages, when it is about what the message MEANS; "
        "when_matches with a regex ONLY when the thing being matched is a "
        "literal string such as a domain.",
        "- session: how the session started. Give when_origin: owner or automation.",
        # Generated from the built fields, never a hand-kept slice. A literal
        # `[:3]` here silently dropped `command_has` the day it was added, and
        # the compiler then reported that inline secrets "cannot be inspected" —
        # about the one check built to inspect them.
        "- action: the call about to run, checked BEFORE it runs. Give "
        "when_action as an object with any of: "
        + "; ".join(
            {
                "tool": "tool: <name> — this exact tool",
                "path_under": "path_under: <path> — it would touch anything "
                "under there, including via a shell command",
                "command_starts_with": "command_starts_with: <prefix> — the "
                "shell command begins with this, e.g. 'pip' or 'gh issue'. THIS "
                "is how \"use X instead of raw Y\" is expressed: trigger on the "
                "raw command and never_use run_command",
                "command_has": f"command_has: {rules.COMMAND_HAS_SECRET} — the "
                "command contains one of their stored secrets (the only value "
                "this takes; it is a lookup, not a pattern you write)",
            }[f]
            for f in rules.ACTION_FIELDS
            if f in ("tool", "path_under", "command_starts_with", "command_has")
        )
        + ".",
        "- result: a condition on what a TOOL came back with, for \"if X comes "
        "back empty, tell me rather than quietly using something else\". Give "
        "when_result_of (the tool) and when_result_was ("
        + ", ".join(sorted(rules.RESULT_STATES)) + "). It restricts nothing "
        "until that actually happens.",
        "- answer: a condition on the FINISHED answer, for a rule whose 'if' is "
        "about what came out rather than what went in — \"if you quote me a "
        "price, you must have read the store page\". Give when_matches with a "
        "regex, or when_like with example answers when it is about meaning; "
        "when_in (opening, ending) narrows it to one paragraph. Only obligations "
        "checked at turn end may go with it — never never_use or answer_from.",
        "- always: every turn. Correct when the obligation is on the answer and "
        "there is no 'if' at all. If there IS an 'if' about the answer, use the "
        "answer subject instead of forcing it onto every turn.",
    ]
    return (
        "SUBJECTS (when_subject is exactly one of these):\n" + "\n".join(subjects)
        + "\n\nOBLIGATIONS (at least one; they only ever RESTRICT — there is no "
        "verb that grants permission or auto-approves anything):\n"
        + "\n".join(verbs) + "\n\n" + WORKED_EXAMPLES
    )


# Six finished rules, one per subject, covering the forms a description alone
# does not teach. Every one is a real rule from the owner's own corpus.
#
# The prompt had none: it described the fields and never showed a whole rule, so
# the model had to infer the shape of the thing it was being asked for. Adding
# these is the cheapest fix in the layer — and the failures they address were
# ones where the vocabulary already had exactly the right form and the compiler
# reported it impossible.
WORKED_EXAMPLES = """\
WORKED EXAMPLES — real rules, one per subject. Match these shapes.

"Answer me before you go running commands."
  {"name": "answer-first", "when_subject": "always", "must_first": "answer"}

"Use uv, never bare pip."
  {"name": "python-via-uv", "when_subject": "action",
   "when_action": {"command_starts_with": "pip"}, "never_use": ["run_command"]}

"Never put one of my secrets straight into a command."
  {"name": "no-inline-secrets", "when_subject": "action",
   "when_action": {"command_has": "a_secret"}, "never_use": ["run_command"]}

"No flattery and no apologies when I correct you."
  {"name": "no-flattery", "when_subject": "always",
   "answer_must_not_include": {"like": ["You are absolutely right, sorry about that",
   "Great question! I am glad you asked", "I apologise for the confusion"],
   "in": "opening"}}

"If you quote me a price, you must have read the seller's page."
  {"name": "live-price", "when_subject": "answer",
   "when_matches": "[0-9][0-9., ]*\\\\s?(PLN|EUR|USD)", "must_first": "read_url"}

"When I ask to be shown something, show me a picture or a video."
  {"name": "show-me", "when_subject": "prompt",
   "when_like": ["show me what it looks like", "pokaz mi jak to wyglada",
   "what is the difference between these two places"],
   "answer_must_include": {"any_of": ["picture", "video"]}}

"When something else started the session, never delete my memories."
  {"name": "no-forget-unattended", "when_subject": "session",
   "when_origin": "automation", "never_use": ["forget_memory"]}

"If the transcript comes back empty, tell me instead of using something else."
  {"name": "transcript-failure", "when_subject": "result",
   "when_result_of": "youtube_analyze", "when_result_was": "empty",
   "never_use": ["web_search"],
   "answer_must_include": {"like": ["the transcript came back empty so I could not read the video",
   "nie udalo sie pobrac transkrypcji"]}}"""


PROMPT = """\
You turn one instruction from a person into the field values of a rule their \
assistant will be forced to obey. You are not the assistant and you are not \
answering them — you are only translating.

THEIR INSTRUCTION:
{request}

TOOLS THAT EXIST (nothing else may be named):
{tools}

{vocabulary}

Reply with ONE JSON object and nothing else. Keys:
  name              short-kebab-case, e.g. "bounded-material"
  description       one line, in THEIR words, saying what is required
  when_subject      exactly one of the SUBJECTS above
  when_has / when_like / when_matches / when_in / when_origin / when_action
                    whichever that subject takes — see above
  plus at least one obligation key from the list above
  prose             two or three sentences on WHY this matters to them. Never \
restate the obligation — the fields above are what is enforced.

Two things decide whether this is right:

1. CHECK THE ANSWER, NOT THE REQUEST, wherever you can. "Always show pictures" \
is not a rule about messages that mention pictures — a wholly ordinary request \
can still produce an answer that wants one. That is when_subject "always" plus \
an obligation on the answer, and it is both cheaper and more correct than any \
trigger.

Say what the PERSON would notice, never how aish produces it. "the answer must \
include a picture", not "the answer must include the output of some tool" — \
which tool ran is an implementation detail they never see.

2. NEVER USE A WORD LIST FOR A MEANING. when_matches on "show|display|picture" \
fires on "the Docker image is broken" and misses the same sentence in another \
language, and adding more words makes both worse. If the condition is about \
what the message MEANS — "when I ask to be shown something", "when I am \
planning a trip" — use when_like and write 3-5 whole example messages the \
way that person actually types, including in their other language if they use \
one. Examples are matched by meaning, not by letters.

3. A RULE HAS THREE PARTS, NOT TWO. The condition, the thing that is \
ENFORCED, and the GUIDANCE — what they should do instead when it is refused. \
Guidance goes in `prose`. It is not a second obligation and its absence from \
`then:` is not a failure to express the request.

"Never edit the source code, always open a GitHub issue instead" is ONE \
enforcement and one piece of guidance. The enforcement is when_action \
path_under plus never_use. "Open an issue instead" is what makes the refusal \
useful — a good error says what went wrong AND what to do about it — so it \
belongs in `prose`, and the rule is COMPLETE, not half-written.

Read every "…instead", "…rather than", "…use X for this" the same way: if it \
tells the assistant what to do rather than what not to do, it is guidance.

4. IF PART OF IT GENUINELY CANNOT BE EXPRESSED, SAY SO AND STILL WRITE THE \
REST. Add a key "could_not_express" whose value names the part you had to \
leave out, in one sentence, in their words. The person is shown that on the \
approval card next to what the rule DOES do, and decides. Half a rule they can \
see is useful; half a rule they cannot see is the thing this warns against.

Only reply with a JSON object of exactly one key, "cannot", when NOTHING in \
the instruction can be expressed at all. Its value says what could not be \
expressed and why, in one sentence, in their terms."""


RETRY = """\
That did not compile. The problem:

{errors}

Reply again with one corrected JSON object, or with {{"cannot": "..."}} if the \
instruction genuinely cannot be expressed with these fields."""


CANNOT_TEMPLATE = """\
I could not turn that into a rule: {reason}

What aish can enforce today: {vocabulary_summary}

TELL THE USER THIS, then offer them the choice — do not pick for them:
  · they rephrase it toward what exists, or
  · you open a GitHub issue on epnasis/aish so aish learns to enforce it.

If they choose the issue, open it with this title and body, and nothing you \
invented on top:

  title: rules: cannot express "{request}"
  body:
    The owner asked for a rule and it could not be compiled.

    **What they asked for:** {request}

    **What could not be expressed:** {reason}

    **What the vocabulary offers today:** {vocabulary_summary}

    Filed from a failed compile, so it is a real gap rather than a guess."""


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse(reply: str) -> dict | None:
    """The model's JSON, or None.

    Tolerant of a fenced block or a sentence either side of it. A single greedy
    `{.*}` was not: one brace anywhere outside the object — prose with a
    `{placeholder}`, a `:}` sign-off, a second object — swallowed everything
    between the first and last brace and failed to parse. That only ever cost a
    retry, never a wrong reading, but a retry spends a bounded round on a reply
    that was fine.
    """
    text = (reply or "").strip()
    for candidate in _candidates(text):
        try:
            loaded = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _candidates(text: str):
    """The whole reply, then any fenced block, then each brace-balanced span —
    cheapest and most likely first."""
    yield text
    for match in _FENCE_RE.finditer(text):
        yield match.group(1)
    depth = 0
    start = -1
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start : i + 1]


def compile_request(
    request: str,
    ask: Callable[[str], str],
    capabilities: set[str],
    existing: dict | None = None,
) -> Compiled:
    """One prose instruction → validated field values, or a stated problem.

    `existing` makes this an EDIT: the model is shown the rule as it stands and
    the instruction describes the CHANGE, so the fields it omits are the ones
    already there. That is what keeps "also cover attachments" from silently
    undoing the four things the rule already did.
    """
    prompt = PROMPT.format(
        request=request.strip(),
        tools=", ".join(sorted(capabilities)) or "(none)",
        vocabulary=_vocabulary(),
    )
    if existing:
        prompt += (
            "\n\nTHIS RULE ALREADY EXISTS and their instruction describes a CHANGE "
            "to it. Give only the fields that change; everything you leave out "
            "stays exactly as it is. Do not restate the rule.\n"
            + json.dumps(existing, indent=2, ensure_ascii=False)
        )

    errors: list[str] = []
    for attempt in range(1, MAX_ROUNDS + 1):
        reply = ask(prompt if attempt == 1 else prompt + RETRY.format(
            errors="; ".join(errors) or "the reply was not a JSON object"
        ))
        parsed = _parse(reply)
        if parsed is None:
            errors = ["the reply was not a JSON object"]
            continue
        raw = parsed.get("cannot")
        if raw is not None and not isinstance(raw, str):
            # A non-string went straight into owner-facing text as
            # "I could not turn that into a rule: True". It is a malformed
            # reply, which is a non-answer — so it retries like any other.
            errors = ["`cannot` must be a sentence explaining what could not be expressed"]
            continue
        if refusal := (raw or "").strip()[:CANNOT_CHARS]:
            # Taken at face value and NOT retried. A compiler told the request
            # is inexpressible, then asked again, will invent an approximation
            # — and an approximation is the failure mode this whole layer
            # exists to prevent, because it looks like it worked.
            return Compiled(problem=_cannot(refusal, request), rounds=attempt)
        # COMPILER_FIELDS, not AUTHOR_FIELDS: the prompt never asks for
        # `enabled` or `expires`, and a reply carrying `expires: "2020-01-01"`
        # renders, lints, lands, is dropped at load — and the card describes in
        # full a rule that will never bind once. Accepting a key nobody asked
        # for is pure attack surface, and the request text reaching this can
        # have come from an email.
        fields = {k: v for k, v in parsed.items() if k in rules.COMPILER_FIELDS}
        if existing:
            fields = {**existing, **fields}
        try:
            text = rules.render(fields)
        except rules.LintError as exc:
            errors = [str(exc)]
            continue
        rule, lint_errors = rules.lint(text, capabilities=capabilities)
        if rule is not None:
            # Carried alongside the rule, never instead of it, and shown on the
            # card. Bounded like every other owner-facing sentence here.
            # A STRING or nothing. `str()` on a dict put "{'a': 1}" straight
            # onto the owner's approval card — the same leak already fixed for
            # `cannot`, and the reason it is worth fixing twice is that this
            # text is the only place a dropped clause is ever mentioned.
            dropped = parsed.get("could_not_express")
            return Compiled(
                fields=fields, rounds=attempt,
                dropped=(dropped.strip()[:CANNOT_CHARS]
                         if isinstance(dropped, str) else ""),
            )
        errors = lint_errors

    return Compiled(
        problem=(
            f"I could not write that as a rule aish can enforce. The last problem "
            f"was: {'; '.join(errors)}\n\nTell me in different words what should "
            "always or never happen, and I will try again."
        ),
        rounds=MAX_ROUNDS,
    )


def _cannot(reason: str, request: str = "") -> str:
    """The refusal the owner reads. "Not expressible in the current vocabulary"
    is useless; this names what, why, and what the two options are — and the
    second option is the point. **A failed compile is a feature request in
    structured form**, which is the self-improvement loop working for once.

    The issue text is written HERE rather than left to the acting model,
    because the two facts that make it worth filing — what was asked and what
    could not be expressed — are the two the model was not part of. A gap
    report it composed from memory would be a guess about a guess.
    """
    return CANNOT_TEMPLATE.format(
        request=(request or "(not recorded)").strip()[:REQUEST_CHARS],
        reason=reason.rstrip("."),
        vocabulary_summary=(
            "answer from a named tool or from the material you gave it; never use "
            "certain tools; call something before answering; require a tool's "
            "output to appear in the answer, or require it to be credited when it "
            "is used; a pattern check on the answer's text; tell you when "
            "something failed"
        ),
    )


def make_compiler(model: str | None = None):
    """A prose→text callable over the exact ollama chat convention every backend
    is adapted to. Stateless, no tools, no streaming — the compiler holds no
    conversation and each call is independent."""
    from . import backends

    chat, _provider, name = backends.make_chat(model or "")

    def ask(prompt: str) -> str:
        response = chat(
            model=name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            options={"num_ctx": NUM_CTX},
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
