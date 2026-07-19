"""Suite-wide isolation: Agent construction now scans the global skills dir
(knowledge_index runs at every run_task), so tests must never see the
developer's real ~/.config/aish/skills."""

import pytest

from aish import skills as skills_module


@pytest.fixture(autouse=True)
def isolated_global_skills(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(
        skills_module, "GLOBAL_SKILLS_DIR", tmp_path_factory.mktemp("global-skills")
    )
    monkeypatch.setattr(
        skills_module, "GLOBAL_MEMORY_DIR", tmp_path_factory.mktemp("global-memory")
    )
