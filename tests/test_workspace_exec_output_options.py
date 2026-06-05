from __future__ import annotations

import time

from app.models.workspaces import WorkspaceExecPwshRequest
from app.workspace.exec import build_pwsh_script, decode_command_result, strip_ansi_escape_sequences


def test_workspace_exec_request_output_options_default_to_utf8_only() -> None:
    request = WorkspaceExecPwshRequest(script="Write-Output ok")

    assert request.plain_output is False
    assert request.utf8_output is True


def test_build_pwsh_script_injects_only_requested_preludes() -> None:
    script = build_pwsh_script("Write-Output 'ok'", plain_output=True, utf8_output=True)

    assert "$PSStyle.OutputRendering = 'PlainText'" in script
    assert "[Console]::OutputEncoding" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script
    assert script.endswith("Write-Output 'ok'")


def test_build_pwsh_script_utf8_prelude_configures_python_child_output() -> None:
    script = build_pwsh_script("python -c \"print('中文')\"", plain_output=False, utf8_output=True)

    assert "[Console]::OutputEncoding" in script
    assert "$OutputEncoding" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script
    assert "$env:PYTHONUTF8 = '1'" in script
    assert script.endswith("python -c \"print('中文')\"")


def test_build_pwsh_script_can_auto_activate_workspace_python_venv() -> None:
    script = build_pwsh_script("python --version", plain_output=False, utf8_output=True, activate_python_venv=True, python_venv_dir=".venv")

    assert "$env:VIRTUAL_ENV = $__resolvedPythonVenv" in script
    assert "Join-Path $__resolvedPythonVenv 'Scripts'" in script
    assert "Join-Path $__resolvedPythonVenv 'bin'" in script
    assert script.endswith("python --version")


def test_build_pwsh_script_does_not_change_default_script() -> None:
    assert build_pwsh_script("Write-Output 'ok'", plain_output=False, utf8_output=False) == "Write-Output 'ok'"


def test_strip_ansi_escape_sequences_removes_display_noise() -> None:
    assert strip_ansi_escape_sequences("\x1b[32;1mMode \x1b[0mName") == "Mode Name"


def test_decode_command_result_strips_ansi_only_when_requested() -> None:
    raw = b"\x1b[32;1mMode \x1b[0mName"

    plain = decode_command_result(0, raw, b"", time.perf_counter(), 10_000, timed_out=False, strip_ansi=True)
    raw_result = decode_command_result(0, raw, b"", time.perf_counter(), 10_000, timed_out=False, strip_ansi=False)

    assert plain.stdout == "Mode Name"
    assert raw_result.stdout == "\x1b[32;1mMode \x1b[0mName"
