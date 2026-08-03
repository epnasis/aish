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


@dataclass
class Compiled:
    """Either `fields` (ready for `rules.render`) or `problem` — never both."""

    fields: dict = field(default_factory=dict)
    problem: str = ""
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
        "- answer_from: <tool name>, or the word 'source' meaning the material "
        "the owner handed over (a link, an attachment, a typed path). Every "
        "other source is then refused for that answer.",
        "- never_use: a list of tool names that must not run.",
        "- must_first: one tool that must have RUN before the answer is delivered.",
        "- answer_must_include / answer_must_not: one of the named checks "
        + ", ".join(sorted(rules.ANSWER_DETECTORS)) + ".",
        "- must_tell_me_when: a failure the owner must be told about in plain "
        "words rather than quietly patched over.",
    ]
    subjects = [
        "- prompt: what the owner typed, plus any attachments. Give when_has "
        "(one of " + ", ".join(sorted(rules.CONTAINS_DETECTORS)) + ") or, only if "
        "nothing else fits, when_matches with a regex.",
        "- session: how the session started. Give when_origin: owner or automation.",
        "- action: the call about to run. Give when_action with any of "
        + ", ".join(rules.ACTION_FIELDS[:3]) + ".",
        "- always: every turn. Correct whenever the condition is really about "
        "the ANSWER rather than the request.",
    ]
    return (
        "SUBJECTS (when_subject is exactly one of these):\n" + "\n".join(subjects)
        + "\n\nOBLIGATIONS (at least one; they only ever RESTRICT — there is no "
        "verb that grants permission or auto-approves anything):\n" + "\n".join(verbs)
    )


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
  when_subject      one of: prompt, session, action, always
  when_has / when_matches / when_origin / when_action   as the subject needs
  plus at least one obligation key from the list above
  prose             two or three sentences on WHY this matters to them. Never \
restate the obligation — the fields above are what is enforced.

Two things decide whether this is right:

1. CHECK THE ANSWER, NOT THE REQUEST, wherever you can. "Always show pictures \
with show_image" is not a rule about messages that mention pictures — a wholly \
ordinary request can still produce an answer that wants one. That is \
when_subject "always" plus an obligation on the answer, and it is both cheaper \
and more correct than any trigger.

2. A KEYWORD REGEX IS ALMOST ALWAYS WRONG. when_matches on "image|photo" fires \
on "the Docker image is broken". Reach for when_has first; use when_matches \
only when the thing being matched really is a literal string.

If their instruction cannot be expressed with these fields, reply with a JSON \
object of exactly one key, "cannot", whose value says WHAT could not be \
expressed and WHY, in one sentence, in their terms. Do not approximate: a rule \
that half-does what they asked is worse than no rule, because it looks like it \
worked."""


RETRY = """\
That did not compile. The problem:

{errors}

Reply again with one corrected JSON object, or with {{"cannot": "..."}} if the \
instruction genuinely cannot be expressed with these fields."""


CANNOT_TEMPLATE = """\
I could not turn that into a rule: {reason}

What aish can enforce today: {vocabulary_summary}

Two ways forward — say which:
  · rephrase it toward what exists, or
  · leave it as it is, and this becomes a request to extend aish itself."""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(reply: str) -> dict | None:
    """The model's JSON, or None. Tolerant of a fenced block or a stray
    sentence around it — the failure we care about is a wrong FIELD, which the
    lint catches, not a wrapper the parser can see past."""
    match = _JSON_RE.search(reply or "")
    if not match:
        return None
    try:
        loaded = json.loads(match.group(0))
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def compile_request(
    request: str,
    ask: Callable[[str], str],
    known_tools: set[str],
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
        tools=", ".join(sorted(known_tools)) or "(none)",
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
        if refusal := str(parsed.get("cannot", "") or "").strip():
            # Taken at face value and NOT retried. A compiler told the request
            # is inexpressible, then asked again, will invent an approximation
            # — and an approximation is the failure mode this whole layer
            # exists to prevent, because it looks like it worked.
            return Compiled(problem=_cannot(refusal), rounds=attempt)
        fields = {k: v for k, v in parsed.items() if k in rules.AUTHOR_FIELDS}
        if existing:
            fields = {**existing, **fields}
        try:
            text = rules.render(fields)
        except rules.LintError as exc:
            errors = [str(exc)]
            continue
        rule, lint_errors = rules.lint(text, known_tools=known_tools)
        if rule is not None:
            return Compiled(fields=fields, rounds=attempt)
        errors = lint_errors

    return Compiled(
        problem=(
            f"I could not write that as a rule aish can enforce. The last problem "
            f"was: {'; '.join(errors)}\n\nTell me in different words what should "
            "always or never happen, and I will try again."
        ),
        rounds=MAX_ROUNDS,
    )


def _cannot(reason: str) -> str:
    """The refusal the owner reads. "Not expressible in the current vocabulary"
    is useless; this names what, why, and what the two options are — and the
    second option is the point. **A failed compile is a feature request in
    structured form**, which is the self-improvement loop working for once."""
    return CANNOT_TEMPLATE.format(
        reason=reason.rstrip("."),
        vocabulary_summary=(
            "answer from a named tool or from the material you gave it; never use "
            "certain tools; call something before answering; a named check on the "
            "finished answer (" + ", ".join(sorted(rules.ANSWER_DETECTORS))
            + "); tell you when something failed"
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
