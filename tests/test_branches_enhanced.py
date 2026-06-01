from __future__ import annotations

import asyncio

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.branches import ContinueWorkBranchRequest, CreateWorkBranchRequest
from app.policy.rules import Policy
from app.services.branches import BranchService

MAIN_SHA = "1111111111111111111111111111111111111111"
GPT_SHA = "2222222222222222222222222222222222222222"
FEATURE_SHA = "3333333333333333333333333333333333333333"
NON_ALLOWLISTED_BASE = "feature/android-ci-fix"


class BranchGitHubStub:
    def __init__(self) -> None:
        self.created_refs: list[tuple[str, str]] = []

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        if branch == "gpt/existing":
            return GPT_SHA
        if branch == NON_ALLOWLISTED_BASE:
            return FEATURE_SHA
        return MAIN_SHA

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict:
        self.created_refs.append((branch, sha))
        if branch == "gpt/existing":
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "already exists", status_code=409)
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict:
        return {"name": branch, "commit": {"sha": GPT_SHA, "url": "https://api.github.test/commit"}, "protected": False}


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


def test_create_work_branch_base_ref_is_not_limited_to_base_allowlist() -> None:
    github = BranchGitHubStub()
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo")
    service = BranchService(github, Policy(settings), settings)

    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref=NON_ALLOWLISTED_BASE, branch="gpt/from-feature", purpose_slug="fix"),
        )
    )

    assert response.base_ref == NON_ALLOWLISTED_BASE
    assert response.base_sha == FEATURE_SHA
    assert response.head_sha == FEATURE_SHA
    assert github.created_refs == [("gpt/from-feature", FEATURE_SHA)]


def test_continue_work_branch_is_still_available_as_backend_compatibility() -> None:
    service = make_service()

    continued = asyncio.run(service.continue_work_branch("acme", "demo", ContinueWorkBranchRequest(branch="gpt/existing")))

    assert continued.head_sha == GPT_SHA
    assert continued.protected is False
