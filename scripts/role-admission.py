#!/usr/bin/env -S uv run python
"""Run a charter's exam through the REAL invocation path, and record the pass.

    uv run python scripts/role-admission.py                    # every charter
    uv run python scripts/role-admission.py snippet-reader     # one
    uv run python scripts/role-admission.py --model gemini:gemini-3.5-flash
    uv run python scripts/role-admission.py --dry-run          # cases only, no calls

**Why this is a script and not a test.** #297's D6 wanted golden pairs run
through the real path before a role is admitted, and this repository's suite may
not reach the network (`CLAUDE.md`, testing). Nor can the exam run at process
start: offline, every charter would fail to load, and for a load-bearing role a
charter that does not load HOLDS the step — which would wedge browsing on a
flaky connection.

So admission is its own deliberate step, here, and what load time checks is that
a pass was RECORDED for this charter version and this model. `docs/roles.md`
says the same in the same words, so nothing promises a check the code
downgrades.

**It spends real money.** One metered call per case, plus one more for each case
whose first answer does not validate.

**Two halves, and the second is the owner's.** The charter's own sanitized cases
ship inside the package and are what the load gate binds on. His full-fidelity
mined cases live in `~/.config/aish/roles/<charter>/cases.yaml` and are additive
— absent is normal, and `scripts/role-mine-cases.py` is what fills them. A
RECORDED failure in that half is a failure of the whole admission, because the
automation only ever exercises the public half: a charter green where the
machine looks and wrong where he lives is exactly what the counters exist to
make visible.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import roles, web  # noqa: E402


def state_dir() -> Path:
    """Where the admission record lands. The same resolution both entry points
    use, so a pass recorded here is one the running agent reads."""
    return Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )


def rows_of(charter: roles.Charter, case: roles.Case) -> tuple[int, ...]:
    """The row numbers this case's input carries.

    Asked of the SAME parser the live call site uses, so a case that is not an
    exam case for the real path fails here rather than passing vacuously.
    """
    first = charter.inputs[0].name
    return tuple(row.n for row in web.parse_results(case.inputs.get(first, "")))


def run_case(charter, case, model, verbose):
    rows = rows_of(charter, case)
    if not rows:
        return False, ["the case's input parses to no rows at all"], None
    result = roles.run(
        charter,
        case.inputs,
        rows,
        model_spec=model,
        # The exam is what EARNS admission, so it cannot require it.
        check_admission=False,
    )
    if result.status != roles.Status.OK or result.value is None:
        return False, [f"{result.status}: {result.why}"], result
    return not (problems := roles.check_case(case, result.value)), problems, result


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    model = ""
    if "--model" in argv:
        at = argv.index("--model")
        model = argv[at + 1] if at + 1 < len(argv) else ""
        argv = argv[:at] + argv[at + 2 :]
    model = model or os.environ.get("AISH_ROLE_MODEL", "")
    wanted = [a for a in argv if not a.startswith("-")]

    try:
        catalogue = roles.load_charters()
        roles.check_wirings(catalogue)
    except roles.CharterError as exc:
        print(f"the charter catalogue does not load: {exc}")
        return 2

    if not model and not dry_run:
        print(
            "no model — pass --model <provider:name> or set AISH_ROLE_MODEL.\n"
            "It must be one a role can actually run on: claude-max has no "
            "stateless chat seam (docs/roles.md)."
        )
        return 2

    failed = 0
    for name, charter in sorted(catalogue.items()):
        if wanted and name not in wanted:
            continue
        shipped = list(charter.cases)
        private = list(roles.owner_cases(charter))
        print(
            f"\n{name} v{charter.version} — {len(shipped)} shipped case(s), "
            f"{len(private)} of the owner's"
            + (f", against {model}" if model else "")
        )
        if dry_run:
            for case in shipped + private:
                print(f"  · {case.source:7} {case.name}  ({len(rows_of(charter, case))} rows)")
            continue

        tally = {"charter": [0, 0], "owner": [0, 0]}
        spent = {"input": 0, "output": 0, "ms": 0}
        for case in shipped + private:
            ok, problems, result = run_case(charter, case, model, verbose=True)
            tally[case.source][1] += 1
            tally[case.source][0] += int(ok)
            if result is not None:
                spent["input"] += int(result.usage.get("input") or 0)
                spent["output"] += int(result.usage.get("output") or 0)
                spent["ms"] += result.ms
            mark = "ok  " if ok else "FAIL"
            extra = f"  [{result.attempts} attempt(s), {result.ms} ms]" if result else ""
            print(f"  {mark} {case.source:7} {case.name}{extra}")
            for problem in problems:
                print(f"       ↳ {problem}")

        total = tally["charter"][1] + tally["owner"][1]
        if total:
            print(
                f"  spent: ↑{spent['input']} ↓{spent['output']} tokens over "
                f"{total} case(s), {spent['ms']} ms total, "
                f"{spent['ms'] // total} ms each"
            )
        admission = roles.Admission(
            charter=name,
            version=charter.version,
            model=model,
            at=datetime.datetime.now().isoformat(timespec="seconds"),
            passed=tally["charter"][0],
            total=tally["charter"][1],
            owner_passed=tally["owner"][0],
            owner_total=tally["owner"][1],
            # WHAT was examined, not just that something was. This is the pair
            # `roles.admitted` binds to, and it is what makes an edit through a
            # door nobody enumerated retire the admission instead of riding it.
            charter_digest=charter.digest,
            cases_digest=roles.owner_cases_digest(charter),
        )
        path = roles.write_admission(state_dir(), admission)
        # Written whether it passed or failed. A recorded FAILURE is the useful
        # artifact — `roles.admitted` reads it and keeps the role out, and a
        # reader can see the exam ran rather than inferring it from silence.
        print(f"  {'ADMITTED' if admission.ok else 'NOT ADMITTED'} — recorded in {path}")
        failed += not admission.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
