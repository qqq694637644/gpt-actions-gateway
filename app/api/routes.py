from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
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
    CIStatusQueryRequest,
    CIStatusResponse,
    FailedLogQueryRequest,
    FailedCILogResponse,
    GatewayDebugPingResponse,
    GitHubDebugResponse,
    RepoDebugPingResponse,
    RerunCIRequest,
    RerunCIResponse,
    WorkflowRunsDebugRequest,
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
from app.models.pulls import (
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
)
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.services.ci import CIService
from app.services.commits import CommitService
from app.services.files import FileService
from app.services.pulls import PullRequestService
from app.storage.audit import AuditStore

DEBUG_ROUTE_VERSION = "2026-05-31-debug-v8"

debug_router = APIRouter(tags=["Debug"])
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


@router.post(
    "/pulls/merge",
    operation_id="mergePullRequest",
    summary="合并 GPT 创建的拉取请求",
    description="默认关闭。需要设置 ALLOW_AUTO_MERGE=true，并且只允许合并头分支为 gpt/*、目标分支位于 BASE_BRANCH_ALLOWLIST 中的 PR。",
    response_model=MergePullRequestResponse,
)
async def merge_pull_request(
    owner: str,
    repo: str,
    request: MergePullRequestRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
) -> MergePullRequestResponse:
    return await PullRequestService(github, pol).merge_pull_request(owner, repo, request)


@router.post(
    "/ci/status/query",
    operation_id="queryCiStatus",
    summary="通过 POST 查询 GitHub Actions 状态",
    description="与 getCiStatus 逻辑相同，但参数通过 JSON body 传递，用于绕过某些动作网关对 GET query string 的处理问题。",
    response_model=CIStatusResponse,
)
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


