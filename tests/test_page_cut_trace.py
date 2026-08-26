"""A page cut, in the record (#274).

#269 made the cut recoverable — the model is handed a key and told to page. That
turns one failure into two, and the trace could see neither: a page cut wrote no
`truncation` block at all, so "improvised after a dead end" and "improvised
despite being offered page 2" were the same blank row. They are different
incidents with different repairs (contract §3.4), and the second is the one to
expect from here, because the fix is a sentence the model has to choose to act
on.

Nothing here runs a model, a browser or a network call: the browser is patched
away and the agent is driven by `FakeChat`, exactly as `tests/test_agent.py` and
`tests/test_browse.py` do.
"""

import json

import pytest

from aish import agent as agent_module
from aish import browse as browse_mod
from aish import explain, signin, tool_plugins
from aish import web as web_module
from tests.test_agent import FakeChat, model_says, tool_call


def long_page(rows=250, filler=400):
    """A page shaped like the ratings list that filed #269."""
    return "\n".join(f"{n}. Title {n}\n" + ("x" * filler) for n in range(1, rows + 1))


class TestAPageCutWritesItselfDown:
    """The record itself, at the seam that makes it — `web` cuts, and the value
    it hands back carries what it did."""

    def test_a_cut_page_carries_the_contract_block(self, tmp_path):
        cut = web_module.PageCut(
            lambda text, shown: tool_plugins.store_continuation(
                text, tmp_path, shown=shown
            )
        )
        page = long_page()
        out = web_module._present_snapshot(
            browse_mod.Snapshot(url="https://x.test/", title="", text=page), cut=cut
        )
        sealed = web_module.sealed(out, cut)
        record = sealed.meta["truncation"]

        assert record["truncator"] == web_module.TRUNCATOR == "web"
        assert record["cap_source"] == "constant:PAGE_MAX_CHARS"
        assert record["kept"] == record["head"] == web_module.PAGE_MAX_CHARS
        # Head-only: a page's tail is the site's footer, so there is no second
        # window and saying otherwise would misdescribe where page 2 resumes.
        assert record["tail"] == 0
        assert record["omitted"] == len(page) - web_module.PAGE_MAX_CHARS
        assert record["offered"] is True
        assert record["continuation"]
        assert sealed.meta["bytes"] == len(page)

    def test_a_cut_with_nowhere_to_cache_says_SO_rather_than_nothing(self, tmp_path):
        """The distinction the whole issue is about. A dead end must be
        recorded AS a dead end — not left absent, which is the one thing the
        contract forbids as evidence (corollary 2)."""
        cut = web_module.PageCut(lambda _t, _s: "")
        out = web_module._present_snapshot(
            browse_mod.Snapshot(url="https://x.test/", title="", text=long_page()),
            cut=cut,
        )
        record = web_module.sealed(out, cut).meta["truncation"]
        assert record["offered"] is False
        assert record["continuation"] == ""
        assert record["omitted"] > 0

    def test_a_page_that_fits_carries_no_envelope_at_all(self):
        """An absent block means nothing was cut, which is only safe because a
        cut that happened always writes one."""
        cut = web_module.PageCut(lambda _t, _s: "k")
        out = web_module._present_snapshot(
            browse_mod.Snapshot(url="https://x.test/", title="", text="short"), cut=cut
        )
        assert cut.record is None
        assert web_module.sealed(out, cut) is out
        assert not hasattr(web_module.sealed(out, cut), "meta")

    def test_a_topic_narrowed_read_records_its_own_cap(self):
        """A narrowed read is cut by a different budget, and a record naming
        the wrong one is worse than none — `cap_source` exists precisely so a
        log can show which cap bit."""
        cut = web_module.PageCut(lambda _t, _s: "k")
        web_module._present(
            "https://x.test/", "\n".join(f"line {n} match" for n in range(4000)),
            [], topic="match", cut=cut,
        )
        assert cut.record["cap_source"] == "constant:DOCS_MAX_CHARS"
        assert cut.record["kept"] == web_module.DOCS_MAX_CHARS

    def test_the_recorder_is_per_call_because_reads_run_in_parallel(self):
        """read_url is on the parallel read path. A recorder on the agent would
        attribute one read's cut to another read's result — the same race that
        put a tool's verdict on the VALUE rather than on the agent."""
        first, second = web_module.PageCut(lambda _t, _s: "a"), web_module.PageCut(
            lambda _t, _s: "b"
        )
        page = browse_mod.Snapshot(url="https://x.test/", title="", text=long_page())
        web_module._present_snapshot(page, cut=first)
        web_module._present_snapshot(
            browse_mod.Snapshot(url="https://y.test/", title="", text="short"),
            cut=second,
        )
        assert first.record["continuation"] == "a"
        assert second.record is None


