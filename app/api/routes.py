from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.deps import audit_store, github_client, policy
from app.auth.dependencies import require_auth
from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode, ErrorResponse
from app.github.client import GitHubClient
from app.models.branches import CreateWorkBranchRequest, CreateWorkBranchResponse
from app.models.ci import (
    CIDebugError,
    CIDebugStatusResponse,
    CIStatusResponse,
    FailedCILogResponse,
    GatewayDebugPingResponse,
    RepoDebugPingResponse,
    RerunCIRequest,
    RerunCIResponse,
)
from app.models.commits import CommitFilesRequest, CommitFilesResponse
from app.models.files import (
    FileContentResponse,
    FileRangeResponse,
    ListTreeResponse,
    ReadFileRangeRequest,
    ReadFileRequest,
    ReadFilesRequest,
    ReadFilesResponse,
)
from app.models.pulls import CreatePullRequestRequest, CreatePullRequestResponse
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.services.ci import CIService
from app.services.commits import CommitService
from app.services.files import FileService
from app.services.pulls import PullRequestService
from app.storage.audit import AuditStore

DEBUG_ROUTE_VERSION = "2026-05-31-debug-v1"

debug_router = APIRouter(tags=["Debug"])
router = APIRouter(
    prefix="/repos/{owner}/{repo}",
    tags=["GPT Actions Gateway"],
    dependencies=[Depends(require_auth)],
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)


@debug_router.get(
    "/debug/ping",
    operation_id="debugPing",
    summary="调试网关基础连通性",
    description="不需要鉴权。始终返回 200，用于确认当前对外暴露的网关实例是否已经加载了最新代码。",
    response_model=GatewayDebugPingResponse,
)
async def debug_ping(
    settings: Annotated[Settings, Depends(get_settings)],
) -> GatewayDebugPingResponse:
    return GatewayDebugPingResponse(
        ok=True,
        route="debugPing",
        version=DEBUG_ROUTE_VERSION,
        app_env=settings.app_env,
        public_base_url=settings.public_base_url,
    )


@router.get(
    "/tree",
    operation_id="listTree",
    summary="List repository tree entries or search candidate paths",
    description="Lists repository tree entries at a ref, optionally filtered by path prefix and file extensions. Excludes common generated/vendor directories and returns at most 200 entries.",
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
    extensions: list[str] | None = Query(default=None, description="Optional repeated or comma-separated file extensions, e.g. .py&extensions=.toml."),
    max_results: int = Query(default=200, ge=1, le=200),
) -> ListTreeResponse:
    return await FileService(github, pol, settings).list_tree(owner, repo, ref=ref, path=path, extensions=extensions, max_results=max_results)


