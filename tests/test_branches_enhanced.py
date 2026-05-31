from __future__ import annotations

import asyncio

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.branches import (
    BranchProtectionRequest,
    ContinueWorkBranchRequest,
    CreateWorkBranchRequest,
    GetBranchRequest,
    ListBranchesRequest,
)
from app.policy.rules import Policy
from app.services.branches import BranchService

MAIN_SHA = "1111111111111111111111111111111111111111"
GPT_SHA = "2222222222222222222222222222222222222222"


class BranchGitHubStub:
    def __init__(self) -> None:
        self.created_refs: list[tuple[str, str]] = []

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        if branch == "main":
            return MAIN_SHA
        if branch == "gpt/existing":
            return GPT_SHA
        return MAIN_SHA

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict:
        self.created_refs.append((branch, sha))
        if branch == "gpt/existing":
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "already exists", status_code=409)
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return {"head": {"ref": "gpt/pr-head", "sha": GPT_SHA}}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict:
        return {"name": branch, "commit": {"sha": GPT_SHA, "url": "https://api.github.test/commit"}, "protected": False}

    async def list_branches(self, owner: str, repo: str, *, protected: bool | None = None, per_page: int = 100) -> list[dict]:
        return [
            {"name": "main", "commit": {"sha": MAIN_SHA}, "protected": True},
            {"name": "gpt/existing", "commit": {"sha": GPT_SHA}, "protected": False},
        ]

    async def get_branch_protection(self, owner: str, repo: str, branch: str) -> dict:
        return {"required_status_checks": {"strict": True}}


def make_service() -> BranchService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo")
    return BranchService(BranchGitHubStub(), Policy(settings), settings)


def test_create_work_branch_can_continue_existing_named_branch() -> None:
    service = make_service()
    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref="main", branch="gpt/existing", purpose_slug="fix", continue_if_exists=True),
        )
    )

    assert response.branch == "gpt/existing"
    assert response.base_sha == MAIN_SHA
    assert response.head_sha == GPT_SHA
    assert response.already_exists is True


def test_create_work_branch_can_base_on_source_pr() -> None:
    service = make_service()
    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(source_pr_number=5, branch="gpt/new", purpose_slug="fix"),
        )
    )

    assert response.base_ref == "gpt/pr-head"
    assert response.base_sha == GPT_SHA
    assert response.head_sha == GPT_SHA


def test_continue_get_list_and_branch_protection() -> None:
    service = make_service()

    continued = asyncio.run(service.continue_work_branch("acme", "demo", ContinueWorkBranchRequest(branch="gpt/existing")))
    branch = asyncio.run(service.get_branch("acme", "demo", GetBranchRequest(branch="gpt/existing")))
    branches = asyncio.run(service.list_branches("acme", "demo", ListBranchesRequest()))
    protection = asyncio.run(service.get_branch_protection("acme", "demo", BranchProtectionRequest(branch="main")))

    assert continued.head_sha == GPT_SHA
    assert branch.branch.name == "gpt/existing"
    assert [item.name for item in branches.branches] == ["main", "gpt/existing"]
    assert protection.protection["required_status_checks"]["strict"] is True
