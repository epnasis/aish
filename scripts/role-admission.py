#!/usr/bin/env -S uv run python
"""Run a charter's exam through the REAL invocation path, and record the pass.

    uv run python scripts/role-admission.py                    # every charter
    uv run python scripts/role-admission.py snippet-reader     # one
    uv run python scripts/role-admission.py --model gemini:gemini-3.5-flash
    uv run python scripts/role-admission.py --runs 5 tracker   # more runs (default 3)
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

**It spends real money — `--runs` times over.** The exam runs `--runs N` times
(default and floor 3, `roles.RUNS_REQUIRED`; fewer is refused, since such a
record could never admit and would replace the current one) and admits only if
EVERY run passes EVERY case (#472: a model that missed a case in 2 of 3 runs
had been admitted on the third). So
the bill is N× one exam: one metered call per case per run, plus one more for
each answer that does not validate. Every run is spent even after one fails, so
the record's per-case fail counts cover the whole batch; the token total of all
runs is printed at the end.

This is a tripwire for gross flakiness, not proof of stability: a case a model
passes 90% of the time passes a 3-run batch about 73% of the time (0.9³).

**A run that could not complete records nothing.** A case ending
`Status.UNAVAILABLE` in any run — no key, a refused or failed call — says
nothing about the model's quality, so the batch stops there, the existing
record is left exactly as it was (earlier complete runs included), and the
exit code is 3 (`EXIT_INCOMPLETE`). It stops rather than spending the
remaining calls, because nothing they returned could be recorded. Before
this, an all-503 run overwrote a charter's record with 0/16.

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

# Distinct from 1 (examined and failed) and 2 (could not start): the exam began
# and could not finish, so there is no verdict and nothing was written.
EXIT_INCOMPLETE = 3

DEFAULT_RUNS = roles.RUNS_REQUIRED


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
    rows: tuple[int, ...] = ()
    if charter.output.kind == "rows":
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


def case_id(case: roles.Case) -> str:
    """How the record names a case. The owner's mined cases by digest, never by
    name: `roles.case_digest` exists so the state dir does not copy his half."""
    if case.source == "owner":
        return f"owner:{roles.case_digest(case)}"
    return case.name


def exam_once(charter, cases, model, spent) -> tuple[dict, roles.Case | None]:
    """One run of every case. The run's tally, and the case that could not
    complete if one did (the run stops there)."""
    tally: dict = {"passed": 0, "total": 0, "owner_passed": 0, "owner_total": 0, "failing": []}
    for case in cases:
        ok, problems, result = run_case(charter, case, model, verbose=True)
        prefix = "owner_" if case.source == "owner" else ""
        tally[prefix + "total"] += 1
        tally[prefix + "passed"] += int(ok)
        if not ok:
            tally["failing"].append(case_id(case))
        if result is not None:
            spent["input"] += int(result.usage.get("input") or 0)
            spent["output"] += int(result.usage.get("output") or 0)
            spent["ms"] += result.ms
            spent["calls"] += result.attempts
        mark = "ok  " if ok else "FAIL"
        extra = f"  [{result.attempts} attempt(s), {result.ms} ms]" if result else ""
        print(f"    {mark} {case.source:7} {case.name}{extra}")
        for problem in problems:
            print(f"         ↳ {problem}")
        # roles.py's own vocabulary decides this, never the error text: a
        # provider's wording is not a contract, the status is.
        if result is not None and result.status == roles.Status.UNAVAILABLE:
            return tally, case
    return tally, None


def runs_of(argv: list[str]) -> tuple[int | None, list[str]]:
    """`--runs N`, or None when N is not a whole number of at least one."""
    if "--runs" not in argv:
        return DEFAULT_RUNS, argv
    at = argv.index("--runs")
    raw = argv[at + 1] if at + 1 < len(argv) else ""
    rest = argv[:at] + argv[at + 2 :]
    return (int(raw), rest) if raw.isdigit() and int(raw) >= 1 else (None, rest)


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    model = ""
    if "--model" in argv:
        at = argv.index("--model")
        model = argv[at + 1] if at + 1 < len(argv) else ""
        argv = argv[:at] + argv[at + 2 :]
    model = model or os.environ.get("AISH_ROLE_MODEL", "")
    runs, argv = runs_of(argv)
    if runs is None:
        print("--runs takes a whole number of at least 1")
        return 2
    if runs < roles.RUNS_REQUIRED and not dry_run:
        # A batch below the floor could only write a record `roles.admitted`
        # refuses, replacing whatever admitted the role before it.
        print(
            f"--runs {runs} is below the {roles.RUNS_REQUIRED} runs admission needs: "
            "its record could never admit, and it would replace the current one"
        )
        return 2
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
    incomplete = 0
    for name, charter in sorted(catalogue.items()):
        if wanted and name not in wanted:
            continue
        shipped = list(charter.cases)
        private = list(roles.owner_cases(charter))
        print(
            f"\n{name} v{charter.version} — {len(shipped)} shipped case(s), "
            f"{len(private)} of the owner's"
            + (f", against {model}, {runs} run(s)" if model else "")
        )
        if dry_run:
            for case in shipped + private:
                shape = (
                    f"{len(rows_of(charter, case))} rows"
                    if charter.output.kind == "rows"
                    else charter.output.kind
                )
                print(f"  · {case.source:7} {case.name}  ({shape})")
            continue

        spent = {"input": 0, "output": 0, "ms": 0, "calls": 0}
        tallies: list[dict] = []
        unavailable: roles.Case | None = None
        for n in range(1, runs + 1):
            print(f"  run {n} of {runs}")
            tally, unavailable = exam_once(charter, shipped + private, model, spent)
            if unavailable is not None:
                break
            tallies.append(tally)
            print(
                f"    {tally['passed']}/{tally['total']} shipped, "
                f"{tally['owner_passed']}/{tally['owner_total']} of the owner's"
            )

        started = len(tallies) + (unavailable is not None)
        print(
            f"  spent over {started} run(s): ↑{spent['input']} ↓{spent['output']} "
            f"tokens, {spent['calls']} metered call(s), {spent['ms']} ms"
        )
        if unavailable is not None:
            print(
                f"  COULD NOT COMPLETE — {unavailable.name} ended "
                f"{roles.Status.UNAVAILABLE}; nothing was recorded, and the existing "
                f"record for {name} against {model} is untouched"
            )
            incomplete += 1
            continue

        case_failures: dict[str, int] = {}
        for tally in tallies:
            for failing in tally["failing"]:
                case_failures[failing] = case_failures.get(failing, 0) + 1
        for failing, count in sorted(case_failures.items()):
            print(f"  failed in {count} of {runs} run(s): {failing}")
        admission = roles.Admission(
            charter=name,
            version=charter.version,
            model=model,
            at=datetime.datetime.now().isoformat(timespec="seconds"),
            passed=sum(t["passed"] for t in tallies),
            total=sum(t["total"] for t in tallies),
            owner_passed=sum(t["owner_passed"] for t in tallies),
            owner_total=sum(t["owner_total"] for t in tallies),
            # WHAT was examined, not just that something was. This is the pair
            # `roles.admitted` binds to, and it is what makes an edit through a
            # door nobody enumerated retire the admission instead of riding it.
            charter_digest=charter.digest,
            cases_digest=roles.owner_cases_digest(charter),
            runs_required=runs,
            runs_passed=sum(not t["failing"] for t in tallies),
            runs=tuple(tallies),
            case_failures=case_failures,
        )
        path = roles.write_admission(state_dir(), admission)
        # Written whether it passed or failed. A recorded FAILURE is the useful
        # artifact — `roles.admitted` reads it and keeps the role out, and a
        # reader can see the exam ran rather than inferring it from silence.
        # The verdict printed is the reader's own, so the script cannot say
        # ADMITTED about a record the role would be refused on.
        why = roles.admitted(charter, model, state_dir())
        print(
            f"  {'ADMITTED' if why is None else 'NOT ADMITTED — ' + why}"
            f" ({admission.runs_passed} of {runs} run(s) passed every case)"
            f" — recorded in {path}"
        )
        failed += why is not None
    if incomplete:
        return EXIT_INCOMPLETE
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
