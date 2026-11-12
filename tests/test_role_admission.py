"""scripts/role-admission.py, driven with a scripted `roles.run` (#472).

No model and no network: the script's one model seam is `roles.run`, replaced
here by a function that returns pre-built `Result`s, exactly as the agent's is
replaced by FakeChat. What this pins is what the script WRITES for each way an
exam can end — and that a run which could not complete writes nothing.
"""

from __future__ import annotations

import importlib.util
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
        code, _ = run([roles.Status.UNAVAILABLE, roles.Status.OK])
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
        code, _ = run([roles.Status.INVALID, roles.Status.OK])
        assert code == 1
        found = roles.read_admissions(state_dir)[charter.name][MODEL]
        assert (found.passed, found.total) == (1, 2)
