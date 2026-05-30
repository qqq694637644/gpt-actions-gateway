from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GatewayBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class IdempotentRequest(GatewayBaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200, description="Stable key used to safely retry the same task.")


class ChangedFile(GatewayBaseModel):
    path: str
    operation: str
    previous_sha: str | None = None
    new_sha: str | None = None


class ErrorExample(BaseModel):
    error_code: str = "BRANCH_HEAD_CHANGED"
    message: str = "The branch head has changed since the client last read it."
    suggestion: str | None = "Read the latest branch head, then retry with the new expected_head_sha."
    details: dict[str, Any] = {"expected_head_sha": "abc", "actual_head_sha": "def"}
