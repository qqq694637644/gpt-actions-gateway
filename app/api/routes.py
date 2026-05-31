from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import audit_store, github_client, policy
from app.auth.dependencies import require_auth
from app.config.settings import Settings, get_settings
from app.errors import ErrorResponse
from app.github.client import GitHubClient
from app.models.branches import ContinueWorkBranchRequest, ContinueWorkBranchResponse, CreateWorkBranchRequest, CreateWorkBranchResponse
from app.models.ci import (
    CIActionAcceptedResponse,
    CIDebugStatusResponse,
    CIStatusQueryRequest,
    CIStatusResponse,
    DispatchWorkflowRequest,
    FailedLogQueryRequest,
    FailedCILogResponse,
    GatewayDebugPingResponse,
    GetCiJobRequest,
    GetCiJobResponse,
    GetCiJobsRequest,
    GetCiJobsResponse,
    GetCiRunRequest,
    GetCiRunResponse,
    GetJobLogRequest,
    GetRunLogRequest,
    GitHubDebugResponse,
    JobLogResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    ReadArtifactTextRequest,
    ReadArtifactTextResponse,
    RepoDebugPingResponse,
    RerunFailedJobsRequest,
    RerunJobRequest,
    RerunWorkflowRunRequest,
    RunLogResponse,
)
from app.models.commits import ApplyPatchAndCommitRequest, ApplyPatchAndCommitResponse, CommitFilesRequest, CommitFilesResponse
from app.models.files import (
    FileContentResponse,
    FileRangeResponse,
    ListTreeResponse,
    ReadFileRangeRequest,
    ReadFileRequest,
    ReadFilesRequest,
    ReadFilesResponse,
)
from app.models.pulls import (
    AddLabelsRequest,
    AddLabelsResponse,
    CommentPullRequestRequest,
    CommentPullRequestResponse,
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    GetPullRequestRequest,
    GetPullRequestResponse,
    ListPullRequestsRequest,
    ListPullRequestsResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    RequestReviewersRequest,
    RequestReviewersResponse,
    UpdatePullRequestRequest,
    UpdatePullRequestResponse,
)
from app.models.repos import (
    CompareRefsRequest,
    CompareRefsResponse,
    EmptyRequest,
    ExportRepoSnapshotRequest,
    ExportRepoSnapshotResponse,
    GetBranchProtectionRequest,
    GetBranchProtectionResponse,
    GetBranchRequest,
    GetBranchResponse,
    GetDefaultBranchResponse,
    GetRepositoryResponse,
    ListBranchesRequest,
    ListBranchesResponse,
    SearchCodeRequest,
    SearchCodeResponse,
)
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.services.ci import CIService
from app.services.commits import CommitService
from app.services.files import FileService
from app.services.pulls import PullRequestService
from app.services.repos import RepoService
from app.storage.audit import AuditStore

DEBUG_ROUTE_VERSION = "2026-05-31-p0-p1-p2"

_error_responses = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

router = APIRouter(
    prefix="/repos/{owner}/{repo}",
    tags=["GPT Actions Gateway"],
    dependencies=[Depends(require_auth)],
    responses=_error_responses,
)
debug_router = APIRouter(tags=["Debug"])
repo_debug_router = APIRouter(
    prefix="/repos/{owner}/{repo}",
    tags=["Debug"],
    dependencies=[Depends(require_auth)],
    responses=_error_responses,
)


@debug_router.get(
    "/debug/ping",
    operation_id="debugPing",
    summary="调试网关基础连通性",
    response_model=GatewayDebugPingResponse,
)
async def debug_ping(settings: Annotated[Settings, Depends(get_settings)]) -> GatewayDebugPingResponse:
    return GatewayDebugPingResponse(ok=True, route="debugPing", version=DEBUG_ROUTE_VERSION, app_env=settings.app_env, public_base_url=settings.public_base_url)


@repo_debug_router.get(
    "/debug/ping",
    operation_id="debugRepoPing",
    summary="调试仓库路由与鉴权链路",
    response_model=RepoDebugPingResponse,
)
async def debug_repo_ping(owner: str, repo: str, settings: Annotated[Settings, Depends(get_settings)]) -> RepoDebugPingResponse:
    return RepoDebugPingResponse(
        ok=True,
        route="debugRepoPing",
        version=DEBUG_ROUTE_VERSION,
        owner=owner,
        repo=repo,
        app_env=settings.app_env,
        allow_all_repos=settings.allow_all_repos,
        allow_workflow_edit=settings.allow_workflow_edit,
        allow_rerun_ci=settings.allow_rerun_ci,
    )


