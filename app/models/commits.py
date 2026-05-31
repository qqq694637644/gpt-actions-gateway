from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import ChangedFile, GatewayBaseModel, IdempotentRequest


class CommitFileChange(GatewayBaseModel):
    path: str
    operation: Literal["upsert", "delete"] = "upsert"
    content: str | None = Field(default=None, description="Required when operation=upsert. UTF-8 text only.")
    previous_sha: str | None = Field(default=None, description="Optional file SHA read before editing; checked before commit if provided.")

    @model_validator(mode="after")
    def validate_content(self) -> "CommitFileChange":
        if self.operation == "upsert" and self.content is None:
            raise ValueError("content is required when operation=upsert")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("content must not be provided when operation=delete")
        return self


class CommitFilesRequest(IdempotentRequest):
    branch: str
    expected_head_sha: str = Field(min_length=7)
    commit_message: str = Field(min_length=1, max_length=300)
    files: list[CommitFileChange] = Field(min_length=1, max_length=20)

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "branch": "gpt/fix-windows-ci-20260530-ab12cd",
                    "expected_head_sha": "1111111111111111111111111111111111111111",
                    "commit_message": "Fix Windows CI path handling",
                    "idempotency_key": "task-20260530-001-commit-1",
                    "files": [
                        {
                            "path": "src/path_utils.py",
                            "operation": "upsert",
                            "previous_sha": "2222222222222222222222222222222222222222",
                            "content": "def normalize(path):\n    return path.replace('\\\\', '/')\n",
                        }
                    ],
                }
            ]
        }
    }


class CommitFilesResponse(GatewayBaseModel):
    commit_sha: str
    previous_head_sha: str
    new_head_sha: str
    changed_files: list[ChangedFile]
    commit_url: str


class PatchChangedFile(GatewayBaseModel):
    path: str
    operation: Literal["added", "modified", "deleted", "renamed"]
    previous_path: str | None = None
    additions: int = 0
    deletions: int = 0


class ApplyPatchAndCommitRequest(IdempotentRequest):
    branch: str
    expected_head_sha: str = Field(min_length=7)
    patch: str = Field(min_length=1, description="Unified diff / git diff text.")
    commit_message: str = Field(min_length=1, max_length=300)
    dry_run: bool = Field(default=False, description="Validate and apply the patch in memory without creating a commit.")


class ApplyPatchAndCommitResponse(GatewayBaseModel):
    commit_sha: str | None = None
    previous_head_sha: str
    new_head_sha: str
    changed_files: list[PatchChangedFile]
    commit_url: str | None = None
    dry_run: bool = False


# Backward-compatible alias used by earlier gateway tests/clients.
ApplyPatchRequest = ApplyPatchAndCommitRequest