@router.post(
    "/files/read",
    operation_id="getFile",
    summary="Read a single text file",
    description="Reads one file from an allowed branch or exact commit SHA. Large files are truncated; binary files return metadata without text content.",
    response_model=FileContentResponse,
)
async def get_file(
    owner: str,
    repo: str,
    request: ReadFileRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileContentResponse:
    return await FileService(github, pol, settings).read_file(owner, repo, request)


@router.post(
    "/files/read-range",
    operation_id="getFileRange",
    summary="Read a specific line range from a text file",
    description="Reads a line range from a file. Useful after getFile reports truncation or when GPT needs a small area of a large file.",
    response_model=FileRangeResponse,
)
async def get_file_range(
    owner: str,
    repo: str,
    request: ReadFileRangeRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileRangeResponse:
    return await FileService(github, pol, settings).read_file_range(owner, repo, request)


@router.post(
    "/files/read-many",
    operation_id="getFiles",
    summary="Read multiple related text files",
    description="Reads up to 20 files with a total response size limit. Use this after listTree/searching paths to gather edit context.",
    response_model=ReadFilesResponse,
)
async def get_files(
    owner: str,
    repo: str,
    request: ReadFilesRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadFilesResponse:
    return await FileService(github, pol, settings).read_files(owner, repo, request)


@router.post(
    "/branches/create-work-branch",
    operation_id="createWorkBranch",
    summary="Create an idempotent gpt/* work branch",
    description="Creates a gpt/* branch from an allowed base branch. Does not overwrite existing branches; idempotency_key safely returns the same branch on retries.",
    response_model=CreateWorkBranchResponse,
)
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


@router.post(
    "/commits/commit-files",
    operation_id="commitFiles",
    summary="Commit multiple text file changes to a gpt/* branch",
    description="Commits up to 20 text files using Git Database API tree→commit→update-ref flow. Requires expected_head_sha and uses force=false to avoid overwriting concurrent changes.",
    response_model=CommitFilesResponse,
)
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


@router.post(
    "/pulls/create",
    operation_id="createPullRequest",
    summary="Create or reuse an open pull request",
    description="Creates a PR from a gpt/* head branch into an allowed base branch. If an open PR already exists for the same head/base, returns the existing PR.",
    response_model=CreatePullRequestResponse,
)
async def create_pull_request(
    owner: str,
    repo: str,
    request: CreatePullRequestRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
) -> CreatePullRequestResponse:
    return await PullRequestService(github, pol).create_pull_request(owner, repo, request)


@router.get(
    "/ci/status",
    operation_id="getCiStatus",
    summary="Get normalized GitHub Actions status",
    description="Queries CI by commit_sha, PR number, or branch. commit_sha is preferred; branch queries are resolved to the current head SHA to avoid stale runs.",
    response_model=CIStatusResponse,
)
async def get_ci_status(
    owner: str,
    repo: str,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    commit_sha: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    pr_number: int | None = Query(default=None, ge=1),
    workflow_id: str | None = Query(default=None, description="Workflow file name or ID, optional."),
    event: str | None = Query(default=None, description="push or pull_request, optional."),
    created_after: str | None = Query(default=None, description="ISO date or datetime used as GitHub created >= filter."),
) -> CIStatusResponse:
    return await CIService(github, pol, settings).get_ci_status(
        owner,
        repo,
        commit_sha=commit_sha,
        branch=branch,
        pr_number=pr_number,
        workflow_id=workflow_id,
        event=event,
        created_after=created_after,
    )


@router.get(
    "/debug/ping",
    operation_id="debugRepoPing",
    summary="调试仓库路由与鉴权链路",
    description="需要鉴权，但不会访问 GitHub。始终返回 200，用于确认 repos 路由、Bearer 鉴权与最新配置是否已生效。",
    response_model=RepoDebugPingResponse,
)
async def debug_repo_ping(
    owner: str,
    repo: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RepoDebugPingResponse:
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


@router.get(
    "/ci/status-debug",
    operation_id="debugGetCiStatus",
    summary="调试 GitHub Actions 状态查询",
    description="始终返回 200。成功时返回标准 CI 状态；失败时把内部错误码、HTTP 状态、建议和详细信息放进 error 字段，便于 GPT Actions 排查查询异常。",
    response_model=CIDebugStatusResponse,
)
async def debug_get_ci_status(
    owner: str,
    repo: str,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    commit_sha: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    pr_number: int | None = Query(default=None, ge=1),
    workflow_id: str | None = Query(default=None, description="Workflow 文件名或 ID，可选。"),
    event: str | None = Query(default=None, description="push 或 pull_request，可选。"),
    created_after: str | None = Query(default=None, description="GitHub created >= 过滤条件，ISO 日期或时间。"),
) -> JSONResponse:
    service = CIService(github, pol, settings)
    response = CIDebugStatusResponse(
        ok=False,
        owner=owner,
        repo=repo,
        commit_sha=commit_sha,
        branch=branch,
        pr_number=pr_number,
        workflow_id=workflow_id,
        event=event,
        created_after=created_after,
        )
    try:
        result = await service.get_ci_status(
            owner,
            repo,
            commit_sha=commit_sha,
            branch=branch,
            pr_number=pr_number,
            workflow_id=workflow_id,
            event=event,
            created_after=created_after,
        )
        response.ok = True
        response.result = result
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    except ApiError as exc:
        response.error = CIDebugError(
            status_code=exc.status_code,
            error_code=str(exc.error_code),
            message=exc.message,
            suggestion=exc.suggestion,
            details=exc.details,
            exception_type=type(exc).__name__,
        )
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    except Exception as exc:
        response.error = CIDebugError(
            status_code=502,
            error_code=str(ErrorCode.GITHUB_ERROR),
            message="CI 状态查询发生未处理异常。",
            suggestion="检查服务日志和外部网络连通性，然后重试同一请求。",
            details={"error": str(exc), "exception_repr": repr(exc)},
            exception_type=type(exc).__name__,
        )
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


@router.get(
    "/ci/failed-log",
    operation_id="getFailedCiLog",
    summary="Get concise failed CI log excerpts",
    description="Downloads failed job logs and returns error summaries, annotations, an excerpt around failure, and final lines. Output is capped by MAX_LOG_BYTES and MAX_LOG_LINES.",
    response_model=FailedCILogResponse,
)
async def get_failed_ci_log(
    owner: str,
    repo: str,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
    run_id: int = Query(..., ge=1),
    run_attempt: int | None = Query(default=None, ge=1),
    job_id: int | None = Query(default=None, ge=1),
    max_lines: int | None = Query(default=None, ge=1),
) -> FailedCILogResponse:
    return await CIService(github, pol, settings).get_failed_ci_log(owner, repo, run_id=run_id, run_attempt=run_attempt, job_id=job_id, max_lines=max_lines)


@router.post(
    "/ci/rerun-failed",
    operation_id="rerunFailedCi",
    summary="Optionally rerun failed CI jobs",
    description="Disabled by default. Requires ALLOW_RERUN_CI=true, Actions: Write permission, and the workflow run must belong to a gpt/* head branch.",
    response_model=RerunCIResponse,
)
async def rerun_failed_ci(
    owner: str,
    repo: str,
    request: RerunCIRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RerunCIResponse:
    return await CIService(github, pol, settings).rerun_failed_ci(owner, repo, request)
