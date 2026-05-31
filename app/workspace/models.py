from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    owner: str
    repo: str
    branch: str
    default_branch: str
    head_sha: str
    source_pr_number: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def meta_path(workspace_dir: Path) -> Path:
    return workspace_dir / "meta.json"


def load_meta(workspace_dir: Path) -> WorkspaceMeta:
    return WorkspaceMeta.model_validate_json(meta_path(workspace_dir).read_text(encoding="utf-8"))


def save_meta(workspace_dir: Path, meta: WorkspaceMeta) -> None:
    meta.updated_at = datetime.now(timezone.utc).isoformat()
    meta_path(workspace_dir).write_text(meta.model_dump_json(indent=2), encoding="utf-8")


class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