class TestTheRecordReachesTheTrace:
    """Through the agent's single funnel, which is where a native tool's result
    used to have its envelope thrown away."""

    def _run(self, monkeypatch, tmp_path, responses, page):
        monkeypatch.setattr(agent_module.web, "_require_public", lambda _url: None)
        monkeypatch.setattr(
            agent_module.browser,
            "browse_open",
            lambda url, *, topic="", **_kw: browse_mod.Snapshot(
                url=url, title="Your ratings", text=page, controls=[]
            ),
        )
        steps: list[dict] = []
        agent = agent_module.Agent(
            model="fake",
            approve=lambda _c: True,
            client_chat=FakeChat(responses),
            approve_tool=lambda *_a: True,
            state_dir=str(tmp_path),
            on_step=steps.append,
        )
        agent.run_task("read all my ratings")
        return agent, [s for s in steps if s.get("kind") == "tool"]

    def test_the_step_carries_the_truncation_block(self, monkeypatch, tmp_path):
        _agent, steps = self._run(
            monkeypatch,
            tmp_path,
            [
                model_says(tool_calls=[tool_call("browse", url="https://imdb.test/r/")]),
                model_says("done"),
            ],
            long_page(),
        )
        step = next(s for s in steps if s["name"] == "browse")
        assert step["truncation"]["truncator"] == "web"
        assert step["truncation"]["offered"] is True
        assert step["bytes"] > web_module.PAGE_MAX_CHARS

    def test_the_evidence_survives_the_prefix_sniff(self, monkeypatch, tmp_path):
        """The bug this half fixes. A native tool has no `verdict_by` rule that
        describes "a page came back", so it states no status and the legacy
        prefix sniff still decides — but the sniff used to REPLACE the envelope
        wholesale, taking the evidence with it. Evidence and verdict are
        different things and only one of them is being claimed here."""
        _agent, steps = self._run(
            monkeypatch,
            tmp_path,
            [
                model_says(tool_calls=[tool_call("browse", url="https://imdb.test/r/")]),
                model_says("done"),
            ],
            long_page(),
        )
        step = next(s for s in steps if s["name"] == "browse")
        assert step["verdict_by"] == "prefix", "the status is still the sniff's"
        assert step["ok"] is True
        assert "truncation" in step, "the sniff threw the evidence away"

    def test_a_page_that_fits_records_no_truncation(self, monkeypatch, tmp_path):
        _agent, steps = self._run(
            monkeypatch,
            tmp_path,
            [
                model_says(tool_calls=[tool_call("browse", url="https://imdb.test/r/")]),
                model_says("done"),
            ],
            long_page(rows=3),
        )
        step = next(s for s in steps if s["name"] == "browse")
        assert "truncation" not in step

    def test_a_paging_call_records_WHICH_output_it_read(self, monkeypatch, tmp_path):
        """Otherwise joining "offered" to "used" means parsing the summary
        string back out of prose."""
        page = long_page()
        agent, steps = self._run(
            monkeypatch,
            tmp_path,
            [
                model_says(tool_calls=[tool_call("browse", url="https://imdb.test/r/")]),
                model_says("done"),
            ],
            page,
        )
        key = next(s for s in steps if s["name"] == "browse")["truncation"]["continuation"]
        assert str(agent._read_tool_output({"continuation": key, "page": 2}))
        outcome = agent._read_tool_output({"continuation": key, "page": 2})
        assert outcome.meta["continuation"] == key
        assert outcome.meta["source"] == "cache"


