from __future__ import annotations

import base64
import secrets
from datetime import datetime, timezone

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.branches import CreateWorkBranchRequest, CreateWorkBranchResponse
from app.policy.rules import Policy, sanitize_purpose_slug
from app.storage.audit import AuditStore


class BranchService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, audit: AuditStore) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.audit = audit

    async def create_work_branch(self, owner: str, repo: str, request: CreateWorkBranchRequest) -> CreateWorkBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_base_branch_allowed(request.base_branch)
        scope = f"{owner}/{repo}:create_work_branch"
        request_payload = request.model_dump()
        if request.idempotency_key:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload)
            if cached:
                return CreateWorkBranchResponse(**cached)

        try:
            base_sha = await self.github.get_branch_head(owner, repo, request.base_branch)
        except ApiError as exc:
            if not self._is_empty_repository_error(exc):
                raise
            if not request.initialize_if_empty:
                raise ApiError(
                    ErrorCode.GITHUB_CONFLICT,
                    "目标仓库为空，无法基于现有分支创建工作分支。",
                    status_code=409,
                    suggestion="请先创建初始提交，或在 createWorkBranch 请求中设置 initialize_if_empty=true。",
                    details={"repo": f"{owner}/{repo}", "base_branch": request.base_branch, **exc.details},
                ) from exc
            base_sha = await self._initialize_empty_repository(owner, repo, request.base_branch)
        slug = sanitize_purpose_slug(request.purpose_slug)
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")

        last_error: Exception | None = None
        for _ in range(5):
            suffix = secrets.token_hex(3)
            branch = f"{self.settings.write_branch_prefix}{slug}-{date_part}-{suffix}"
            self.policy.assert_write_branch_allowed(branch)
            try:
                await self.github.create_ref(owner, repo, branch, base_sha)
                response = CreateWorkBranchResponse(branch=branch, base_branch=request.base_branch, base_sha=base_sha, head_sha=base_sha)
                if request.idempotency_key:
                    self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
                return response
            except Exception as exc:  # very unlikely branch suffix collision; retry before surfacing.
                last_error = exc
        raise last_error  # type: ignore[misc]

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
