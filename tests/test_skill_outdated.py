"""`aish skill outdated` (#467 slice 4b). The network steps — `_ls_remote`
and `_clone` — are replaced at their seams; everything between them runs."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from aish import cli, skill_import, skill_outdated
from aish import skills as skills_module

URL = "https://github.com/acme/skills"
OLD = "a" * 40
NEW = "b" * 40


def _write_skill(repo, folder="myskill", body="Do the thing."):
    skill = repo / folder
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    # No final newline and a non-English keyword: both change at install, so
    # a raw upstream-vs-lock comparison would call this file changed forever.
    (skill / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: d\nkeywords: demo, zażółć\n---\n{body}"
    )
    (skill / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (skill / "scripts" / "run.sh").chmod(0o755)
    (skill / "logo.png").write_bytes(b"\x89PNG\xff\xfe")


@pytest.fixture
def upstream(approved_skill_source, monkeypatch, tmp_path):
    """Install from the approved source at OLD, then let the test move HEAD."""
    state = SimpleNamespace(head=NEW, asked=[], repo=approved_skill_source.repo,
                            clones=approved_skill_source.clones, commit=OLD)
    monkeypatch.setattr(skill_import, "clone_commit", lambda tmp: state.commit if tmp else None)

    def ls_remote(url):
        state.asked.append(url)
        return state.head

    monkeypatch.setattr(skill_import, "_ls_remote", ls_remote)

    def install(folder="myskill"):
        qroot = tmp_path / "q"
        skill_import.stage_to_disk(URL, folder, root=qroot)
        skill_import.install(folder, root=qroot)
        approved_skill_source.clones.clear()
        state.commit = NEW

    state.install = install
    return state


def _rows():
    return {row.name: row.line for row in skill_outdated.check()}


class TestSkillOutdated:
    def test_the_same_head_is_up_to_date_with_no_clone(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        upstream.head = OLD
        assert _rows() == {"myskill": f"up to date (commit {OLD[:12]})"}
        assert upstream.asked == [URL + ".git"]
        assert upstream.clones == []

    def test_new_commits_with_the_same_files_are_said_so(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        (upstream.repo / "README.md").write_text("elsewhere in the repo\n")
        line = _rows()["myskill"]
        assert line == (
            f"new commits upstream ({OLD[:12]} → {NEW[:12]}); this skill's files "
            f"unchanged ({skill_outdated.NOT_COMPARED})"
        )

    def test_changed_files_are_named(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        skill = upstream.repo / "myskill"
        (skill / "SKILL.md").write_text("---\nname: myskill\ndescription: d\n---\nNew body\n")
        (skill / "scripts" / "run.sh").unlink()
        (skill / "extra.py").write_text("print(1)\n")
        (skill / "logo.png").write_bytes(b"\x89PNG\x00changed")  # binary: not compared
        line = _rows()["myskill"]
        assert line.startswith(f"CHANGED upstream ({OLD[:12]} → {NEW[:12]}): ")
        assert "added extra.py" in line
        assert "removed scripts/run.sh" in line
        assert "modified SKILL.md" in line
        assert "logo.png" not in line
        assert skill_outdated.NOT_COMPARED in line

    def test_an_executable_bit_change_is_named(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        (upstream.repo / "myskill" / "scripts" / "run.sh").chmod(0o644)
        assert "executable bit changed on scripts/run.sh" in _rows()["myskill"]

    def test_a_pre_unification_install_shows_a_newline_only_change(self, upstream):
        """An `aish skill approve` before §0 installed no final newline; its
        recorded hash is of those bytes, and the comparison says what it is."""
        _write_skill(upstream.repo)
        upstream.install()
        tree = skills_module.VENDOR_SKILLS_DIR
        lock = json.loads((tree / "skills.lock").read_text())
        manifest = (tree / "myskill" / "SKILL.md").read_bytes()
        lock["skills"]["myskill"]["files"]["SKILL.md"] = skill_import._sha256(manifest[:-1])
        (tree / "skills.lock").write_text(json.dumps(lock))
        assert "modified SKILL.md (only a final newline added)" in _rows()["myskill"]

    def test_a_folder_gone_upstream_is_said(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        import shutil

        shutil.rmtree(upstream.repo / "myskill")
        assert _rows()["myskill"].endswith("its folder is gone upstream")

    def test_symlinks_upstream_are_said_not_followed(self, upstream, tmp_path):
        _write_skill(upstream.repo)
        upstream.install()
        (tmp_path / "secret").write_text("key\n")
        (upstream.repo / "myskill" / "notes.md").symlink_to(tmp_path / "secret")
        line = _rows()["myskill"]
        assert "holds symlinks (notes.md)" in line and "key" not in line

    def test_no_recorded_review_says_delete_and_import(self, upstream):
        folder = skills_module.VENDOR_SKILLS_DIR / "byhand"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: byhand\ndescription: d\n---\nb\n")
        skill_import.record_import(
            skills_module.VENDOR_SKILLS_DIR, "nocommit", URL, "x", None, {"SKILL.md": "0"}
        )
        (skills_module.VENDOR_SKILLS_DIR / "nocommit").mkdir()
        rows = _rows()
        for name in ("byhand", "nocommit"):
            assert rows[name].startswith("no recorded review")
            assert "delete it and import again" in rows[name]
        assert upstream.asked == [] and upstream.clones == []

    def test_a_source_no_longer_approved_is_never_contacted(self, upstream, monkeypatch):
        _write_skill(upstream.repo)
        upstream.install()
        skills_module.SKILL_SOURCES_FILE.write_text("github.com/someone/else\n")

        def tripwire(*_a, **_k):
            raise AssertionError("a process was started for an unapproved source")

        monkeypatch.setattr(skill_import.subprocess, "run", tripwire)
        line = _rows()["myskill"]
        assert line.startswith("source no longer approved — not checked")
        assert upstream.asked == [] and upstream.clones == []

    def test_an_unreachable_source_reports_the_observed_error(self, upstream, monkeypatch):
        _write_skill(upstream.repo)
        upstream.install()

        def fails(url):
            raise skill_import.SkillImportError(f"git ls-remote of {url} failed: 404 here")

        monkeypatch.setattr(skill_import, "_ls_remote", fails)
        assert _rows()["myskill"] == (
            f"could not ask the source: git ls-remote of {URL}.git failed: 404 here"
        )

    def test_one_question_and_one_clone_per_source(self, upstream):
        _write_skill(upstream.repo, "one")
        upstream.install("one")
        _write_skill(upstream.repo, "two")
        upstream.commit = OLD
        upstream.install("two")
        rows = _rows()
        assert set(rows) == {"one", "two"}
        assert upstream.asked == [URL + ".git"]
        assert upstream.clones == [URL + ".git"]

    def test_nothing_installs_and_the_lock_is_not_written(self, upstream):
        _write_skill(upstream.repo)
        upstream.install()
        tree = skills_module.VENDOR_SKILLS_DIR
        before = {p: p.read_bytes() for p in tree.rglob("*") if p.is_file()}
        (upstream.repo / "myskill" / "extra.md").write_text("new\n")
        assert "CHANGED" in _rows()["myskill"]
        assert {p: p.read_bytes() for p in tree.rglob("*") if p.is_file()} == before

    def test_one_failing_skill_costs_only_its_own_row(self, upstream, monkeypatch):
        _write_skill(upstream.repo, "one")
        upstream.install("one")
        _write_skill(upstream.repo, "two")
        upstream.commit = OLD
        upstream.install("two")
        real = skill_import.compare_install
        calls = []

        def first_fails(*args):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return real(*args)

        monkeypatch.setattr(skill_import, "compare_install", first_fails)
        rows = _rows()
        assert rows["one"] == "could not be checked: ValueError: boom"
        assert "this skill's files unchanged" in rows["two"]

    def test_the_terminal_prints_a_row_per_skill(self, upstream, capsys):
        assert cli._skill_cli(["outdated"]) == 0
        assert "(no third-party skills installed)" in capsys.readouterr().out
        _write_skill(upstream.repo)
        upstream.install()
        upstream.head = OLD
        assert cli._skill_cli(["outdated"]) == 0
        out = capsys.readouterr().out
        assert "myskill  up to date" in out and "nothing was installed" in out


class TestTheLsRemoteSeam:
    def _run(self, monkeypatch, returncode, stdout, stderr=""):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr(skill_import.subprocess, "run", fake_run)
        return seen

    def test_redirects_off_no_prompt_and_a_timeout(self, monkeypatch):
        seen = self._run(monkeypatch, 0, f"{NEW}\tHEAD\n")
        assert skill_import._ls_remote(URL + ".git") == NEW
        assert seen["argv"] == [
            "git", "-c", "http.followRedirects=false", "ls-remote", "--", URL + ".git", "HEAD"
        ]
        assert seen["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["kwargs"]["timeout"] == skill_import.LS_REMOTE_TIMEOUT

    def test_a_failure_is_the_observed_error(self, monkeypatch):
        self._run(monkeypatch, 128, "", "fatal: repository moved\n")
        with pytest.raises(skill_import.SkillImportError, match="repository moved"):
            skill_import._ls_remote(URL + ".git")

    def test_an_answer_with_no_head_commit_is_refused(self, monkeypatch):
        self._run(monkeypatch, 0, "not-a-commit\tHEAD\n")
        with pytest.raises(skill_import.SkillImportError, match="no HEAD commit"):
            skill_import._ls_remote(URL + ".git")