class TestExplainCanTellTheTwoIncidentsApart:
    """The point of recording it. `notes()` states facts and never causes
    (#243), so both rows say what happened and neither says why."""

    @staticmethod
    def _doc(calls):
        """The dossier's shape, with nothing in it but the calls under test —
        every other checker sees an empty section and stays silent."""
        return {"did": {"calls": calls, "orphan_gates": [], "verify": []},
                "thought": {"calls": []},
                "flow": {"rounds": [], "loose": []},
                "given": {"briefs": [], "trims": [], "rules": {"groups": {}}},
                "produced": {"status": "ok", "error": "",
                             "verify": {"stopped": [], "advised": []}}}

    @staticmethod
    def _call(n, name, truncation=None, read=""):
        return {"call": n, "name": name, "summary": "", "args": {},
                "args_state": explain.RECORDED, "args_truncated": 0,
                "cap_source": None, "ok": True, "status": "ok", "secs": 0.1,
                "command": "", "error": "", "output": "", "decision": None,
                "verdict_by": "prefix", "truncation": truncation or {},
                "read": read, "gates": [], "refused": [], "completed": True}

    def _checks(self, rows):
        return {row["check"] for row in rows}

    def test_a_dead_end_is_named_as_one(self):
        """#269's own shape: cut, and nothing offered. A missing capability."""
        rows = explain.notes(self._doc([
            self._call(1, "browse", {"omitted": 65047, "truncator": "web",
                                     "offered": False, "continuation": ""})
        ]))["rows"]
        assert "result_cut" in self._checks(rows)
        assert "continuation_unread" not in self._checks(rows)
        assert "65047" in rows[0]["text"] and "no continuation was offered" in rows[0]["text"]

    def test_offered_and_never_read_is_a_DIFFERENT_row(self):
        """The incident to expect now, and the one that was unfalsifiable: the
        model was handed a way to read the rest and did not take it."""
        rows = explain.notes(self._doc([
            self._call(1, "browse", {"omitted": 65047, "truncator": "web",
                                     "offered": True, "continuation": "abc123s12000"})
        ]))["rows"]
        assert "continuation_unread" in self._checks(rows)
        assert "result_cut" not in self._checks(rows)

    def test_offered_and_read_back_is_not_worth_a_row(self):
        """A cut that was recovered is the system working. Flagging it would
        bury the two rows that matter."""
        rows = explain.notes(self._doc([
            self._call(1, "browse", {"omitted": 65047, "truncator": "web",
                                     "offered": True, "continuation": "abc123s12000"}),
            self._call(2, "read_tool_output", read="abc123s12000"),
        ]))["rows"]
        assert self._checks(rows) == set()

    def test_the_paging_call_may_come_several_calls_later(self):
        """The read is matched across the whole turn, not against the next
        call: a model reads three more pages before coming back to this one."""
        rows = explain.notes(self._doc([
            self._call(1, "browse", {"omitted": 10, "truncator": "web",
                                     "offered": True, "continuation": "k1"}),
            self._call(2, "web_search"),
            self._call(3, "read_url"),
            self._call(4, "read_tool_output", read="k1"),
        ]))["rows"]
        assert self._checks(rows) == set()

    def test_reading_a_DIFFERENT_output_does_not_clear_this_one(self):
        rows = explain.notes(self._doc([
            self._call(1, "browse", {"omitted": 10, "truncator": "web",
                                     "offered": True, "continuation": "k1"}),
            self._call(2, "read_tool_output", read="k2"),
        ]))["rows"]
        assert "continuation_unread" in self._checks(rows)

    @pytest.mark.parametrize("check", ["result_cut", "continuation_unread"])
    def test_both_checks_are_declared_so_the_empty_case_can_name_them(self, check):
        """"Nothing unusual" is a claim a checker cannot make; it can only list
        the classes someone thought to code. A check that fires but is not
        declared makes that list a lie."""
        assert check in {cid for cid, _label in explain.CHECKS}

    def test_a_plugin_tools_cut_is_reported_the_same_way(self):
        """Nothing here is page-specific. `tool_plugins` has written this block
        since #192 and no reader ever looked at it."""
        rows = explain.notes(self._doc([
            self._call(1, "youtube_analyze",
                       {"omitted": 19365, "truncator": "tool_plugins",
                        "offered": True, "continuation": "zz"})
        ]))["rows"]
        assert "continuation_unread" in self._checks(rows)
        assert "tool_plugins" in rows[0]["text"]


