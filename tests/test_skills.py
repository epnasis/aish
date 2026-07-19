import os

from aish import skills as skills_module
from aish.skills import (
    INDEX_MEMORY_MAX,
    INDEX_SKILLS_MAX,
    RECALL_TOP,
    _parse,
    knowledge_index,
    list_skills,
    load_entries,
    load_skill,
    rank_entries,
    recall_text,
    save_memory,
)


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
        entry = _parse(path)
        assert entry.name == "sweepy"
        assert entry.description == "inbox sweeper"
        assert entry.body.startswith("# sweepy")
        assert "---" not in entry.body

    def test_defaults_from_filename_and_first_line(self, tmp_path):
        path = write_skill(tmp_path, "tar-helper.md", BARE)
        entry = _parse(path)
        assert entry.name == "tar-helper"
        assert entry.description == "tarball helper"

    def test_keywords_parsed(self, tmp_path):
        path = write_skill(
            tmp_path,
            "kw.md",
            "---\nname: kw\ndescription: d\nkeywords: git, release tags\n---\nbody",
        )
        assert _parse(path).keywords == ["git", "release tags"]


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


class TestLoadEntriesAndCache:
    def test_merges_skills_memory_and_lessons(self, tmp_path):
        write_skill(tmp_path / ".aish" / "skills", "s.md", "---\nname: s\ndescription: d\n---\nx")
        write_skill(tmp_path / ".aish" / "memory", "m.md", "---\nname: m\ndescription: fact\n---\n")
        lessons = tmp_path / "lessons.md"
        lessons.write_text("- old lesson one\n- old lesson two\n")
        entries = load_entries(str(tmp_path), lessons)
        kinds = [(e.kind, e.name) for e in entries]
        assert ("skill", "s") in kinds
        assert ("memory", "m") in kinds
        lesson_descriptions = [e.description for e in entries if e.name.startswith("lesson-")]
        assert lesson_descriptions == ["old lesson two", "old lesson one"]  # newest first

    def test_mtime_cache_reparses_only_changed_files(self, tmp_path, monkeypatch):
        d = tmp_path / ".aish" / "skills"
        a = write_skill(d, "a.md", "---\nname: a\ndescription: one\n---\nx")
        write_skill(d, "b.md", "---\nname: b\ndescription: two\n---\nx")
        load_entries(str(tmp_path))  # warm the cache
        calls = []
        real_parse = skills_module._parse

        def counting_parse(path, kind="skill"):
            calls.append(path.name)
            return real_parse(path, kind)

        monkeypatch.setattr(skills_module, "_parse", counting_parse)
        load_entries(str(tmp_path))
        assert calls == []  # all cached
        a.write_text("---\nname: a\ndescription: changed\n---\nx")
        os.utime(a, (9999999999, 9999999999))
        entries = load_entries(str(tmp_path))
        assert calls == ["a.md"]
        assert any(e.description == "changed" for e in entries)


class TestRankEntries:
    def _corpus(self, tmp_path):
        d = tmp_path / ".aish" / "skills"
        write_skill(
            d,
            "gh-issue.md",
            "---\nname: gh-issue\ndescription: Use when opening a GitHub issue\n---\n"
            "run gh issue create",
        )
        write_skill(
            d,
            "deploy.md",
            "---\nname: deploy\ndescription: Use when deploying\nkeywords: release\n---\n"
            "make deploy",
        )
        write_skill(d, "misc.md",
                    "---\nname: misc\ndescription: other things\n---\nnothing relevant here")
        return load_entries(str(tmp_path))

    def test_exact_name_beats_description_beats_body(self, tmp_path):
        entries = self._corpus(tmp_path)
        assert rank_entries(entries, "gh-issue")[0].name == "gh-issue"
        ranked = rank_entries(entries, "github issue")
        assert ranked[0].name == "gh-issue"

    def test_keywords_match(self, tmp_path):
        entries = self._corpus(tmp_path)
        assert rank_entries(entries, "release")[0].name == "deploy"

    def test_body_match_and_no_match(self, tmp_path):
        entries = self._corpus(tmp_path)
        assert rank_entries(entries, "gh issue create")[0].name == "gh-issue"
        assert rank_entries(entries, "zzz qqq") == []

    def test_empty_query_matches_nothing(self, tmp_path):
        assert rank_entries(self._corpus(tmp_path), "  ") == []


