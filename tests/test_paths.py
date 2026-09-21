"""`paths.state_home` — the one knob for the state tree (#389).

Every test here asserts paths and writes nothing: the default resolves to the
owner's real `~/.local/state/aish`, and the suite's conftest redirects
`AISH_STATE_DIR` precisely so that no test lands there.
"""

from pathlib import Path

from aish import browser, email_poll, explain, ratelimit, secrets, tools, vouches
from aish.paths import DEFAULT_STATE_HOME, state_home


class TestStateHome:
    def test_the_variable_moves_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "elsewhere"))
        assert state_home() == tmp_path / "elsewhere"

    def test_unset_resolves_to_the_default_every_consumer_used(self, monkeypatch):
        """The failure this exists for: `ratelimit._path` had NO default, so
        under launchd — where the plist sets the variable for nothing — the
        server never persisted what it learned. The default is the tree the
        rest of aish was already reading."""
        monkeypatch.delenv("AISH_STATE_DIR", raising=False)
        assert state_home() == DEFAULT_STATE_HOME
        assert DEFAULT_STATE_HOME == Path.home() / ".local" / "state" / "aish"

    def test_empty_means_unset_like_the_config_knob(self, monkeypatch):
        """`config_home` treats an empty variable as absent; so does this one.
        The old per-site spelling made `AISH_STATE_DIR=` mean the CURRENT
        DIRECTORY, which nothing relied on and nobody would want."""
        monkeypatch.setenv("AISH_STATE_DIR", "")
        assert state_home() == DEFAULT_STATE_HOME

    def test_resolves_at_call_time(self, tmp_path, monkeypatch):
        """`create_app` exports the variable at startup so the browser profile,
        the secrets index and the vouches agree with the argument the server
        was built with (#290). A value frozen at import would not."""
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "first"))
        assert state_home() == tmp_path / "first"
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "second"))
        assert state_home() == tmp_path / "second"


class TestEveryConsumerAnswersTheSame:
    """One authority for "where does aish keep its state". A site that spelled
    its own default could opt out of the knob by omission — that is exactly
    what `ratelimit._path` did — so each consumer is pinned to the knob rather
    than to a string it happens to share with it today."""

    def test_all_state_consumers_follow_the_knob(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setenv("AISH_STATE_DIR", str(state))
        governor = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1.0)
        assert vouches.state_dir() == state
        assert browser.state_dir() == state
        assert explain.state_dir() == state
        assert secrets.state_dir() == state
        assert tools._default_job_log_dir() == state / "jobs"
        assert email_poll._health_path() == state / "email_poll_health.json"
        assert governor._path() == state / "rate-limits.json"
