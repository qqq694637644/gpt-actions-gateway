from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models.common import ChangedFile, GatewayBaseModel, IdempotentRequest


class PrepareWorkspaceRequest(IdempotentRequest):
    branch: str | None = Field(default=None, description="gpt/* branch to prepare for read/write maintenance.")
    source_pr_number: int | None = Field(default=None, ge=1, description="Prepare from this PR head branch.")
    base_ref: str | None = Field(default=None, description="Read-only base branch/ref for investigation.")
    workspace_id: str | None = Field(default=None, min_length=3, max_length=80)
    refresh: bool = True
    clean: bool = False


class PrepareWorkspaceResponse(GatewayBaseModel):
    workspace_id: str
    owner: str
    repo: str
    branch: str
    source_pr_number: int | None = None
    head_sha: str
    default_branch: str
    dirty: bool
    changed_files: list[ChangedFile]
    created: bool
    refreshed: bool


class WorkspaceExecPwshRequest(GatewayBaseModel):
    script: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_output_bytes: int | None = Field(default=None, ge=1)
    allow_network: bool = False


class WorkspaceExecPwshResponse(GatewayBaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
    changed_files: list[ChangedFile]
    diff_stat: str


class WorkspaceStatusRequest(GatewayBaseModel):
    refresh: bool = False


class WorkspaceStatusResponse(GatewayBaseModel):
    workspace_id: str
    branch: str
    head_sha: str
    remote_head_sha: str | None = None
    dirty: bool
    ahead: int = 0
    behind: int = 0
    changed_files: list[ChangedFile]
    untracked_files: list[str]
    conflicts: list[str]


class WorkspaceDiffRequest(GatewayBaseModel):
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    stat_only: bool = False
    max_bytes: int | None = Field(default=None, ge=1)


class WorkspaceDiffResponse(GatewayBaseModel):
    workspace_id: str
    diff: str
    diff_stat: str
    changed_files: list[ChangedFile]
    truncated: bool


class WorkspaceCommitAndPushRequest(IdempotentRequest):
    branch: str
    expected_head_sha: str = Field(min_length=7)
    commit_message: str = Field(min_length=1, max_length=300)
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    dry_run: bool = False


class WorkspaceCommitAndPushResponse(GatewayBaseModel):
    previous_head_sha: str
    new_head_sha: str
    commit_sha: str | None = None
    commit_url: str | None = None
    changed_files: list[ChangedFile]
    diff_stat: str
    pushed: bool
    dry_run: bool


class WorkspaceResetRequest(GatewayBaseModel):
    branch: str
    target: Literal["remote_head"] = "remote_head"
    clean_untracked: bool = True


class WorkspaceResetResponse(GatewayBaseModel):
    workspace_id: str
    branch: str
    head_sha: str
    dirty: bool
    removed_untracked_files: list[str]
