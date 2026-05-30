from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import GatewayBaseModel


class FailedStep(GatewayBaseModel):
    name: str | None = None
    number: int | None = None
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class CIJob(GatewayBaseModel):
    job_id: int
    name: str
    status: str | None = None
    conclusion: str | None = None
    html_url: str | None = None
    failed_steps: list[FailedStep] = Field(default_factory=list)


class CIRun(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    workflow_id: int | str | None = None
    workflow_name: str | None = None
    event: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    jobs: list[CIJob] = Field(default_factory=list)


class CIStatusResponse(GatewayBaseModel):
    matched_by: str
    status: str
    conclusion: str | None = None
    workflow_runs: list[CIRun]
    warning: str | None = None


class CIDebugError(GatewayBaseModel):
    status_code: int
    error_code: str
    message: str
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    exception_type: str | None = None


class CIDebugStatusResponse(GatewayBaseModel):
    ok: bool
    owner: str
    repo: str
    commit_sha: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    workflow_id: str | None = None
    event: str | None = None
    created_after: str | None = None
    result: CIStatusResponse | None = None
    error: CIDebugError | None = None


class GatewayDebugPingResponse(GatewayBaseModel):
    ok: bool
    route: str
    version: str
    app_env: str
    public_base_url: str


class RepoDebugPingResponse(GatewayBaseModel):
    ok: bool
    route: str
    version: str
    owner: str
    repo: str
    app_env: str
    allow_all_repos: bool
    allow_workflow_edit: bool
    allow_rerun_ci: bool


class Annotation(GatewayBaseModel):
    line: int | None = None
    message: str


class FailedJobLog(GatewayBaseModel):
    job_id: int
    job_name: str
    failed_step: str | None = None
    error_summary: list[str]
    annotations: list[Annotation]
    log_excerpt: str
    last_lines: str
    truncated: bool = False


class FailedCILogResponse(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    failed_jobs: list[FailedJobLog]


class RerunCIRequest(GatewayBaseModel):
    run_id: int


class RerunCIResponse(GatewayBaseModel):
    run_id: int
    accepted: bool
    message: str
