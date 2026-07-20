import pytest

from aish.approval import (
    check_denied,
    escaping_dirs,
    is_auto_approvable,
    is_read_only,
    is_scratch_delete,
    load_prefixes,
    looks_destructive,
    path_within,
    save_prefix,
    split_chain,
    suggest_prefix,
    unvetted_segments,
)

SAFE = [
    "ls -la",
    "ls",
    "cat /etc/hosts",
    "find /usr/bin -maxdepth 1 -type f | wc -l",
    "grep -r 'pattern' /tmp | head -20",
    "du -sh /tmp",
    "ps aux | grep python | wc -l",
    "man tar",
    "stat -f '%z' file.txt",
    "date",
]

UNSAFE = [
    "rm -rf /",
    "cat foo; rm bar",  # command chaining
    "ls && rm x",
    "ls || rm x",
    "ls > /etc/passwd",  # redirect
    "ls < input",
    "cat `whoami`",  # command substitution
    "find . -delete",  # unsafe flag on safe command
    "find . -exec rm {} +",
    "find . -execdir rm {} +",
    "sort -o /etc/hosts input",
    "sort --output=/etc/hosts input",
    "echo $HOME",  # expansion we can't reason about
    "ls | xargs rm",  # unlisted command in pipeline
    "/tmp/fake/ls",  # path form shadowing the real PATH binary
    "ls 'unbalanced",  # unparseable quoting
    "ls | | wc",  # empty pipeline segment
    "",
    "curl http://example.com",  # network is not read-only
    "echo hi & rm x",  # background chaining
]


@pytest.mark.parametrize("command", SAFE)
def test_safe_commands_auto_approve(command):
    assert is_read_only(command), command


@pytest.mark.parametrize("command", UNSAFE)
def test_unsafe_commands_require_prompt(command):
    assert not is_read_only(command), command


def test_quoted_pipe_falls_back_to_prompt():
    """Raw '|' split breaks quoting — must fail closed, not approve blindly."""
    assert not is_read_only('grep "a|b" file.txt')


class TestChaining:
    def test_and_chain_of_safe_commands_is_read_only(self):
        assert is_read_only("ls -la && du -sh /tmp")
        assert is_read_only("ls || echo none")
        assert is_read_only("ls | wc -l && date")

    def test_chain_with_one_unsafe_segment_fails(self):
        assert not is_read_only("ls && rm -rf /")
        assert not is_read_only("date || curl http://evil")

    def test_single_ampersand_background_fails_closed(self):
        assert split_chain("sleep 100 &") is None
        assert split_chain("ls & rm x") is None

    def test_semicolon_still_forbidden(self):
        assert split_chain("ls; rm x") is None


