"""Alias expansion tests.

Two layers: the pure expand/parse/merge logic in aish.aliases, and the
integration proof that expansion happens BEFORE the approval gate — so the
gate/denylist classify the REAL command, never an opaque alias. Follows the
suite pattern (FakeChat, no model / network / real execution) except where a
harmless `echo` proves the `!`-command path end to end.
"""

from types import SimpleNamespace

from aish import aliases
from aish.agent import Agent
from aish.approval import Blocked, check_denied, is_read_only

# --- pure expansion -------------------------------------------------------

class TestExpand:
    def test_first_word_expands(self):
        assert aliases.expand("ll", {"ll": "ls -l"}) == "ls -l"

    def test_args_after_first_word_preserved(self):
        assert aliases.expand("ll -a src", {"ll": "ls -l"}) == "ls -l -a src"

    def test_only_first_word_matches(self):
        # An alias name appearing as a later word is NOT expanded.
        assert aliases.expand("echo ll", {"ll": "ls -l"}) == "echo ll"

    def test_no_match_passes_through(self):
        assert aliases.expand("git status", {"ll": "ls -l"}) == "git status"

    def test_empty_alias_map_passes_through(self):
        assert aliases.expand("ll -a", {}) == "ll -a"

    def test_leading_whitespace_passes_through(self):
        # A leading space means the first token isn't at column 0 — treat as opaque.
        assert aliases.expand("  ll", {"ll": "ls -l"}) == "  ll"

    def test_recursive_expansion(self):
        # ll → la → ls -la, resolving through a chain.
        table = {"ll": "la", "la": "ls -la"}
        assert aliases.expand("ll /tmp", table) == "ls -la /tmp"

    def test_self_reference_expands_once(self):
        # The classic `ls = "ls --color"` must expand exactly once, not loop.
        assert aliases.expand("ls foo", {"ls": "ls --color"}) == "ls --color foo"

    def test_cycle_does_not_hang(self):
        # a → b → a: the seen-set breaks the loop and returns a finite result.
        result = aliases.expand("a x", {"a": "b", "b": "a"})
        assert result in ("a x", "b x")  # terminates; exact stop point is unspecified

    def test_expansion_preserves_multiline_remainder(self):
        assert aliases.expand("run\nmore", {"run": "bash -c"}) == "bash -c\nmore"


# --- sanitize / config parsing -------------------------------------------

class TestSanitize:
    def test_drops_non_string_values(self):
        assert aliases.sanitize({"ll": "ls -l", "bad": 5}) == {"ll": "ls -l"}

    def test_drops_empty_expansion(self):
        assert aliases.sanitize({"x": "   "}) == {}

    def test_drops_odd_names(self):
        assert aliases.sanitize({"..": "cd ..", "ll": "ls -l"}) == {"ll": "ls -l"}

    def test_non_mapping_yields_empty(self):
        assert aliases.sanitize(["ls -l"]) == {}
        assert aliases.sanitize(None) == {}


class TestParseZshOutput:
    def test_single_quoted_values(self):
        text = "ll='ls -l'\ngs='git status'"
        assert aliases.parse_alias_output(text) == {"ll": "ls -l", "gs": "git status"}

    def test_embedded_quote_escape(self):
        # zsh escapes an embedded single quote as '\'' inside the quoted value.
        text = "say='echo '\\''hi'\\'''"
        assert aliases.parse_alias_output(text) == {"say": "echo 'hi'"}

    def test_unquoted_value(self):
        assert aliases.parse_alias_output("run-help=man") == {"run-help": "man"}

    def test_skips_odd_names(self):
        text = "..='cd ..'\n-='cd -'\nll='ls -l'"
        assert aliases.parse_alias_output(text) == {"ll": "ls -l"}


