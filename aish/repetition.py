"""Whether a streamed reply has become one passage repeated over and over (#417).

A model that falls into a loop keeps generating until the provider's output cap:
on 2026-09-26 a `local:` call ran 375 s and 16,384 tokens, and the last 42,734
characters of its reasoning were one 76-character passage, 562 times. Nothing
in aish stopped it, because nothing looked.

This module only OBSERVES. It says "the tail of this text is one passage of N
characters repeated M times"; it never says why, and nothing here reads the
words for meaning. What to do about it is the agent's (`Agent._one_chat`).

The test is EXACT periodicity of the tail, which is why ordinary text does not
trip it: a table's rows, a log's lines and a list's items differ from each other
somewhere (a name, a number, a timestamp), and one differing character anywhere
in the span breaks the period. What remains is text that is character-for-
character the same passage again, many times, over thousands of characters.

The thresholds are measured, not guessed — `docs/agent-core.md` (Backends, "A
reply that repeats itself") has the corpus they were checked against.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The longest passage looked for. Every loop in the owner's logs up to
#: 2026-09-26 repeats a passage of 76 to 1,189 characters (six calls, all
#: `stop: length`); this leaves room above the longest.
MAX_PERIOD_CHARS = 2048
#: How much repeated text it takes, in characters, before a reply is stopped.
#: The recorded 76-character loop trips it at its 79th copy (10,752 characters
#: into the reply) instead of running to 562. The longest exact run in ANY
#: text the owner's logs hold that was not one of the six loops is 3,504
#: characters, in a tool's output; nothing the model wrote came near 600.
MIN_SPAN_CHARS = 6000
#: ...and in copies of the passage, which is what binds for long passages: a
#: 1,189-character passage is stopped at its tenth copy, not its fourth.
MIN_REPEATS = 10
#: How often the tail is examined, in characters of new text. A loop is at
#: least MIN_SPAN_CHARS long before it can fire, so checking every character
#: would buy nothing but CPU.
CHECK_EVERY_CHARS = 256
#: The tail held for examination. Covers the largest span a check can need
#: (MIN_REPEATS x MAX_PERIOD_CHARS) with room to count copies beyond it.
WINDOW_CHARS = 32768
#: The last characters of the tail whose earlier occurrences propose the
#: candidate passage lengths. A true period p puts an exact copy of them p
#: characters back, so ordinary text proposes few candidates or none.
_NEEDLE_CHARS = 32
#: What of the passage itself travels into records and notes.
PASSAGE_SAMPLE_CHARS = 200


@dataclass(frozen=True)
class Repetition:
    """What was observed when the tail turned periodic. Every field is a count
    of characters or copies, measured on the text as it streamed."""

    period_chars: int  # the length of the repeated passage
    repeats: int  # whole copies of it at the end of the text, within the window
    span_chars: int  # how many characters at the end of the text the copies cover
    chars: int  # characters received on the call when it was stopped
    passage: str  # the passage, cut to PASSAGE_SAMPLE_CHARS

    def record(self) -> dict:
        return {
            "period_chars": self.period_chars,
            "repeats": self.repeats,
            "span_chars": self.span_chars,
            "chars": self.chars,
            "passage": self.passage,
        }


def periodic_tail(
    text: str,
    *,
    min_span: int = MIN_SPAN_CHARS,
    min_repeats: int = MIN_REPEATS,
    max_period: int = MAX_PERIOD_CHARS,
) -> tuple[int, int, int] | None:
    """(period, whole copies, span) of the shortest passage whose exact repeats
    cover the end of `text` by at least `max(min_span, min_repeats * period)`
    characters — or None when there is none."""
    n = len(text)
    if n < max(min_span, 2 * _NEEDLE_CHARS):
        return None
    needle = text[-_NEEDLE_CHARS:]
    floor = max(0, n - _NEEDLE_CHARS - max_period)
    end = n - 1  # excludes the needle's own position at n - _NEEDLE_CHARS
    while True:
        at = text.rfind(needle, floor, end)
        if at < 0:
            return None
        period = n - _NEEDLE_CHARS - at
        need = max(min_span, min_repeats * period)
        if need > n:
            # Longer candidates need at least as much text: none can fit.
            return None
        if text[n - need + period:] == text[n - need:n - period]:
            start = n - need
            while start - period >= 0 and text[start - period:start] == text[start:start + period]:
                start -= period
            span = n - start
            return period, span // period, span
        end = at + _NEEDLE_CHARS - 1


class RepetitionWatch:
    """Fed a reply's text as it streams; answers a `Repetition` the first time
    the tail is one passage repeated past the thresholds, None until then."""

    def __init__(self) -> None:
        self._tail = ""
        self._pending: list[str] = []
        self._pending_chars = 0
        self.chars = 0

    def feed(self, text: str) -> Repetition | None:
        if not text:
            return None
        self._pending.append(text)
        self._pending_chars += len(text)
        self.chars += len(text)
        if self._pending_chars < CHECK_EVERY_CHARS:
            return None
        self._tail = (self._tail + "".join(self._pending))[-WINDOW_CHARS:]
        self._pending.clear()
        self._pending_chars = 0
        found = periodic_tail(self._tail)
        if found is None:
            return None
        period, repeats, span = found
        passage = self._tail[len(self._tail) - period:]
        return Repetition(
            period_chars=period,
            repeats=repeats,
            span_chars=span,
            chars=self.chars,
            passage=passage[:PASSAGE_SAMPLE_CHARS],
        )
