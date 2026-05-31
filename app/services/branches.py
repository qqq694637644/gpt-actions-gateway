from __future__ import annotations

import base64
import secrets
from datetime import datetime, timezone

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.branches import ContinueWorkBranchRequest, ContinueWorkBranchResponse, CreateWorkBranchRequest, CreateWorkBranchResponse
from app.models.repos import GetBranchProtectionRequest, GetBranchProtectionResponse, GetBranchRequest, GetBranchResponse, ListBranchesRequest, ListBranchesResponse
from app.policy.rules import Policy, is_sha, sanitize_purpose_slug
from app.storage.audit import AuditStore
from app.services.repos import RepoService


class BranchService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, audit: AuditStore | None = None) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.audit = audit

    async def create_work_branch(self, owner: str, repo: str, request: CreateWorkBranchRequest) -> CreateWorkBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        if request.branch:
            self.policy.assert_write_branch_allowed(request.branch)

        scope = f"{owner}/{repo}:create_work_branch"
        request_payload = request.model_dump()
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload)
            if cached:
                return CreateWorkBranchResponse(**cached)

        base_sha, base_ref, base_branch = await self._resolve_base(owner, repo, request)

        if request.branch:
            try:
                await self.github.create_ref(owner, repo, request.branch, base_sha)
                response = CreateWorkBranchResponse(
                    branch=request.branch,
                    base_branch=base_branch,
                    base_ref=base_ref,
                    base_sha=base_sha,
                    head_sha=base_sha,
                    created=True,
                    continued=False,
                    source_pr_number=request.source_pr_number,
                    already_exists=False,
                )
            except ApiError as exc:
                if exc.error_code != ErrorCode.GITHUB_CONFLICT or not request.continue_if_exists:
                    raise
                existing = await self.github.get_branch_head(owner, repo, request.branch)
                response = CreateWorkBranchResponse(
                    branch=request.branch,
                    base_branch=base_branch,
                    base_ref=base_ref,
                    base_sha=base_sha,
                    head_sha=existing,
                    created=False,
                    continued=True,
                    source_pr_number=request.source_pr_number,
                    already_exists=True,
                )
            if request.idempotency_key and self.audit:
                self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
            return response

        last_error: Exception | None = None
        for _ in range(5):
            branch = self._generate_branch_name(request.purpose_slug)
            self.policy.assert_write_branch_allowed(branch)
            try:
                await self.github.create_ref(owner, repo, branch, base_sha)
                response = CreateWorkBranchResponse(
                    branch=branch,
                    base_branch=base_branch,
                    base_ref=base_ref,
                    base_sha=base_sha,
                    head_sha=base_sha,
                    created=True,
                    continued=False,
                    source_pr_number=request.source_pr_number,
                    already_exists=False,
                )
                if request.idempotency_key and self.audit:
                    self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
                return response
            except Exception as exc:  # very unlikely branch suffix collision; retry before surfacing.
                last_error = exc
        raise last_error  # type: ignore[misc]

    async def continue_work_branch(self, owner: str, repo: str, request: ContinueWorkBranchRequest) -> ContinueWorkBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.branch)
        head_sha = await self.github.get_branch_head(owner, repo, request.branch)
        return ContinueWorkBranchResponse(branch=request.branch, head_sha=head_sha, continued=True)


    async def list_branches(self, owner: str, repo: str, request: ListBranchesRequest) -> ListBranchesResponse:
        return await RepoService(self.github, self.policy, self.settings).list_branches(owner, repo, request)

    async def get_branch(self, owner: str, repo: str, request: GetBranchRequest) -> GetBranchResponse:
        return await RepoService(self.github, self.policy, self.settings).get_branch(owner, repo, request)

    async def get_branch_protection(self, owner: str, repo: str, request: GetBranchProtectionRequest) -> GetBranchProtectionResponse:
        return await RepoService(self.github, self.policy, self.settings).get_branch_protection(owner, repo, request)

    async def _resolve_base(self, owner: str, repo: str, request: CreateWorkBranchRequest) -> tuple[str, str, str | None]:
        if request.source_pr_number is not None:
            pr = await self.github.get_pull_request(owner, repo, request.source_pr_number)
            head_branch = pr["head"]["ref"]
            self.policy.assert_read_ref_allowed(head_branch)
            return pr["head"]["sha"], head_branch, (pr.get("base") or {}).get("ref")

        if request.base_sha is not None:
            await self.github.get_commit_object(owner, repo, request.base_sha)
            return request.base_sha, request.base_sha, request.base_branch

        base_ref = request.effective_base_ref
        if is_sha(base_ref):
            await self.github.get_commit_object(owner, repo, base_ref)
            return base_ref, base_ref, request.base_branch

        self.policy.assert_read_ref_allowed(base_ref)
        try:
            base_sha = await self.github.get_branch_head(owner, repo, base_ref)
        except ApiError as exc:
            if not self._is_empty_repository_error(exc):
                raise
            if not request.initialize_if_empty:
                raise ApiError(
                    ErrorCode.GITHUB_CONFLICT,
                    "目标仓库为空，无法基于现有分支创建工作分支。",
                    status_code=409,
                    suggestion="请先创建初始提交，或在 createWorkBranch 请求中设置 initialize_if_empty=true。",
                    details={"repo": f"{owner}/{repo}", "base_ref": base_ref, **exc.details},
                ) from exc
            self.policy.assert_base_branch_allowed(base_ref)
            base_sha = await self._initialize_empty_repository(owner, repo, base_ref)
        base_branch = base_ref if not is_sha(base_ref) else request.base_branch
        return base_sha, base_ref, base_branch

    def _generate_branch_name(self, purpose_slug: str) -> str:
        slug = sanitize_purpose_slug(purpose_slug)
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        suffix = secrets.token_hex(3)
        return f"{self.settings.write_branch_prefix}{slug}-{date_part}-{suffix}"

    async def _try_get_branch_head(self, owner: str, repo: str, branch: str) -> str | None:
        try:
            return await self.github.get_branch_head(owner, repo, branch)
        except ApiError as exc:
            if exc.error_code == ErrorCode.GITHUB_NOT_FOUND:
                return None
            return None if exc.status_code == 404 else (_raise(exc))

    @staticmethod
    def _is_empty_repository_error(exc: ApiError) -> bool:
        body = str(exc.details.get("body", "")).lower()
        return "git repository is empty" in body

    async def _initialize_empty_repository(self, owner: str, repo: str, base_branch: str) -> str:
        readme_content = f"# {repo}\n\n此仓库由 GPT Actions Gateway 自动初始化。\n"
        response = await self.github.create_or_update_file(
            owner,
            repo,
            "README.md",
            message="chore: 初始化仓库",
            content_base64=base64.b64encode(readme_content.encode("utf-8")).decode("ascii"),
            branch=base_branch,
        )
        return response["commit"]["sha"]


def _raise(exc: ApiError) -> None:
    raise exc
