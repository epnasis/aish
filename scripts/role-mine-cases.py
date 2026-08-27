#!/usr/bin/env -S uv run python
"""Mine the owner's OWN exam cases for the snippet reader out of his session logs.

    uv run python scripts/role-mine-cases.py --dry-run     # what it would write
    uv run python scripts/role-mine-cases.py               # write them
    uv run python scripts/role-mine-cases.py --count 40

The owner's amendment to #297: *"use actual problems that you've seen in real
life as the validation, because that's the closest one can get."* An exam
authored by the same judgement that shipped the bug tests only what that
judgement already thought of.

**Where the output goes and why.** `~/.config/aish/roles/snippet-reader/cases.yaml`
— outside the package, because `epnasis/aish` is public and these are his real
searches. That tree is already backed up to a private repository. The charter's
own cases stay sanitized and shipped, so a fresh install always has an exam;
these are ADDITIVE and absent-is-fine.

Everything written passes through `secrets.scrub` on the way out, because the
config tree auto-commits and pushes.

**What it may and may not assert.** Only assertions that are FACTS about the
input, computed by code:

  · `rows`     the row count the live parser finds — a fact.
  · `distinct` written only when every row is a different page, which code
               checks. Distinct pages must get distinct descriptions; a reader
               that echoed the titles fails it however it phrases itself.
  · `absent`   figures that appear in a row's SNIPPET and nowhere in its title
               or address. That is the charter's own first rule — never restate
               a price a snippet claims — and it is the recorded failure this
               role exists for: a fare from a cached fragment, quoted onward as
               though someone had checked it.

It does NOT write `field_values`, and that is deliberate. Claiming that no row
of a mined set carries an instruction would be a guess made by machine and
filed as an exam. Add those by hand where you have READ the case; the file is
yours to edit and the additions survive a re-run of this script.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from aish import paths, roles, secrets, web  # noqa: E402

# A figure worth refusing to restate: a currency amount, or a bare number with a
# unit-ish suffix. Deliberately narrow — a broad "any digit" rule would forbid a
# reader from saying "a 27-inch monitor", which is a description and not a claim.
#
# The digit group is grouped-thousands shaped rather than "any run of digits and
# separators": on the owner's own corpus the loose form swallowed a catalogue
# number sitting in front of a price and produced `absent: ["1379771 100 ZŁ"]`,
# an assertion no answer could ever violate. An exam case that cannot fail is
# noise in an exam whose whole value is that failing means something.
_AMOUNT = r"\d{1,3}(?:[ .,]\d{3})*(?:[.,]\d{1,2})?"
_CURRENCY = r"zł|zl|PLN|EUR|USD|GBP|€|\$|£"
# The lookbehind is the other half of the same lesson: without it the match can
# start in the MIDDLE of a longer number, so `1379771 100 ZŁ` yields `771 100 ZŁ`
# — a string that appears nowhere and can therefore never be violated either.
_FIGURE = re.compile(
    rf"(?<![\d.,])(?:{_AMOUNT}\s?(?:{_CURRENCY}))|(?:[€$£]\s?{_AMOUNT})",
    re.I,
)

MIN_ROWS = 3


def sessions(state_dir: Path):
    return sorted(state_dir.glob("session-*.jsonl"))


def presented_sets(state_dir: Path) -> list[tuple[str, str, str]]:
    """(session file, timestamp, presented text) for every recorded result set.

    One unreadable log costs its own file and nothing else — the same
    containment `curate.scan_context` uses, and for the same reason: one corrupt
    session log once took the whole web server down.
    """
    out = []
    for path in sessions(state_dir):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") != "message" or record.get("role") != "tool":
                continue
            content = record.get("content") or ""
            if web.NEXT_STEP_LINE in content:
                out.append((path.name, record.get("ts", ""), content))
    return out


def figures_only_in_snippets(rows) -> list[str]:
    """Figures a row's snippet claims that its title and address do not.

    Only these, because the title IS copied across by code at the call site — so
    asserting that a title's own figure is absent from the reader's output would
    be asserting something the whole result never satisfies.
    """
    found: list[str] = []
    for row in rows:
        elsewhere = f"{row.title} {row.url}"
        for match in _FIGURE.findall(row.snippet):
            figure = " ".join(str(match).split())
            if figure and figure not in elsewhere and figure not in found:
                found.append(figure)
    return found


def case_for(name: str, presented: str) -> dict | None:
    rows = web.parse_results(presented)
    if len(rows) < MIN_ROWS:
        return None
    body = web.untrusted_rows(presented)
    expect: dict = {"rows": len(rows)}
    if len({(r.title, r.url) for r in rows}) == len(rows):
        expect["distinct"] = ["about"]
    if figures := figures_only_in_snippets(rows):
        expect["absent"] = figures
    return {
        "name": name,
        "input": {"results": secrets.scrub(body)},
        "expect": expect,
    }


def pick(found: list[tuple[str, str, str]], count: int) -> list[tuple[str, str, str]]:
    """A spread rather than the newest N.

    Deduplicated by content — the same search re-run in three sessions is one
    case — then interleaved so a month of heavy shopping cannot fill the whole
    exam with shopping. Cases carrying a figure are taken first, because that is
    the recorded failure class this role was chosen for.
    """
    seen: set[str] = set()
    unique = []
    for entry in found:
        digest = hashlib.sha256(entry[2].encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(entry)

    priced, plain = [], []
    for entry in unique:
        (priced if figures_only_in_snippets(web.parse_results(entry[2])) else plain).append(entry)
    picked = []
    for bucket, share in ((priced, count // 2), (plain, count - count // 2)):
        if not bucket:
            continue
        step = max(1, len(bucket) // max(1, share))
        picked += bucket[::step][:share]
    return picked[:count]


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    count = 20
    if "--count" in argv:
        at = argv.index("--count")
        count = int(argv[at + 1])

    state_dir = Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    found = presented_sets(state_dir)
    print(f"{len(sessions(state_dir))} session log(s), {len(found)} recorded result set(s)")

    cases = []
    for index, (session_name, ts, presented) in enumerate(pick(found, count), 1):
        case = case_for(f"mined-{index:02d}-{ts[:10] or 'undated'}", presented)
        if case is None:
            continue
        case["_from"] = f"{session_name} @ {ts}"
        cases.append(case)

    if not cases:
        print("nothing to mine — no recorded result set had enough rows")
        return 1

    with_figures = sum(1 for c in cases if "absent" in c["expect"])
    print(
        f"{len(cases)} case(s); {with_figures} carry a figure a snippet claims "
        "and its title does not"
    )
    target = roles.owner_cases_dir(roles.SNIPPET_READER) / "cases.yaml"
    if dry_run:
        for case in cases:
            print(f"  · {case['name']}  {case['expect']}   ← {case['_from']}")
        print(f"\n--dry-run: nothing written. Would write {target}")
        return 0

    for case in cases:
        case.pop("_from", None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Mined from recorded sessions by scripts/role-mine-cases.py.\n"
        "# Scrubbed for stored secrets on the way out; this tree is pushed.\n"
        "# Only code-derived assertions are here — see the script's docstring\n"
        "# for what it may not claim. Hand-added assertions are yours to keep.\n"
        + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=1000)
    )
    print(f"wrote {target}")
    print("now run: uv run python scripts/role-admission.py snippet-reader --model <spec>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