class TestTheDossierShowsIt:
    """A row in `notes` points AT the dossier, so the dossier has to hold the
    thing it points at."""

    def test_the_call_lines_name_the_cut_and_the_way_back(self):
        call = TestExplainCanTellTheTwoIncidentsApart._call(
            1, "browse",
            {"omitted": 65047, "truncator": "web", "offered": True,
             "cap_source": "constant:PAGE_MAX_CHARS", "continuation": "abc123s12000"},
        )
        text = "\n".join(explain._call_lines(call))
        assert "65047 result characters cut" in text
        assert "constant:PAGE_MAX_CHARS" in text
        assert 'read_tool_output(continuation="abc123s12000")' in text

    def test_a_dead_end_says_so_in_the_dossier_too(self):
        call = TestExplainCanTellTheTwoIncidentsApart._call(
            1, "browse",
            {"omitted": 65047, "truncator": "web", "offered": False,
             "cap_source": "constant:PAGE_MAX_CHARS", "continuation": ""},
        )
        text = "\n".join(explain._call_lines(call))
        assert "no continuation offered" in text

    def test_a_call_with_no_cut_says_nothing_about_cuts(self):
        call = TestExplainCanTellTheTwoIncidentsApart._call(1, "browse")
        text = "\n".join(explain._call_lines(call))
        assert "characters cut" not in text
        assert "read back from cache" not in text

    def test_the_dossier_survives_a_log_written_before_any_of_this(self):
        """Old logs have no `truncation` key and must render unchanged — the
        reader law: absence is never evidence, and never a crash either."""
        step = {"kind": "tool", "name": "browse", "call": 1, "ok": True,
                "status": "ok", "secs": 0.2, "summary": "https://x.test/"}
        turn = explain.Turn(ordinal=0, steps=[step], messages=[])
        did = explain._did(turn)
        assert did["calls"][0]["truncation"] == {}
        assert did["calls"][0]["read"] == ""
        assert "characters cut" not in "\n".join(explain._call_lines(did["calls"][0]))


