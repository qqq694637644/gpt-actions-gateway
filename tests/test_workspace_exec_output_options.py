from __future__ import annotations

import time

from app.models.workspaces import WorkspaceExecPwshRequest
from app.workspace.exec import build_pwsh_script, decode_command_result, strip_ansi_escape_sequences


def test_workspace_exec_request_output_options_default_to_off() -> None:
    request = WorkspaceExecPwshRequest(script="Write-Output ok")

    assert request.plain_output is False
    assert request.utf8_output is False


def test_build_pwsh_script_injects_only_requested_preludes() -> None:
    script = build_pwsh_script("Write-Output 'ok'", plain_output=True, utf8_output=True)

    assert "$PSStyle.OutputRendering = 'PlainText'" in script
    assert "[Console]::OutputEncoding" in script
    assert script.endswith("Write-Output 'ok'")


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