class TestUserAllowlist:
    def test_prefix_match_per_segment(self, tmp_path):
        assert is_auto_approvable("git status", ["git status"])
        assert is_auto_approvable("git status --short", ["git status"])
        assert not is_auto_approvable("git stash drop", ["git status"])

    def test_every_segment_evaluated_independently(self):
        prefixes = ["git status", "git log"]
        assert is_auto_approvable("git status && git log -5", prefixes)
        assert is_auto_approvable("git log | head -3", prefixes)  # head is read-only
        assert not is_auto_approvable("git status && rm -rf /", prefixes)
        assert not is_auto_approvable("git status | xargs rm", prefixes)

    def test_allowed_prefix_with_forbidden_chars_still_prompts(self):
        assert not is_auto_approvable("git status > /etc/passwd", ["git status"])
        assert not is_auto_approvable("git status; rm x", ["git status"])

    def test_unvetted_segments_lists_only_unknown_parts(self):
        segs = unvetted_segments("git status && cargo build | wc -l", ["git status"])
        assert segs == ["cargo build"]

    def test_suggest_prefix_two_tokens_for_subcommands(self):
        assert suggest_prefix("git status --short") == "git status"
        assert suggest_prefix("brew list") == "brew list"
        assert suggest_prefix("ls -la /tmp") == "ls"
        assert suggest_prefix("cat /etc/hosts") == "cat"

    def test_suggest_prefix_multi_level_clis(self):
        assert suggest_prefix('gh issue create --title "t" --body "b"') == "gh issue create"
        assert suggest_prefix("docker run -d -p 80:80 nginx") == "docker run"
        assert suggest_prefix("npm run dev") == "npm run dev"
        assert suggest_prefix('git commit -m "first commit"') == "git commit"
        assert suggest_prefix("aws s3 ls") == "aws s3 ls"

    def test_suggest_prefix_strips_binary_path(self):
        assert suggest_prefix('/opt/homebrew/bin/gh issue create --title "t"') == "gh issue create"

    def test_suggest_prefix_stops_at_dynamic_arguments(self):
        assert suggest_prefix("gh issue view 20") == "gh issue view"  # depth ceiling
        assert suggest_prefix("git add .") == "git add"
        assert suggest_prefix("kubectl get pods/web-1") == "kubectl get"
        assert suggest_prefix("npm run --silent dev") == "npm run"

    def test_suggest_prefix_keeps_exec_wrapper_script(self):
        # bare 'python' never auto-approves (EXEC_WRAPPERS), so the script
        # name must stay in the suggestion to make a usable rule
        assert suggest_prefix("python manage.py runserver") == "python manage.py"

    def test_save_and_load_roundtrip_with_dedupe(self, tmp_path):
        path = tmp_path / "allow.txt"
        save_prefix(path, "git status")
        save_prefix(path, "git status")  # dedupe
        save_prefix(path, "  brew list  ")  # stripped
        save_prefix(path, "")  # ignored
        assert load_prefixes(path) == ["git status", "brew list"]

    def test_missing_file_loads_empty(self, tmp_path):
        assert load_prefixes(tmp_path / "nope.txt") == []

    def test_prefix_does_not_re_enable_unsafe_flags(self):
        # allow-listing a benign `find` must NOT auto-approve destructive variants
        assert is_auto_approvable("find . -name foo", ["find"])
        assert is_auto_approvable("find . -newer x", ["find"])
        assert not is_auto_approvable("find /important -delete", ["find"])
        assert not is_auto_approvable("find . -exec rm {} +", ["find"])
        assert not is_auto_approvable("sort -o /etc/hosts in", ["sort"])

    def test_bare_interpreter_prefix_does_not_grant_execution(self):
        assert not is_auto_approvable("python -c 'import os'", ["python"])
        assert not is_auto_approvable("bash script.sh", ["bash"])
        assert not is_auto_approvable("xargs rm", ["xargs"])
        # but an explicitly scoped multi-token prefix is honored
        assert is_auto_approvable("python manage.py check", ["python manage.py"])


class TestPathInvocation:
    """A full-path invocation counts as the bare name only when it resolves to
    the exact binary PATH lookup finds — a shadowing or off-PATH binary still
    fails closed."""

    def test_same_binary_by_path_is_read_only(self, tmp_path, monkeypatch):
        binary = tmp_path / "ls"
        binary.write_text("")
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: str(binary))
        assert is_read_only(f"{binary} -la")

    def test_shadowing_path_still_prompts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: "/usr/bin/ls")
        assert not is_read_only(f"{tmp_path}/ls -la")

    def test_binary_missing_from_path_prompts(self, monkeypatch):
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: None)
        assert not is_read_only("/somewhere/ls")

    def test_full_path_matches_saved_prefix(self, monkeypatch):
        monkeypatch.setattr(
            "aish.approval.shutil.which", lambda name: "/opt/homebrew/bin/gh"
        )
        assert is_auto_approvable("/opt/homebrew/bin/gh pr list", ["gh pr list"])
        assert not is_auto_approvable("/opt/homebrew/bin/gh repo delete x", ["gh pr list"])

    def test_symlinked_install_matches(self, tmp_path, monkeypatch):
        real = tmp_path / "cellar" / "gh"
        real.parent.mkdir()
        real.write_text("")
        link = tmp_path / "bin" / "gh"
        link.parent.mkdir()
        link.symlink_to(real)
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: str(link))
        assert is_auto_approvable(f"{real} pr list", ["gh pr list"])

    def test_unsafe_flags_still_checked_after_canonicalization(self, monkeypatch):
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: "/usr/bin/find")
        assert not is_read_only("/usr/bin/find . -delete")

    def test_full_path_interpreter_still_blocked(self, monkeypatch):
        monkeypatch.setattr("aish.approval.shutil.which", lambda name: "/bin/bash")
        assert not is_auto_approvable("/bin/bash -c 'rm x'", ["bash"])


DENIED = [
    "rm -rf /",
    "rm -fr ~/dev",
    "rm -r --force x",
    "sudo rm -rf /tmp/x",
    "/bin/rm -rf x",
    "ls && rm -rf /",
    "shred secrets.txt",
    "srm file",
    "mkfs.ext4 /dev/disk2",
    "dd if=/dev/zero of=/dev/disk2",
    "diskutil eraseDisk APFS X /dev/disk2",
    "git clean -fdx",
    "git push --force origin main",
    "git push -f",
    "nohup rm -rf /tmp/y",
]