@repo_debug_router.get(
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


def _debug_error_response(
    *,
    owner: str,
    repo: str,
    route: str,
    params: dict[str, object],
    exc: Exception,
) -> JSONResponse:
    if isinstance(exc, ApiError):
        error = CIDebugError(
            status_code=exc.status_code,
            error_code=str(exc.error_code),
            message=exc.message,
            suggestion=exc.suggestion,
            details=exc.details,
            exception_type=type(exc).__name__,
        )
    else:
        error = CIDebugError(
            status_code=502,
            error_code=str(ErrorCode.GITHUB_ERROR),
            message="GitHub 调试请求发生未处理异常。",
            suggestion="检查服务日志和外部网络连通性，然后重试同一请求。",
            details={"error": str(exc), "exception_repr": repr(exc)},
            exception_type=type(exc).__name__,
        )
    response = GitHubDebugResponse(
        ok=False,
        route=route,
        version=DEBUG_ROUTE_VERSION,
        owner=owner,
        repo=repo,
        params=params,
        error=error,
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


def _summarize_workflow_runs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runs = payload.get("workflow_runs", [])
    return {
        "total_count": payload.get("total_count"),
        "workflow_runs": [
            {
                "id": run.get("id"),
                "name": run.get("name"),
                "event": run.get("event"),
                "head_branch": run.get("head_branch"),
                "head_sha": run.get("head_sha"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "run_attempt": run.get("run_attempt"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "html_url": run.get("html_url"),
                "workflow_id": run.get("workflow_id"),
            }
            for run in runs[:5]
        ],
    }


@repo_debug_router.post(
    "/debug/github-workflow-runs-ping-post",
    operation_id="debugGitHubWorkflowRunsPingPost",
    summary="调试 workflow runs 路由外层链路（POST）",
    description="不访问 GitHub。通过 JSON body 回显参数并始终返回 200 JSON，用于确认是否是 query string 链路有问题。",
    response_model=GitHubDebugResponse,
)
async def debug_github_workflow_runs_ping_post(
    owner: str,
    repo: str,
    request: WorkflowRunsDebugRequest,
) -> JSONResponse:
    params = request.model_dump()
    response = GitHubDebugResponse(
        ok=True,
        route="debugGitHubWorkflowRunsPingPost",
        version=DEBUG_ROUTE_VERSION,
        owner=owner,
        repo=repo,
        params=params,
        payload={"probe": "workflow-runs-ping-post", "params_echo": params},
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


@repo_debug_router.post(
    "/debug/github-workflow-runs-fail-post",
    operation_id="debugGitHubWorkflowRunsFailPost",
    summary="调试 workflow runs 错误包装链路（POST）",
    description="不访问 GitHub。通过 JSON body 构造一个调试错误并以 200 JSON 返回，用于确认是否是 query string 链路有问题。",
    response_model=GitHubDebugResponse,
)
async def debug_github_workflow_runs_fail_post(
    owner: str,
    repo: str,
    request: WorkflowRunsDebugRequest,
) -> JSONResponse:
    params = request.model_dump()
    return _debug_error_response(
        owner=owner,
        repo=repo,
        route="debugGitHubWorkflowRunsFailPost",
        params=params,
        exc=ApiError(
            ErrorCode.GITHUB_ERROR,
            "这是 workflow runs POST 调试用的强制错误响应。",
            status_code=502,
            suggestion="如果你能看到这个 200 JSON，而 GET 版本失败，就说明问题集中在 query string / GET 链路，而不是 repos 调试路由本身。",
            details={"probe": "workflow-runs-fail-post", **params},
        ),
    )


@repo_debug_router.post(
    "/debug/github-workflow-runs-post",
    operation_id="debugGitHubWorkflowRunsPost",
    summary="调试 GitHub workflow runs 查询（POST）",
    description="与 debugGitHubWorkflowRuns 目标相同，但参数通过 JSON body 传递，用于绕过 GET query string 链路问题。",
    response_model=GitHubDebugResponse,
)
async def debug_github_workflow_runs_post(
    owner: str,
    repo: str,
    request: WorkflowRunsDebugRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    params = request.model_dump()
    github_params: dict[str, object] = {"per_page": 5}
    if request.head_sha:
        github_params["head_sha"] = request.head_sha
    if request.branch:
        github_params["branch"] = request.branch
    if request.event:
        github_params["event"] = request.event
    try:
        async def _fetch_runs() -> dict[str, Any]:
            async with httpx.AsyncClient(
                base_url=settings.github_api_base_url.rstrip("/"),
                timeout=httpx.Timeout(5.0),
                trust_env=settings.github_use_env_proxy,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": settings.github_api_version,
                    "User-Agent": "gpt-actions-gateway/debug-workflow-runs-post",
                },
                follow_redirects=False,
            ) as client:
                token = await github._auth.get_token(client)
                if request.workflow_id:
                    path = f"/repos/{owner}/{repo}/actions/workflows/{github._q(request.workflow_id)}/runs"
                else:
                    path = f"/repos/{owner}/{repo}/actions/runs"
                raw_response = await client.get(path, params=github_params, headers={"Authorization": f"Bearer {token}"})
                if raw_response.status_code >= 400:
                    return {
                        "github_status": raw_response.status_code,
                        "body_excerpt": raw_response.text[:4000],
                    }
                return raw_response.json()

        payload = await asyncio.wait_for(_fetch_runs(), timeout=6.0)
        if "github_status" in payload:
            response = GitHubDebugResponse(
                ok=False,
                route="debugGitHubWorkflowRunsPost",
                version=DEBUG_ROUTE_VERSION,
                owner=owner,
                repo=repo,
                params=params,
                payload=payload,
            )
            return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
        response = GitHubDebugResponse(
            ok=True,
            route="debugGitHubWorkflowRunsPost",
            version=DEBUG_ROUTE_VERSION,
            owner=owner,
            repo=repo,
            params=params,
            payload=_summarize_workflow_runs_payload(payload),
        )
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    except TimeoutError:
        return _debug_error_response(
            owner=owner,
            repo=repo,
            route="debugGitHubWorkflowRunsPost",
            params={**params, "debug_timeout_seconds": 6},
            exc=ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub workflow runs POST 调试请求超时。",
                status_code=502,
                suggestion="这通常表示 runs 接口响应过慢或外部链路超时；请检查服务到 GitHub 的网络连通性。",
                details=params,
            ),
        )
    except Exception as exc:
        return _debug_error_response(owner=owner, repo=repo, route="debugGitHubWorkflowRunsPost", params=params, exc=exc)


@repo_debug_router.post(
    "/ci/status-debug/query",
    operation_id="debugQueryCiStatus",
    summary="通过 POST 调试 GitHub Actions 状态查询",
    description="与 debugGetCiStatus 逻辑相同，但参数通过 JSON body 传递，用于绕过 GET query string 链路问题，并始终返回 200 JSON。",
    response_model=CIDebugStatusResponse,
)
async def debug_query_ci_status(
    owner: str,
    repo: str,
    request: CIStatusQueryRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    service = CIService(github, pol, settings)
    response = CIDebugStatusResponse(
        ok=False,
        owner=owner,
        repo=repo,
        commit_sha=request.commit_sha,
        branch=request.branch,
        pr_number=request.pr_number,
        workflow_id=request.workflow_id,
        event=request.event,
        created_after=request.created_after,
    )
    try:
        result = await service.get_ci_status(
            owner,
            repo,
            commit_sha=request.commit_sha,
            branch=request.branch,
            pr_number=request.pr_number,
            workflow_id=request.workflow_id,
            event=request.event,
            created_after=request.created_after,
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
            message="CI 状态 POST 调试查询发生未处理异常。",
            suggestion="检查服务日志和外部网络连通性，然后重试同一请求。",
            details={"error": str(exc), "exception_repr": repr(exc)},
            exception_type=type(exc).__name__,
        )
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


@router.post(
    "/ci/failed-log/query",
    operation_id="queryFailedCiLog",
    summary="通过 POST 获取失败 CI 日志摘要",
    description="与 getFailedCiLog 逻辑相同，但参数通过 JSON body 传递，用于绕过某些动作网关对 GET query string 的处理问题。",
    response_model=FailedCILogResponse,
)
async def query_failed_ci_log(
    owner: str,
    repo: str,
    request: FailedLogQueryRequest,
    github: Annotated[GitHubClient, Depends(github_client)],
    pol: Annotated[Policy, Depends(policy)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FailedCILogResponse:
    return await CIService(github, pol, settings).get_failed_ci_log(
        owner,
        repo,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        job_id=request.job_id,
        max_lines=request.max_lines,
    )


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
