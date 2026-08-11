"""The ship guard (`scripts/ship.sh --check`).

`uv tool install` builds the wheel from the WORKING TREE, not from HEAD, so an
uncommitted file ships silently. This is tested by running the REAL script
against REAL throwaway git repos — the shipped guard, not a Python re-statement
of it, which would pass while the script that actually runs was broken.

`--check` exists for exactly this: it runs the preflight and stops before
anything is installed or restarted, so the guard is testable without a build,
a launchctl call, or a live service.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SHIP = REPO / "scripts" / "ship.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not SHIP.exists(), reason="needs git and scripts/ship.sh"
)


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={"HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )


@pytest.fixture
def repo(tmp_path):
    """A throwaway checkout with the real script in it, on `main`, clean."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "aish").mkdir()
    shutil.copy(SHIP, root / "scripts" / "ship.sh")
    (root / "scripts" / "ship.sh").chmod(0o755)
    (root / "aish" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "README.md").write_text("hi\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def check(repo, *args):
    return subprocess.run(
        [str(repo / "scripts" / "ship.sh"), "--check", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


class TestShipGuard:
    def test_a_clean_tree_passes(self, repo):
        result = check(repo)
        assert result.returncode == 0, result.stderr
        assert "preflight passed" in result.stdout

    def test_it_names_the_commit_being_shipped(self, repo):
        """So what went out is identifiable afterwards — the question that
        started this was "which build is live?" and nothing answered it."""
        result = check(repo)
        assert "initial" in result.stdout

    def test_a_modified_tracked_file_refuses(self, repo):
        (repo / "aish" / "__init__.py").write_text("# edited\n")
        result = check(repo)
        assert result.returncode == 1
        assert "refusing to ship" in result.stderr

    def test_an_untracked_file_refuses_too(self, repo):
        """Untracked is the sharper case: hatchling packages files off disk, so
        a new module nobody committed ships exactly like a modified one — and
        `git diff` shows nothing at all."""
        (repo / "aish" / "sneaky.py").write_text("print('shipped')\n")
        result = check(repo)
        assert result.returncode == 1
        assert "refusing to ship" in result.stderr
        assert "sneaky.py" in result.stderr

    def test_the_refusal_says_which_changes_land_in_the_wheel(self, repo):
        """A refusal that does not distinguish a stray README from a modified
        module just trains people to reach for --dirty."""
        (repo / "aish" / "core.py").write_text("x = 1\n")
        (repo / "README.md").write_text("edited\n")
        result = check(repo)
        assert "INSIDE the installed build" in result.stderr
        wheel_section = result.stderr.split("INSIDE the installed build")[1]
        assert "aish/core.py" in wheel_section
        assert "README.md" not in wheel_section

    def test_dirt_outside_the_wheel_still_refuses_but_says_so(self, repo):
        """Still a refusal — "what shipped is what was tested" needs the whole
        tree pinned, not just the packaged part — but it says the wheel is
        unaffected rather than implying a broken build."""
        (repo / "README.md").write_text("edited\n")
        result = check(repo)
        assert result.returncode == 1
        assert "none of them land in the wheel" in result.stderr

    def test_dirty_flag_is_the_deliberate_override(self, repo):
        (repo / "aish" / "__init__.py").write_text("# edited\n")
        result = check(repo, "--dirty")
        assert result.returncode == 0
        assert "uncommitted changes" in result.stdout

    def test_a_branch_warns_without_blocking(self, repo):
        """Shipping a branch to your own machine to try it is legitimate;
        having forgotten to merge is not. Warn, do not refuse."""
        _git(repo, "checkout", "-q", "-b", "feature")
        result = check(repo)
        assert result.returncode == 0
        assert "not 'main'" in result.stdout

    def test_check_installs_nothing(self, repo):
        """The guard must be observable without side effects, or nothing can
        test it and it rots."""
        result = check(repo)
        assert "nothing installed" in result.stdout
        assert not (repo / "dist").exists()

    def test_an_unknown_option_is_refused_not_ignored(self, repo):
        """A typo'd flag silently doing a real ship is the failure mode."""
        result = check(repo, "--no-tests")  # the real flag is --no-test
        assert result.returncode == 2
        assert "unknown option" in result.stderr
