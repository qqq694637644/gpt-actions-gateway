from __future__ import annotations

import asyncio

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.branches import CreateWorkBranchRequest
from app.policy.rules import Policy
from app.services.branches import BranchService
from app.storage.audit import AuditStore


class EmptyRepoGitHubStub:
    def __init__(self) -> None:
        self.created_refs: list[tuple[str, str, str, str]] = []
        self.created_files: list[tuple[str, str, str, str | None]] = []

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        raise ApiError(
            ErrorCode.GITHUB_CONFLICT,
            "GitHub reported a conflict.",
            status_code=409,
            details={"github_status": 409, "body": "Git Repository is empty."},
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        message: str,
        content_base64: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> dict[str, dict[str, str]]:
        self.created_files.append((path, message, content_base64, branch))
        return {"commit": {"sha": "commit-sha"}}

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, str]:
        self.created_refs.append((owner, repo, branch, sha))
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}


def make_service(tmp_path, github: EmptyRepoGitHubStub) -> BranchService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo", audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}")
    return BranchService(github, Policy(settings), settings, AuditStore(settings.audit_db_url))


def test_create_work_branch_rejects_empty_repo_without_initialize(tmp_path) -> None:
    service = make_service(tmp_path, EmptyRepoGitHubStub())

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.create_work_branch(
                "acme",
                "demo",
                CreateWorkBranchRequest(base_branch="main", purpose_slug="bootstrap-test"),
            )
        )

    assert exc.value.error_code == ErrorCode.GITHUB_CONFLICT
    assert "目标仓库为空" in exc.value.message


def test_create_work_branch_initializes_empty_repo_when_enabled(tmp_path) -> None:
    github = EmptyRepoGitHubStub()
    service = make_service(tmp_path, github)

    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_branch="main", purpose_slug="bootstrap-test", initialize_if_empty=True),
        )
    )

    assert github.created_files == [
        (
            "README.md",
            "chore: 初始化仓库",
            "IyBkZW1vCgrmraTku5PlupPnlLEgR1BUIEFjdGlvbnMgR2F0ZXdheSDoh6rliqjliJ3lp4vljJbjgIIK",
            "main",
        )
    ]
    assert github.created_refs == [("acme", "demo", github.created_refs[0][2], "commit-sha")]
    assert github.created_refs[0][2].startswith("gpt/bootstrap-test-")
    assert response.base_branch == "main"
    assert response.base_sha == "commit-sha"
    assert response.head_sha == "commit-sha"
