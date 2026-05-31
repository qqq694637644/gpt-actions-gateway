from __future__ import annotations

from pydantic import Field, model_validator

from app.models.common import GatewayBaseModel, IdempotentRequest
from app.models.repos import BranchInfo, GetBranchProtectionRequest, GetBranchProtectionResponse, GetBranchRequest, GetBranchResponse, ListBranchesRequest, ListBranchesResponse

# Backward-compatible names for branch metadata requests.
BranchProtectionRequest = GetBranchProtectionRequest


class CreateWorkBranchRequest(IdempotentRequest):
    base_branch: str | None = Field(default=None, description="Legacy alias for base_ref. Kept for backward compatibility.")
    base_ref: str | None = Field(default=None, description="Branch name or exact commit SHA to branch from. Supports main/master/develop and gpt/*.")
    base_sha: str | None = Field(default=None, min_length=7, description="Exact commit SHA to branch from. Takes precedence over base_ref/base_branch.")
    source_pr_number: int | None = Field(default=None, ge=1, description="Create from the current head SHA of this pull request.")
    branch: str | None = Field(default=None, description="Optional explicit gpt/* branch name. If it already exists, continue_if_exists controls behavior.")
    purpose_slug: str = Field(default="task", min_length=1, max_length=80)
    continue_if_exists: bool = Field(default=True, description="Return an existing gpt/* branch instead of failing when branch is provided and already exists.")
    initialize_if_empty: bool = Field(default=False, description="仓库为空时，先在 base branch 创建初始提交，再创建工作分支。")

    @model_validator(mode="after")
    def validate_base_selectors(self) -> "CreateWorkBranchRequest":
        selected = [self.base_sha is not None, self.source_pr_number is not None]
        if sum(selected) > 1:
            raise ValueError("base_sha and source_pr_number are mutually exclusive")
        return self

    @property
    def effective_base_ref(self) -> str:
        return self.base_ref or self.base_branch or "main"

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "base_ref": "main",
                    "purpose_slug": "fix-windows-ci",
                    "initialize_if_empty": True,
                    "idempotency_key": "task-20260530-001",
                },
                {
                    "branch": "gpt/fix-windows-ci",
                    "continue_if_exists": True,
                    "purpose_slug": "fix-windows-ci",
                },
            ],
        }
    }


class CreateWorkBranchResponse(GatewayBaseModel):
    branch: str
    base_branch: str | None = None
    base_ref: str | None = None
    base_sha: str
    head_sha: str
    created: bool = True
    continued: bool = False
    source_pr_number: int | None = None
    already_exists: bool = False


class ContinueWorkBranchRequest(GatewayBaseModel):
    branch: str


class ContinueWorkBranchResponse(GatewayBaseModel):
    branch: str
    head_sha: str
    continued: bool = True
