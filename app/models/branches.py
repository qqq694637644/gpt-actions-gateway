from __future__ import annotations

from pydantic import Field

from app.models.common import GatewayBaseModel, IdempotentRequest


class CreateWorkBranchRequest(IdempotentRequest):
    base_branch: str = Field(default="main")
    purpose_slug: str = Field(min_length=1, max_length=80)
    initialize_if_empty: bool = Field(default=False, description="仓库为空时，先在 base_branch 创建初始提交，再创建工作分支。")

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "base_branch": "main",
                    "purpose_slug": "fix-windows-ci",
                    "initialize_if_empty": True,
                    "idempotency_key": "task-20260530-001",
                }
            ],
        }
    }


class CreateWorkBranchResponse(GatewayBaseModel):
    branch: str
    base_branch: str
    base_sha: str
    head_sha: str
