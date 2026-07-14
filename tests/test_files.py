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
