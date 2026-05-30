from __future__ import annotations

from typing import Literal

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


class MergePullRequestRequest(GatewayBaseModel):
    pr_number: int = Field(ge=1)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    commit_title: str | None = Field(default=None, min_length=1, max_length=200)
    commit_message: str | None = Field(default=None, max_length=20000)


class MergePullRequestResponse(GatewayBaseModel):
    merged: bool
    message: str
    sha: str | None = None