class TestTheContractSaysWhatTheCodeDoes:
    """§3.4 is binding, and this phase adds a truncator to a closed vocabulary
    of three. A doc that lists three while the code writes four sends the next
    reader looking for a truncator that does not exist — which is the exact
    failure `truncator` was added to prevent."""

    @staticmethod
    def _contract():
        import pathlib
        return pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "docs/trace-contract.md"
        ).read_text(encoding="utf-8")

    def test_the_truncator_vocabulary_names_web(self):
        assert "`web`" in self._contract().split("truncation.truncator")[1][:600]

    def test_the_page_caps_are_named_as_cap_sources(self):
        contract = self._contract()
        assert "constant:PAGE_MAX_CHARS" in contract
        assert "constant:DOCS_MAX_CHARS" in contract

    def test_the_paging_step_field_is_the_one_python_can_actually_emit(self):
        """The contract said `"from": "cache"` and the code has always written
        `"source"`, because `from` is a Python keyword and cannot be a kwarg.
        A binding spec that cannot be implemented is a spec nobody follows."""
        # The SPECIFYING sentence, not the whole file: the paragraph below it
        # quotes the old wording on purpose, to say what was corrected.
        spec = self._contract().split("A continuation fetch is an ordinary")[1]
        spec = spec[: spec.index("\n")]
        assert '"source": "cache"' in spec
        assert '"from"' not in spec
        assert '"continuation"' in spec

    def test_the_json_example_parses_and_matches_what_web_writes(self):
        """The block in the doc is the block in the log, field for field."""
        contract = self._contract()
        block = contract.split("### 3.4")[1]
        example = block[block.index('"truncation"'):]
        example = example[example.index("{"):]
        depth, end = 0, 0
        for i, ch in enumerate(example):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                end = i + 1
                break
        parsed = json.loads(example[:end].replace("…", ""))
        cut = web_module.PageCut(lambda _t, _s: "k")
        cut.keep("x" * 100, 10, "constant:PAGE_MAX_CHARS")
        assert set(parsed) == set(cut.record)

    @staticmethod
    def _example(key):
        """The named object out of section 3.4's example block, parsed."""
        block = TestTheContractSaysWhatTheCodeDoes._contract().split("### 3.4")[1]
        example = block[block.index(f'"{key}"'):]
        example = example[example.index("{"):]
        depth, end = 0, 0
        for i, ch in enumerate(example):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                end = i + 1
                break
        return json.loads(example[:end].replace("…", ""))

    @staticmethod
    def _signin_row():
        row = TestTheContractSaysWhatTheCodeDoes._contract().split("| `signin` |")[1]
        return row[: row.index("\n")]

    @staticmethod
    def _verdict_section():
        """§3.4.1 — the specifying text for `signin.verdict` / `.observed`."""
        contract = TestTheContractSaysWhatTheCodeDoes._contract()
        section = contract.split("#### 3.4.1")[1]
        return section[: section.index("\nA continuation fetch")]

    def test_the_signin_example_is_what_a_judged_failure_actually_writes(self):
        """#325. The observations behind a failed sign-in were computed and
        discarded — only the owner-facing sentence survived, so nothing on
        disk could contradict a claim about why one failed. A contract change
        with no contract test is how the `"from": "cache"` drift above lasted
        a year."""
        from aish import browser

        seen = web_module.SignInSeen()
        seen.note("eon.pl", browser.SignInResult(
            frame="/store/frames/login.jpg",
            console=["error: Failed to load resource: recaptcha"],
            covered="clb clb-container",
            verdict=signin.FAILED_NEVER_SENT,
            observed=browser.SignInObserved(declared_widget="reCAPTCHA"),
        ))
        record = seen.record()
        parsed = self._example("signin")
        # `frame_skipped` is in the doc beside `frame` to show the vocabulary;
        # the writer emits exactly one of that pair (contract section 3.4).
        assert set(parsed) - {"frame_skipped"} == set(record)
        assert set(parsed["observed"]) == set(record["observed"])
        assert record["verdict"] in signin.FAILURE_VERDICTS

    def test_the_documented_verdict_vocabulary_is_the_one_the_code_can_return(self):
        """A closed vocabulary read back by a log scan and, later, by an exam
        built from real recorded failures. A doc naming a token the table
        cannot return sends the next reader looking for a failure that does
        not exist — and a token the doc omits is one they will not know to
        look for."""
        spec = self._verdict_section()
        for token in signin.FAILURE_VERDICTS:
            assert f"`{token}`" in spec
        # ...and the outcome that was REMOVED is not quietly back in the
        # vocabulary by way of the document (#321).
        assert "`captcha`" not in spec and "FAILED_CAPTCHA" in spec

    def test_the_contract_says_no_credential_and_nothing_page_authored(self):
        """Section 8.6 forbids fetched content and `console` needed a stated
        exception for it. These keys must not quietly ride that exception:
        four booleans and aish's own brand name for a token it matched."""
        spec = self._verdict_section()
        assert "§8.6 needs no exception here" in spec
        assert "never a span of the document" in spec

    def test_every_observation_the_code_writes_is_in_the_contract(self):
        """The pair travels together, so the doc has to name both halves and
        every field inside the second — a field written into every log from
        here and described nowhere is one a later reader cannot trust."""
        from aish import browser

        seen = web_module.SignInSeen()
        seen.note("eon.pl", browser.SignInResult(
            verdict=signin.FAILED_UNEXPLAINED, observed=browser.SignInObserved(),
        ))
        spec = self._verdict_section()
        for name in seen.record()["observed"]:
            assert f"`{name}`" in spec
        # ...and the row that used to hold all of this still points at it, so
        # the reader of the table is not left with two keys and no rules.
        assert "3.4.1" in self._signin_row()
