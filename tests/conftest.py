"""Suite-wide isolation: Agent construction now scans the global skills and
tools dirs (knowledge_index + the plugin-tool rescan run at every run_task),
so tests must never see the developer's real ~/.config/aish — nor reach the
developer's real phone."""

import os

import pytest

from aish import browser as browser_module
from aish import notify as notify_module
from aish import ratelimit as ratelimit_module
from aish import rule_compiler as rule_compiler_module
from aish import rules as rules_module
from aish import secrets as secrets_module
from aish import signin as signin_module
from aish import skills as skills_module
from aish import tool_plugins as tool_plugins_module


@pytest.fixture(autouse=True)
def isolated_global_dirs(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("config-home")
    # The knob itself (#254), for whatever resolves the config home LATER —
    # config.toml, or a subprocess that imports aish. The four constants below
    # are bound at import, so they are rebound directly rather than left to it.
    monkeypatch.setenv("AISH_CONFIG_HOME", str(home))
    for module, attr, name in (
        (skills_module, "GLOBAL_SKILLS_DIR", "skills"),
        (skills_module, "GLOBAL_MEMORY_DIR", "memory"),
        (tool_plugins_module, "GLOBAL_TOOLS_DIR", "tools"),
        # Rules (#191) are evaluated at the top of EVERY task, so the
        # developer's own corpus would otherwise start governing the suite the
        # day they write their first rule — a test failing because of a file
        # outside the repo.
        (rules_module, "GLOBAL_RULES_DIR", "rules"),
    ):
        directory = home / name
        directory.mkdir()
        monkeypatch.setattr(module, attr, directory)


@pytest.fixture
def project_scope(monkeypatch):
    """Explicit opt-in to project-scope .aish discovery (#178 P0-1).

    OFF by default everywhere — a cloned repo's .aish/{skills,memory,tools}
    must never reach the model's prompt or execute. Tests that exercise the
    project-scope MECHANICS (shadowing, project-wins ordering, project tool
    dirs) request this fixture; the default-off security property itself is
    pinned in test_skills.py / test_tool_plugins.py / test_agent.py.
    """
    monkeypatch.setattr(skills_module, "INCLUDE_PROJECT_DIRS", True)
    monkeypatch.setattr(tool_plugins_module, "INCLUDE_PROJECT_DIRS", True)


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Never push to the developer's real phone.

    `notify.configured()` reads the live macOS Keychain, so any test that runs
    a triggered session to completion with no viewer — the restart-recovery
    tests do exactly that — sent a REAL Pushover notification on every suite
    run. The kill switch short-circuits `pushover()` before it touches
    credentials or the network. Tests that assert on notification behaviour
    still monkeypatch `notify.pushover`/`configured` themselves, which
    overrides this; the one module that exercises the sender itself
    (test_notify) clears the variable explicitly.
    """
    monkeypatch.setenv("AISH_NOTIFY", "0")
    assert not notify_module.enabled()


@pytest.fixture(autouse=True)
def no_real_secrets(tmp_path_factory, monkeypatch):
    """Never read the developer's real login Keychain.

    Same reasoning as the notifier guard, and now on a much hotter path: the
    secret scrub joins against the stored values on EVERY tool result, and the
    name index it starts from is a real file in the developer's state dir. Left
    alone, a suite run would shell out to `security` once per stored secret per
    tool call — reading their live credentials thousands of times to decide
    that a test fixture's output does not contain them.

    Pointing the index at an empty tmp file makes the answer "no secrets
    stored", which costs nothing and reaches nothing. test_secrets patches the
    same constant itself (plus the `security` binary), so it still exercises
    the store; that patch lands inside the test and wins.
    """
    monkeypatch.setattr(
        secrets_module,
        "NAMES_INDEX",
        tmp_path_factory.mktemp("secret-names") / "secret-names.txt",
    )
    # The SAME guard, one store over (#280). Site sign-ins are scrubbed on the
    # same terms as named secrets, so the scrub asks `signin.origins()` on every
    # tool result — and that reads a real file in the developer's state dir,
    # which is how this fixture's own pinning test started shelling out to
    # `security` for their live LinkedIn password. An empty store answers "no
    # sign-ins", costs nothing, and reaches nothing.
    monkeypatch.setattr(
        signin_module,
        "STATE",
        tmp_path_factory.mktemp("signins") / "signins.json",
    )
    secrets_module._invalidate()  # the cache outlives a test; the patch does not
    yield
    secrets_module._invalidate()


@pytest.fixture(autouse=True)
def no_real_browser(tmp_path_factory, monkeypatch):
    """Never launch a real Chrome, and never touch the owner's real profile.

    Same reasoning as the notifier guard: `browser` reaches a live, persistent
    thing outside the process — a profile holding the owner's actual logins —
    so it needs a suite-wide guard rather than per-test discipline. A test that
    reaches the launch path fails loudly here instead of opening a window on
    the developer's desktop, or worse, driving a signed-in session.

    AISH_STATE_DIR is redirected too, so `logins.txt` written by a test can
    never make the real agent gate (or stop gating) a real host.
    """

    class BrowserLaunched(BaseException):
        """BaseException: `_browser_read` swallows Exception to fall back to
        the plain fetch, which would eat an AssertionError and leave the guard
        silent exactly where a test is most likely to be wrong."""

    def refuse(*args, **kwargs):
        raise BrowserLaunched(
            "a test reached the real browser — patch browser.read / "
            "browser.open_for_login instead"
        )

    monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    monkeypatch.setattr(browser_module, "_submit", refuse)
    # `web_search` reads a results page through the browser on EVERY call
    # (#263), so the guard above turned every test that searches into a hard
    # failure — eight of them at once, none of them about the browser. Unlike
    # `read`, this one has an honest "there is no browser here" outcome that
    # the caller is built to handle, and most machines running this suite
    # genuinely have no Chrome. So it is the default, and a test that wants the
    # second index patches `read_cold` itself.
    # Stashed so the one test that is ABOUT read_cold can still reach the real
    # one; monkeypatch removes the attribute again afterwards.
    monkeypatch.setattr(
        browser_module, "_real_read_cold", browser_module.read_cold, raising=False
    )
    monkeypatch.setattr(
        browser_module,
        "read_cold",
        lambda url, **kwargs: (_ for _ in ()).throw(
            browser_module.BrowserUnavailable("no browser in tests")
        ),
    )


@pytest.fixture(autouse=True)
def no_leaked_browser_hosts():
    """`BROWSER_HOSTS` is process-global memory of "this host needed Chrome", and
    nothing in the module ever clears it.

    So a test that reads a host successfully through the patched browser routes
    every LATER test's read of that host through the browser too — which is
    precisely the routing #236 added for signed-in hosts, meaning a leak here can
    make a read look correctly routed for entirely the wrong reason, and mask the
    regression the routing exists to prevent."""
    browser_module.BROWSER_HOSTS.clear()
    yield
    browser_module.BROWSER_HOSTS.clear()


@pytest.fixture(autouse=True)
def no_real_rule_compiler(monkeypatch):
    """The prose→rule compiler talks to a MODEL. Every test today injects
    `rule_compiler_ask`, so nothing reaches a backend — but that is per-test
    discipline, and the notification lesson in CLAUDE.md is that a module which
    reaches outside the process needs a suite-wide guard instead. A test that
    forgets to inject now fails loudly here rather than hanging on a connection
    or, on a developer machine with ollama up, quietly consuming a real model.
    """

    class CompilerReached(BaseException):
        """BaseException, not Exception: `_compiled_fields` treats any
        Exception as "the backend is down" and falls back to named fields, so
        an AssertionError here was swallowed on the request+fields shape and
        the guard went quiet exactly where a test is most likely to be wrong.
        An assertion is never an operational fallback condition."""

    def refuse(model=None):
        raise CompilerReached(
            "a test tried to build the real prose→rule compiler — inject "
            "Agent(rule_compiler_ask=...) or patch rule_compiler.make_compiler"
        )

    monkeypatch.setattr(rule_compiler_module, "make_compiler", refuse)


@pytest.fixture(autouse=True)
def no_real_backoff_sleep(monkeypatch):
    """Never spend real seconds proving that the retry policy waits.

    Same reasoning as the notifier and secrets guards: `ratelimit.wait` is a
    module that really sleeps, sitting on a path any test exercising a failing
    model call reaches — and after #261 those waits are seconds, not the
    instant retry they replaced. Two tests of a dead backend used to cost 3
    seconds of wall clock apiece for nothing.

    The DELAY is still computed and still recorded, so `waited_s` and the
    retry-vs-give-up decision stay under test; only the sleeping is skipped.
    `tests/test_ratelimit.py` captures the real function at import time and
    restores it, which is what keeps the waiting itself covered.
    """
    monkeypatch.setattr(
        ratelimit_module, "wait", lambda delay, stop, note=None: stop.is_set()
    )


@pytest.fixture(autouse=True)
def isolated_rate_governor(monkeypatch):
    """A fresh governor per test.

    It is process-global by design (one API key, many session threads), so
    without this its rolling window and — worse — the ceiling it INFERS from a
    429 outlive the test that caused them. One test exercising a rate limit
    would silently throttle every test after it, and the failure would land
    somewhere unrelated.

    The env overrides are cleared for the same reason in reverse: a developer
    who has stated their real tier must not have the suite's behaviour depend
    on it.
    """
    for name in list(os.environ):
        if name.startswith("AISH_RATE_LIMIT"):
            monkeypatch.delenv(name, raising=False)
    ratelimit_module.reset_governor()
    yield
    ratelimit_module.reset_governor()
