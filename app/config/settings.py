from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?b?)?\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_size_to_bytes(value: int | str | None, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("size value is required")
        return default
    if isinstance(value, int):
        return value
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"Invalid size value: {value!r}")
    number, suffix = match.groups()
    return int(float(number) * _SIZE_MULTIPLIERS[(suffix or "").lower()])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    gpt_action_secret: str = ""

    github_auth_mode: Literal["pat", "github_app"] = "pat"
    github_api_base_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    github_use_env_proxy: bool = False
    github_token: str | None = None
    github_git_username: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_installation_id: str | None = None

    allowed_repos: str = ""
    allow_all_repos: bool = False
    read_branch_allowlist: str = "main,master,develop,gpt/*"
    write_branch_prefix: str = "gpt/"
    default_base_branch: str = "main"

    allow_workflow_edit: bool = False
    allow_delete_files: bool = False

    max_log_bytes: int = Field(default=80_000)
    max_log_lines: int = 500
    max_blob_read_bytes: int = Field(default=2 * 1024 * 1024)

    workspace_root: str = "./data/workspaces"
    workspace_mirror_root: str = "./data/mirrors"
    workspace_default_timeout_seconds: int = 60
    workspace_max_timeout_seconds: int = 300
    workspace_max_output_bytes: int = Field(default=80_000)
    workspace_max_diff_bytes: int = Field(default=200_000)
    workspace_max_patch_bytes: int = Field(default=200_000)
    workspace_max_write_bytes: int = Field(default=200_000)
    workspace_max_changed_files: int = 200
    workspace_ttl_hours: int = 48
    workspace_max_count: int = 50
    workspace_allow_network: bool = False
    workspace_shell: str = "pwsh"
    workspace_git_user_name: str = "gpt-actions-gateway"
    workspace_git_user_email: str = "gpt-actions-gateway@users.noreply.github.com"

    rate_limit_per_minute: int = 60
    audit_db_url: str = "sqlite:///./data/audit.db"
    request_timeout_seconds: float = 30.0

    excluded_tree_dirs: str = "node_modules,dist,build,.git,vendor,.venv,venv,__pycache__,coverage,.next,.turbo"

    @field_validator(
        "max_log_bytes",
        "max_blob_read_bytes",
        "workspace_max_output_bytes",
        "workspace_max_diff_bytes",
        "workspace_max_patch_bytes",
        "workspace_max_write_bytes",
        mode="before",
    )
    @classmethod
    def _parse_size_fields(cls, value: int | str | None) -> int:
        return parse_size_to_bytes(value)

    @property
    def secrets(self) -> list[str]:
        return parse_csv(self.gpt_action_secret)

    @property
    def allowed_repo_set(self) -> set[str]:
        return {repo.lower() for repo in parse_csv(self.allowed_repos)}

    @property
    def read_branch_patterns(self) -> list[str]:
        return parse_csv(self.read_branch_allowlist)

    @property
    def excluded_dir_names(self) -> set[str]:
        return set(parse_csv(self.excluded_tree_dirs))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