ALLOWED_TO_PROMPT = [
    "rm file.txt",  # single non-recursive delete: prompt, not block
    "rm -r somedir",  # recursive without force: prompt
    "dd if=/dev/zero of=image.img",  # writing to a file, not a device
    "diskutil list",
    "git clean -n",  # dry run
    "git push --force-with-lease",  # the safe variant
    "git push origin main",
    "mkfifo /tmp/pipe",  # starts with mk but is not mkfs
]


class TestDenylist:
    def test_unrecoverable_commands_blocked(self):
        for command in DENIED:
            assert check_denied(command), command

    def test_recoverable_commands_still_prompt(self):
        for command in ALLOWED_TO_PROMPT:
            assert check_denied(command) is None, command

    def test_rm_rf_caught_even_in_unparseable_compound(self):
        assert check_denied("echo hi; rm -rf /")  # ';' defeats split_chain
        assert check_denied("$(rm -rf /tmp/x)")

    def test_user_prefixes_extend_denylist(self):
        assert check_denied("dropdb production", ["dropdb"])
        assert check_denied("ls && dropdb production", ["dropdb"])
        assert check_denied("dropdb-tool x", ["dropdb"]) is None  # prefix+space only

    def test_denied_through_shell_wrappers(self):
        # sh -c / bash -c payloads must be inspected, not waved through
        assert check_denied('sh -c "rm -rf /tmp/x"')
        assert check_denied('bash -c "shred -u secret"')
        assert check_denied("bash -c 'mkfs.ext4 /dev/sda1'")

    def test_denied_through_compound_and_redirects(self):
        # any metachar used to defeat split_chain must not defeat the denylist
        assert check_denied("echo hi; shred -u secret")
        assert check_denied("rm -r -f /tmp/x; echo done")
        assert check_denied("dd of=/dev/sda if=/dev/zero; true")
        assert check_denied("mkfs.ext4 /dev/sda1 >/dev/null")
        assert check_denied("git push --force origin main; echo x")

    def test_denied_through_exec_wrappers(self):
        assert check_denied("xargs rm -rf < list")
        assert check_denied("find . -name x -exec rm -rf {} +")
        assert check_denied("env FOO=bar mkfs.ext4 /dev/sda1")

    def test_benign_commands_not_falsely_denied(self):
        # rm/shred appearing as data, not as a verb, must stay allowed
        assert check_denied('git commit -m "cleanup rm -rf logic"') is None
        assert check_denied("echo shredder") is None
        assert check_denied("grep -r mkfs /etc") is None


class TestLooksDestructive:
    def test_flags_destructive_commands(self):
        for command in ("rm x", "sudo ls", "mv a b", "kill 123", "npm i --force",
                        "pkill node", "chmod -R 777 .", "sudo pmset -a hibernatemode 0"):
            assert looks_destructive(command), command

    def test_quiet_on_benign(self):
        for command in ("ls -la", "git status", "cat f | wc -l",
                        "git push --force-with-lease"):
            assert not looks_destructive(command), command

    def test_redirects_and_quoted_gt_do_not_warn(self):
        # regression: 2>/dev/null and '>' inside quoted programs were flagging
        # every read-only diagnostic, breeding approval fatigue
        for command in (
            "swapctl -l 2>/dev/null",
            "pmset -g 2>/dev/null | grep -i sleep",
            'sysctl vm.swapusage 2>/dev/null; echo "---"; ls -lh /var/vm 2>/dev/null',
            "ps -e -o rss,comm | sort -rn | awk '{a[NR]=$1} END{for(i=15;i>=1;i--) print a[i]}'",
            "echo done > out.txt",
            "tar -xf archive.tar",
            "grep -f patterns.txt file",
        ):
            assert not looks_destructive(command), command


