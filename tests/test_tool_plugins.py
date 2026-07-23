"""Unit tests for the plugin-tools layer (TOOL.md discovery/validate/execute).

No model, no network — the executables are tiny local shell wrappers.
"""

import stat

from aish import tool_plugins as tp
from aish.tool_plugins import (
    _parse_tool,
    discover,
    execute,
    resolve_executable,
    signature,
    to_tool_def,
    validate_args,
)

ECHO = "#!/bin/sh\ncat\n"

VALID = """---
name: echoer
description: echo the text back
exec: ./run.sh
mutating: no
schema: {"text": {"type": "string", "required": true}}
---
Echo tool body.
"""


def write_tool(tool_dir, manifest, *, script=ECHO, script_name="run.sh"):
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "TOOL.md").write_text(manifest)
    if script is not None:
        p = tool_dir / script_name
        p.write_text(script)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return tool_dir / "TOOL.md"


class TestParse:
    def test_valid_parses(self, tmp_path):
        manifest = write_tool(tmp_path / "echoer", VALID)
        tool, errors = _parse_tool(manifest)
        assert errors == []
        assert tool.name == "echoer"
        assert tool.mutating is False
        assert tool.timeout == tp.DEFAULT_TIMEOUT
        assert tool.schema["text"]["type"] == "string"

    def test_default_name_from_dir(self, tmp_path):
        manifest = write_tool(
            tmp_path / "namey",
            "---\ndescription: d\nexec: ./run.sh\nmutating: no\n---\nbody",
        )
        tool, errors = _parse_tool(manifest)
        assert errors == []
        assert tool.name == "namey"

    def test_missing_mutating_is_failclosed(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t", "---\nname: t\ndescription: d\nexec: ./run.sh\n---\nb"
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("mutating" in e for e in errors)

    def test_bad_schema_json_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t",
            "---\nname: t\ndescription: d\nexec: ./run.sh\nmutating: no\n"
            "schema: {not json}\n---\nb",
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("schema" in e for e in errors)

    def test_bad_schema_type_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t",
            '---\nname: t\ndescription: d\nexec: ./run.sh\nmutating: no\n'
            'schema: {"x": {"type": "blob"}}\n---\nb',
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("type" in e for e in errors)

    def test_exec_not_resolving_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t",
            "---\nname: t\ndescription: d\nexec: ./nope.sh\nmutating: no\n---\nb",
            script=None,
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("exec" in e for e in errors)

    def test_invalid_name_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t",
            "---\nname: bad name!\ndescription: d\nexec: ./run.sh\nmutating: no\n---\nb",
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("name" in e for e in errors)

    def test_bad_timeout_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "t",
            "---\nname: t\ndescription: d\nexec: ./run.sh\nmutating: no\ntimeout: soon\n---\nb",
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None


class TestResolveExecutable:
    def test_bare_name_on_path(self, tmp_path):
        assert resolve_executable(tmp_path, "sh") is not None

    def test_wrapper_in_dir(self, tmp_path):
        write_tool(tmp_path / "d", VALID)
        assert resolve_executable(tmp_path / "d", "./run.sh") is not None

    def test_absolute_rejected(self, tmp_path):
        assert resolve_executable(tmp_path, "/bin/sh") is None

    def test_escaping_dir_rejected(self, tmp_path):
        d = tmp_path / "d"
        write_tool(d, VALID)
        assert resolve_executable(d, "../run.sh") is None

    def test_non_executable_rejected(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir(parents=True)
        (d / "plain.sh").write_text("hi")  # not chmod +x
        assert resolve_executable(d, "./plain.sh") is None


class TestToolDef:
    def test_shape_and_required(self, tmp_path):
        tool, _ = _parse_tool(write_tool(tmp_path / "echoer", VALID))
        d = to_tool_def(tool)
        assert d["type"] == "function"
        assert d["function"]["name"] == "echoer"
        assert d["function"]["parameters"]["properties"]["text"]["type"] == "string"
        assert d["function"]["parameters"]["required"] == ["text"]


class TestValidateArgs:
    def _tool(self, tmp_path):
        tool, _ = _parse_tool(write_tool(tmp_path / "echoer", VALID))
        return tool

    def test_ok(self, tmp_path):
        assert validate_args(self._tool(tmp_path), {"text": "hi"}) is None

    def test_missing_required(self, tmp_path):
        assert "missing required" in validate_args(self._tool(tmp_path), {})

    def test_unknown_arg(self, tmp_path):
        assert "unknown arg" in validate_args(self._tool(tmp_path), {"text": "x", "nope": 1})

    def test_type_mismatch(self, tmp_path):
        assert "should be string" in validate_args(self._tool(tmp_path), {"text": 5})


class TestExecute:
    def test_echoes_stdin_with_exit_code(self, tmp_path):
        tool, _ = _parse_tool(write_tool(tmp_path / "echoer", VALID))
        out = execute(tool, {"text": "hello"}, cwd=str(tmp_path))
        assert '"text": "hello"' in out
        assert "[exit code: 0]" in out

    def test_nonzero_exit_surfaced(self, tmp_path):
        tool, _ = _parse_tool(
            write_tool(tmp_path / "failer",
                       VALID.replace("echoer", "failer"), script="#!/bin/sh\nexit 3\n")
        )
        assert "[exit code: 3]" in execute(tool, {"text": "x"}, cwd=str(tmp_path))

    def test_timeout(self, tmp_path):
        manifest = write_tool(
            tmp_path / "slow",
            '---\nname: slow\ndescription: d\nexec: ./run.sh\nmutating: no\ntimeout: 1\n'
            'schema: {"text": {"type": "string"}}\n---\nb',
            script="#!/bin/sh\nsleep 3\n",
        )
        tool, _ = _parse_tool(manifest)
        assert "timed out" in execute(tool, {"text": "x"}, cwd=str(tmp_path))


class TestDiscover:
    def test_project_wins_and_warns_invalid(self, tmp_path):
        proj = tmp_path / ".aish" / "tools"
        write_tool(proj / "echoer", VALID)
        write_tool(proj / "broken", "---\nname: broken\ndescription: d\nexec: ./run.sh\n---\nb")
        found, warnings = discover(str(tmp_path))
        names = {t.name for t in found}
        assert "echoer" in names
        assert "broken" not in names
        assert any("mutating" in w for w in warnings)

    def test_signature_moves_on_edit(self, tmp_path):
        manifest = write_tool(tmp_path / ".aish" / "tools" / "echoer", VALID)
        import os
        os.utime(manifest, (2000, 2000))
        sig1 = signature(str(tmp_path))
        os.utime(manifest, (3000, 3000))
        assert signature(str(tmp_path)) != sig1


def test_wraps_parsed(tmp_path):
    manifest = write_tool(
        tmp_path / "t",
        "---\nname: t\ndescription: d\nexec: ./run.sh\nmutating: no\n"
        "wraps: gh issue create\n---\nb",
    )
    tool, errors = _parse_tool(manifest)
    assert errors == []
    assert tool.wraps == "gh issue create"


def test_wraps_defaults_empty(tmp_path):
    manifest = write_tool(tmp_path / "t", VALID)
    tool, _ = _parse_tool(manifest)
    assert tool.wraps == ""


SECRET_TOOL = """---
name: secret_echo
description: echo a secret from env
exec: ./run.sh
mutating: no
secrets: MY_TOKEN
schema: {}
---
Prints the injected MY_TOKEN env var.
"""

SECRET_SCRIPT = '#!/bin/sh\nprintf %s "$MY_TOKEN"\n'


class TestSecretInjection:
    def test_secrets_parsed(self, tmp_path):
        manifest = write_tool(tmp_path / "s", SECRET_TOOL, script=SECRET_SCRIPT)
        tool, errors = _parse_tool(manifest)
        assert errors == []
        assert tool.secrets == ("MY_TOKEN",)

    def test_invalid_secret_name_skipped(self, tmp_path):
        manifest = write_tool(
            tmp_path / "s",
            SECRET_TOOL.replace("secrets: MY_TOKEN", "secrets: bad-name"),
            script=SECRET_SCRIPT,
        )
        tool, errors = _parse_tool(manifest)
        assert tool is None
        assert any("secret name" in e for e in errors)

    def test_secret_injected_into_env(self, tmp_path):
        tool, _ = _parse_tool(write_tool(tmp_path / "s", SECRET_TOOL, script=SECRET_SCRIPT))
        out = execute(tool, {}, cwd=str(tmp_path), get_secret=lambda n: "sk-live-xyz")
        assert "sk-live-xyz" in out
        assert "[exit code: 0]" in out

    def test_missing_secret_errors_without_running(self, tmp_path):
        tool, _ = _parse_tool(write_tool(tmp_path / "s", SECRET_TOOL, script=SECRET_SCRIPT))
        out = execute(tool, {}, cwd=str(tmp_path), get_secret=lambda n: None)
        assert "needs secret 'MY_TOKEN'" in out
        assert "aish secret set MY_TOKEN" in out

    def test_no_secrets_means_default_env(self, tmp_path):
        # a tool with no secrets runs with inherited env (env=None), unchanged
        tool, _ = _parse_tool(write_tool(tmp_path / "e", VALID))
        out = execute(tool, {"text": "hi"}, cwd=str(tmp_path))
        assert "[exit code: 0]" in out
