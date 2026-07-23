"""Skill-import staging tests. Uses local-path sources so no git/network."""

import pytest

from aish import skill_import


def make_repo(root):
    skill = root / "myskill"
    (skill).mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: Use when demoing import\n---\nDo the thing.\n"
    )
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (scripts / "run.sh").chmod(0o755)
    (skill / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02\x03\xff\xfe")  # binary
    return root


class TestStage:
    def test_stage_local_repo(self, tmp_path):
        make_repo(tmp_path)
        name, files, skipped, tmp = skill_import.stage(str(tmp_path), "myskill")
        assert tmp == ""  # local source, nothing to clean
        assert name == "myskill"
        rels = {r for r, _, _ in files}
        assert "SKILL.md" in rels
        assert "scripts/run.sh" in rels
        assert skipped == ["logo.png"]  # binary skipped
        # exec bit preserved on the script
        assert any(rel == "scripts/run.sh" and is_exec for rel, _, is_exec in files)

    def test_missing_skill_md(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(skill_import.SkillImportError, match="no SKILL.md"):
            skill_import.stage(str(tmp_path), "empty")

    def test_path_escape_rejected(self, tmp_path):
        make_repo(tmp_path)
        with pytest.raises(skill_import.SkillImportError, match="escapes"):
            skill_import.stage(str(tmp_path / "myskill"), "../../etc")

    def test_nonexistent_source(self, tmp_path):
        with pytest.raises(skill_import.SkillImportError, match="not a directory"):
            skill_import.stage(str(tmp_path / "nope"))

    def test_repo_root_skill(self, tmp_path):
        (tmp_path / "SKILL.md").write_text(
            "---\nname: rootskill\ndescription: at the root\n---\nbody\n"
        )
        name, files, _, _ = skill_import.stage(str(tmp_path))
        assert name == "rootskill"

    def test_url_detection(self):
        assert skill_import.looks_like_url("https://github.com/x/y")
        assert skill_import.looks_like_url("git@github.com:x/y.git")
        assert not skill_import.looks_like_url("/local/path")
