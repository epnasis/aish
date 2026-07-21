import os

from aish import skills as skills_module
from aish.skills import (
    INDEX_MEMORY_MAX,
    INDEX_SKILLS_MAX,
    PREFLIGHT_ENTRY_CHARS,
    PREFLIGHT_TOP,
    RECALL_TOP,
    _parse,
    forget_memory,
    knowledge_index,
    list_skills,
    load_entries,
    load_skill,
    preflight,
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
        legacy = [e for e in entries if e.kind == "memory" and e.path is None]
        assert [e.description for e in legacy] == ["old lesson two", "old lesson one"]
        # names are content slugs (stable, rankable), not position numbers
        assert [e.name for e in legacy] == ["old-lesson-two", "old-lesson-one"]

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


class TestForgetMemory:
    @staticmethod
    def _mem_dir(tmp_path):
        return tmp_path / ".aish" / "memory"

    def test_deletes_named_slug_only(self, tmp_path):
        d = self._mem_dir(tmp_path)
        save_memory("keep this", d, name="keep")
        save_memory("drop this", d, name="drop")
        result = forget_memory("drop", cwd=str(tmp_path))
        assert result.startswith("forgot")
        assert (d / "keep.md").is_file()
        assert not (d / "drop.md").exists()

    def test_invalid_name_rejected(self, tmp_path):
        assert forget_memory("../evil", cwd=str(tmp_path)).startswith("ERROR")
        assert forget_memory("a/b", cwd=str(tmp_path)).startswith("ERROR")
        assert forget_memory("", cwd=str(tmp_path)).startswith("ERROR")

    def test_missing_slug_is_handled(self, tmp_path):
        result = forget_memory("nope", cwd=str(tmp_path))
        assert not result.startswith("ERROR")
        assert "no memory named" in result

    def test_cannot_delete_outside_memory_store(self, tmp_path):
        outside = tmp_path / "secret.md"
        outside.write_text("x")
        # '..' is rejected outright; a plain slug only ever targets
        # <memory_dir>/<slug>.md, never a sibling of the store.
        forget_memory("..", cwd=str(tmp_path))
        forget_memory("secret", cwd=str(tmp_path))
        assert outside.is_file()

    def test_reaches_global_memory(self, tmp_path):
        save_memory("global fact", skills_module.GLOBAL_MEMORY_DIR, name="g")
        assert (skills_module.GLOBAL_MEMORY_DIR / "g.md").is_file()
        assert forget_memory("g", cwd=str(tmp_path)).startswith("forgot")
        assert not (skills_module.GLOBAL_MEMORY_DIR / "g.md").exists()


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


class TestPreflight:
    """Pre-flight retrieval (issue #40): run_task injects matching knowledge
    proactively; oversized skills arm the agent's read gate via `unread`."""

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", tmp_path / "gm")

    def _skill(self, tmp_path, name, body, keywords=""):
        kw = f"keywords: {keywords}\n" if keywords else ""
        write_skill(
            tmp_path / ".aish" / "skills",
            f"{name}.md",
            f"---\nname: {name}\ndescription: playbook for {name}\n{kw}---\n{body}\n",
        )

    def test_reverse_match_on_keyword_in_long_task(self, tmp_path, monkeypatch):
        # A multi-sentence task never satisfies the forward all-words tiers;
        # the entry's keyword appearing IN the task must be enough.
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "web-release", "Steps to release.", keywords="deploy")
        preload = preflight(str(tmp_path), None, "please deploy the new version to the server")
        assert preload.names == ["web-release"]
        assert "[skill: web-release]" in preload.text
        assert "Steps to release." in preload.text

    def test_reverse_match_on_name_respects_word_boundaries(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "gh", "GitHub CLI usage.")
        assert preflight(str(tmp_path), None, "work late into the night").names == []
        assert preflight(str(tmp_path), None, "open a pr with gh please").names == ["gh"]

    def test_dashed_name_matches_spaced_phrase(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "deploy-web", "How to deploy.")
        preload = preflight(str(tmp_path), None, "time to deploy web again")
        assert preload.names == ["deploy-web"]

    def test_fuzzy_only_match_not_injected(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "sweepy", "Run the sweeper.")
        assert preflight(str(tmp_path), None, "sweepi").names == []  # tier 1 only

    def test_top_n_cap_and_order(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        for i in range(PREFLIGHT_TOP + 2):
            self._skill(tmp_path, f"tool{i}", f"About tool{i}.", keywords="widget")
        preload = preflight(str(tmp_path), None, "fix the widget on the page")
        assert len(preload.names) == PREFLIGHT_TOP
        assert preload.names == [f"tool{i}" for i in range(PREFLIGHT_TOP)]

    def test_small_skill_injected_fully(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "tarball", "Use tar czf with care.")
        preload = preflight(str(tmp_path), None, "make a tarball of the project")
        assert "[skill: tarball] playbook for tarball\nUse tar czf with care." in preload.text
        assert preload.unread == []

    def test_reverse_match_on_description_content_word(self, tmp_path, monkeypatch):
        # Issue #41: the Bali regression. The fact lives entirely in the
        # description, the keywords miss the task's synonym ("villa"), and the
        # body is empty — the description word must both trigger the match and
        # appear in the injected block.
        self._isolate(tmp_path, monkeypatch)
        write_skill(
            tmp_path / ".aish" / "memory",
            "hotels-use-trippy.md",
            "---\nname: hotels-use-trippy\n"
            "description: For hotel, villa, or accommodation searches always run trippy.\n"
            "keywords: hotels, bookings\n---\n",
        )
        task = "find me villa in bali with private pool close to beach for 3 adults"
        preload = preflight(str(tmp_path), None, task)
        assert preload.names == ["hotels-use-trippy"]
        assert "always run trippy" in preload.text

    def test_generic_description_word_does_not_fire(self, tmp_path, monkeypatch):
        # Issue #42: "show me photo of BMW i3" preloaded the hotels memory
        # because its description ended "…bookings and photos". Generic task
        # vocabulary in a description must not trigger; topic words still do.
        self._isolate(tmp_path, monkeypatch)
        write_skill(
            tmp_path / ".aish" / "memory",
            "hotels-use-trippy.md",
            "---\nname: hotels-use-trippy\n"
            "description: For hotel or villa searches run trippy for live "
            "bookings and photos.\n---\n",
        )
        assert preflight(str(tmp_path), None, "show me photo of BMW i3").names == []
        assert preflight(str(tmp_path), None, "find a villa on Crete").names == [
            "hotels-use-trippy"
        ]

    def test_keyword_overrides_stopword(self, tmp_path, monkeypatch):
        # Stopwords only mute description prose — an author who WANTS a
        # generic word to trigger puts it in keywords, which are unfiltered.
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "shotwell", "Photo library workflow.", keywords="photo")
        assert preflight(str(tmp_path), None, "organize my photos please").names == [
            "shotwell"
        ]

    def test_description_stopwords_do_not_fire(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        write_skill(
            tmp_path / ".aish" / "memory",
            "glue.md",
            "---\nname: glue\n"
            "description: Always make sure this tool runs when the user asks.\n---\n",
        )
        assert preflight(str(tmp_path), None, "the user asks about this and that").names == []

    def test_keyword_plural_folding(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "stay-finder", "Book stays.", keywords="hotels")
        assert preflight(str(tmp_path), None, "book a hotel in rome for two").names == [
            "stay-finder"
        ]

    def test_oversized_skill_truncated_and_flagged(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        body = "step\n" * 1000  # ~5000 chars > PREFLIGHT_ENTRY_CHARS
        self._skill(tmp_path, "bigplay", body)
        preload = preflight(str(tmp_path), None, "run bigplay now")
        assert preload.unread == ["bigplay"]
        assert "TRUNCATED" in preload.text
        assert 'read_skill("bigplay")' in preload.text
        assert len(preload.text) < len(body)

    def test_memory_never_gated_and_capped(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        body = "fact " * 1000  # ~5000 chars, oversized for a memory too
        write_skill(
            tmp_path / ".aish" / "memory",
            "serverfacts.md",
            f"---\nname: serverfacts\ndescription: server facts\nkeywords: server\n---\n{body}\n",
        )
        preload = preflight(str(tmp_path), None, "restart the server for me")
        assert preload.names == ["serverfacts"]
        assert preload.unread == []
        assert "[memory: serverfacts]" in preload.text
        assert len(preload.text) <= PREFLIGHT_ENTRY_CHARS + 100  # header slack
        assert preload.text.endswith("…")

    def test_total_budget_respected(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        for i in range(PREFLIGHT_TOP):
            self._skill(tmp_path, f"fat{i}", "x" * 2500, keywords="widget")
        budget = 4000
        preload = preflight(str(tmp_path), None, "widget work", char_budget=budget)
        assert preload.text
        assert len(preload.text) <= budget

    def test_no_match_returns_empty_preload(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        self._skill(tmp_path, "tarball", "Use tar.")
        preload = preflight(str(tmp_path), None, "completely unrelated request")
        assert preload.text == ""
        assert preload.names == []
        assert preload.unread == []
