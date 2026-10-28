"""The launchd installer for the daily inbox report (#142 W5), driven for real
with `--print` against a throwaway HOME: a calendar job at 07:30 local that
runs `aish-mailreport`, carries no secret, and changes nothing when printing.
Shellcheck when it is installed."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-mailreport-service.sh"


def printed(tmp_path: Path, *args: str, env_file: bool = False) -> dict:
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    if env_file:
        (home / ".config" / "mm-config").mkdir(parents=True)
        (home / ".config" / "mm-config" / "aish-web.env").write_text("AISH_WEB_TOKEN=secret\n")
        envrun = home / ".local" / "bin" / "envrun"
        envrun.write_text("#!/bin/sh\n")
        envrun.chmod(0o755)
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(["bash", str(SCRIPT), "--print", *args], capture_output=True,
                          text=True, env=env, check=True)
    assert not (home / "Library").exists(), "--print must change nothing"
    assert "secret" not in done.stdout
    return plistlib.loads(done.stdout.encode())


def test_the_job_is_a_daily_calendar_run_at_seven_thirty(tmp_path):
    job = printed(tmp_path, "me@example.org")
    assert job["Label"] == "com.aish.mailreport"
    assert job["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert "StartInterval" not in job and job["RunAtLoad"] is False
    args = job["ProgramArguments"]
    assert args[0].endswith("/.local/bin/aish-mailreport")
    assert list(zip(args, args[1:], strict=False)).count(("--account", "me@example.org")) == 1
    assert "--dry-run" not in args
    assert job["EnvironmentVariables"]["AISH_WEB_URL"].startswith("http://")


def test_the_token_comes_from_the_keychain_or_env_file_never_the_plist(tmp_path):
    job = printed(tmp_path, "me@example.org", env_file=True)
    assert job["ProgramArguments"][0].endswith("/.local/bin/envrun")
    assert job["ProgramArguments"][1].endswith("aish-web.env")
    assert set(job["EnvironmentVariables"]) == {"HOME", "PATH", "AISH_WEB_URL"}


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)
