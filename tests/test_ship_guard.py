"""The ship guard (`scripts/ship.sh --check`).

`uv tool install` builds the wheel from the WORKING TREE, not from HEAD, so an
uncommitted file ships silently. This is tested by running the REAL script
against REAL throwaway git repos — the shipped guard, not a Python re-statement
of it, which would pass while the script that actually runs was broken.

`--check` exists for exactly this: it runs the preflight and stops before
anything is installed or restarted, so the guard is testable without a build,
a launchctl call, or a live service.
"""

import os
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


# ------------------------------------------------------- install and restart
#
# The rest of the script — install, restart, health — driven for real against
# PATH shims. `uv`, `launchctl`, `netstat` and `curl` are stand-ins that log
# what they were asked and keep the job's loaded state in a file, and HOME is a
# throwaway, so nothing here reaches the real launchd, the real tool env or the
# network. The launchctl stand-in reproduces the two behaviours of the real one
# the script depends on, both observed against a throwaway job: `bootout`
# returns before the job is gone (`print` keeps finding it, and a `bootstrap`
# meanwhile fails with 5), and a not-loaded `bootout` fails with 3.

LAUNCHCTL_SHIM = r"""#!/bin/bash
S="$SHIM_STATE"
echo "launchctl $1" >> "$S/calls"
linger="$(cat "$S/linger" 2>/dev/null || echo 0)"
case "$1" in
    print)
        if [ -f "$S/loaded" ]; then
            printf '\tstate = running\n\tlast exit code = (never exited)\n'; exit 0
        fi
        if [ "$linger" -gt 0 ]; then
            echo $((linger - 1)) > "$S/linger"
            errors="$(cat "$S/print_errors" 2>/dev/null || echo "${SHIM_PRINT_ERRORS:-0}")"
            if [ "$errors" -gt 0 ]; then
                # A failure that is not "not found" — says nothing about the job.
                echo $((errors - 1)) > "$S/print_errors"; echo "some other error" >&2; exit 1
            fi
            printf '\tstate = SIGTERMed\n'; exit 0
        fi
        echo "Could not find service" >&2; exit 113 ;;
    bootout)
        if [ -f "$S/loaded" ]; then
            rm "$S/loaded"; echo "${SHIM_LINGER:-0}" > "$S/linger"; exit 0
        fi
        echo "Boot-out failed: 3: No such process" >&2; exit 3 ;;
    bootstrap)
        if [ -f "$S/loaded" ] || [ "$linger" -gt 0 ] || [ -n "${SHIM_BOOTSTRAP_FAIL:-}" ]; then
            echo "Bootstrap failed: 5: Input/output error" >&2; exit 5
        fi
        touch "$S/loaded"; exit 0 ;;
    kickstart)
        [ -f "$S/loaded" ] || exit 113; exit 0 ;;
esac
exit 0
"""

UV_SHIM = r"""#!/bin/bash
if [ "$1 $2" = "tool install" ]; then
    state=no; [ -f "$SHIM_STATE/loaded" ] && state=yes
    echo "uv tool install loaded=$state" >> "$SHIM_STATE/calls"
    # The terminal going away mid-install: the script is sent SIGHUP.
    [ -n "${SHIM_INSTALL_SIGNAL:-}" ] && kill "-$SHIM_INSTALL_SIGNAL" "$PPID"
    if [ -n "${SHIM_INSTALL_FAIL:-}" ]; then echo "error: the build broke" >&2; exit 1; fi
    mkdir -p "$HOME/.local/bin"
    for exe in aish aish-web; do
        printf '#!/bin/sh\n' > "$HOME/.local/bin/$exe"; chmod +x "$HOME/.local/bin/$exe"
    done
fi
exit 0
"""

NETSTAT_SHIM = r"""#!/bin/bash
[ -f "$SHIM_STATE/loaded" ] && echo "tcp4  0  0  127.0.0.1.8787  *.*  LISTEN"
exit 0
"""

CURL_SHIM = r"""#!/bin/bash
printf '%s' "${SHIM_HTTP:-200}"
"""

# The first sleep can deliver a signal to the script — an interrupt landing
# while it waits for the booted-out job to leave launchd. Otherwise it sleeps.
SLEEP_SHIM = r"""#!/bin/bash
if [ -n "${SHIM_SLEEP_SIGNAL:-}" ] && [ ! -f "$SHIM_STATE/signalled" ]; then
    touch "$SHIM_STATE/signalled"; kill "-$SHIM_SLEEP_SIGNAL" "$PPID"; exit 0
fi
exec /bin/sleep "$@"
"""


