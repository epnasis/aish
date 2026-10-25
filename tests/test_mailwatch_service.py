"""The launchd installer for the shadow watcher (#142 W5 S1.5), driven for
real with `--print` against a throwaway HOME: it must describe an interval
job over BOTH inboxes that runs `--once`, carries no secret, and changes
nothing when printing. Shellcheck when it is installed."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-mailwatch-service.sh"


def printed(tmp_path: Path, *args: str, env_file: bool = False) -> dict:
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    if env_file:
        (home / ".config" / "mm-config").mkdir(parents=True)
        (home / ".config" / "mm-config" / "aish-web.env").write_text("GEMINI_API_KEY=secret\n")
        envrun = home / ".local" / "bin" / "envrun"
        envrun.write_text("#!/bin/sh\n")
        envrun.chmod(0o755)
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(["bash", str(SCRIPT), "--print", *args], capture_output=True,
                          text=True, env=env, check=True)
    assert not (home / "Library").exists(), "--print must change nothing"
    assert "secret" not in done.stdout
    return plistlib.loads(done.stdout.encode())


def test_the_job_is_an_interval_pass_over_both_inboxes(tmp_path):
    job = printed(tmp_path, "me@example.org", "gemini:flash", "me@tuta.example", "a&b@x.example")
    args = job["ProgramArguments"]
    assert args[0].endswith("/.local/bin/aish-mailwatch") and "--once" in args
    pairs = list(zip(args, args[1:], strict=False))
    assert ("--source", "gmail") in pairs and ("--source", "tuta") in pairs
    assert ("--account", "me@example.org") in pairs and ("--model", "gemini:flash") in pairs
    assert ("--owner", "me@tuta.example") in pairs and ("--owner", "a&b@x.example") in pairs
    assert job["StartInterval"] == 900 and job["RunAtLoad"] is False
    assert job["Label"] == "com.aish.mailwatch"
    assert "/opt/homebrew/bin" in job["EnvironmentVariables"]["PATH"]  # himalaya


def test_provider_keys_come_from_the_env_file_not_the_plist(tmp_path):
    job = printed(tmp_path, "me@example.org", "gemini:flash", env_file=True)
    assert job["ProgramArguments"][0].endswith("/.local/bin/envrun")
    assert job["ProgramArguments"][1].endswith("aish-web.env")
    assert set(job["EnvironmentVariables"]) == {"HOME", "PATH"}


def test_install_refuses_an_account_with_no_credentials(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    done = subprocess.run(["bash", str(SCRIPT), "nobody@example.org", "m"], capture_output=True,
                          text=True, env=env)
    assert done.returncode == 2 and "no credential directory" in done.stderr
    assert not (tmp_path / "Library").exists()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)
