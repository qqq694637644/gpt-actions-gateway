from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import GatewayBaseModel


class CIStep(GatewayBaseModel):
    name: str | None = None
    number: int | None = None
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class FailedStep(CIStep):
    pass


class CIJob(GatewayBaseModel):
    job_id: int
    run_id: int | None = None
    name: str
    status: str | None = None
    conclusion: str | None = None
    html_url: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    runner_name: str | None = None
    steps: list[CIStep] = Field(default_factory=list)
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


class GitHubDebugResponse(GatewayBaseModel):
    ok: bool
    route: str
    version: str
    owner: str
    repo: str
    params: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] | list[Any] | None = None
    error: CIDebugError | None = None


class WorkflowRunsDebugRequest(GatewayBaseModel):
    head_sha: str | None = None
    branch: str | None = None
    workflow_id: str | None = None
    event: str | None = None


class CIStatusQueryRequest(GatewayBaseModel):
    commit_sha: str | None = None
    branch: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    workflow_id: str | None = None
    event: str | None = None
    created_after: str | None = None


class GetCiRunRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    include_jobs: bool = False


class GetCiRunResponse(GatewayBaseModel):
    workflow_run: CIRun
    run: CIRun


class GetCiJobsRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    run_attempt: int | None = Field(default=None, ge=1)


class GetCiJobsResponse(GatewayBaseModel):
    run_id: int
    run_attempt: int | None = None
    jobs: list[CIJob]
    total_count: int


class GetCiJobRequest(GatewayBaseModel):
    job_id: int = Field(ge=1)


class GetCiJobResponse(GatewayBaseModel):
    job: CIJob


class FailedLogQueryRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    run_attempt: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(default=None, ge=1)


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


class GetJobLogRequest(GatewayBaseModel):
    job_id: int = Field(ge=1)
    step_name: str | None = None
    max_lines: int | None = Field(default=None, ge=1)


class JobLogResponse(GatewayBaseModel):
    job_id: int
    step_name: str | None = None
    log_excerpt: str
    log: str
    last_lines: str
    total_lines: int
    truncated: bool = False


class GetRunLogRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    path_contains: str | None = None
    max_files: int = Field(default=20, ge=1, le=50)
    max_lines_per_file: int | None = Field(default=None, ge=1)


class RunLogFile(GatewayBaseModel):
    path: str
    name: str
    log_excerpt: str
    log: str
    last_lines: str
    total_lines: int
    truncated: bool = False


class RunLogResponse(GatewayBaseModel):
    run_id: int
    files: list[RunLogFile]
    entries: list[RunLogFile]
    truncated: bool = False


class DispatchWorkflowRequest(GatewayBaseModel):
    workflow_id: str
    ref: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class CIActionAcceptedResponse(GatewayBaseModel):
    accepted: bool
    message: str
    run_id: int | None = None
    job_id: int | None = None
    workflow_id: str | None = None


class RerunWorkflowRunRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)


class RerunFailedJobsRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)


class RerunJobRequest(GatewayBaseModel):
    job_id: int = Field(ge=1)


class Artifact(GatewayBaseModel):
    artifact_id: int
    name: str
    size_in_bytes: int | None = None
    archive_download_url: str | None = None
    expired: bool | None = None
    created_at: str | None = None
    expires_at: str | None = None
    updated_at: str | None = None


class ListArtifactsRequest(GatewayBaseModel):
    run_id: int = Field(ge=1)
    max_results: int = Field(default=100, ge=1, le=100)


class ListArtifactsResponse(GatewayBaseModel):
    run_id: int
    artifacts: list[Artifact]
    total_count: int


class ReadArtifactTextRequest(GatewayBaseModel):
    artifact_id: int = Field(ge=1)
    path: str | None = Field(default=None, description="Optional exact path, prefix, or glob inside the artifact zip.")
    max_files: int = Field(default=20, ge=1, le=50)
    max_bytes_per_file: int | None = Field(default=None, ge=1)


class ArtifactTextFile(GatewayBaseModel):
    path: str
    name: str
    content: str
    size: int
    truncated: bool = False


class ReadArtifactTextResponse(GatewayBaseModel):
    artifact_id: int
    files: list[ArtifactTextFile]
    entries: list[ArtifactTextFile]
    total_files: int
    truncated: bool = False
