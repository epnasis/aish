"""Directory-picker ignore list: name-level matcher, config load, seeding (#87)."""

import tomllib

from aish import dir_ignore


class TestMatches:
    def test_hides_common_noise_dirs(self):
        pats = list(dir_ignore.DEFAULT_IGNORE)
        for name in ("node_modules", "__pycache__", ".git", "venv", ".venv"):
            assert dir_ignore.matches(name, pats, is_dir=True), name

    def test_keeps_normal_dirs(self):
        pats = list(dir_ignore.DEFAULT_IGNORE)
        for name in ("src", "app", "tests", "my_project", "docs"):
            assert not dir_ignore.matches(name, pats, is_dir=True), name

    def test_fnmatch_glob(self):
        assert dir_ignore.matches("aish.egg-info", ["*.egg-info"], is_dir=True)
        assert not dir_ignore.matches("egg-info.txt", ["*.egg-info"], is_dir=False)

    def test_dotfile_junk_matches_as_file(self):
        # .DS_Store is a file, not a dir; the matcher applies to both.
        assert dir_ignore.matches(".DS_Store", list(dir_ignore.DEFAULT_IGNORE), is_dir=False)

    def test_trailing_slash_means_dirs_only(self):
        assert dir_ignore.matches("build", ["build/"], is_dir=True)
        assert not dir_ignore.matches("build", ["build/"], is_dir=False)

    def test_case_sensitive(self):
        # fnmatchcase — deterministic across platforms, so a differently-cased
        # name is NOT a match.
        assert not dir_ignore.matches("Node_Modules", ["node_modules"], is_dir=True)


class TestSanitize:
    def test_drops_non_list_and_bad_entries(self):
        assert dir_ignore.sanitize("nope") == []
        assert dir_ignore.sanitize(None) == []
        assert dir_ignore.sanitize([".git", "", 5, "  ", " venv "]) == [".git", "venv"]


class TestLoadPatterns:
    def test_missing_section_falls_back_to_defaults(self):
        assert dir_ignore.load_patterns({}) == list(dir_ignore.DEFAULT_IGNORE)

    def test_blank_list_falls_back_to_defaults(self):
        cfg = {"directory_picker": {"ignore": []}}
        assert dir_ignore.load_patterns(cfg) == list(dir_ignore.DEFAULT_IGNORE)

    def test_malformed_section_falls_back_to_defaults(self):
        assert dir_ignore.load_patterns({"directory_picker": "oops"}) == list(
            dir_ignore.DEFAULT_IGNORE
        )

    def test_user_list_wins(self):
        cfg = {"directory_picker": {"ignore": ["node_modules", "*.tmp"]}}
        assert dir_ignore.load_patterns(cfg) == ["node_modules", "*.tmp"]


class TestSeedConfig:
    def test_seeds_missing_config(self, tmp_path):
        path = tmp_path / "config.toml"
        dir_ignore.seed_config(path)
        text = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        assert parsed["directory_picker"]["ignore"] == list(dir_ignore.DEFAULT_IGNORE)

    def test_appends_without_clobbering_existing(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[aliases]\nll = "ls -l"\n', encoding="utf-8")
        dir_ignore.seed_config(path)
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["aliases"]["ll"] == "ls -l"
        assert "node_modules" in parsed["directory_picker"]["ignore"]

    def test_never_overwrites_user_edited_section(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[directory_picker]\nignore = ["only_mine"]\n', encoding="utf-8")
        dir_ignore.seed_config(path)
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["directory_picker"]["ignore"] == ["only_mine"]
