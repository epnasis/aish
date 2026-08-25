import re
from pathlib import Path

from aish.files import (
    commit,
    contains,
    is_outside_roots,
    is_sensitive_path,
    plan_edit,
    plan_write,
    read_file,
    resolved,
    within_roots,
)


class TestIsSensitivePath:
    def test_flags_secret_paths(self):
        for path in (
            "~/.ssh/id_rsa", "~/.ssh/config", "~/.aws/credentials",
            "/home/u/.gnupg/secring", "project/.env", ".env.production",
            "server.pem", "tls.key", "~/.netrc", "certs/store.p12",
        ):
            assert is_sensitive_path(path, "/tmp"), path

    def test_allows_ordinary_paths(self):
        for path in ("README.md", "src/main.py", "notes.txt", "data.json", "environment.rst"):
            assert not is_sensitive_path(path, "/tmp"), path


class TestReadFile:
    def test_numbered_output(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("first\nsecond\n")
        out = read_file(str(f), str(tmp_path))
        assert "1  first" in out
        assert "2  second" in out

    def test_relative_to_cwd(self, tmp_path):
        (tmp_path / "r.txt").write_text("hi")
        assert "hi" in read_file("r.txt", str(tmp_path))

    def test_missing(self, tmp_path):
        assert read_file("nope.txt", str(tmp_path)).startswith("ERROR: no such file")

    def test_empty(self, tmp_path):
        (tmp_path / "e.txt").write_text("")
        assert read_file("e.txt", str(tmp_path)) == "(empty file)"

    def test_offset_and_limit_read_a_range_with_true_line_numbers(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 101)))
        out = read_file(str(f), str(tmp_path), offset=40, limit=3)
        assert "40  line40" in out
        assert "42  line42" in out
        assert "line39" not in out and "line43" not in out
        assert "58 more lines" in out
        assert "offset=43" in out  # continuation hint points at the next line

    def test_offset_past_end_errors(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text("only\n")
        assert read_file(str(f), str(tmp_path), offset=9).startswith("ERROR: offset 9")

    def test_default_read_unchanged_and_truncation_hints_offset(self, tmp_path):
        from aish.files import READ_MAX_LINES

        f = tmp_path / "huge.txt"
        f.write_text("\n".join(f"l{i}" for i in range(1, READ_MAX_LINES + 51)))
        out = read_file(str(f), str(tmp_path))
        assert f"{READ_MAX_LINES}  l{READ_MAX_LINES}" in out
        assert "50 more lines" in out
        assert f"offset={READ_MAX_LINES + 1}" in out


class TestPlanWrite:
    def test_new_file_diff_all_additions(self, tmp_path):
        plan = plan_write("new.py", "print('hi')\n", str(tmp_path))
        assert plan.is_new
        assert plan.added == 1 and plan.removed == 0
        assert "+print('hi')" in plan.diff
        assert commit(plan) == f"created {tmp_path / 'new.py'} (+1 -0 lines)"
        assert (tmp_path / "new.py").read_text() == "print('hi')\n"

    def test_overwrite_shows_add_and_remove(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("old line\n")
        plan = plan_write("x.txt", "new line\n", str(tmp_path))
        assert not plan.is_new
        assert plan.added == 1 and plan.removed == 1
        assert "-old line" in plan.diff and "+new line" in plan.diff

    def test_trailing_newline_added(self, tmp_path):
        plan = plan_write("n.txt", "no newline", str(tmp_path))
        commit(plan)
        assert (tmp_path / "n.txt").read_text() == "no newline\n"

    def test_creates_parent_dirs(self, tmp_path):
        plan = plan_write("sub/deep/f.txt", "x\n", str(tmp_path))
        assert commit(plan).startswith("created")
        assert (tmp_path / "sub" / "deep" / "f.txt").exists()

    def test_directory_target_errors(self, tmp_path):
        (tmp_path / "d").mkdir()
        plan = plan_write("d", "x", str(tmp_path))
        assert plan.error and "directory" in plan.error


class TestPlanEdit:
    def test_unique_replacement(self, tmp_path):
        f = tmp_path / "c.py"
        f.write_text("a = 1\nb = 2\nc = 3\n")
        plan = plan_edit("c.py", "b = 2", "b = 20", str(tmp_path))
        assert plan.error is None
        assert "-b = 2" in plan.diff and "+b = 20" in plan.diff
        commit(plan)
        assert f.read_text() == "a = 1\nb = 20\nc = 3\n"

    def test_missing_string_errors(self, tmp_path):
        (tmp_path / "c.py").write_text("a = 1\n")
        plan = plan_edit("c.py", "nonexistent", "x", str(tmp_path))
        assert plan.error and "not found" in plan.error

    def test_ambiguous_string_errors_with_count(self, tmp_path):
        (tmp_path / "c.py").write_text("x = 1\nx = 1\n")
        plan = plan_edit("c.py", "x = 1", "x = 2", str(tmp_path))
        assert plan.error and "2 times" in plan.error

    def test_nonexistent_file_errors(self, tmp_path):
        plan = plan_edit("gone.py", "a", "b", str(tmp_path))
        assert plan.error and "write_file" in plan.error


class TestEditRescue:
    """edit_file's two rescue layers for the failure loop small models hit:
    pasting read_file's numbered output, or slightly-off indentation."""

    def test_line_number_prefixes_stripped_from_old_and_new(self, tmp_path):
        f = tmp_path / "t.js"
        f.write_text("if (x) {\n  go();\n}\n")
        plan = plan_edit(
            str(f),
            "  518  if (x) {\n  519    go();\n  520  }",
            "  518  if (x) {\n  519    stop();\n  520  }",
            str(tmp_path),
        )
        assert plan.error is None
        assert commit(plan)
        assert f.read_text() == "if (x) {\n  stop();\n}\n"

    def test_wrong_indentation_rescued_when_unique(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("def f():\n        return 1\n")
        plan = plan_edit(str(f), "def f():\n  return 1", "def f():\n  return 2", str(tmp_path))
        assert plan.error is None
        assert "return 2" in plan.new

    def test_ambiguous_relaxed_match_still_errors(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("  x = 1\n    x = 1\n")
        plan = plan_edit(str(f), "x = 1", "x = 2", str(tmp_path))
        assert plan.error is not None  # two stripped-equal locations: no guessing

    def test_not_found_error_names_the_line_number_trap(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("hello\n")
        plan = plan_edit(str(f), "goodbye", "farewell", str(tmp_path))
        assert plan.error and "line-number prefixes" in plan.error


# ---- one path-containment function (#309) ---------------------------------

AISH = Path(__file__).resolve().parent.parent / "aish"

# The containment idioms, by the way they are actually written. `is_relative_to`
# is the modern spelling; `relative_to` inside a try/except ValueError is the
# older one and reads as parsing rather than as a check, which is exactly how it
# survived five separate rewrites of the same question.
CONTAINMENT = {
    "is_relative_to": r"\.is_relative_to\(",
    "relative_to + except ValueError": r"\.relative_to\([^)]*\)\s*\n\s*except ValueError",
    "commonpath": r"\bcommonpath\(",
    "in .parents": r"\bin\s+[\w.()\[\]]+\.parents\b",
}


def _containment_in(paths, allowed=()):
    """{module name: [idioms it hand-rolls]} for everything not exempt."""
    found = {}
    for path in paths:
        if path.name in allowed:
            continue
        text = path.read_text()
        hits = [what for what, pat in CONTAINMENT.items() if re.search(pat, text)]
        if hits:
            found[path.name] = hits
    return found


class TestContains:
    """`files.contains` is the one answer to "is this path inside that one?".

    It resolves BOTH sides — `~` expanded, relatives anchored to cwd, `..` and
    every symlink defused — because the version that did not is what let a
    symlink inside a session root point at /etc/passwd and still auto-approve
    (#178 P1-1). Every property below used to be re-derived per module."""

    def test_a_file_in_the_root_is_inside(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        assert contains(tmp_path, tmp_path / "a.txt")

    def test_a_sibling_is_outside(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("x")
        assert not contains(root, tmp_path / "secret.txt")

    def test_dotdot_is_defused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("x")
        assert not contains(root, f"{root}/../secret.txt")

    def test_a_symlink_out_of_the_root_is_outside(self, tmp_path):
        """The #178 P1-1 shape: the link LIVES in the root, its target does not."""
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        (root / "notes.txt").symlink_to(secret)
        assert not contains(root, root / "notes.txt")

    def test_a_symlink_into_the_root_is_inside(self, tmp_path):
        """Resolution runs on both sides, so a link is judged by where it lands
        and not by where it sits."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("x")
        link = tmp_path / "outside-link.txt"
        link.symlink_to(root / "real.txt")
        assert contains(root, link)

    def test_a_root_reached_through_a_symlink_still_matches(self, tmp_path):
        """/tmp vs /private/tmp on macOS: one directory, two spellings. Resolving
        only the candidate would call every file under it an escape."""
        real = tmp_path / "real-root"
        real.mkdir()
        (real / "a.txt").write_text("x")
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        assert contains(alias, real / "a.txt")

    def test_a_relative_candidate_anchors_to_cwd(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("x")
        assert contains(root, "a.txt", str(root))
        assert not contains(root, "../a.txt", str(root))

    def test_a_tilde_candidate_expands(self):
        assert contains(Path.home(), "~/anything-at-all")

    def test_the_root_itself_is_inside_unless_strict(self, tmp_path):
        assert contains(tmp_path, tmp_path)
        assert not contains(tmp_path, tmp_path, strict=True)

    def test_strict_still_admits_what_is_below(self, tmp_path):
        assert contains(tmp_path, tmp_path / "a" / "b", strict=True)

    def test_a_missing_path_is_still_judged(self, tmp_path):
        """Containment is about WHERE, not about whether the file is there —
        callers check existence themselves, and a write plans a path that does
        not exist yet."""
        assert contains(tmp_path, tmp_path / "not-yet.txt")
        assert not contains(tmp_path, "/nowhere/not-yet.txt")

    def test_an_unspellable_path_is_outside(self, tmp_path):
        """Fail closed. A NUL byte raises ValueError rather than OSError, which
        is why the resolver catches both."""
        assert not contains(tmp_path, "bad\x00name", str(tmp_path))
        assert resolved("bad\x00name", str(tmp_path)) is None


class TestWithinRoots:
    def test_inside_any_root_is_inside(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert within_roots([a, b], b / "x.txt")
        assert not within_roots([a, b], tmp_path / "x.txt")

    def test_no_roots_means_nothing_is_inside(self, tmp_path):
        """The fail-closed reading, and what is_outside_roots has always said."""
        assert not within_roots([], tmp_path / "x.txt")
        assert is_outside_roots(str(tmp_path / "x.txt"), str(tmp_path), [])

    def test_is_outside_roots_still_fails_closed_on_a_bad_path(self, tmp_path):
        assert is_outside_roots("bad\x00name", str(tmp_path), [tmp_path])


class TestOnePathContainment:
    """The containment check lives in `files.py` and nowhere else (#309).

    The file layer resolved symlinks before asking whether a path escaped the
    session roots; the approval parser did not; and export's local-image
    resolution, the /file handler and the scratch check each resolved their own
    way. Four correct implementations and one wrong one is the predictable
    outcome of five, and the wrong one is never the one being read.

    So this is a list, not a habit: a module either asks `files.contains`, or
    is named here with the reason it need not."""

    # The reason is the point — an entry with a bad reason is how this test
    # stops meaning anything.
    ALLOWED = {
        "files.py": "IS the containment test — contains/within_roots live here",
    }

    def _sources(self):
        return sorted(AISH.rglob("*.py"))

    def test_the_sweep_is_not_empty(self):
        """A move or a rename that empties the sweep must fail loudly, not pass."""
        names = {path.name for path in self._sources()}
        assert len(names) >= 30, sorted(names)
        assert {"files.py", "approval.py", "export.py", "server.py"} <= names

    def test_nothing_but_files_rolls_its_own_containment(self):
        offenders = _containment_in(self._sources(), self.ALLOWED)
        assert not offenders, (
            "these answer 'is this path inside that one?' themselves — call "
            "files.contains / files.within_roots, or name the module in ALLOWED "
            f"with why: {offenders}"
        )

    def test_the_allow_list_has_no_stale_entries(self):
        names = {path.name for path in self._sources()}
        gone = sorted(set(self.ALLOWED) - names)
        assert not gone, f"named in ALLOWED but no longer exists: {gone}"

    def test_the_sweep_actually_catches_one(self, tmp_path):
        """A guard that cannot fail is decoration. This plants each idiom #309
        was written about and the sweep has to find every one."""
        planted = tmp_path / "rogue.py"
        planted.write_text(
            "from pathlib import Path\n"
            "def a(root, p):\n"
            "    return Path(p).is_relative_to(root)\n"
            "def b(root, p):\n"
            "    try:\n"
            "        Path(p).relative_to(root)\n"
            "    except ValueError:\n"
            "        return False\n"
            "def c(root, p):\n"
            "    return os.path.commonpath([root, p]) == root\n"
            "def d(root, p):\n"
            "    return root in Path(p).parents\n"
        )
        assert sorted(_containment_in([planted])["rogue.py"]) == sorted(CONTAINMENT)
