"""The per-task tool menu (#444 H1, `docs/tool-menu.md`).

No model, no network: the model side is `FakeChat`, embeddings are a stand-in
index or a real `SemanticIndex` over a fake embed function, and every tool
called is either harmless or monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from aish import agent as agent_module
from aish import embeddings, evidence, tool_menu, tool_plugins, tooluse
from aish import rules as rules_module
from aish.paths import config_home
from aish.session import SessionLog
from tests.test_agent import FakeChat, make_agent, model_says, tool_call, write_rule


def schema(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


FULL = [
    schema("read_url", "Fetch one web page."),
    schema("web_search", "Search the web."),
    schema("gmail_search", "Search the owner's Gmail mailbox for messages."),
    schema("gmail_send", "Send an email from the owner's Gmail."),
    schema("drive_list", "List files in Google Drive folders."),
    schema("bank_transactions", "List bank account transactions and balances."),
    schema("read_docs", "Read a man page."),
]


def by_name(table: dict[str, float]):
    """A `scores` stand-in: similarity looked up by tool name."""
    return lambda _query, lines: {id(line): table.get(line.name, 0.0) for line in lines}


def sent_names(chat, call: int = 0) -> list[str]:
    return [t["function"]["name"] for t in chat.calls[call]["tools"]]


class FakeIndex:
    """Stands in for `SemanticIndex`: `sibling` hands back itself, and
    similarity is looked up by (first line of the query, tool name)."""

    def __init__(self, table: dict[str, dict[str, float]]):
        self.table = table
        self.error = None
        self.siblings: list[str] = []

    def sibling(self, cache_name: str):
        self.siblings.append(cache_name)
        return self

    def scores(self, query, entries):
        prefs = self.table.get(query.split("\n")[0], {})
        return {id(e): prefs.get(e.name, 0.0) for e in entries}

    def sentence_scores(self, _task, sentences):
        return dict.fromkeys(sentences, 0.0)


def logged_agent(responses, tmp_path, **kwargs):
    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    records: list[dict] = []

    def step_log(record):
        records.append(record)
        log.step(record)

    agent, chat = make_agent(
        responses, on_message=log.message, step_log=step_log, state_dir=tmp_path, **kwargs
    )
    return agent, chat, records


def of_kind(records, kind):
    return [r for r in records if r.get("kind") == kind]


@pytest.fixture
def read_docs_stub(monkeypatch):
    monkeypatch.setattr(agent_module.tools, "read_docs", lambda command, topic=None: "doc")


class TestToolMenuSelection:
    def test_the_same_inputs_give_the_same_menu_in_the_full_menus_order(self):
        def pick():
            return tool_menu.select(
                FULL, "mail", "mail", core=("read_url",),
                scores=by_name({"gmail_send": 0.9, "drive_list": 0.8}), top_k=2,
            )

        first, second = pick(), pick()
        assert first.category == second.category
        sent = tool_menu.menu_for(FULL, first.names)
        assert [s["function"]["name"] for s in sent] == ["read_url", "gmail_send", "drive_list"]
        assert json.dumps(sent) == json.dumps(tool_menu.menu_for(FULL, second.names))

    def test_each_name_is_recorded_under_the_first_category_that_wants_it(self):
        selection = tool_menu.select(
            FULL, "x", "x", core=("read_url",),
            required=("read_url", "drive_list"),
            continuity=("drive_list", "bank_transactions"),
            scores=by_name({}), top_k=0,
        )
        assert selection.category == {
            "read_url": "core", "drive_list": "required", "bank_transactions": "continuity",
        }

    def test_a_name_not_on_the_full_menu_is_never_invented(self):
        selection = tool_menu.select(
            FULL, "x", "x", core=("no_such_tool",), required=("also_missing",),
            continuity=("gone",), top_k=0,
        )
        assert selection.names == frozenset()

    def test_similarity_takes_the_top_k_and_ties_break_by_menu_order(self):
        selection = tool_menu.select(
            FULL, "x", "x", core=(),
            scores=by_name({"bank_transactions": 0.7, "drive_list": 0.5, "gmail_send": 0.5}),
            top_k=2,
        )
        assert selection.mode == tool_menu.MODE_SEMANTIC
        # gmail_send and drive_list tie at 0.5; gmail_send is earlier on the menu.
        assert [r["name"] for r in selection.relevant] == ["bank_transactions", "gmail_send"]

    def test_a_named_tool_family_is_admitted_even_past_top_k(self):
        selection = tool_menu.select(
            FULL, "check my gmail please", "check my gmail please", core=(),
            scores=by_name({"bank_transactions": 0.99}), top_k=1,
        )
        assert {"gmail_search", "gmail_send"} <= selection.names
        assert "bank_transactions" not in selection.names
        assert all(r.get("rail") for r in selection.relevant)

    def test_a_stopword_prefix_never_names_a_family(self):
        # `read` is in the knowledge layer's stopword list, so "read" in a
        # message is not a name hit for read_docs.
        selection = tool_menu.select(FULL, "read this for me", "read this for me", core=())
        assert "read_docs" not in selection.names

    def test_the_lexical_floor_ranks_when_embeddings_fail(self):
        selection = tool_menu.select(
            FULL, "show my bank transactions", "show my bank transactions", core=(),
            scores=lambda _q, _l: None, error=lambda: "embedder down",
        )
        assert selection.mode == tool_menu.MODE_LEXICAL
        assert selection.error == "embedder down"
        assert "bank_transactions" in selection.names
        # Lexical admits only what scored: no fill.
        assert "drive_list" not in selection.names

    def test_no_scorer_at_all_is_the_lexical_floor_without_an_error(self):
        selection = tool_menu.select(FULL, "list my drive files", "list my drive files", core=())
        assert selection.mode == tool_menu.MODE_LEXICAL
        assert selection.error == ""
        assert "drive_list" in selection.names


class TestTheMenuIsFrozenPerTask:
    def test_every_call_of_a_task_sends_the_same_menu_and_selection_runs_once(
        self, tmp_path, monkeypatch
    ):
        calls = []
        real = tool_menu.select
        monkeypatch.setattr(
            tool_menu, "select", lambda *a, **k: calls.append(1) or real(*a, **k)
        )
        agent, chat = make_agent(
            [
                model_says(tool_calls=[tool_call("recall", query="tea")]),
                model_says(tool_calls=[tool_call("recall", query="coffee")]),
                model_says("done"),
            ],
            cwd=str(tmp_path),
        )
        assert agent.run_task("what do I drink") == "done"
        assert len(chat.calls) == 3 and len(calls) == 1
        menus = {json.dumps(c["tools"]) for c in chat.calls}
        assert len(menus) == 1
        assert len(chat.calls[0]["tools"]) < len(agent._full_menu())

    def test_one_brief_per_task_while_nothing_grows(self, tmp_path):
        """The selected path's half of brief interning (the legacy half runs
        under `full_menu` in test_explain): several calls on an unchanged
        selected menu name it once."""
        agent, chat, records = logged_agent(
            [
                model_says(tool_calls=[tool_call("recall", query="tea")]),
                model_says(tool_calls=[tool_call("recall", query="coffee")]),
                model_says("done"),
            ],
            tmp_path,
        )
        agent.run_task("what do I drink")
        assert len(chat.calls) == 3
        (brief,) = of_kind(records, "brief")
        assert brief["tools"]["count"] == len(chat.calls[0]["tools"])
        assert brief["tools"]["selection"]["off_menu"] == 0

    def test_the_size_estimate_never_changes_the_menu(self, tmp_path):
        """The estimate counts a tool created mid-task (it WILL be sent) but
        admits nothing and says nothing: measuring is not sending."""
        agent, _ = make_agent([model_says("done")], cwd=str(tmp_path))
        agent.run_task("hello")
        agent._select_menu("hello", "hello", plan_open=False)
        assert agent._menu is not None
        before = dict(agent._menu.category)
        agent._plugin_defs = [*agent._plugin_defs, schema("brand_new", "new.")]
        notes: list[str] = []
        agent._note = notes.append
        estimated = agent._sent_menu(refresh=False, admit=False)
        assert "brand_new" in [s["function"]["name"] for s in estimated]
        assert agent._menu.category == before and notes == []

    def test_the_next_task_selects_again_from_its_own_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tool_menu, "RELEVANT_TOP_K", 1)
        index = FakeIndex({"alpha": {"create_rule": 0.9}, "beta": {"forget_memory": 0.9}})
        agent, chat = make_agent(
            [model_says("one"), model_says("two")], cwd=str(tmp_path), semantic=index
        )
        agent.run_task("alpha")
        agent.run_task("beta")
        assert "create_rule" in sent_names(chat, 0) and "forget_memory" not in sent_names(chat, 0)
        assert "forget_memory" in sent_names(chat, 1) and "create_rule" not in sent_names(chat, 1)
        assert index.siblings == [tool_menu.TOOL_EMBEDDINGS_FILE]

    def test_tools_called_in_the_previous_task_ride_the_next(self, tmp_path, read_docs_stub):
        agent, chat, records = logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("first"),
                model_says("second"),
            ],
            tmp_path,
        )
        agent.run_task("one")
        assert "read_docs" not in sent_names(chat, 0)
        agent.run_task("two")
        assert "read_docs" in sent_names(chat, 2)
        menu = of_kind(records, "context")[-1]["menu"]
        assert menu["names"]["continuity"] == ["read_docs"]

    def test_a_tool_created_mid_task_joins_the_menu(self, tmp_path):
        agent, chat = make_agent(
            [model_says(tool_calls=[tool_call("recall", query="x")]), model_says("done")],
            cwd=str(tmp_path),
        )
        real_refresh = agent._refresh_plugin_tools

        def refresh():
            real_refresh()
            if agent._menu is not None and agent._model_call >= 1:
                agent._plugin_defs = [*agent._plugin_defs, schema("brand_new", "new.")]

        agent._refresh_plugin_tools = refresh
        agent.run_task("anything")
        assert "brand_new" not in sent_names(chat, 0)
        assert "brand_new" in sent_names(chat, 1)
        assert agent._menu is not None and agent._menu.category["brand_new"] == "created"


class TestTheOffMenuNet:
    def test_an_unsent_tool_runs_joins_and_is_recorded_as_off_menu(
        self, tmp_path, read_docs_stub
    ):
        agent, chat, records = logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("done")],
            tmp_path,
        )
        assert agent.run_task("hello") == "done"
        assert "read_docs" not in sent_names(chat, 0)
        assert "read_docs" in sent_names(chat, 1)
        result = [m for m in agent.messages if m.get("role") == "tool"][0]
        assert "doc" in str(result["content"])
        (step,) = [r for r in records if r.get("kind") == "tool" and r["name"] == "read_docs"]
        assert step["off_menu"] is True and step["ok"] is True and "error" not in step
        briefs = of_kind(records, "brief")
        assert briefs[0]["tools"]["selection"]["off_menu"] == 0
        assert briefs[-1]["tools"]["selection"]["off_menu"] == 1
        assert briefs[-1]["tools"]["count"] == briefs[0]["tools"]["count"] + 1

    def test_growth_breaks_the_prefix_inside_the_task_and_says_so(
        self, tmp_path, read_docs_stub
    ):
        """The documented COST of the net (`docs/tool-menu.md`, the cache
        tension): an admitted tool takes its place in the full menu's order,
        so the growth call's tools differ from the call before at an early
        position — on a cache that cannot be trimmed, a cold re-read of the
        whole request inside the same task. Pinned so the doc cannot drift
        back to claiming within-task stability unconditionally."""
        agent, chat, _ = logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        before, after = sent_names(chat, 0), sent_names(chat, 1)
        # read_docs is the first native schema, so it lands at the very head.
        assert after[0] == "read_docs" and before[0] != "read_docs"
        common = 0
        for a, b in zip(chat.calls[0]["tools"], chat.calls[1]["tools"], strict=False):
            if json.dumps(a) != json.dumps(b):
                break
            common += 1
        assert common == 0

    def test_a_tool_that_does_not_exist_stays_an_unknown_tool(self, tmp_path):
        agent, chat, records = logged_agent(
            [model_says(tool_calls=[tool_call("frobnicate", x=1)]), model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        result = [m for m in agent.messages if m.get("role") == "tool"][0]
        assert str(result["content"]).startswith(agent_module.tools.UNKNOWN_TOOL_PREFIX)
        (step,) = [r for r in records if r.get("kind") == "tool"]
        assert "off_menu" not in step
        assert json.dumps(chat.calls[1]["tools"]) == json.dumps(chat.calls[0]["tools"])

    def test_a_mutating_plugin_without_an_approver_is_never_admitted(self, tmp_path):
        marker = tmp_path / "ran"
        directory = tool_plugins.GLOBAL_TOOLS_DIR / "wiper"
        directory.mkdir(parents=True)
        (directory / "TOOL.md").write_text(
            "---\nname: wiper\ndescription: wipe things\nexec: ./run.sh\nmutating: yes\n"
            'returns: text\nschema: {"text": {"type": "string", "required": true}}\n---\nbody\n'
        )
        script = directory / "run.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
        agent, chat, records = logged_agent(
            [model_says(tool_calls=[tool_call("wiper", text="x")]), model_says("done")],
            tmp_path,
        )
        agent.run_task("wipe things please")
        assert not marker.exists()
        assert "wiper" not in sent_names(chat, 0) and "wiper" not in sent_names(chat, 1)
        (step,) = [r for r in records if r.get("kind") == "tool"]
        assert "off_menu" not in step and step["ok"] is False

    def test_the_rule_gate_still_judges_an_off_menu_call(self, tmp_path, monkeypatch):
        ran = []
        monkeypatch.setattr(
            agent_module.tools, "read_docs", lambda command, topic=None: ran.append(1) or "doc"
        )
        directory = tmp_path / "rules"
        write_rule(directory, "no-docs", (
            "---\nname: no-docs\ndescription: Never read man pages.\nwhen: always\n"
            "then:\n  never_use: [read_docs]\n---\n"
        ))
        monkeypatch.setattr(rules_module, "GLOBAL_RULES_DIR", directory)
        agent, chat, records = logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        assert ran == []
        (step,) = [r for r in records if r.get("kind") == "tool"]
        assert step["off_menu"] is True and step["ok"] is False


class TestTheKillSwitch:
    def test_the_env_switch_sends_the_full_menu(self, tmp_path, monkeypatch):
        monkeypatch.setenv(tool_menu.MENU_ALL_ENV, "1")
        agent, chat, records = logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        assert sent_names(chat) == [s["function"]["name"] for s in agent._full_menu()]
        assert of_kind(records, "context")[0]["menu"] == {"mode": "all", "via": "env"}
        assert "selection" not in of_kind(records, "brief")[0]["tools"]

    def test_the_config_switch_sends_the_full_menu(self, tmp_path):
        home = config_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('tool_menu = "all"\n')
        agent, chat, records = logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        assert len(sent_names(chat)) == len(agent._full_menu())
        assert of_kind(records, "context")[0]["menu"] == {"mode": "all", "via": "config"}

    def test_zero_and_unset_select(self, tmp_path, monkeypatch):
        monkeypatch.setenv(tool_menu.MENU_ALL_ENV, "0")
        assert tool_menu.requested_mode() == ("", "")
        agent, chat = make_agent([model_says("done")], cwd=str(tmp_path))
        agent.run_task("hello")
        assert len(sent_names(chat)) < len(agent._full_menu())

    def test_a_missing_config_or_key_means_not_asked(self):
        assert not (config_home() / "config.toml").exists()
        assert tool_menu.requested_mode() == ("", "")
        config_home().mkdir(parents=True, exist_ok=True)
        (config_home() / "config.toml").write_text('model = "x"\n')
        assert tool_menu.requested_mode() == ("", "")

    def test_an_unparseable_config_fails_closed_to_the_full_menu(self, tmp_path):
        config_home().mkdir(parents=True, exist_ok=True)
        (config_home() / "config.toml").write_text('tool_menu = "all\n')  # broken TOML
        assert tool_menu.requested_mode() == ("all", tool_menu.VIA_CONFIG_UNREADABLE)
        notes: list[str] = []
        agent, chat, records = logged_agent([model_says("done")], tmp_path)
        agent._note = notes.append
        agent.run_task("hello")
        assert len(sent_names(chat)) == len(agent._full_menu())
        assert of_kind(records, "context")[0]["menu"] == {
            "mode": "all", "via": tool_menu.VIA_CONFIG_UNREADABLE,
        }
        assert any("not usable" in n for n in notes)

    def test_an_unreadable_config_fails_closed(self):
        config_home().mkdir(parents=True, exist_ok=True)
        (config_home() / "config.toml").mkdir()  # exists, cannot be read as a file
        assert tool_menu.requested_mode() == ("all", tool_menu.VIA_CONFIG_UNREADABLE)

    def test_an_unknown_value_fails_closed(self):
        config_home().mkdir(parents=True, exist_ok=True)
        (config_home() / "config.toml").write_text('tool_menu = "al"\n')
        assert tool_menu.requested_mode() == ("all", tool_menu.VIA_CONFIG_UNKNOWN)


class TestTheCatalogArm:
    """Arm C: core + structure, everything else one catalog line inside a
    `load_tool` meta-tool that changes the menu and executes nothing."""

    @pytest.fixture(autouse=True)
    def _catalog(self):
        home = config_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('tool_menu = "catalog"\n')

    def test_core_plus_the_catalog_and_nothing_ranked(self, tmp_path):
        agent, chat, records = logged_agent([model_says("done")], tmp_path)
        agent.run_task("search my gmail for invoices")
        names = sent_names(chat)
        assert names[-1] == tool_menu.LOAD_TOOL
        assert set(names[:-1]) == {n for n in tool_menu.CORE_TOOLS if n in names}
        catalog = chat.calls[0]["tools"][-1]["function"]["description"]
        assert "- read_docs:" in catalog and "- read_url:" not in catalog
        menu = of_kind(records, "context")[0]["menu"]
        assert menu["mode"] == tool_menu.MODE_CATALOG and menu["via"] == "config"
        assert menu["relevant"] == []

    def test_load_tool_puts_a_tool_on_the_next_call_and_runs_nothing(
        self, tmp_path, monkeypatch
    ):
        ran = []
        monkeypatch.setattr(
            agent_module.tools, "read_docs", lambda command, topic=None: ran.append(1) or "doc"
        )
        agent, chat, records = logged_agent(
            [
                model_says(tool_calls=[
                    tool_call(tool_menu.LOAD_TOOL, names=["read_docs", "nope"])
                ]),
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("done"),
            ],
            tmp_path,
        )
        agent.run_task("hello")
        assert "read_docs" not in sent_names(chat, 0) and "read_docs" in sent_names(chat, 1)
        load_result = [m for m in agent.messages if m.get("role") == "tool"][0]["content"]
        assert "Loaded: read_docs" in str(load_result) and "No such tool: nope" in str(load_result)
        steps = [r for r in records if r.get("kind") == "tool"]
        assert [s["name"] for s in steps] == [tool_menu.LOAD_TOOL, "read_docs"]
        assert all("off_menu" not in s for s in steps) and ran == [1]
        assert of_kind(records, "brief")[-1]["tools"]["selection"]["loaded"] == 1

    def test_loading_only_unknown_names_is_an_error(self, tmp_path):
        agent, _, records = logged_agent(
            [model_says(tool_calls=[tool_call(tool_menu.LOAD_TOOL, names=["nope"])]),
             model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        (step,) = [r for r in records if r.get("kind") == "tool"]
        assert step["ok"] is False

    def test_load_tool_outside_catalog_mode_is_an_unknown_tool(self, tmp_path):
        (config_home() / "config.toml").write_text("")
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call(tool_menu.LOAD_TOOL, names=["read_docs"])]),
             model_says("done")],
            cwd=str(tmp_path),
        )
        agent.run_task("hello")
        result = [m for m in agent.messages if m.get("role") == "tool"][0]["content"]
        assert str(result).startswith(agent_module.tools.UNKNOWN_TOOL_PREFIX)


class TestMenuRecords:
    def test_the_context_record_says_how_every_sent_tool_got_there(self, tmp_path):
        agent, chat, records = logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        menu = of_kind(records, "context")[0]["menu"]
        sent = sent_names(chat)
        assert menu["count"] == len(sent)
        assert menu["full_count"] == len(agent._full_menu())
        assert sorted(n for names in menu["names"].values() for n in names) == sorted(sent)
        core_on_menu = [n for n in tool_menu.CORE_TOOLS if n in sent]
        assert menu["names"]["core"] == sorted(core_on_menu)
        assert menu["mode"] == tool_menu.MODE_LEXICAL

    def test_a_tool_a_rule_needs_is_on_the_menu(self, tmp_path, monkeypatch, read_docs_stub):
        directory = tmp_path / "rules"
        write_rule(directory, "docs-first", (
            "---\nname: docs-first\ndescription: Read the man page first.\nwhen: always\n"
            "then:\n  must_first: read_docs\n---\n"
        ))
        monkeypatch.setattr(rules_module, "GLOBAL_RULES_DIR", directory)
        agent, chat, records = logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        assert "read_docs" in sent_names(chat)
        (step,) = [r for r in records if r.get("kind") == "tool"]
        assert "off_menu" not in step
        assert of_kind(records, "context")[0]["menu"]["names"]["required"] == ["read_docs"]

    def test_an_open_plan_puts_plan_in_core(self, tmp_path):
        agent, _ = make_agent([], cwd=str(tmp_path))
        record = agent._select_menu("hello", "hello", plan_open=True)
        assert "plan" in record["names"]["core"]
        record = agent._select_menu("hello", "hello", plan_open=False)
        assert "plan" not in record["names"].get("core", [])

    def test_tooluse_reads_the_menu_that_was_sent(self, tmp_path, read_docs_stub):
        agent, chat, _ = logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("done")],
            tmp_path,
        )
        agent.run_task("hello")
        report = tooluse.scan(root=tmp_path)
        (day,) = report.menu_days
        sizes = sorted(
            len(json.dumps(c["tools"], sort_keys=True, ensure_ascii=False)) for c in chat.calls
        )
        assert (day.calls, day.recorded) == (2, 2)
        assert day.max_chars == sizes[-1]
        assert day.median_tools == (len(chat.calls[0]["tools"]) + len(chat.calls[1]["tools"])) / 2
        assert report.tools["read_docs"].off_menu == 1
        assert report.tools["read_docs"].unknown == 0
        assert evidence.get(evidence.digest_of(json.dumps(
            chat.calls[0]["tools"], sort_keys=True, ensure_ascii=False
        )), tmp_path) is not None


class TestTheEmbeddingSeam:
    @staticmethod
    def _embed(_model, texts):
        # Deterministic and content-addressed: a vector from the words.
        return [
            [float(len(t) % 7 + 1), float(t.count("e") + 1), float("mail" in t) + 0.1]
            for t in texts
        ]

    def test_tool_vectors_live_in_their_own_file(self, tmp_path):
        index = embeddings.SemanticIndex(tmp_path, embed=self._embed)
        agent, chat, records = logged_agent([model_says("done")], tmp_path, semantic=index)
        agent.run_task("search my mail")
        assert (tmp_path / tool_menu.TOOL_EMBEDDINGS_FILE).is_file()
        menu = of_kind(records, "context")[0]["menu"]
        assert menu["mode"] == tool_menu.MODE_SEMANTIC
        assert len(menu["relevant"]) == tool_menu.RELEVANT_TOP_K
        cached = json.loads((tmp_path / tool_menu.TOOL_EMBEDDINGS_FILE).read_text())
        assert len(cached) == len(agent._full_menu()) - len(menu["names"]["core"])

    def test_a_failing_embedder_falls_back_to_the_lexical_floor(self, tmp_path):
        def down(_model, _texts):
            raise ConnectionError("embedder down")

        index = embeddings.SemanticIndex(tmp_path, embed=down)
        agent, chat, records = logged_agent([model_says("done")], tmp_path, semantic=index)
        assert agent.run_task("search my mail") == "done"
        menu = of_kind(records, "context")[0]["menu"]
        assert menu["mode"] == tool_menu.MODE_LEXICAL
        assert "embedder down" in menu["error"]


def test_fake_chat_is_the_suite_one():
    """Guards the import above: these tests drive the same FakeChat as the
    rest of the suite, never a local copy."""
    assert FakeChat.__module__ == "tests.test_agent"
