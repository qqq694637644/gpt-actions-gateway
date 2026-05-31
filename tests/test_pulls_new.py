from __future__ import annotations

import asyncio

from app.config.settings import Settings
from app.models.pulls import (
    AddLabelsRequest,
    CommentPullRequestRequest,
    GetPullRequestFilesRequest,
    GetPullRequestRequest,
    ListPullRequestsRequest,
    RequestReviewersRequest,
    UpdatePullRequestRequest,
)
from app.policy.rules import Policy
from app.services.pulls import PullRequestService


def pr_payload(number: int = 7) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.test/acme/demo/pull/{number}",
        "state": "open",
        "title": "Fix CI",
        "body": "body",
        "head": {"ref": "gpt/fix", "sha": "2222222222222222222222222222222222222222"},
        "base": {"ref": "main", "sha": "1111111111111111111111111111111111111111"},
        "draft": False,
        "merged": False,
        "mergeable": True,
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
    }


class PullGitHubStub:
    def __init__(self) -> None:
        self.updated: dict | None = None
        self.comments: list[str] = []
        self.reviewers: dict | None = None
        self.labels: list[str] = []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return pr_payload(pr_number)

    async def list_pull_requests(self, owner: str, repo: str, *, head: str | None = None, base: str | None = None, state: str = "open") -> list[dict]:
        assert state in {"open", "closed", "all"}
        return [pr_payload(7)]

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, *, per_page: int = 100) -> list[dict]:
        return [
            {"filename": "app.py", "status": "modified", "additions": 3, "deletions": 1},
            {"filename": "new.py", "status": "added", "additions": 2, "deletions": 0},
        ]

    async def update_pull_request(self, owner: str, repo: str, pr_number: int, **kwargs) -> dict:
        self.updated = kwargs
        payload = pr_payload(pr_number)
        payload["title"] = kwargs.get("title") or payload["title"]
        payload["body"] = kwargs.get("body") or payload["body"]
        return payload

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        self.comments.append(body)
        return {"id": 99, "html_url": "https://github.test/comment/99"}

    async def request_pull_request_reviewers(self, owner: str, repo: str, pr_number: int, *, reviewers: list[str], team_reviewers: list[str]) -> dict:
        self.reviewers = {"reviewers": reviewers, "team_reviewers": team_reviewers}
        return {"requested_reviewers": [{"login": item} for item in reviewers], "requested_teams": [{"slug": item} for item in team_reviewers]}

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> list[dict]:
        self.labels.extend(labels)
        return [{"name": item} for item in labels]


def make_service(github: PullGitHubStub) -> PullRequestService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo")
    return PullRequestService(github, Policy(settings))


def test_pull_request_query_and_files() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    pr = asyncio.run(service.get_pull_request("acme", "demo", GetPullRequestRequest(pr_number=7)))
    prs = asyncio.run(service.list_pull_requests("acme", "demo", ListPullRequestsRequest(head_branch="gpt/fix", base_branch="main")))
    files = asyncio.run(service.get_pull_request_files("acme", "demo", GetPullRequestFilesRequest(pr_number=7)))

    assert pr.pull_request.head_branch == "gpt/fix"
    assert prs.pull_requests[0].pr_number == 7
    assert [item.operation for item in files.files] == ["modified", "added"]


def test_pull_request_update_comment_reviewers_and_labels() -> None:
    github = PullGitHubStub()
    service = make_service(github)

    updated = asyncio.run(service.update_pull_request("acme", "demo", UpdatePullRequestRequest(pr_number=7, title="New title", body="New body")))
    comment = asyncio.run(service.comment_pull_request("acme", "demo", CommentPullRequestRequest(pr_number=7, body="CI fixed")))
    reviewers = asyncio.run(
        service.request_reviewers(
            "acme",
            "demo",
            RequestReviewersRequest(pr_number=7, reviewers=["octo"], team_reviewers=["core"]),
        )
    )
    labels = asyncio.run(service.add_labels("acme", "demo", AddLabelsRequest(pr_number=7, labels=["ready-for-review", "ci-passed"])))

    assert updated.pull_request.title == "New title"
    assert comment.comment_id == 99
    assert reviewers.requested_reviewers == ["octo"]
    assert reviewers.requested_teams == ["core"]
    assert labels.labels == ["ready-for-review", "ci-passed"]
    assert github.comments == ["CI fixed"]
