"""scripts/role-admission.py, driven with a scripted `roles.run` (#472).

No model and no network: the script's one model seam is `roles.run`, replaced
here by a function that returns pre-built `Result`s, exactly as the agent's is
replaced by FakeChat. What this pins is what the script WRITES for each way an
exam can end — and that a run which could not complete writes nothing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from test_roles import charter_text

from aish import roles

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "role-admission.py"
MODEL = "fake:m"

TWO_CASES = (
    "\n## Golden pairs\n\n"
    "```yaml\n"
    "name: first\n"
    "input:\n"
    '  results: "1. A"\n'
    "expect:\n"
    "  rows: 1\n"
    "```\n\n"
    "```yaml\n"
    "name: second\n"
    "input:\n"
    '  results: "1. B"\n'
    "expect:\n"
    "  rows: 1\n"
    "```\n"
)


def load_script():
    spec = importlib.util.spec_from_file_location("role_admission", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(charter: roles.Charter, status: str, why: str = "") -> roles.Result:
    value = (
        roles.Rows(({"n": 1, "about": "a", "instructs_the_reader": "no"},))
        if status == roles.Status.OK
        else None
    )
    return roles.Result(
        charter=charter.name, version=charter.version, status=status, model=MODEL,
        value=value, why=why, attempts=1, ms=10, usage={"input": 100, "output": 10},
    )


class ScriptedRun:
    """`roles.run` for the script: one status per call, in order."""

    def __init__(self, charter: roles.Charter, statuses: list[str]):
        self.charter = charter
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, charter, inputs, rows, **kwargs):
        assert kwargs["check_admission"] is False
        self.calls += 1
        status = self.statuses.pop(0)
        return result(self.charter, status, why="" if status == "ok" else "scripted")


@pytest.fixture
def exam(tmp_path, monkeypatch):
    """The script, pointed at one two-case charter and a tmp state dir."""
    charter = roles.parse_charter(charter_text(_body=TWO_CASES))
    monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(roles, "load_charters", lambda *a, **k: {charter.name: charter})
    monkeypatch.setattr(roles, "check_wirings", lambda *a, **k: None)
    script = load_script()

    def run(statuses: list[str], *argv: str) -> tuple[int, ScriptedRun]:
        scripted = ScriptedRun(charter, statuses)
        monkeypatch.setattr(roles, "run", scripted)
        return script.main(["--model", MODEL, *argv]), scripted

    return charter, tmp_path, run, script


def a_recorded_pass(charter: roles.Charter, state_dir: Path) -> bytes:
    roles.write_admission(state_dir, roles.Admission(
        charter=charter.name, version=charter.version, model=MODEL,
        at="2026-11-01T00:00:00", passed=2, total=2,
        charter_digest=charter.digest, cases_digest=roles.owner_cases_digest(charter),
    ))
    return roles.admission_path(state_dir).read_bytes()


class TestAnIncompleteRun:
    """A case ending `unavailable` is a run that could not complete — not a
    quality failure. Found live: an all-503 run overwrote a record with 0/16."""

    def test_it_writes_nothing_and_exits_with_its_own_code(self, exam, capsys):
        charter, state_dir, run, script = exam
        before = a_recorded_pass(charter, state_dir)
        code, _ = run([roles.Status.UNAVAILABLE])
        assert code == script.EXIT_INCOMPLETE == 3
        assert roles.admission_path(state_dir).read_bytes() == before
        out = capsys.readouterr().out
        assert "COULD NOT COMPLETE" in out
        assert "nothing was recorded" in out

    def test_with_no_record_it_creates_none(self, exam):
        charter, state_dir, run, script = exam
        code, _ = run([roles.Status.OK, roles.Status.UNAVAILABLE])
        assert code == script.EXIT_INCOMPLETE
        assert not roles.admission_path(state_dir).exists()

    def test_it_stops_spending_once_a_case_could_not_complete(self, exam):
        """Nothing a later call returned could be recorded."""
        _, _, run, _ = exam
        _, scripted = run([roles.Status.UNAVAILABLE, roles.Status.OK])
        assert scripted.calls == 1

    def test_an_invalid_answer_is_a_failure_and_is_recorded(self, exam):
        """`invalid` is the model answering badly — a verdict on it, unlike
        `unavailable`. Only the status decides which, never the error text."""
        charter, state_dir, run, _ = exam
        code, _ = run([roles.Status.INVALID, roles.Status.OK] * 3)
        assert code == 1
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert (found.passed, found.total) == (3, 6)


OK, FAIL = roles.Status.OK, roles.Status.INVALID


class TestRepeatedRuns:
    """One pass can be luck (#472): a model that missed a case in 2 of 3 runs
    was admitted on the third. The exam runs `--runs N` times (default 3) and
    admits only if every run passes every case."""

    def test_three_clean_runs_admit_and_the_record_carries_each(self, exam, capsys):
        charter, state_dir, run, script = exam
        code, scripted = run([OK] * 6)
        assert code == 0 and scripted.calls == 6 == 2 * script.DEFAULT_RUNS
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert (found.runs_required, found.runs_passed) == (3, 3)
        assert (found.passed, found.total) == (6, 6)
        assert [r["failing"] for r in found.runs] == [[], [], []]
        assert found.case_failures == {} and not found.single_run
        assert roles.admitted(charter, MODEL, state_dir) is None
        out = capsys.readouterr().out
        assert "ADMITTED (3 of 3 run(s) passed every case)" in out

    def test_one_failing_run_keeps_it_out_and_names_the_case(self, exam, capsys):
        charter, state_dir, run, _ = exam
        code, scripted = run([OK, OK, OK, FAIL, OK, OK])
        assert code == 1
        assert scripted.calls == 6, "every run is spent, so the fail counts cover the batch"
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert (found.runs_required, found.runs_passed) == (3, 2)
        assert found.case_failures == {"second": 1}
        assert [r["failing"] for r in found.runs] == [[], ["second"], []]
        assert "did not pass" in roles.admitted(charter, MODEL, state_dir)
        out = capsys.readouterr().out
        assert "failed in 1 of 3 run(s): second" in out
        assert "NOT ADMITTED" in out

    def test_runs_is_a_knob(self, exam):
        charter, state_dir, run, _ = exam
        code, scripted = run([OK] * 8, "--runs", "4")
        assert scripted.calls == 8
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert (found.runs_required, found.runs_passed) == (4, 4)

    def test_a_nonsense_runs_value_starts_nothing(self, exam):
        _, state_dir, run, _ = exam
        code, scripted = run([], "--runs", "0")
        assert code == 2 and scripted.calls == 0
        assert not roles.admission_path(state_dir).exists()

    def test_an_incomplete_later_run_discards_the_complete_ones(self, exam):
        charter, state_dir, run, script = exam
        before = a_recorded_pass(charter, state_dir)
        code, _ = run([OK, OK, OK, OK, OK, roles.Status.UNAVAILABLE])
        assert code == script.EXIT_INCOMPLETE
        assert roles.admission_path(state_dir).read_bytes() == before

    def test_the_cost_of_every_run_is_printed(self, exam, capsys):
        _, _, run, _ = exam
        run([OK] * 6)
        # ScriptedRun reports 100 in / 10 out and one attempt per call.
        assert "spent over 3 run(s): ↑600 ↓60 tokens, 6 metered call(s)" in (
            capsys.readouterr().out)

    def test_an_owner_case_is_recorded_by_digest_never_by_name(self, exam, monkeypatch):
        charter, state_dir, run, _ = exam
        mined = roles.Case("his-private-mail-subject", "owner", {"results": "1. C"},
                           {"rows": 1})
        monkeypatch.setattr(roles, "owner_cases", lambda c: (mined,))
        run([OK, OK, FAIL] * 3)
        text = roles.admission_path(state_dir).read_text()
        assert "his-private-mail-subject" not in text
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert found.case_failures == {f"owner:{roles.case_digest(mined)}": 3}
        assert (found.owner_passed, found.owner_total) == (0, 3)


class TestWhatARecordSupersedes:
    """A later passing batch does not erase failed ones: the writer keeps what
    each record replaced, newest first, capped."""

    def test_a_pass_after_a_failure_keeps_the_failure(self, exam):
        charter, state_dir, run, _ = exam
        run([OK, OK, OK, FAIL, OK, OK])
        run([OK] * 6)
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert roles.admitted(charter, MODEL, state_dir) is None
        (previous,) = found.supersedes
        assert previous["runs_passed"] == 2 and previous["runs_required"] == 3
        assert previous["failing_cases"] == ["second"]
        assert previous["at"]

    def test_a_single_run_predecessor_is_labelled_and_names_no_cases(self, exam):
        """It never recorded which cases failed; an empty list would claim
        nothing did."""
        charter, state_dir, run, _ = exam
        a_recorded_pass(charter, state_dir)
        run([OK] * 6)
        (previous,) = roles.read_admissions(state_dir)[charter.name][MODEL].supersedes
        assert previous["single_run"] is True
        assert "failing_cases" not in previous

    def test_the_history_is_capped(self, exam):
        charter, state_dir, run, _ = exam
        for _ in range(roles.SUPERSEDED_KEPT + 3):
            run([OK] * 6)
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert len(found.supersedes) == roles.SUPERSEDED_KEPT

    def test_a_single_run_record_is_labelled_in_the_file(self, tmp_path):
        charter = roles.parse_charter(charter_text())
        roles.write_admission(tmp_path, roles.Admission(
            charter.name, charter.version, "a:m", "t", passed=1, total=1,
            charter_digest=charter.digest,
        ))
        raw = json.loads(roles.admission_path(tmp_path).read_text())
        assert raw[charter.name]["models"]["a:m"]["single_run"] is True
