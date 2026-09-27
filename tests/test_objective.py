"""The Objective record and the distiller (#424) — `docs/objective.md`.

No model, no network: the distiller's answers are scripted (`FakeRoleChat`),
and every log is built in a tmp dir through the real `SessionLog` writer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aish import objective, roles
from aish import session as session_module
from aish.session import SessionLog

CHAT = "session-20260101-000000-000000"
READ_ONLY = frozenset({"read_url", "web_search", "read_file"})


# --------------------------------------------------------------- fixtures


def says(text: str):
    return SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))


class FakeRoleChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        response = says(reply if isinstance(reply, str) else json.dumps(reply))
        response.usage = {"input": 900, "output": 120}
        return response


@pytest.fixture
def charter() -> roles.Charter:
    return roles.load_charters()[objective.DISTILLER]


@pytest.fixture
def shape(charter) -> roles.Shape:
    return charter.output


def item(ref, kind, turn, text):
    return {"ref": ref, "kind": kind, "turn": turn, "text": text}


BASIC_NEW = [
    item("m:a1", "owner", 1, "Potrzebuję codziennie rano kurs EUR/PLN"),
    item("m:a2", "answer", 1, "Mogę napisać skrypt. Chcesz?"),
    item("m:a3", "owner", 2, "tak, napisz skrypt i trzymaj klucze w aish secrets"),
    item("t2.c1", "action", 2, "write_file: /scratch/eur.py"),
    item("m:a4", "answer", 2, "Skrypt eur.py zapisany; dzisiejszy kurs to 4,27."),
]


def material_text(new=BASIC_NEW, previous=None, earlier=(), boundary=2, chat=CHAT) -> str:
    return json.dumps(
        {
            "chat": chat,
            "boundary_turn": boundary,
            "previous": previous,
            "earlier": list(earlier),
            "new": list(new),
        }
    )


def goal(gid="g1", text="Kurs EUR/PLN każdego ranka", state="active", cites=("m:a1",), **kw):
    return {"id": gid, "text": text, "state": state, "cites": list(cites), **kw}


def task(tid="t1", text="napisać skrypt", state="done", cites=("m:a3", "t2.c1"), **kw):
    return {"id": tid, "text": text, "state": state, "cites": list(cites), **kw}


def good_answer(**over):
    body = {
        "change": "new",
        "goals": [
            goal(
                cites=("m:a1", "m:a3"),
                constraints=[{"text": "trzymaj klucze w aish secrets", "cites": ["m:a3"]}],
                tasks=[task()],
            )
        ],
    }
    body.update(over)
    return body


def check(shape, payload, text=None):
    return roles.validate(shape, payload, (), {"material": text or material_text()})


def rejects(shape, payload, fragment, text=None):
    with pytest.raises(ValueError) as caught:
        check(shape, payload, text)
    assert fragment in str(caught.value), str(caught.value)


# --------------------------------------------------------------- the record kind


class TestTheRecordKind:
    def test_it_is_renderless_both_ways(self, tmp_path):
        """An `objective` record reaches no renderer live (it is written through
        the log sink only) and is skipped on replay — the pair contract §1.2
        requires, since either alone opens an empty live trace card."""
        assert "objective" in session_module.RENDERLESS_STEPS
        log = SessionLog(tmp_path / f"{CHAT}.jsonl")
        log.message({"role": "user", "content": "hello there, owner here"})
        log.message({"role": "assistant", "content": "hi"})
        log.step({"kind": "objective", "turn": 1, "revision": 1, "origin": "extractive"})
        events = SessionLog.reconstruct_events(log.path)
        assert [e["type"] for e in events] == ["user", "done"]

    def test_it_is_not_activity(self, tmp_path):
        """A revision written after the answer must not mark the chat as having
        done something — it renders nowhere (L3)."""
        path = tmp_path / f"{CHAT}.jsonl"
        path.write_text(
            json.dumps({"ts": "2026-01-01T10:00:00", "kind": "message", "role": "user",
                        "content": "hello there", "turn": "a"}) + "\n"
            + json.dumps({"ts": "2026-01-01T12:00:00", "kind": "trace",
                          "step": {"kind": "objective", "turn": 1}}) + "\n"
        )
        parsed = SessionLog._parse(path)
        assert parsed.activity_ts == session_module.record_epoch(
            {"ts": "2026-01-01T10:00:00"}
        )

    def test_the_stopped_answer_spelling_matches_the_session_layer(self):
        assert objective.STOPPED_ANSWER == session_module.STOPPED_ANSWER


# --------------------------------------------------------------- the material


def write_log(tmp_path, records) -> Path:
    path = tmp_path / f"{CHAT}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return path


def trace(**step):
    return {"kind": "trace", "step": step}


def user(content, ident, call=0, **kw):
    return {"kind": "message", "role": "user", "content": content, "turn": ident,
            "model_call": call, **kw}


def assistant(content, ident, interim=False, **kw):
    record = {"kind": "message", "role": "assistant", "content": content, "turn": ident}
    if interim:
        record["interim"] = True
    return {**record, **kw}


def a_chat():
    """Two tasks, the way the web writes them: bracketed, turn-stamped."""
    return [
        {"kind": "task_start", "prompt": "find the forecast for tomorrow"},
        user("find the forecast for tomorrow", "u1"),
        trace(kind="reasoning", turn=1),
        assistant("Looking…", "i1", interim=True),
        trace(kind="tool", name="read_url", ok=True, call=1, turn=1, summary="https://x"),
        trace(kind="tool", name="write_file", ok=True, call=2, turn=1, summary="/s/f.py"),
        user("[aish: a rule applies to your answer]", "n1", call=2),
        assistant("Tomorrow: rain from 14:00.", "a1"),
        {"kind": "task_end", "status": "ok"},
        {"kind": "task_start", "prompt": "save it"},
        user("save it as a skill please", "u2"),
        trace(kind="reasoning", turn=2),
        trace(kind="tool", name="run_command", ok=False, call=1, turn=2, decision="denied",
              comment="never print my keys", command="echo $KEY"),
        trace(kind="tool", name="run_command", ok=False, call=2, turn=2, decision="denied",
              command="rm -rf x"),
        assistant(objective.STOPPED_ANSWER, "a2"),
        {"kind": "task_end", "status": "failed", "error": "model unavailable: 429"},
    ]


class TestMaterial:
    def test_what_the_distiller_is_given_and_nothing_else(self):
        items = objective.material(a_chat(), READ_ONLY)
        got = [(i.ref, i.kind, i.turn) for i in items]
        assert got == [
            ("m:u1", "owner", 1),
            ("t1.c2", "action", 1),
            ("m:a1", "answer", 1),
            ("m:u2", "owner", 2),
            ("t2.c1", "comment", 2),
            ("t2.c2", "denial", 2),
            ("t2.end", "failed", 2),
            ("m:a2", "cancel", 2),
        ]

    def test_never_tool_outputs_never_reminders_never_interim_text(self):
        blob = json.dumps([i.as_json() for i in objective.material(a_chat(), READ_ONLY)])
        assert "a rule applies" not in blob  # aish's own note (L4)
        assert "Looking" not in blob  # interim text
        assert "https://x" not in blob  # a read-only tool

    def test_a_card_comment_is_the_owners_own_words(self):
        items = {i.ref: i for i in objective.material(a_chat(), READ_ONLY)}
        assert items["t2.c1"].text == "never print my keys"
        assert items["t2.c1"].kind in objective.OWNER_WORDS

    def test_a_superseded_attempt_is_not_material(self):
        records = a_chat()
        records.insert(9, {**user("discarded question", "x1"), "superseded": True})
        refs = [i.ref for i in objective.material(records, READ_ONLY)]
        assert "m:x1" not in refs

    def test_a_distills_own_records_do_not_decide_a_tasks_turn(self):
        """A slow distill of turn 1 can land inside task 2, before anything of
        task 2 is stamped. Its records say turn 1 and must not make task 2
        turn 1."""
        records = a_chat()
        at = records.index({"kind": "task_start", "prompt": "save it"}) + 1
        records.insert(at, trace(kind="objective", turn=1, revision=1))
        records.insert(at, trace(kind="role", turn=1, charter="distiller"))
        turns = {i.ref: i.turn for i in objective.material(records, READ_ONLY)}
        assert turns["m:u2"] == 2

    def test_a_log_with_no_task_brackets_is_grouped_by_typed_messages(self):
        """The CLI writes no task_start/task_end."""
        records = [
            user("first question here", "u1"),
            trace(kind="reasoning", turn=1),
            assistant("first answer", "a1"),
            user("second question here", "u2"),
            trace(kind="reasoning", turn=2),
            assistant("second answer", "a2"),
        ]
        got = [(i.ref, i.turn) for i in objective.material(records, READ_ONLY)]
        assert got == [("m:u1", 1), ("m:a1", 1), ("m:u2", 2), ("m:a2", 2)]

    def test_a_long_answer_is_cut_and_says_so(self):
        records = [user("q" * 20, "u1"), assistant("x" * 5000, "a1")]
        answer = objective.material(records, READ_ONLY)[-1]
        assert len(answer.text) < 1300 and "5000 chars in all" in answer.text


class TestTheFloor:
    def items(self, *texts, kind="owner"):
        return [objective.Item(f"m:{n}", kind, n, t) for n, t in enumerate(texts, 1)]

    def test_it_filters_by_length_prefix_and_exact_duplicates_only(self):
        items = self.items(
            "tak",  # too short
            "[I ran `aish secret set` myself]",  # synthetic
            "use aish secrets for API keys",
            "use aish secrets for API keys",  # exact duplicate
            "Use aish secrets for API keys",  # not exact: kept
        )
        kept = [i.ref for i in objective.floor(items, 0, 99)]
        assert kept == ["m:3", "m:5"]

    def test_it_keeps_the_words_verbatim(self):
        text = "  Potrzebuję   info o deszczu na dzień przed  "
        (kept,) = objective.floor(self.items(text), 0, 9)
        assert kept.text == text

    def test_only_the_owners_words_and_only_in_range(self):
        items = [
            *self.items("an answer that is long enough", kind="answer"),
            objective.Item("m:9", "owner", 5, "a question from turn five"),
            objective.Item("t6.c1", "comment", 6, "never print my keys"),
        ]
        assert [i.ref for i in objective.floor(items, 5, 6)] == ["t6.c1"]

    def test_there_is_no_word_list(self):
        """A mechanical filter only: a meaningful short reply is dropped and
        long filler is kept. Deciding otherwise is a judgement, which is the
        distiller's job, never the floor's."""
        items = self.items("yes, do it", "what's the answer?")
        assert [i.text for i in objective.floor(items, 0, 9)] == ["what's the answer?"]


# --------------------------------------------------------------- validation


class TestValidation:
    def test_a_valid_answer_is_typed_and_its_cites_resolve(self, shape):
        value = check(shape, good_answer())
        g = value.goals[0]
        assert g["cites"] == [{"session": CHAT, "ref": "m:a1"}, {"session": CHAT, "ref": "m:a3"}]
        assert g["state_by"] == g["text_by"] == "distiller"
        assert g["tasks"][0]["state"] == "done"
        assert value.covers_to_turn == 2 and value.uncited == []

    def test_a_cite_to_nothing_in_the_material_is_refused(self, shape):
        rejects(shape, good_answer(goals=[goal(cites=("m:zz",))]), "names nothing")

    def test_done_without_evidence_is_refused(self, shape):
        answer = good_answer(goals=[goal(cites=("m:a1", "m:a3"), tasks=[task(cites=("m:a3",))])])
        rejects(shape, answer, "done needs evidence")

    def test_done_on_an_answer_or_an_action_is_accepted(self, shape):
        for cites in (("m:a4",), ("t2.c1",)):
            answer = good_answer(goals=[goal(cites=("m:a1", "m:a3"), tasks=[task(cites=cites)])])
            assert check(shape, answer).goals[0]["tasks"][0]["state"] == "done"

    def test_a_goal_nobody_asked_for_is_refused(self, shape):
        rejects(shape, good_answer(goals=[goal(cites=("m:a2",))]), "owner asked")

    def test_unknown_is_always_legal(self, shape):
        answer = good_answer(
            goals=[goal(state="unknown", cites=(), tasks=[task(state="unknown", cites=())])]
        )
        check(shape, answer)

    def test_there_is_no_dropped_silently_state(self, shape):
        answer = good_answer(goals=[goal(tasks=[task(state="dropped_silently")])])
        rejects(shape, answer, "state must be one of")

    def test_a_goal_is_dropped_only_on_the_owners_word_after_he_asked(self, shape):
        """Citing the message that CREATED the goal does not license dropping
        it: that is inference, not his word."""
        rejects(shape, good_answer(goals=[goal(state="dropped", cites=("m:a1",))]),
                "dropped needs the owner's own word")
        text = material_text(new=[
            *BASIC_NEW,
            item("m:a5", "owner", 2, "zapomnij o kursie, nie jest mi potrzebny"),
        ])
        check(shape, good_answer(goals=[goal(state="dropped", cites=("m:a1", "m:a5"))]), text)

    def test_a_drop_cannot_rest_on_what_the_base_already_had(self, shape):
        prev = previous_revision()
        earlier = [*EARLIER, item("m:a0", "owner", 2, "zapomnij o kursie")]
        text = material_text(new=[item("m:b1", "answer", 3, "OK")], previous=prev,
                             earlier=earlier, boundary=3)
        answer = {"change": "refined", "goals": [goal(state="dropped", cites=("m:a1", "m:a0"))]}
        rejects(shape, answer, "dropped needs the owner's own word", text)

    def test_stopped_needs_his_act(self, shape):
        answer = good_answer(goals=[goal(tasks=[task(state="stopped", cites=("m:a4",))])])
        rejects(shape, answer, "stopped needs the owner's own act")

    def test_superseded_names_its_replacement(self, shape):
        bad = good_answer(goals=[goal(tasks=[task(state="superseded", cites=())])])
        rejects(shape, bad, "replaced_by")
        dangling = good_answer(
            goals=[goal(tasks=[task(state="superseded", cites=(), replaced_by="t9")])]
        )
        rejects(shape, dangling, "replaced_by must name another task")
        good = good_answer(goals=[goal(tasks=[
            task(state="superseded", cites=(), replaced_by="t2"),
            task(tid="t2", state="pending", cites=("m:a3",)),
        ])])
        check(shape, good)

    def test_a_constraint_is_a_verbatim_quote(self, shape):
        paraphrase = good_answer(goals=[goal(cites=("m:a1", "m:a3"), constraints=[
            {"text": "keep keys in aish secrets", "cites": ["m:a3"]}])])
        rejects(shape, paraphrase, "not a verbatim quote")
        spaced = good_answer(goals=[goal(cites=("m:a1", "m:a3"), constraints=[
            {"text": "trzymaj   klucze w\naish secrets", "cites": ["m:a3"]}])])
        check(shape, spaced)

    def test_a_constraint_may_not_be_quoted_from_an_answer(self, shape):
        answer = good_answer(goals=[goal(cites=("m:a1", "m:a3"), constraints=[
            {"text": "Mogę napisać skrypt", "cites": ["m:a2"]}])])
        rejects(shape, answer, "not a verbatim quote")

    def test_at_most_one_active_goal(self, shape):
        answer = good_answer(goals=[goal(), goal(gid="g2", cites=("m:a3",))])
        rejects(shape, answer, "at most one goal may be active")

    def test_the_change_word_is_a_closed_vocabulary(self, shape):
        rejects(shape, good_answer(change="rewritten"), "change must be one of")


def previous_revision(**over):
    body = {
        "revision": 1,
        "covers_to_turn": 2,
        "goals": [
            {"id": "g1", "text": "Kurs EUR/PLN każdego ranka", "state": "active",
             "owner_set": [], "cites": ["m:a1"], "constraints": [],
             "tasks": [{"id": "t1", "text": "napisać skrypt", "state": "done",
                        "owner_set": [], "cites": ["t2.c1"]}]},
            {"id": "g0", "text": "Porównać API walutowe", "state": "parked",
             "owner_set": ["state"], "cites": ["m:a1"], "constraints": [], "tasks": []},
        ],
    }
    body.update(over)
    return body


EARLIER = [
    item("m:a1", "owner", 1, "Potrzebuję codziennie rano kurs EUR/PLN"),
    item("t2.c1", "action", 2, "write_file: /scratch/eur.py"),
]


def later(*new, previous=None):
    return material_text(new=list(new), previous=previous or previous_revision(),
                         earlier=EARLIER, boundary=3)


class TestTransitions:
    def test_a_goal_left_out_is_carried_forward_never_overwritten(self, shape):
        text = later(item("m:b1", "owner", 3, "teraz dodaj też kurs USD/PLN proszę"))
        answer = {"change": "expanded", "goals": [
            goal(cites=("m:a1", "m:b1"), tasks=[task(tid="t2", state="pending", cites=("m:b1",))]),
        ]}
        value = check(shape, answer, text)
        ids = {g["id"]: g for g in value.goals}
        assert set(ids) == {"g1", "g0"}
        assert ids["g0"]["state"] == "parked" and ids["g0"]["state_by"] == "owner"
        # …and the task the answer left out is carried under its goal.
        assert [t["id"] for t in ids["g1"]["tasks"]] == ["t2", "t1"]
        assert ids["g1"]["tasks"][1]["cites"] == [{"session": CHAT, "ref": "t2.c1"}]

    def test_an_owner_set_field_may_not_be_changed(self, shape):
        text = later(item("m:b1", "owner", 3, "wróćmy do porównania API"))
        answer = {"change": "pivoted", "goals": [
            goal(state="parked"),
            goal(gid="g0", text="Porównać API walutowe", state="active", cites=("m:b1",)),
        ]}
        rejects(shape, answer, "set by the owner", text)

    def test_done_reopens_only_on_his_word(self, shape):
        text = later(
            item("m:b1", "owner", 3, "skrypt nie działa, kurs jest pusty"),
            item("m:b2", "answer", 3, "Sprawdzę to."),
        )
        on_an_answer = {"change": "refined", "goals": [
            goal(tasks=[task(state="in_progress", cites=("m:b2",))])]}
        rejects(shape, on_an_answer, "reopening a done item needs an owner cite", text)
        on_his_word = {"change": "refined", "goals": [
            goal(tasks=[task(state="in_progress", cites=("m:b1",))])]}
        value = check(shape, on_his_word, text)
        assert value.goals[0]["tasks"][0]["state"] == "in_progress"

    def test_superseded_is_terminal_except_for_unknown(self, shape):
        prev = previous_revision(goals=[
            {"id": "g1", "text": "g", "state": "active", "owner_set": [], "cites": ["m:a1"],
             "constraints": [], "tasks": [
                 {"id": "t1", "text": "a", "state": "superseded", "owner_set": [],
                  "cites": [], "replaced_by": "t2"},
                 {"id": "t2", "text": "b", "state": "pending", "owner_set": [],
                  "cites": ["m:a1"]}]}])
        text = later(item("m:b1", "owner", 3, "zrób jednak wersję a proszę"), previous=prev)
        back = {"change": "refined", "goals": [goal(tasks=[
            task(state="pending", cites=("m:b1",)), task(tid="t2", state="pending",
                                                        cites=("m:a1",))])]}
        rejects(shape, back, "may only become unknown", text)
        unknown = {"change": "refined", "goals": [goal(cites=("m:a1", "m:b1"), tasks=[
            task(state="unknown", cites=()), task(tid="t2", state="pending", cites=("m:a1",))])]}
        check(shape, unknown, text)

    def test_an_unchanged_constraint_is_carried_without_requoting(self, shape):
        prev = previous_revision()
        prev["goals"][0]["constraints"] = [{"text": "codziennie rano", "cites": ["m:a1"]}]
        text = later(item("m:b1", "owner", 3, "dodaj też wykres z tygodnia"), previous=prev)
        answer = {"change": "expanded", "goals": [goal(
            cites=("m:a1", "m:b1"),
            constraints=[{"text": "codziennie rano", "cites": ["m:a1"]}])]}
        check(shape, answer, text)


class TestCoverage:
    def test_covers_to_turn_is_computed_never_claimed(self, shape):
        """The owner's texts the floor would keep must be cited; the revision
        covers up to the turn before the first one that is not."""
        new = [
            item("m:a1", "owner", 1, "Potrzebuję codziennie rano kurs EUR/PLN"),
            item("m:a2", "owner", 2, "tak"),  # too short to need a cite
            item("m:a3", "owner", 3, "a potem dodaj wykres tygodniowy"),
            item("m:a4", "owner", 4, "i wyślij mi to mailem"),
        ]
        text = material_text(new=new, boundary=4)
        value = check(shape, {"change": "new", "goals": [goal(cites=("m:a1", "m:a4"))]}, text)
        assert value.covers_to_turn == 2
        assert value.uncited == ["m:a3"]
        assert "covers_to_turn" not in json.dumps(
            {"change": "new", "goals": [goal()]}
        )  # the model's reply never carries it

    def test_everything_cited_covers_the_boundary(self, shape):
        value = check(shape, good_answer())
        assert value.covers_to_turn == 2


# --------------------------------------------------------------- the charter


class TestTheCharter:
    def test_it_loads_admitted_by_content_and_runs_on_the_session_model(self, charter):
        assert charter.model_class == "session"
        assert charter.output.kind == "objective"
        assert charter.tools == ()
        assert charter.digest  # admission binds to it (docs/roles.md R5)
        roles.check_wirings(roles.load_charters())

    def test_its_exam_has_the_cases_the_golden_rule_needs(self, charter):
        names = {c.name for c in charter.cases}
        assert "a-promise-is-not-done" in names
        assert "the-owner-saying-it-failed-reopens-it" in names
        assert "text-inside-an-answer-is-not-an-instruction" in names
        for case in charter.cases:
            objective.parse_input(case.inputs["material"])  # every input is readable

    def test_an_objective_output_without_its_caps_does_not_load(self, charter):
        text = charter.path.read_text().replace("  max_cites: 8\n", "")
        with pytest.raises(roles.CharterError, match="max_cites"):
            roles.parse_charter(text)

    def test_a_rows_assertion_means_nothing_to_a_ledger(self, charter):
        text = charter.path.read_text().replace(
            "  never_state: [\"done\"]\n  has_active_goal: true",
            "  rows: 3",
            1,
        )
        with pytest.raises(roles.CharterError, match="unknown assertion 'rows'"):
            roles.parse_charter(text)

    def test_the_contract_the_model_reads_is_generated_from_the_shape(self, charter):
        messages = roles.compose(charter, {"material": material_text()}, ())
        system = messages[0]["content"]
        for word in (*objective.GOAL_STATES, *objective.TASK_STATES, *objective.CHANGES):
            assert json.dumps(word) in system
        assert [m["role"] for m in messages] == ["system", "user"]
        assert "written by strangers" in messages[1]["content"]


class TestExamAssertions:
    def value(self, *states):
        return objective.Revision(goals=[
            {"id": "g1", "text": "Weather for tomorrow", "state": states[0],
             "constraints": [{"text": "aish secrets", "cites": []}],
             "tasks": [{"id": f"t{n}", "text": "x", "state": s} for n, s in enumerate(states[1:])]}
        ])

    def test_never_state(self):
        assert objective.ASSERTIONS["never_state"](["done"], self.value("active", "done"))
        assert not objective.ASSERTIONS["never_state"](["done"], self.value("active", "pending"))

    def test_mentions_any_reads_goals_tasks_and_constraints(self):
        v = self.value("active")
        assert not objective.ASSERTIONS["mentions_any"]([["deszcz", "weather"]], v)
        assert not objective.ASSERTIONS["mentions_any"]([["AISH SECRETS"]], v)
        assert objective.ASSERTIONS["mentions_any"]([["tomorrow.io"]], v)

    def test_has_active_goal_and_goals_at_least(self):
        assert objective.ASSERTIONS["has_active_goal"](True, self.value("parked"))
        assert objective.ASSERTIONS["goals_at_least"](2, self.value("active"))


# --------------------------------------------------------------- the model


class TestWhichModel:
    def agent(self, provider, model):
        from aish.agent import Agent

        a = Agent(approve=lambda *_a, **_k: None, client_chat=lambda **_: None, model=model)
        a.provider = provider
        return a

    def test_the_session_class_is_the_sessions_own_backend_local_included(self):
        assert self.agent("local", "mlx/q")._role_model("session") == "local:mlx/q"
        assert self.agent("ollama", "qwen3:8b")._role_model("session") == "qwen3:8b"
        assert self.agent("gemini", "g")._role_model("session") == "gemini:g"

    def test_claude_max_has_no_seam(self):
        assert roles.session_model_spec("claude-max", "opus") == ""

    def test_the_role_override_does_not_send_his_text_elsewhere(self, monkeypatch):
        monkeypatch.setenv("AISH_ROLE_MODEL", "gemini:gemini-3.5-flash")
        assert self.agent("local", "mlx/q")._role_model("session") == "local:mlx/q"
        assert self.agent("local", "mlx/q")._role_model() == "gemini:gemini-3.5-flash"


# --------------------------------------------------------------- the distill


def web_log(tmp_path) -> Path:
    """A chat written through the real SessionLog, the way the server does."""
    log = SessionLog(tmp_path / f"{CHAT}.jsonl")
    log.task_start("Potrzebuję codziennie rano kurs EUR/PLN")
    log.message({"role": "user", "content": "Potrzebuję codziennie rano kurs EUR/PLN",
                 "turn": "a1", "model_call": 0})
    log.step({"kind": "reasoning", "turn": 1})
    log.step({"kind": "tool", "name": "write_file", "ok": True, "call": 1, "turn": 1,
              "summary": "/scratch/eur.py"})
    log.message({"role": "assistant", "content": "Skrypt eur.py zapisany; kurs 4,27.",
                 "turn": "a2"})
    log.task_end()
    return log.path


def boundary(path: Path, turn=1, spec="fake:m", state_dir=None) -> objective.Boundary:
    return objective.Boundary(path, path.stat().st_size, turn, spec,
                              str(state_dir) if state_dir else None)


FIRST = {"change": "new", "goals": [goal(
    text="Kurs EUR/PLN rano", cites=("m:a1",),
    tasks=[task(text="skrypt", cites=("t1.c1",))])]}


def steps(path: Path, kind: str) -> list[dict]:
    return [r["step"] for r in objective.read_records(path) if r.get("kind") == "trace"
            and r["step"].get("kind") == kind]


class TestDistill:
    def test_a_valid_answer_writes_a_role_record_then_the_revision(self, tmp_path):
        path = web_log(tmp_path)
        chat = FakeRoleChat([FIRST])
        out = objective.distill(boundary(path), chat_fn=chat, model_name="m",
                                check_admission=False)
        role, revision = out
        assert role["kind"] == "role" and role["status"] == "ok" and role["turn"] == 1
        assert role["charter"] == "distiller" and role["usage"] == {"input": 900, "output": 120}
        assert role["flags"] == {"goal_state": {"active": 1}, "task_state": {"done": 1}}
        assert revision["kind"] == "objective" and revision["origin"] == "distiller"
        assert revision["revision"] == 1 and revision["base"] is None
        assert revision["covers_to_turn"] == 1 and revision["confirmed"] is False
        assert revision["chat"] == CHAT
        assert chat.calls[0]["tools"] == []

    def test_the_input_is_the_material_and_nothing_from_the_task(self, tmp_path):
        path = web_log(tmp_path)
        chat = FakeRoleChat([FIRST])
        objective.distill(boundary(path), chat_fn=chat, model_name="m", check_admission=False)
        sent = json.loads(chat.calls[0]["messages"][1]["content"].split(">>>\n", 1)[1]
                          .rsplit("\n<<<END", 1)[0])
        assert sent["previous"] is None and sent["boundary_turn"] == 1
        assert [n["ref"] for n in sent["new"]] == ["m:a1", "t1.c1", "m:a2"]

    def test_an_invalid_answer_leaves_the_previous_revision_and_writes_the_floor(self, tmp_path):
        path = web_log(tmp_path)
        bad = {"change": "new", "goals": [goal(cites=("m:nothing",))]}
        out = objective.distill(boundary(path), chat_fn=FakeRoleChat([bad, bad]),
                                model_name="m", check_admission=False)
        role, floor = out
        assert role["status"] == "invalid" and "names nothing" in role["why"]
        assert role["attempts"] == 2
        assert floor["origin"] == "extractive" and floor["goals"] == []
        assert floor["extract"] == [
            {"ref": "m:a1", "turn": 1, "text": "Potrzebuję codziennie rano kurs EUR/PLN"}
        ]
        assert floor["covers_to_turn"] == 1

    def test_the_corrective_retry_carries_the_validators_words(self, tmp_path):
        path = web_log(tmp_path)
        bad = {"change": "new", "goals": [goal(cites=("m:a1",), tasks=[
            task(cites=("m:a1",))])]}
        chat = FakeRoleChat([bad, FIRST])
        out = objective.distill(boundary(path), chat_fn=chat, model_name="m",
                                check_admission=False)
        assert out[1]["origin"] == "distiller" and out[0]["attempts"] == 2
        assert "done needs evidence" in chat.calls[1]["messages"][-1]["content"]

    def test_unadmitted_is_recorded_and_no_model_is_called(self, tmp_path):
        path = web_log(tmp_path)
        chat = FakeRoleChat([])
        out = objective.distill(boundary(path, state_dir=tmp_path), chat_fn=chat)
        assert out[0]["status"] == "unadmitted" and out[0]["why"] == "no admission recorded"
        assert out[1]["origin"] == "extractive"
        assert chat.calls == []

    def test_a_backend_with_no_seam_is_recorded_and_the_floor_stands_in(self, tmp_path):
        """claude-max: `session_model_spec` answers "" and the role says so."""
        path = web_log(tmp_path)
        out = objective.distill(boundary(path, spec=""))
        assert out[0]["status"] == "unavailable"
        assert "no stateless chat seam" in out[0]["why"]
        assert out[1]["origin"] == "extractive"

    def test_the_next_distill_starts_from_the_last_one(self, tmp_path):
        path = web_log(tmp_path)
        for record in objective.distill(boundary(path), chat_fn=FakeRoleChat([FIRST]),
                                        model_name="m", check_admission=False):
            SessionLog(path).step(record)
        log = SessionLog(path)
        log.task_start("dodaj USD")
        log.message({"role": "user", "content": "dodaj też kurs USD/PLN", "turn": "b1",
                     "model_call": 0})
        log.step({"kind": "reasoning", "turn": 2})
        log.message({"role": "assistant", "content": "Dodam USD.", "turn": "b2"})
        log.task_end()
        second = {"change": "expanded", "goals": [goal(
            text="Kurs EUR/PLN rano", cites=("m:a1", "m:b1"),
            tasks=[task(tid="t2", text="USD", state="pending", cites=("m:b1",))])]}
        chat = FakeRoleChat([second])
        out = objective.distill(boundary(path, turn=2), chat_fn=chat, model_name="m",
                                check_admission=False)
        revision = out[1]
        assert revision["revision"] == 2 and revision["base"] == 1
        assert [t["id"] for t in revision["goals"][0]["tasks"]] == ["t2", "t1"]
        sent = chat.calls[0]["messages"][1]["content"]
        assert '"ref": "m:b1"' in sent and "Skrypt eur.py" not in sent  # delta only
        assert '"ref": "t1.c1"' in sent  # …but a carried cite still resolves

    def test_a_floor_is_never_the_base(self, tmp_path):
        path = web_log(tmp_path)
        for record in objective.distill(boundary(path, spec="")):
            SessionLog(path).step(record)
        chat = FakeRoleChat([FIRST])
        out = objective.distill(boundary(path), chat_fn=chat, model_name="m",
                                check_admission=False)
        assert out[1]["base"] is None and out[1]["revision"] == 2
        assert '"previous": null' in chat.calls[0]["messages"][1]["content"]

    def test_a_superseded_revision_is_skipped_but_its_number_is_not_reissued(self, tmp_path):
        path = web_log(tmp_path)
        with path.open("a") as fh:
            fh.write(json.dumps({"kind": "trace", "superseded": True, "step": {
                "kind": "objective", "revision": 4, "origin": "distiller",
                "covers_to_turn": 1, "goals": []}}) + "\n")
        records = objective.read_records(path)
        assert objective.current(records) is None
        assert objective.next_revision(records) == 5

    def test_it_reads_the_chat_as_it_stood_at_the_boundary(self, tmp_path):
        path = web_log(tmp_path)
        edge = boundary(path)
        log = SessionLog(path)
        log.task_start("later")
        log.message({"role": "user", "content": "a later question entirely", "turn": "z1"})
        chat = FakeRoleChat([FIRST])
        objective.distill(edge, chat_fn=chat, model_name="m", check_admission=False)
        assert "later question" not in chat.calls[0]["messages"][1]["content"]

    def test_nothing_uncovered_writes_no_floor(self, tmp_path):
        path = tmp_path / f"{CHAT}.jsonl"
        log = SessionLog(path)
        log.task_start("ok")
        log.message({"role": "user", "content": "ok", "turn": "a1", "model_call": 0})
        log.message({"role": "assistant", "content": "done", "turn": "a2"})
        log.task_end()
        out = objective.distill(boundary(path, spec=""))
        assert [r["kind"] for r in out] == ["role"]


class TestItNeverReachesTheTask:
    def test_a_raising_distill_is_recorded_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AISH_OBJECTIVE", raising=False)
        path = web_log(tmp_path)

        def explode(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(objective, "distill", explode)
        written: list[dict] = []
        objective.distill_at_boundary(boundary(path), written.append)
        assert written[0]["kind"] == "role" and written[0]["status"] == "unavailable"
        assert "RuntimeError: boom" in written[0]["why"]

    def test_a_refused_write_ends_it_quietly(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AISH_OBJECTIVE", raising=False)
        path = web_log(tmp_path)

        def refuse(_record):
            raise session_module.SessionLogMoved(path)

        assert objective.distill_at_boundary(boundary(path, spec=""), refuse) == []

    def test_the_switch_turns_it_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_OBJECTIVE", "0")
        written: list[dict] = []
        objective.distill_at_boundary(boundary(web_log(tmp_path)), written.append)
        assert written == []


# --------------------------------------------------------------- emission


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestEmission:
    def test_the_web_writes_a_revision_after_the_answer_and_renders_nothing(
        self, tmp_path, monkeypatch
    ):
        from tests.test_server import connected, make_client, model_says, recv_until

        monkeypatch.delenv("AISH_OBJECTIVE", raising=False)
        env = {
            "state_dir": tmp_path / "state",
            "allow_path": tmp_path / "allow.txt",
            "deny_path": tmp_path / "deny.txt",
            "config_path": tmp_path / "config.toml",
            "lessons_path": tmp_path / "lessons.md",
            "cwd": str(tmp_path),
        }
        client, _ = make_client(env, [model_says("Here is tomorrow's forecast.")])
        seen: list[dict] = []
        with connected(client) as (ws, hello, _replay):
            ws.send_json({"type": "task", "text": "what is the forecast for tomorrow?"})
            seen.append(recv_until(ws, "done"))
            path = Path(env["state_dir"]) / hello["session"]
            assert wait_for(lambda: bool(steps(path, "objective")))
        role = steps(path, "role")[-1]
        revision = steps(path, "objective")[-1]
        # The test app's model is not admitted, so no model was asked: the
        # record says so, and the floor stands in for the revision.
        assert role["charter"] == "distiller" and role["status"] == "unadmitted"
        assert revision["origin"] == "extractive" and revision["turn"] == 1
        assert revision["extract"][0]["text"] == "what is the forecast for tomorrow?"
        # Written AFTER the turn's end, and never as a live step.
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        kinds = [r.get("kind") for r in lines]
        assert kinds.index("task_end") < max(
            i for i, r in enumerate(lines) if r.get("step", {}).get("kind") == "objective"
        )
        assert not [e for e in seen if e.get("kind") in ("objective", "role")]

    def test_the_cli_writes_one_off_its_own_thread(self, tmp_path, monkeypatch):
        from aish.cli import LogRef, distill_in_background

        monkeypatch.delenv("AISH_OBJECTIVE", raising=False)
        path = web_log(tmp_path)
        agent = SimpleNamespace(provider="claude-max", model="", _turn=1)
        thread = distill_in_background(agent, LogRef(SessionLog(path)), tmp_path)
        assert thread is not None
        thread.join(10)
        assert steps(path, "role")[-1]["status"] == "unavailable"
        assert steps(path, "objective")[-1]["origin"] == "extractive"
