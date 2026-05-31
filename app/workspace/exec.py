from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.workspace.git import kill_process_tree
from app.workspace.models import CommandResult
from app.workspace.security import sanitized_environment, validate_script


class PwshExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def execute(self, repo_dir: Path, *, script: str, timeout_seconds: int, max_output_bytes: int, allow_network: bool) -> CommandResult:
        validate_script(script, allow_network=allow_network, settings=self.settings)
        started = time.perf_counter()
        preexec_fn = os.setsid if os.name != "nt" else None
        args = [self.settings.workspace_shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(repo_dir),
                env=sanitized_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                ErrorCode.WORKSPACE_EXEC_FAILED,
                f"Workspace shell was not found: {self.settings.workspace_shell}",
                status_code=500,
                suggestion="Install PowerShell 7+ or set WORKSPACE_SHELL to an approved pwsh executable.",
            ) from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            await kill_process_tree(proc)
            stdout_b, stderr_b = await proc.communicate()
            result = decode_command_result(proc.returncode or -9, stdout_b, stderr_b, started, max_output_bytes, timed_out=True)
            raise ApiError(
                ErrorCode.WORKSPACE_TIMEOUT,
                "PowerShell command timed out and was terminated.",
                status_code=408,
                details={"timeout_seconds": timeout_seconds, "stdout": result.stdout, "stderr": result.stderr},
            ) from exc
        return decode_command_result(proc.returncode or 0, stdout_b, stderr_b, started, max_output_bytes, timed_out=False)


def decode_command_result(exit_code: int, stdout_b: bytes, stderr_b: bytes, started: float, max_output_bytes: int, *, timed_out: bool) -> CommandResult:
    total = len(stdout_b) + len(stderr_b)
    truncated = total > max_output_bytes
    if truncated:
        stdout_limit = max_output_bytes // 2
        stderr_limit = max_output_bytes - stdout_limit
        stdout_b = stdout_b[:stdout_limit]
        stderr_b = stderr_b[:stderr_limit]
    suffix = "\n...[truncated]" if truncated else ""
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout_b.decode("utf-8", errors="replace") + (suffix if truncated and stdout_b else ""),
        stderr=stderr_b.decode("utf-8", errors="replace") + (suffix if truncated and stderr_b else ""),
        duration_ms=round((time.perf_counter() - started) * 1000),
        truncated=truncated,
        timed_out=timed_out,
    )
