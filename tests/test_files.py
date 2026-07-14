from aish.files import commit, is_sensitive_path, plan_edit, plan_write, read_file


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