# File and repository read APIs
@router.get(
    "/tree",
    operation_id="listTree",
    summary="List repository tree entries or search candidate paths",
    response_model=ListTreeResponse,
)
async def list_tree(
    owner: str,
    repo: str,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    ref: str = Query(default="main", description="Branch name or exact commit SHA."),
    path: str | None = Query(default=None, description="Optional path prefix."),
    extensions: list[str] | None = Query(default=None, description="Optional repeated or comma-separated file extensions."),
    max_results: int = Query(default=200, ge=1, le=200),
) -> ListTreeResponse:
    return await FileService(github, pol, settings).list_tree(owner, repo, ref=ref, path=path, extensions=extensions, max_results=max_results)


@router.post("/files/read", operation_id="getFile", summary="Read a single text file", response_model=FileContentResponse, include_in_schema=False)
async def get_file(
    owner: str,
    repo: str,
    request: ReadFileRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileContentResponse:
    return await FileService(github, pol, settings).read_file(owner, repo, request)


@router.post("/files/read-range", operation_id="getFileRange", summary="Read a specific line range from a text file", response_model=FileRangeResponse, include_in_schema=False)
async def get_file_range(
    owner: str,
    repo: str,
    request: ReadFileRangeRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileRangeResponse:
    return await FileService(github, pol, settings).read_file_range(owner, repo, request)


@router.post("/files/read-many", operation_id="getFiles", summary="Read multiple related text files", response_model=ReadFilesResponse, include_in_schema=False)
async def get_files(
    owner: str,
    repo: str,
    request: ReadFilesRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadFilesResponse:
    return await FileService(github, pol, settings).read_files(owner, repo, request)


@router.post("/code/search", operation_id="searchCode", summary="Search text code at a ref", response_model=SearchCodeResponse, include_in_schema=False)
async def search_code(
    owner: str,
    repo: str,
    request: SearchCodeRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchCodeResponse:
    return await RepoService(github, pol, settings).search_code(owner, repo, request)


@router.post("/snapshots/export", operation_id="exportRepoSnapshot", summary="Export repository snapshot metadata and optional base64 archive", response_model=ExportRepoSnapshotResponse)
async def export_repo_snapshot(
    owner: str,
    repo: str,
    request: ExportRepoSnapshotRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExportRepoSnapshotResponse:
    return await RepoService(github, pol, settings).export_snapshot(owner, repo, request)


# Branch APIs
@router.post("/branches/create-work-branch", operation_id="createWorkBranch", summary="Create or continue a gpt/* work branch", response_model=CreateWorkBranchResponse)
async def create_work_branch(
    owner: str,
    repo: str,
    request: CreateWorkBranchRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> CreateWorkBranchResponse:
    return await BranchService(github, pol, settings, audit).create_work_branch(owner, repo, request)


@router.post("/branches/continue-work-branch", operation_id="continueWorkBranch", summary="Continue an existing gpt/* work branch", response_model=ContinueWorkBranchResponse)
async def continue_work_branch(
    owner: str,
    repo: str,
    request: ContinueWorkBranchRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> ContinueWorkBranchResponse:
    return await BranchService(github, pol, settings, audit).continue_work_branch(owner, repo, request)


@router.post("/branches/list", operation_id="listBranches", summary="List repository branches", response_model=ListBranchesResponse)
async def list_branches(
    owner: str,
    repo: str,
    request: ListBranchesRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ListBranchesResponse:
    return await RepoService(github, pol, settings).list_branches(owner, repo, request)


@router.post("/branches/get", operation_id="getBranch", summary="Get branch details", response_model=GetBranchResponse)
async def get_branch(
    owner: str,
    repo: str,
    request: GetBranchRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GetBranchResponse:
    return await RepoService(github, pol, settings).get_branch(owner, repo, request)


@router.post("/branches/protection/get", operation_id="getBranchProtection", summary="Get branch protection", response_model=GetBranchProtectionResponse, include_in_schema=False)
async def get_branch_protection(
    owner: str,
    repo: str,
    request: GetBranchProtectionRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GetBranchProtectionResponse:
    return await RepoService(github, pol, settings).get_branch_protection(owner, repo, request)


# Commit / compare APIs
@router.post("/commits/commit-files", operation_id="commitFiles", summary="Commit multiple text file changes to a gpt/* branch", response_model=CommitFilesResponse, include_in_schema=False)
async def commit_files(
    owner: str,
    repo: str,
    request: CommitFilesRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> CommitFilesResponse:
    return await CommitService(github, pol, settings, audit).commit_files(owner, repo, request)


@router.post("/commits/apply-patch", operation_id="applyPatchAndCommit", summary="Apply a git diff patch and commit it to a gpt/* branch", response_model=ApplyPatchAndCommitResponse)
async def apply_patch_and_commit(
    owner: str,
    repo: str,
    request: ApplyPatchAndCommitRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditStore, Depends(audit_store)],
) -> ApplyPatchAndCommitResponse:
    return await CommitService(github, pol, settings, audit).apply_patch_and_commit(owner, repo, request)


@router.post("/compare", operation_id="compareRefs", summary="Compare two refs using GitHub's compare view", response_model=CompareRefsResponse)
async def compare_refs(
    owner: str,
    repo: str,
    request: CompareRefsRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CompareRefsResponse:
    return await RepoService(github, pol, settings).compare_refs(owner, repo, request)


# Pull request APIs
@router.post("/pulls/create", operation_id="createPullRequest", summary="Create or reuse an open pull request", response_model=CreatePullRequestResponse)
async def create_pull_request(owner: str, repo: str, request: CreatePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> CreatePullRequestResponse:
    return await PullRequestService(github, pol).create_pull_request(owner, repo, request)


@router.post("/pulls/get", operation_id="getPullRequest", summary="Get pull request details", response_model=GetPullRequestResponse)
async def get_pull_request(owner: str, repo: str, request: GetPullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> GetPullRequestResponse:
    return await PullRequestService(github, pol).get_pull_request(owner, repo, request)


@router.post("/pulls/list", operation_id="listPullRequests", summary="List pull requests", response_model=ListPullRequestsResponse)
async def list_pull_requests(owner: str, repo: str, request: ListPullRequestsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> ListPullRequestsResponse:
    return await PullRequestService(github, pol).list_pull_requests(owner, repo, request)


@router.post("/pulls/files", operation_id="getPullRequestFiles", summary="List pull request changed files", response_model=PullRequestFilesResponse)
async def get_pull_request_files(owner: str, repo: str, request: PullRequestFilesRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> PullRequestFilesResponse:
    return await PullRequestService(github, pol).get_pull_request_files(owner, repo, request)


@router.post("/pulls/update", operation_id="updatePullRequest", summary="Update pull request title/body/state/base", response_model=UpdatePullRequestResponse)
async def update_pull_request(owner: str, repo: str, request: UpdatePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> UpdatePullRequestResponse:
    return await PullRequestService(github, pol).update_pull_request(owner, repo, request)


@router.post("/pulls/comment", operation_id="commentPullRequest", summary="Comment on a GPT pull request", response_model=CommentPullRequestResponse)
async def comment_pull_request(owner: str, repo: str, request: CommentPullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> CommentPullRequestResponse:
    return await PullRequestService(github, pol).comment_pull_request(owner, repo, request)


@router.post("/pulls/request-reviewers", operation_id="requestReviewers", summary="Request reviewers for a GPT pull request", response_model=RequestReviewersResponse, include_in_schema=False)
async def request_reviewers(owner: str, repo: str, request: RequestReviewersRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> RequestReviewersResponse:
    return await PullRequestService(github, pol).request_reviewers(owner, repo, request)


@router.post("/issues/labels/add", operation_id="addLabels", summary="Add labels to a pull request issue", response_model=AddLabelsResponse, include_in_schema=False)
async def add_labels(owner: str, repo: str, request: AddLabelsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> AddLabelsResponse:
    return await PullRequestService(github, pol).add_labels(owner, repo, request)


@router.post("/pulls/merge", operation_id="mergePullRequest", summary="合并 GPT 创建的拉取请求", response_model=MergePullRequestResponse)
async def merge_pull_request(owner: str, repo: str, request: MergePullRequestRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)]) -> MergePullRequestResponse:
    return await PullRequestService(github, pol).merge_pull_request(owner, repo, request)


# Repository metadata APIs
@router.post("/metadata/get", operation_id="getRepository", summary="Get repository metadata", response_model=GetRepositoryResponse)
async def get_repository(owner: str, repo: str, request: EmptyRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetRepositoryResponse:
    return await RepoService(github, pol, settings).get_repository(owner, repo, request)


@router.post("/metadata/default-branch", operation_id="getDefaultBranch", summary="Get repository default branch", response_model=GetDefaultBranchResponse)
async def get_default_branch(owner: str, repo: str, request: EmptyRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetDefaultBranchResponse:
    return await RepoService(github, pol, settings).get_default_branch(owner, repo, request)


# CI/CD APIs
@router.post("/ci/status/query", operation_id="queryCiStatus", summary="通过 POST 查询 GitHub Actions 状态", response_model=CIStatusResponse)
async def query_ci_status(
    owner: str,
    repo: str,
    request: CIStatusQueryRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CIStatusResponse:
    return await CIService(github, pol, settings).get_ci_status(
        owner,
        repo,
        commit_sha=request.commit_sha,
        branch=request.branch,
        pr_number=request.pr_number,
        workflow_id=request.workflow_id,
        event=request.event,
        created_after=request.created_after,
    )


@router.post("/ci/runs/get", operation_id="getCiRun", summary="Get a workflow run", response_model=GetCiRunResponse)
async def get_ci_run(owner: str, repo: str, request: GetCiRunRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetCiRunResponse:
    return await CIService(github, pol, settings).get_ci_run(owner, repo, request)


@router.post("/ci/jobs/list", operation_id="getCiJobs", summary="List jobs for a workflow run", response_model=GetCiJobsResponse)
async def get_ci_jobs(owner: str, repo: str, request: GetCiJobsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetCiJobsResponse:
    return await CIService(github, pol, settings).get_ci_jobs(owner, repo, request)


@router.post("/ci/jobs/get", operation_id="getCiJob", summary="Get one workflow job", response_model=GetCiJobResponse)
async def get_ci_job(owner: str, repo: str, request: GetCiJobRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> GetCiJobResponse:
    return await CIService(github, pol, settings).get_ci_job(owner, repo, request)


@router.post("/ci/jobs/log", operation_id="getJobLog", summary="Read a workflow job log", response_model=JobLogResponse)
async def get_job_log(owner: str, repo: str, request: GetJobLogRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> JobLogResponse:
    return await CIService(github, pol, settings).get_job_log(owner, repo, request)


@router.post("/ci/runs/log", operation_id="getRunLog", summary="Read workflow run log archive text files", response_model=RunLogResponse)
async def get_run_log(owner: str, repo: str, request: GetRunLogRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> RunLogResponse:
    return await CIService(github, pol, settings).get_run_log(owner, repo, request)


@router.post("/ci/failed-log/query", operation_id="queryFailedCiLog", summary="通过 POST 获取失败 CI 日志摘要", response_model=FailedCILogResponse)
async def query_failed_ci_log(
    owner: str,
    repo: str,
    request: FailedLogQueryRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FailedCILogResponse:
    return await CIService(github, pol, settings).get_failed_ci_log(owner, repo, run_id=request.run_id, run_attempt=request.run_attempt, job_id=request.job_id, max_lines=request.max_lines)


@router.post("/ci/workflows/dispatch", operation_id="dispatchWorkflow", summary="Dispatch a workflow_dispatch workflow", response_model=CIActionAcceptedResponse)
async def dispatch_workflow(owner: str, repo: str, request: DispatchWorkflowRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> CIActionAcceptedResponse:
    return await CIService(github, pol, settings).dispatch_workflow(owner, repo, request)


@router.post("/ci/runs/rerun", operation_id="rerunWorkflowRun", summary="Rerun a workflow run", response_model=CIActionAcceptedResponse)
async def rerun_workflow_run(owner: str, repo: str, request: RerunWorkflowRunRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> CIActionAcceptedResponse:
    return await CIService(github, pol, settings).rerun_workflow_run(owner, repo, request)


@router.post("/ci/runs/rerun-failed", operation_id="rerunFailedJobs", summary="Rerun failed jobs for a workflow run", response_model=CIActionAcceptedResponse)
async def rerun_failed_jobs(owner: str, repo: str, request: RerunFailedJobsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> CIActionAcceptedResponse:
    return await CIService(github, pol, settings).rerun_failed_jobs(owner, repo, request)


@router.post("/ci/jobs/rerun", operation_id="rerunJob", summary="Rerun a workflow job", response_model=CIActionAcceptedResponse)
async def rerun_job(owner: str, repo: str, request: RerunJobRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> CIActionAcceptedResponse:
    return await CIService(github, pol, settings).rerun_job(owner, repo, request)


@router.post("/ci/artifacts/list", operation_id="listArtifacts", summary="List workflow run artifacts", response_model=ListArtifactsResponse)
async def list_artifacts(owner: str, repo: str, request: ListArtifactsRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> ListArtifactsResponse:
    return await CIService(github, pol, settings).list_artifacts(owner, repo, request)


@router.post("/ci/artifacts/read-text", operation_id="readArtifactText", summary="Read text files from an artifact zip", response_model=ReadArtifactTextResponse)
async def read_artifact_text(owner: str, repo: str, request: ReadArtifactTextRequest, github: Annotated[GitHubClient, Depends(github_client)], pol: Annotated[Policy, Depends(policy)], settings: Annotated[Settings, Depends(get_settings)]) -> ReadArtifactTextResponse:
    return await CIService(github, pol, settings).read_artifact_text(owner, repo, request)