class TestMergeConfig:
    def test_appends_new_table_when_absent(self):
        out = aliases.merge_into_config_text('model = "x"\n', {"ll": "ls -l"})
        assert "[aliases]" in out
        assert 'll = "ls -l"' in out
        # Result must be valid TOML that round-trips the new alias.
        import tomllib
        assert tomllib.loads(out)["aliases"]["ll"] == "ls -l"

    def test_inserts_under_existing_table(self):
        text = 'model = "x"\n\n[aliases]\ngs = "git status"\n'
        out = aliases.merge_into_config_text(text, {"ll": "ls -l"})
        import tomllib
        parsed = tomllib.loads(out)
        assert parsed["aliases"] == {"gs": "git status", "ll": "ls -l"}

    def test_escapes_quotes_in_value(self):
        out = aliases.merge_into_config_text("", {"say": 'echo "hi"'})
        import tomllib
        assert tomllib.loads(out)["aliases"]["say"] == 'echo "hi"'


# --- integration: expansion happens BEFORE the approval gate --------------

def _scripted(responses):
    class Chat:
        def __init__(self, r):
            self.r = list(r)

        def __call__(self, **_):
            return self.r.pop(0)

    return Chat(responses)


def _tool_call(name, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def _model_says(content="", tool_calls=None):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )


def _gate_approver(seen):
    """Mirror the real approver order: denylist first, then read-only
    auto-approve, else approve. Records exactly what string it classifies."""

    def approve(command):
        seen.append(command)
        reason = check_denied(command)
        if reason:
            return Blocked(reason)
        return True

    return approve


class TestGateSeesExpanded:
    def test_approver_receives_expanded_command(self):
        seen: list[str] = []
        chat = _scripted(
            [
                _model_says(tool_calls=[_tool_call("run_command", command="ll -a src")]),
                _model_says("done"),
            ]
        )
        agent = Agent(
            model="fake",
            approve=_gate_approver(seen),
            client_chat=chat,
            aliases={"ll": "ls -l"},
        )
        agent.run_task("list")
        # The gate classified the REAL command, not the alias name.
        assert seen == ["ls -l -a src"]

    def test_alias_to_denylisted_command_is_blocked(self, tmp_path):
        """An alias expanding to an unrecoverable command is still caught: the
        gate sees `rm -rf /...`, returns Blocked, and nothing runs."""
        seen: list[str] = []
        marker = tmp_path / "victim"
        marker.mkdir()
        chat = _scripted(
            [
                _model_says(tool_calls=[_tool_call("run_command", command="nuke")]),
                _model_says("won't do that"),
            ]
        )
        agent = Agent(
            model="fake",
            approve=_gate_approver(seen),
            client_chat=chat,
            aliases={"nuke": f"rm -rf {marker}"},
        )
        agent.run_task("clean up")
        assert seen == [f"rm -rf {marker}"]
        assert check_denied(seen[0]) is not None  # the expansion is denylisted
        assert marker.exists()  # never executed

    def test_alias_to_read_only_is_classified_read_only(self):
        # The expanded form is what the read-only classifier judges.
        assert is_read_only(aliases.expand("ll", {"ll": "ls -l"}))
        assert not is_read_only(aliases.expand("nuke", {"nuke": "rm -rf /tmp/x"}))


class TestUserCommandPath:
    def test_bang_command_expands(self):
        """The `!`-command path (run_user_command) expands too, end to end."""
        agent = Agent(model="fake", approve=lambda _c: True, aliases={"greet": "echo hello"})
        result = agent.run_user_command("greet")
        assert "hello" in result
        # The transcript records the REAL command that ran, not the alias.
        assert any(
            "echo hello" in m.get("content", "")
            for m in agent.messages
            if m.get("role") == "user"
        )


class TestConfigLoading:
    def test_load_config_reads_aliases_table(self, tmp_path):
        from aish.cli import load_config

        cfg = tmp_path / "config.toml"
        cfg.write_text('model = "m"\n\n[aliases]\nll = "ls -l"\ngs = "git status"\n')
        config = load_config(cfg)
        assert config["aliases"] == {"ll": "ls -l", "gs": "git status"}

    def test_agent_sanitizes_config_aliases(self):
        # A malformed value in the table is dropped, not fatal.
        agent = Agent(
            model="fake",
            approve=lambda _c: True,
            aliases={"ll": "ls -l", "bogus": 123},
        )
        assert agent.aliases == {"ll": "ls -l"}

    def test_agent_without_aliases_defaults_empty(self):
        agent = Agent(model="fake", approve=lambda _c: True)
        assert agent.aliases == {}
        assert agent.expand_alias("ll -a") == "ll -a"
