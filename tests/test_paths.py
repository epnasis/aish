"""`paths.state_home` — the one knob for the state tree (#389).

Every test here asserts paths and writes nothing: the default resolves to the
owner's real `~/.local/state/aish`, and the suite's conftest redirects
`AISH_STATE_DIR` precisely so that no test lands there.
"""

from pathlib import Path

import pytest

from aish import (
    browser,
    curate,
    email_poll,
    explain,
    ratelimit,
    secrets,
    signin,
    skill_import,
    tools,
    vouches,
)
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
        # The three that #389 left bound to the real home at import (#399).
        # `signin.store` is the REAL function through the conftest stash: the
        # suite guard replaces the name with a tmp path, and asserting on the
        # patched name would prove only that the guard is in place.
        assert signin._real_store() == state / "browser" / "signins.json"
        assert skill_import.quarantine_root() == state / "skill-imports"
        assert self._curate_default(monkeypatch) == state

    @staticmethod
    def _curate_default(monkeypatch) -> Path:
        """The tree a `run_curate()` with no `state_dir` scans. `scan_ledger`
        is its first use of the path and sits outside any try, so a stub that
        records the argument and stops is the whole pass."""
        seen: list[Path] = []

        class Stop(Exception):
            pass

        def capture(state_dir, **_kwargs):
            seen.append(Path(state_dir))
            raise Stop

        monkeypatch.setattr(curate, "scan_ledger", capture)
        with pytest.raises(Stop):
            curate.run_curate(
                judge=lambda _p: "", scores=lambda _q, _e: {}, notify_fn=lambda *_a: None,
            )
        assert len(seen) == 1
        return seen[0]


class TestTheSignInStoreFollowsTheKnob:
    """`signin.STATE` was bound at import and read no variable (#399): a run
    that had moved its state tree — the verify harness, a preview, this suite
    — still read the owner's real recorded sign-ins while believing it was
    isolated. Asserts paths only; nothing here writes."""

    def test_resolves_at_call_time(self, tmp_path, monkeypatch):
        """`create_app` publishes AISH_STATE_DIR after every module is
        imported, so a value frozen at import is the one thing that would not
        follow it."""
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "first"))
        assert signin._real_store() == tmp_path / "first" / "browser" / "signins.json"
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "second"))
        assert signin._real_store() == tmp_path / "second" / "browser" / "signins.json"

    def test_unset_is_the_path_the_owner_already_has(self, monkeypatch):
        """Byte-identical to the constant it replaces: the records beside his
        browser profile are still where his `/browser` listing reads them."""
        monkeypatch.delenv("AISH_STATE_DIR", raising=False)
        assert (
            signin._real_store()
            == Path.home() / ".local" / "state" / "aish" / "browser" / "signins.json"
        )

    def test_the_suite_never_reaches_the_real_sign_in_store(
        self, tmp_path_factory, monkeypatch
    ):
        """The conftest guard (`no_real_secrets`) redirects the store by
        patching the FUNCTION, so it holds whatever the environment says — the
        test above unsets the knob and the scrub inside any tool result would
        otherwise read the owner's sign-ins. If the name the guard patches
        ever stops being the one the readers call, this is what fails."""
        monkeypatch.delenv("AISH_STATE_DIR", raising=False)
        patched = signin.store()
        assert patched != signin._real_store()
        assert tmp_path_factory.getbasetemp() in patched.parents
        assert not patched.exists()  # empty store: "no sign-ins", reaches nothing
        # And the readers go through it — a reader that spelled the path itself
        # would be exactly the bypass the guard cannot see.
        assert signin.records() == []
        patched.parent.mkdir(parents=True, exist_ok=True)
        patched.write_text(
            '[{"origin": "https://a.test", "url": "https://a.test/login"}]', encoding="utf-8"
        )
        assert signin.origins() == ["https://a.test"]
