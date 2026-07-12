import pytest

from aish.approval import is_read_only

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