@pytest.fixture
def shipenv(repo, tmp_path):
    """Everything outside the checkout that a real ship touches, faked."""
    bin_dir, home, state = tmp_path / "bin", tmp_path / "home", tmp_path / "state"
    for d in (bin_dir, home / "Library" / "LaunchAgents", state):
        d.mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "com.aish.web.plist").write_text("<plist/>\n")
    for name, body in (
        ("launchctl", LAUNCHCTL_SHIM),
        ("uv", UV_SHIM),
        ("netstat", NETSTAT_SHIM),
        ("curl", CURL_SHIM),
        ("sleep", SLEEP_SHIM),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    path = f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/usr/local/bin:/opt/homebrew/bin"
    for name in ("launchctl", "uv", "netstat", "curl", "sleep"):
        # The label is the real one; only PATH order keeps the real launchctl out.
        assert shutil.which(name, path=path) == str(bin_dir / name)
    (state / "loaded").touch()  # the ordinary case: the service is running
    return {"repo": repo, "state": state, "home": home, "path": path}


def ship(env, *, close_stderr=False, **shim_env):
    """Run the real script past the preflight; return it with the effects the
    stand-ins recorded, in order (`print` is a query, so it is left out).

    `close_stderr` starts the script with fd 2 closed, so every write to it
    fails — what a script sees once the terminal it was writing to has gone."""
    result = subprocess.run(
        [str(env["repo"] / "scripts" / "ship.sh"), "--no-test"],
        cwd=env["repo"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if close_stderr else subprocess.PIPE,
        preexec_fn=(lambda: os.close(2)) if close_stderr else None,
        text=True,
        timeout=60,
        env={"HOME": str(env["home"]), "PATH": env["path"], "SHIM_STATE": str(env["state"])}
        | shim_env,
    )
    calls_file = env["state"] / "calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, [c for c in calls if c != "launchctl print"]


class TestShipRestart:
    def test_the_install_runs_with_the_job_out_of_launchd(self, shipenv):
        """While the job is loaded, KeepAlive lets launchd start it against the
        env uv is replacing. Out of launchd for the install, nothing can."""
        result, effects = ship(shipenv)
        assert result.returncode == 0, result.stderr
        assert effects == ["launchctl bootout", "uv tool install loaded=no", "launchctl bootstrap"]
        assert "health (127.0.0.1:8787): HTTP 200" in result.stdout
        assert "shipped" in result.stdout
        assert (shipenv["state"] / "loaded").exists()

    def test_it_waits_for_bootout_to_finish_before_going_on(self, shipenv):
        """`bootout` returns while the process is still exiting, and a
        bootstrap in that window fails — so the job must be seen GONE first."""
        result, _ = ship(shipenv, SHIM_LINGER="3")
        assert result.returncode == 0, result.stderr
        assert (shipenv["state"] / "loaded").exists()

    def test_a_failed_install_brings_the_old_install_back(self, shipenv):
        """A failed install must not become an outage: the job goes back into
        launchd on whatever is on disk, and the ship still says it failed."""
        result, effects = ship(shipenv, SHIM_INSTALL_FAIL="1")
        assert result.returncode != 0
        assert effects == ["launchctl bootout", "uv tool install loaded=no", "launchctl bootstrap"]
        assert (shipenv["state"] / "loaded").exists()
        assert "the build broke" in result.stderr
        assert "health (127.0.0.1:8787): HTTP 200" in result.stdout

    def test_a_job_not_loaded_at_start_is_loaded_after_the_install(self, shipenv):
        (shipenv["state"] / "loaded").unlink()
        result, effects = ship(shipenv)
        assert result.returncode == 0, result.stderr
        assert "was not loaded" in result.stdout
        assert effects[-2:] == ["uv tool install loaded=no", "launchctl bootstrap"]
        assert (shipenv["state"] / "loaded").exists()

    def test_a_failed_bootstrap_says_the_service_is_down_and_how_to_recover(self, shipenv):
        result, _ = ship(shipenv, SHIM_BOOTSTRAP_FAIL="1")
        assert result.returncode == 1
        assert "DOWN" in result.stderr
        plist = shipenv["home"] / "Library" / "LaunchAgents" / "com.aish.web.plist"
        assert f"launchctl bootstrap gui/{os.getuid()} {plist}" in result.stderr

    def test_an_unhealthy_service_reports_what_launchd_says(self, shipenv):
        """What launchctl reports is shown as an observation — the script
        names no cause for it."""
        result, _ = ship(shipenv, SHIM_HTTP="502")
        assert result.returncode == 1
        assert "HTTP 502" in result.stdout
        assert "state = running" in result.stderr

    def test_a_failed_install_with_stderr_gone_still_restores(self, shipenv):
        """Under `set -e` a failing command inside the EXIT trap ends the trap,
        and an `echo >&2` fails once stderr is gone — so a message printed
        before the bootstrap must not be able to stop it."""
        result, effects = ship(shipenv, close_stderr=True, SHIM_INSTALL_FAIL="1")
        assert result.returncode != 0
        assert effects == ["launchctl bootout", "uv tool install loaded=no", "launchctl bootstrap"]
        assert (shipenv["state"] / "loaded").exists()

    def test_a_hangup_during_the_install_still_restores(self, shipenv):
        """The terminal dropping mid-install: SIGHUP, and nowhere to write."""
        result, effects = ship(
            shipenv, close_stderr=True, SHIM_INSTALL_SIGNAL="HUP", SHIM_INSTALL_FAIL="1"
        )
        assert result.returncode != 0
        assert effects == ["launchctl bootout", "uv tool install loaded=no", "launchctl bootstrap"]
        assert (shipenv["state"] / "loaded").exists()

    def test_an_interrupt_while_the_job_is_leaving_waits_before_restoring(self, shipenv):
        """Ctrl-C while bootout is still in flight: bootstrapping at once would
        hit the same 5 the pre-install wait exists for, and leave it down."""
        result, effects = ship(shipenv, SHIM_LINGER="3", SHIM_SLEEP_SIGNAL="INT")
        assert result.returncode == 130
        assert effects == ["launchctl bootout", "launchctl bootstrap"]
        assert (shipenv["state"] / "loaded").exists()
        assert "DOWN" not in result.stderr

    def test_a_print_failure_that_is_not_not_found_is_not_read_as_gone(self, shipenv):
        """Only 113 means the job is gone. Any other failure of `print` says
        nothing, and treating it as gone races the bootstrap."""
        result, _ = ship(shipenv, SHIM_LINGER="3", SHIM_PRINT_ERRORS="1")
        assert result.returncode == 0, result.stderr
        assert (shipenv["state"] / "loaded").exists()
