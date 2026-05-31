from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import GatewayBaseModel


class EmptyRequest(GatewayBaseModel):
    pass


class RepositoryInfo(GatewayBaseModel):
    full_name: str
    private: bool | None = None
    default_branch: str
    html_url: str | None = None
    description: str | None = None
    fork: bool | None = None
    archived: bool | None = None
    disabled: bool | None = None
    visibility: str | None = None
    allow_squash_merge: bool | None = None
    allow_merge_commit: bool | None = None
    allow_rebase_merge: bool | None = None
    permissions: dict[str, Any] = Field(default_factory=dict)


class GetRepositoryResponse(GatewayBaseModel):
    repository: RepositoryInfo


class GetDefaultBranchResponse(GatewayBaseModel):
    default_branch: str


class ListBranchesRequest(GatewayBaseModel):
    protected: bool | None = None
    max_results: int = Field(default=100, ge=1, le=100)


class BranchInfo(GatewayBaseModel):
    name: str
    commit_sha: str
    protected: bool | None = None
    protection_url: str | None = None


class ListBranchesResponse(GatewayBaseModel):
    branches: list[BranchInfo]
    total_count: int


class GetBranchRequest(GatewayBaseModel):
    branch: str


class GetBranchResponse(GatewayBaseModel):
    branch: BranchInfo


class GetBranchProtectionRequest(GatewayBaseModel):
    branch: str


class GetBranchProtectionResponse(GatewayBaseModel):
    branch: str
    protected: bool
    protection: dict[str, Any] | None = None


class CompareRefsRequest(GatewayBaseModel):
    base: str
    head: str
    include_patch: bool = True


class CompareFile(GatewayBaseModel):
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    previous_filename: str | None = None
    patch: str | None = None
    sha: str | None = None


class CompareRefsResponse(GatewayBaseModel):
    status: str
    ahead_by: int
    behind_by: int
    total_commits: int
    base_commit_sha: str | None = None
    merge_base_commit_sha: str | None = None
    files: list[CompareFile]
    html_url: str | None = None
    diff_url: str | None = None
    patch_url: str | None = None


class ExportRepoSnapshotRequest(GatewayBaseModel):
    ref: str = "main"
    archive_format: Literal["zip", "tar"] = "zip"
    include_git: bool = Field(default=False, description="GitHub-generated archives do not contain .git; true returns a warning.")
    include_archive_base64: bool = Field(default=False, description="Return the archive bytes as base64 in JSON when size permits.")
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=lambda: ["node_modules/**", "dist/**", "build/**", ".git/**"])
    max_bytes: int | None = Field(default=None, ge=1)


class ExportRepoSnapshotResponse(GatewayBaseModel):
    ref: str
    head_sha: str
    default_branch: str
    archive_url: str
    archive_format: Literal["zip", "tar"]
    archive_base64: str | None = None
    sha256: str | None = None
    file_count: int
    total_bytes: int
    truncated: bool = False
    warning: str | None = None


class SearchCodeRequest(GatewayBaseModel):
    ref: str = "main"
    query: str = Field(min_length=1)
    path_prefix: str | None = None
    extensions: list[str] = Field(default_factory=list)
    max_results: int = Field(default=100, ge=1, le=100)


class SearchCodeMatch(GatewayBaseModel):
    path: str
    sha: str
    line_number: int
    line_excerpt: str


class SearchCodeResponse(GatewayBaseModel):
    ref: str
    matches: list[SearchCodeMatch]
    total_count: int
    truncated: bool = False
