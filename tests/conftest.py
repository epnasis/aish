"""Suite-wide isolation: Agent construction now scans the global skills and
tools dirs (knowledge_index + the plugin-tool rescan run at every run_task),
so tests must never see the developer's real ~/.config/aish — nor reach the
developer's real phone."""

import pytest

from aish import notify as notify_module
from aish import rule_compiler as rule_compiler_module
from aish import rules as rules_module
from aish import skills as skills_module
from aish import tool_plugins as tool_plugins_module


@pytest.fixture(autouse=True)
def isolated_global_dirs(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(
        skills_module, "GLOBAL_SKILLS_DIR", tmp_path_factory.mktemp("global-skills")
    )
    monkeypatch.setattr(
        skills_module, "GLOBAL_MEMORY_DIR", tmp_path_factory.mktemp("global-memory")
    )
    monkeypatch.setattr(
        tool_plugins_module, "GLOBAL_TOOLS_DIR", tmp_path_factory.mktemp("global-tools")
    )
    # Rules (#191) are evaluated at the top of EVERY task, so the developer's
    # own corpus would otherwise start governing the suite the day they write
    # their first rule — a test failing because of a file outside the repo.
    monkeypatch.setattr(
        rules_module, "GLOBAL_RULES_DIR", tmp_path_factory.mktemp("global-rules")
    )


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
