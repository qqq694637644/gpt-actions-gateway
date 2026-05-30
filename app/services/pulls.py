from __future__ import annotations

from app.github.client import GitHubClient
from app.models.pulls import (
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
)
from app.policy.rules import Policy


class PullRequestService:
    def __init__(self, github: GitHubClient, policy: Policy) -> None:
        self.github = github
        self.policy = policy

    async def create_pull_request(self, owner: str, repo: str, request: CreatePullRequestRequest) -> CreatePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.head_branch)
        self.policy.assert_base_branch_allowed(request.base_branch)

        existing = await self.github.list_pull_requests(owner, repo, head=f"{owner}:{request.head_branch}", base=request.base_branch, state="open")
        if existing:
            pr = existing[0]
            return CreatePullRequestResponse(
                pr_number=pr["number"],
                pr_url=pr["html_url"],
                state=pr["state"],
                head_sha=pr["head"]["sha"],
                base_branch=pr["base"]["ref"],
                already_exists=True,
            )

        pr = await self.github.create_pull_request(
            owner,
            repo,
            head=request.head_branch,
            base=request.base_branch,
            title=request.title,
            body=request.body,
        )
        return CreatePullRequestResponse(
            pr_number=pr["number"],
            pr_url=pr["html_url"],
            state=pr["state"],
            head_sha=pr["head"]["sha"],
            base_branch=pr["base"]["ref"],
            already_exists=False,
        )

    async def merge_pull_request(self, owner: str, repo: str, request: MergePullRequestRequest) -> MergePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_auto_merge_allowed()

        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        self.policy.assert_write_branch_allowed(pr["head"]["ref"])
        self.policy.assert_base_branch_allowed(pr["base"]["ref"])

        merged = await self.github.merge_pull_request(
            owner,
            repo,
            request.pr_number,
            merge_method=request.merge_method,
            commit_title=request.commit_title,
            commit_message=request.commit_message,
        )
        return MergePullRequestResponse(
            merged=bool(merged.get("merged")),
            message=str(merged.get("message", "")),
            sha=merged.get("sha"),
        )
