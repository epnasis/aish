import os

from aish import skills as skills_module
from aish.skills import INDEX_SKILLS_MAX, _parse, knowledge_index, list_skills, load_skill


def write_skill(directory, filename, content):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content)
    return path


FULL = """---
name: sweepy
description: inbox sweeper
---
# sweepy
Run it as uv run sweepy.
"""

BARE = """# tarball helper
How to build tarballs safely.
"""


class TestParse:
    def test_frontmatter_wins(self, tmp_path):
        path = write_skill(tmp_path, "anything.md", FULL)
        name, description, body = _parse(path)
        assert name == "sweepy"
        assert description == "inbox sweeper"
        assert body.startswith("# sweepy")
        assert "---" not in body

    def test_defaults_from_filename_and_first_line(self, tmp_path):
        path = write_skill(tmp_path, "tar-helper.md", BARE)
        name, description, _ = _parse(path)
        assert name == "tar-helper"
        assert description == "tarball helper"


class TestListAndLoad:
    def test_lists_across_dirs_project_wins(self, tmp_path):
        project = tmp_path / "project"
        globald = tmp_path / "global"
        write_skill(project, "deploy.md", "# project deploy\nproject way")
        write_skill(globald, "deploy.md", "# global deploy\nglobal way")
        write_skill(globald, "other.md", "# other\nx")
        skills = dict(list_skills([project, globald]))
        assert skills["deploy"] == "project deploy"
        assert "other" in skills

    def test_load_by_frontmatter_name(self, tmp_path):
        write_skill(tmp_path, "weird-filename.md", FULL)
        result = load_skill("sweepy", [tmp_path])
        assert result.startswith("[skill: sweepy]")
        assert "uv run sweepy" in result

    def test_unknown_lists_available(self, tmp_path):
        write_skill(tmp_path, "a.md", "# a\nx")
        result = load_skill("nope", [tmp_path])
        assert result.startswith("ERROR")
        assert "Available skills: a" in result

    def test_invalid_name_rejected(self, tmp_path):
        assert load_skill("../etc/passwd", [tmp_path]).startswith("ERROR: invalid")
        assert load_skill("", [tmp_path]).startswith("ERROR: invalid")

    def test_missing_dirs_are_fine(self, tmp_path):
        assert list_skills([tmp_path / "nope"]) == []
        assert "Available skills: none" in load_skill("x", [tmp_path / "nope"])


class TestKnowledgeIndex:
    def test_empty_when_no_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "none")
        assert knowledge_index(str(tmp_path)) == ""

    def test_lists_skills_with_instruction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "global")
        write_skill(tmp_path / ".aish" / "skills", "demo.md", FULL)
        text = knowledge_index(str(tmp_path))
        assert "- sweepy: inbox sweeper" in text
        assert "read_skill" in text

    def test_project_wins_on_name_clash(self, tmp_path, monkeypatch):
        globald = tmp_path / "global"
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", globald)
        write_skill(
            tmp_path / ".aish" / "skills",
            "deploy.md",
            "---\nname: deploy\ndescription: project way\n---\nx",
        )
        write_skill(globald, "deploy.md", "---\nname: deploy\ndescription: global way\n---\nx")
        text = knowledge_index(str(tmp_path))
        assert text.count("- deploy:") == 1
        assert "project way" in text

    def test_caps_globals_by_recency_with_overflow_note(self, tmp_path, monkeypatch):
        globald = tmp_path / "global"
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", globald)
        for i in range(INDEX_SKILLS_MAX + 2):
            path = write_skill(globald, f"skill{i:03d}.md", f"# skill{i:03d}\nbody")
            os.utime(path, (1000 + i, 1000 + i))
        text = knowledge_index(str(tmp_path))
        assert f"- skill{INDEX_SKILLS_MAX + 1:03d}:" in text  # newest kept
        assert "- skill000:" not in text  # oldest dropped
        assert "2 more skills" in text

    def test_project_skills_never_capped_out(self, tmp_path, monkeypatch):
        globald = tmp_path / "global"
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", globald)
        for i in range(INDEX_SKILLS_MAX):
            write_skill(globald, f"g{i:03d}.md", f"# g{i:03d}\nbody")
        write_skill(tmp_path / ".aish" / "skills", "mine.md", "# mine\nbody")
        text = knowledge_index(str(tmp_path))
        assert "- mine:" in text
