from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import GatewayBaseModel


class CreatePullRequestRequest(GatewayBaseModel):
    head_branch: str
    base_branch: str = "main"
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20000)

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "head_branch": "gpt/fix-windows-ci-20260530-ab12cd",
                    "base_branch": "main",
                    "title": "Fix Windows CI path handling",
                    "body": "Created by GPT Actions Gateway. CI status should be checked before merge.",
                }
            ]
        }
    }


class CreatePullRequestResponse(GatewayBaseModel):
    pr_number: int
    pr_url: str
    state: str
    head_sha: str
    base_branch: str
    already_exists: bool = False


class PullRequestInfo(GatewayBaseModel):
    pr_number: int
    pr_url: str
    state: str
    title: str
    body: str | None = None
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str | None = None
    merged: bool | None = None
    draft: bool | None = None
    mergeable: bool | None = None
    labels: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class GetPullRequestRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)


class GetPullRequestResponse(GatewayBaseModel):
    pull_request: PullRequestInfo


class ListPullRequestsRequest(GatewayBaseModel):
    state: Literal["open", "closed", "all"] = "open"
    head_branch: str | None = None
    base_branch: str | None = None
    max_results: int = Field(default=50, ge=1, le=100)


class ListPullRequestsResponse(GatewayBaseModel):
    pull_requests: list[PullRequestInfo]
    total_count: int


class PullRequestFilesRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    max_results: int = Field(default=100, ge=1, le=100)


class PullRequestFile(GatewayBaseModel):
    filename: str
    status: str
    operation: str = Field(default="", description="Alias of GitHub file status for older clients.")
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    previous_filename: str | None = None
    patch: str | None = None
    sha: str | None = None
    blob_url: str | None = None
    raw_url: str | None = None


class PullRequestFilesResponse(GatewayBaseModel):
    pr_number: int
    files: list[PullRequestFile]
    total_count: int


class UpdatePullRequestRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=20000)
    state: Literal["open", "closed"] | None = None
    base_branch: str | None = None


class UpdatePullRequestResponse(GatewayBaseModel):
    pull_request: PullRequestInfo


class CommentPullRequestRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20000)


class CommentPullRequestResponse(GatewayBaseModel):
    comment_id: int
    comment_url: str
    body: str
    created_at: str | None = None


class RequestReviewersRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    reviewers: list[str] = Field(default_factory=list, max_length=15)
    team_reviewers: list[str] = Field(default_factory=list, max_length=15)


class RequestReviewersResponse(GatewayBaseModel):
    pr_number: int
    requested_reviewers: list[str]
    requested_team_reviewers: list[str] = Field(default_factory=list)
    requested_teams: list[str] = Field(default_factory=list)


class AddLabelsRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    labels: list[str] = Field(min_length=1, max_length=20)


class AddLabelsResponse(GatewayBaseModel):
    pr_number: int
    labels: list[str]
    raw: list[dict[str, Any]] = Field(default_factory=list)


class MergePullRequestRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    commit_title: str | None = Field(default=None, min_length=1, max_length=200)
    commit_message: str | None = Field(default=None, max_length=20000)


class MergePullRequestResponse(GatewayBaseModel):
    merged: bool
    message: str
    sha: str | None = None


# Backward-compatible aliases used by earlier gateway tests/clients.
GetPullRequestFilesRequest = PullRequestFilesRequest
GetPullRequestFilesResponse = PullRequestFilesResponse