class TestRecallText:
    def test_two_phase_and_caps(self, tmp_path):
        d = tmp_path / ".aish" / "skills"
        for i in range(RECALL_TOP + 3):
            write_skill(
                d,
                f"tool{i:02d}.md",
                f"---\nname: tool{i:02d}\ndescription: Use for widget work\n---\nwidget body",
            )
        listing = recall_text(str(tmp_path), None, "widget")
        assert listing.count("[skill]") == RECALL_TOP
        assert "more, weaker matches" in listing
        detail = recall_text(str(tmp_path), None, "widget", name="tool00")
        assert detail.startswith("[skill: tool00]")

    def test_unknown_name_errors_with_hint(self, tmp_path):
        write_skill(tmp_path / ".aish" / "skills", "real.md",
                    "---\nname: real\ndescription: Use for widget work\n---\nx")
        result = recall_text(str(tmp_path), None, "widget", name="fake")
        assert result.startswith("ERROR")
        assert "real" in result

    def test_no_match_suggests_saving_a_skill(self, tmp_path):
        result = recall_text(str(tmp_path), None, "zzz")
        assert "Nothing saved matches" in result
        assert "skill" in result

    def test_sessions_section_appended(self, tmp_path):
        result = recall_text(
            str(tmp_path), None, "widget",
            sessions_search=lambda q: "- session-x · today · widget talk",
        )
        assert "Past sessions that mention it:" in result
        assert "session-x" in result


class TestSaveMemory:
    def test_creates_slug_file_with_frontmatter(self, tmp_path):
        result = save_memory("macOS ps: use ps aux -m", tmp_path / "memory")
        assert result.startswith("remembered")
        files = list((tmp_path / "memory").glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "description: macOS ps: use ps aux -m" in text

    def test_update_by_name_replaces_fact(self, tmp_path):
        d = tmp_path / "memory"
        save_memory("old fact", d, name="thing")
        save_memory("new fact", d, name="thing")
        text = (d / "thing.md").read_text()
        assert "new fact" in text and "old fact" not in text
        assert len(list(d.glob("*.md"))) == 1

    def test_dedupes_same_fact_under_other_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", tmp_path / "memory")
        save_memory("same fact", tmp_path / "memory", name="one", cwd=str(tmp_path))
        result = save_memory("same fact", tmp_path / "memory", name="two", cwd=str(tmp_path))
        assert result == "(already remembered)"

    def test_invalid_name_and_empty_fact_error(self, tmp_path):
        assert save_memory("", tmp_path).startswith("ERROR")
        assert save_memory("x", tmp_path, name="../evil").startswith("ERROR")

    def test_keywords_written(self, tmp_path):
        save_memory("fact", tmp_path / "memory", name="kw", keywords="a, b")
        assert "keywords: a, b" in (tmp_path / "memory" / "kw.md").read_text()


class TestMemoryIndexSection:
    def test_memory_capped_with_overflow_note(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", tmp_path / "gm")
        d = tmp_path / ".aish" / "memory"
        for i in range(INDEX_MEMORY_MAX + 2):
            content = f"---\nname: f{i:03d}\ndescription: fact {i}\n---\n"
            path = write_skill(d, f"f{i:03d}.md", content)
            os.utime(path, (1000 + i, 1000 + i))
        text = knowledge_index(str(tmp_path))
        assert f"fact {INDEX_MEMORY_MAX + 1}" in text  # newest shown
        assert "fact 0" not in text
        assert "recall" in text
