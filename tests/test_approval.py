import pytest

from aish.approval import (
    check_denied,
    is_auto_approvable,
    is_read_only,
    load_prefixes,
    looks_destructive,
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
    "/bin/ls",  # path form could shadow the real binary
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

    def test_save_and_load_roundtrip_with_dedupe(self, tmp_path):
        path = tmp_path / "allow.txt"
        save_prefix(path, "git status")
        save_prefix(path, "git status")  # dedupe
        save_prefix(path, "  brew list  ")  # stripped
        save_prefix(path, "")  # ignored
        assert load_prefixes(path) == ["git status", "brew list"]

    def test_missing_file_loads_empty(self, tmp_path):
        assert load_prefixes(tmp_path / "nope.txt") == []


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
