"""The `processes` tool: aish's own "is X running" (epic #462 S1)."""

from __future__ import annotations

from test_agent import make_agent, model_says, tool_call, tool_messages

from aish import processes, tools

COMM = """\
  101 01:02:03 /usr/local/bin/ollama
  202    05:06 /opt/homebrew/Cellar/python@3.13/3.13.9/bin/python3.13
  303    00:01 /bin/zsh
"""
ARGS = """\
  101 /usr/local/bin/ollama serve
  202 /opt/homebrew/bin/python3.13 /Users/x/.local/bin/aish-web --api-key=sk-SECRET-123
  303 -zsh
"""


def fake_ps(argv: list[str]) -> str:
    assert argv[0] == processes.PS  # a fixed argv, never a shell string
    return COMM if "pid=,etime=,comm=" in argv else ARGS


class TestProcesses:
    def test_it_matches_the_executable_name(self):
        out = processes.report("olla", run=fake_ps)
        assert "pid 101 · ollama · running 01:02:03 · matched by executable name" in out
        assert "202" not in out

    def test_it_matches_an_argument_word_exactly_and_never_shows_arguments(self):
        out = processes.report("aish-web", run=fake_ps)
        assert "pid 202 · python3.13" in out
        assert "matched by an argument word" in out
        assert "SECRET" not in out and "api-key" not in out and "/Users/x" not in out

    def test_an_argument_cannot_be_read_out_by_substring(self):
        """Exact whole-word equality: a guess must be the entire word."""
        for guess in ("sk-SECRET", "--api-key", "api", "SECRET"):
            assert "No running process matches" in processes.report(guess, run=fake_ps)

    def test_nothing_running_says_what_was_checked(self):
        out = processes.report("postgres", run=fake_ps)
        assert out.startswith("No running process matches 'postgres'")

    def test_a_bad_name_is_an_error(self):
        assert processes.report("", run=fake_ps).startswith("ERROR")
        assert processes.report("a\nb", run=fake_ps).startswith("ERROR")

    def test_a_failing_ps_is_reported_not_raised(self):
        def broken(_argv):
            raise OSError("no ps here")

        assert "could not list processes" in processes.report("x", run=broken)

    def test_it_is_a_native_tool_and_the_agent_answers_it(self, monkeypatch):
        assert "processes" in {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
        monkeypatch.setattr(processes, "_run_ps", fake_ps)
        agent, _ = make_agent([
            model_says(tool_calls=[tool_call("processes", program="ollama")]),
            model_says("done"),
        ])
        agent.run_task("is ollama running?")
        assert "pid 101" in tool_messages(agent.messages)[0]["content"]