class TestRootScoping:
    """Auto-approval confined to session roots: path arguments escaping every
    root force a prompt, even for read-only or allowlisted commands."""

    def approvable(self, command, root, cwd=None, prefixes=(), extra_roots=()):
        return is_auto_approvable(
            command, list(prefixes), cwd=cwd or str(root), roots=[root, *extra_roots]
        )

    def test_relative_paths_inside_root_approve(self, tmp_path):
        (tmp_path / "src").mkdir()
        assert self.approvable("ls src", tmp_path)
        assert self.approvable("grep -r TODO src", tmp_path)
        assert self.approvable("cat README.md", tmp_path)

    def test_absolute_path_outside_root_prompts(self, tmp_path):
        assert not self.approvable("cat /etc/hosts", tmp_path)
        assert not self.approvable("ls /", tmp_path)

    def test_absolute_path_inside_root_approves(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("x")
        assert self.approvable(f"cat {target}", tmp_path)

    def test_dotdot_escape_prompts(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("x")
        assert not self.approvable("cat ../secret.txt", root)
        # .. that stays inside the root is fine
        sub = root / "sub"
        sub.mkdir()
        assert self.approvable("cat ../file", root, cwd=str(sub))

    def test_home_anchored_path_prompts(self, tmp_path):
        assert not self.approvable("ls ~", tmp_path)
        assert not self.approvable("cat ~/.zshrc", tmp_path)

    def test_cwd_outside_roots_kills_auto_approval(self, tmp_path):
        root = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        root.mkdir()
        elsewhere.mkdir()
        # even a bare read-only command: relative paths would resolve outside
        assert not self.approvable("ls", root, cwd=str(elsewhere))

    def test_allowlist_does_not_bypass_scoping(self, tmp_path):
        assert not self.approvable(
            "git log /etc", tmp_path, prefixes=["git log"]
        )
        assert self.approvable("git log .", tmp_path, prefixes=["git log"])

    def test_compound_cd_scoped_like_any_path(self, tmp_path):
        """cd is a safe subshell segment; its path argument obeys root scoping,
        so trusting a directory makes `cd <dir> && ls` auto-approve."""
        root = tmp_path / "project"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        (root / "sub").mkdir()
        assert self.approvable("cd sub && ls", root)
        assert not self.approvable(f"cd {other} && ls", root)
        assert self.approvable(f"cd {other} && ls", root, extra_roots=[other])

    def test_added_root_widens_scope(self, tmp_path):
        root = tmp_path / "project"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        assert not self.approvable(f"ls {other}", root)
        assert self.approvable(f"ls {other}", root, extra_roots=[other])

    def test_flag_value_paths_are_checked(self, tmp_path):
        assert not self.approvable("grep --file=/etc/passwd pattern .", tmp_path)
        # plain flags never trip the scan
        assert self.approvable("ls -la", tmp_path)

    def test_symlink_escape_prompts(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        assert not self.approvable(f"ls {root / 'link'}", root)

    def test_no_scope_args_keeps_old_behavior(self):
        assert is_auto_approvable("cat /etc/hosts", [])


class TestEscapingDirs:
    """escaping_dirs names the out-of-root directories a prompt should offer
    to trust — advisory only, so unresolvable escapes are omitted, never
    guessed."""

    def test_in_root_command_has_no_escapes(self, tmp_path):
        (tmp_path / "src").mkdir()
        assert escaping_dirs("ls src", str(tmp_path), [tmp_path]) == []

    def test_directory_argument_is_offered_directly(self, tmp_path):
        root = tmp_path / "project"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        assert escaping_dirs(f"ls {other}", str(root), [root]) == [str(other)]

    def test_file_argument_offers_its_parent(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        secret = tmp_path / "notes.txt"
        secret.write_text("x")
        assert escaping_dirs(f"cat {secret}", str(root), [root]) == [str(tmp_path)]

    def test_drifted_cwd_is_offered(self, tmp_path):
        root = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        root.mkdir()
        elsewhere.mkdir()
        assert escaping_dirs("ls", str(elsewhere), [root]) == [str(elsewhere)]

    def test_duplicate_escapes_collapse(self, tmp_path):
        root = tmp_path / "project"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        (other / "a.txt").write_text("x")
        (other / "b.txt").write_text("x")
        command = f"cat {other / 'a.txt'} {other / 'b.txt'}"
        assert escaping_dirs(command, str(root), [root]) == [str(other)]

    def test_bare_cd_target_is_offered(self, tmp_path):
        root = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        root.mkdir()
        elsewhere.mkdir()
        assert escaping_dirs(f"cd {elsewhere}", str(root), [root]) == [str(elsewhere)]


class TestScratchWorkspace:
    """Issue #70: writes and deletes are auto-approved ONLY when they resolve
    strictly inside the ephemeral scratch dir. Everything else fails closed."""

    @pytest.fixture
    def scratch(self, tmp_path):
        d = (tmp_path / "aish-scratch").resolve()
        d.mkdir()
        return d

    # --- path_within (write scoping) ---

    def test_write_inside_scratch_is_within(self, scratch):
        assert path_within(str(scratch / "body.md"), str(scratch), scratch)

    def test_write_inside_nested_scratch_is_within(self, scratch):
        assert path_within(str(scratch / "sub" / "body.md"), str(scratch), scratch)

    def test_relative_path_anchored_to_cwd_can_be_within(self, scratch):
        # cwd == scratch, a bare relative name resolves inside it.
        assert path_within("body.md", str(scratch), scratch)

    def test_scratch_dir_itself_is_not_within(self, scratch):
        assert not path_within(str(scratch), str(scratch), scratch)

    def test_write_outside_scratch_is_not_within(self, tmp_path, scratch):
        assert not path_within(str(tmp_path / "elsewhere.txt"), str(tmp_path), scratch)

    def test_dotdot_escape_is_not_within(self, scratch):
        assert not path_within(str(scratch / ".." / "escape.txt"), str(scratch), scratch)

    def test_symlink_escape_is_not_within(self, tmp_path, scratch):
        outside = tmp_path / "outside"
        outside.mkdir()
        (scratch / "link").symlink_to(outside)
        assert not path_within(str(scratch / "link" / "x.txt"), str(scratch), scratch)

    # --- is_scratch_delete (rm scoping) ---

    def test_rm_inside_scratch_auto_approves(self, scratch):
        (scratch / "f.txt").write_text("x")
        assert is_scratch_delete(f"rm {scratch / 'f.txt'}", str(scratch), scratch)

    def test_rm_force_inside_scratch_auto_approves(self, scratch):
        assert is_scratch_delete(f"rm -f {scratch / 'f.txt'}", str(scratch), scratch)

    def test_rm_recursive_inside_scratch_auto_approves(self, scratch):
        (scratch / "sub").mkdir()
        assert is_scratch_delete(f"rm -r {scratch / 'sub'}", str(scratch), scratch)

    def test_rm_rf_inside_scratch_still_prompts(self, scratch):
        # recursive+force stays denylisted even inside scratch — fall through.
        assert not is_scratch_delete(f"rm -rf {scratch / 'sub'}", str(scratch), scratch)

    def test_rm_multiple_all_inside_auto_approves(self, scratch):
        cmd = f"rm {scratch / 'a'} {scratch / 'b'}"
        assert is_scratch_delete(cmd, str(scratch), scratch)

    def test_rm_one_operand_outside_prompts(self, tmp_path, scratch):
        cmd = f"rm {scratch / 'a'} {tmp_path / 'b'}"
        assert not is_scratch_delete(cmd, str(scratch), scratch)

    def test_rm_outside_scratch_prompts(self, tmp_path, scratch):
        assert not is_scratch_delete(f"rm {tmp_path / 'x'}", str(tmp_path), scratch)

    def test_rm_dotdot_escape_prompts(self, scratch):
        assert not is_scratch_delete(f"rm {scratch / '..' / 'x'}", str(scratch), scratch)

    def test_rm_symlink_escape_prompts(self, tmp_path, scratch):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim").write_text("x")
        (scratch / "link").symlink_to(outside)
        assert not is_scratch_delete(f"rm {scratch / 'link' / 'victim'}", str(scratch), scratch)

    def test_rm_scratch_dir_itself_prompts(self, scratch):
        assert not is_scratch_delete(f"rm -r {scratch}", str(scratch), scratch)

    def test_bare_rm_no_operand_prompts(self, scratch):
        assert not is_scratch_delete("rm", str(scratch), scratch)

    def test_sudo_rm_inside_scratch_prompts(self, scratch):
        # wrappers are not stripped — a non-bare-rm verb never auto-approves.
        assert not is_scratch_delete(f"sudo rm {scratch / 'f'}", str(scratch), scratch)

    def test_chained_command_prompts(self, scratch):
        cmd = f"rm {scratch / 'a'} && rm /etc/passwd"
        assert not is_scratch_delete(cmd, str(scratch), scratch)

    def test_non_rm_command_prompts(self, scratch):
        assert not is_scratch_delete(f"cat {scratch / 'a'}", str(scratch), scratch)

    def test_metacharacter_prompts(self, scratch):
        assert not is_scratch_delete(f"rm {scratch / 'a'}; rm /etc/passwd", str(scratch), scratch)
