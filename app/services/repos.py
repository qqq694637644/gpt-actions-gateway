from __future__ import annotations

import base64
import fnmatch
import hashlib
from pathlib import PurePosixPath
from typing import Iterable

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.repos import (
    BranchInfo,
    CompareFile,
    CompareRefsRequest,
    CompareRefsResponse,
    EmptyRequest,
    ExportRepoSnapshotRequest,
    ExportRepoSnapshotResponse,
    GetBranchProtectionRequest,
    GetBranchProtectionResponse,
    GetBranchRequest,
    GetBranchResponse,
    GetDefaultBranchResponse,
    GetRepositoryResponse,
    ListBranchesRequest,
    ListBranchesResponse,
    RepositoryInfo,
    SearchCodeMatch,
    SearchCodeRequest,
    SearchCodeResponse,
)
from app.policy.rules import Policy, is_sha


class RepoService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings

    async def get_repository(self, owner: str, repo: str, request: EmptyRequest | None = None) -> GetRepositoryResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw = await self.github.get_repository(owner, repo)
        return GetRepositoryResponse(repository=_repo_info(raw))

    async def get_default_branch(self, owner: str, repo: str, request: EmptyRequest | None = None) -> GetDefaultBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw = await self.github.get_repository(owner, repo)
        return GetDefaultBranchResponse(default_branch=raw.get("default_branch") or self.settings.default_base_branch)

    async def list_branches(self, owner: str, repo: str, request: ListBranchesRequest) -> ListBranchesResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw = await self.github.list_branches(owner, repo, protected=request.protected, per_page=request.max_results)
        branches = [_branch_info(item) for item in raw[: request.max_results]]
        return ListBranchesResponse(branches=branches, total_count=len(branches))

    async def get_branch(self, owner: str, repo: str, request: GetBranchRequest) -> GetBranchResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_read_ref_allowed(request.branch)
        raw = await self.github.get_branch(owner, repo, request.branch)
        return GetBranchResponse(branch=_branch_info(raw))

    async def get_branch_protection(self, owner: str, repo: str, request: GetBranchProtectionRequest) -> GetBranchProtectionResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_base_branch_allowed(request.branch)
        try:
            protection = await self.github.get_branch_protection(owner, repo, request.branch)
        except ApiError as exc:
            if exc.error_code == ErrorCode.GITHUB_NOT_FOUND:
                return GetBranchProtectionResponse(branch=request.branch, protected=False, protection=None)
            raise
        return GetBranchProtectionResponse(branch=request.branch, protected=True, protection=protection)

    async def compare_refs(self, owner: str, repo: str, request: CompareRefsRequest) -> CompareRefsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self._assert_compare_ref_allowed(request.base)
        self._assert_compare_ref_allowed(request.head)
        payload = await self.github.compare_refs(owner, repo, request.base, request.head)
        files = []
        for item in payload.get("files", []) or []:
            files.append(
                CompareFile(
                    filename=item.get("filename", ""),
                    status=item.get("status", ""),
                    additions=int(item.get("additions") or 0),
                    deletions=int(item.get("deletions") or 0),
                    changes=int(item.get("changes") or 0),
                    previous_filename=item.get("previous_filename"),
                    patch=item.get("patch") if request.include_patch else None,
                    sha=item.get("sha"),
                )
            )
        return CompareRefsResponse(
            status=payload.get("status", ""),
            ahead_by=int(payload.get("ahead_by") or 0),
            behind_by=int(payload.get("behind_by") or 0),
            total_commits=int(payload.get("total_commits") or 0),
            base_commit_sha=(payload.get("base_commit") or {}).get("sha"),
            merge_base_commit_sha=(payload.get("merge_base_commit") or {}).get("sha"),
            files=files,
            html_url=payload.get("html_url"),
            diff_url=payload.get("diff_url"),
            patch_url=payload.get("patch_url"),
        )

    async def export_snapshot(self, owner: str, repo: str, request: ExportRepoSnapshotRequest) -> ExportRepoSnapshotResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_read_ref_allowed(request.ref)
        repo_info = await self.github.get_repository(owner, repo)
        head_sha, tree_sha = await self._resolve_tree_sha(owner, repo, request.ref)
        tree = await self.github.get_tree(owner, repo, tree_sha, recursive=True)
        paths = []
        total_bytes = 0
        for entry in tree.get("tree", []) or []:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            if not self._snapshot_path_matches(path, request.include_patterns, request.exclude_patterns):
                continue
            paths.append(path)
            total_bytes += int(entry.get("size") or 0)
        fmt_path = "zipball" if request.archive_format == "zip" else "tarball"
        archive_url = f"{self.settings.github_api_base_url.rstrip('/')}/repos/{owner}/{repo}/{fmt_path}/{request.ref}"
        archive_base64 = None
        digest = None
        warning_parts = []
        if request.include_git:
            warning_parts.append("GitHub-generated snapshot archives do not include .git history.")
        if request.include_patterns or request.exclude_patterns:
            warning_parts.append("include_patterns/exclude_patterns are reflected in metadata only; GitHub archive bytes contain the full repository snapshot.")
        if request.include_archive_base64:
            data = await self.github.download_archive(owner, repo, request.ref, archive_format=request.archive_format)
            max_bytes = request.max_bytes or self.settings.max_blob_read_bytes
            if len(data) > max_bytes:
                raise ApiError(
                    ErrorCode.TOTAL_SIZE_TOO_LARGE,
                    "Repository snapshot archive exceeds the allowed response size.",
                    status_code=413,
                    details={"actual_bytes": len(data), "max_bytes": max_bytes},
                )
            archive_base64 = base64.b64encode(data).decode("ascii")
            digest = hashlib.sha256(data).hexdigest()
        return ExportRepoSnapshotResponse(
            ref=request.ref,
            head_sha=head_sha,
            default_branch=repo_info.get("default_branch") or self.settings.default_base_branch,
            archive_url=archive_url,
            archive_format=request.archive_format,
            archive_base64=archive_base64,
            sha256=digest,
            file_count=len(paths),
            total_bytes=total_bytes,
            truncated=bool(tree.get("truncated")),
            warning=" ".join(warning_parts) or None,
        )

    async def search_code(self, owner: str, repo: str, request: SearchCodeRequest) -> SearchCodeResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_read_ref_allowed(request.ref)
        path_prefix = self.policy.assert_tree_path_allowed(request.path_prefix)
        extensions = _parse_extensions(request.extensions)
        _, tree_sha = await self._resolve_tree_sha(owner, repo, request.ref)
        tree = await self.github.get_tree(owner, repo, tree_sha, recursive=True)
        needle = request.query.lower()
        matches: list[SearchCodeMatch] = []
        truncated = False
        for entry in tree.get("tree", []) or []:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            if self.policy.is_excluded_tree_entry(path):
                continue
            if path_prefix and path != path_prefix and not path.startswith(path_prefix.rstrip("/") + "/"):
                continue
            if extensions and PurePosixPath(path).suffix.lower() not in extensions:
                continue
            size = int(entry.get("size") or 0)
            if size > self.settings.max_blob_read_bytes:
                continue
            if self.policy.has_binary_extension(path):
                continue
            data = await self.github.get_blob(owner, repo, entry["sha"])
            if self.policy.looks_binary(data):
                continue
            text = data.decode("utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append(SearchCodeMatch(path=path, sha=entry["sha"], line_number=line_no, line_excerpt=line.strip()[:300]))
                    if len(matches) >= request.max_results:
                        truncated = True
                        return SearchCodeResponse(ref=request.ref, matches=matches, total_count=len(matches), truncated=truncated)
        return SearchCodeResponse(ref=request.ref, matches=matches, total_count=len(matches), truncated=truncated or bool(tree.get("truncated")))

    async def _resolve_tree_sha(self, owner: str, repo: str, ref: str) -> tuple[str, str]:
        commit_sha = ref if is_sha(ref) else await self.github.get_branch_head(owner, repo, ref)
        commit = await self.github.get_commit_object(owner, repo, commit_sha)
        return commit_sha, commit["tree"]["sha"]

    def _assert_compare_ref_allowed(self, ref: str) -> None:
        branch = ref.split(":", 1)[-1]
        if is_sha(branch):
            return
        self.policy.assert_read_ref_allowed(branch)

    @staticmethod
    def _snapshot_path_matches(path: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
        if include_patterns and not any(fnmatch.fnmatchcase(path, pattern) for pattern in include_patterns):
            return False
        if exclude_patterns and any(fnmatch.fnmatchcase(path, pattern) for pattern in exclude_patterns):
            return False
        return True


def _repo_info(raw: dict) -> RepositoryInfo:
    return RepositoryInfo(
        full_name=raw.get("full_name", ""),
        private=raw.get("private"),
        default_branch=raw.get("default_branch") or "main",
        html_url=raw.get("html_url"),
        description=raw.get("description"),
        fork=raw.get("fork"),
        archived=raw.get("archived"),
        disabled=raw.get("disabled"),
        visibility=raw.get("visibility"),
        allow_squash_merge=raw.get("allow_squash_merge"),
        allow_merge_commit=raw.get("allow_merge_commit"),
        allow_rebase_merge=raw.get("allow_rebase_merge"),
        permissions=raw.get("permissions") or {},
    )


def _branch_info(raw: dict) -> BranchInfo:
    commit = raw.get("commit") or {}
    return BranchInfo(
        name=raw.get("name", ""),
        commit_sha=commit.get("sha", ""),
        protected=raw.get("protected"),
        protection_url=raw.get("protection_url"),
    )


def _parse_extensions(extensions: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in extensions:
        for part in str(value).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if not part.startswith("."):
                part = "." + part
            result.add(part)
    return result


class RepositoryService(RepoService):
    """Backward-compatible facade used by tests and older clients."""

    async def get_repository(self, owner: str, repo: str, request: EmptyRequest | None = None) -> RepositoryInfo:  # type: ignore[override]
        response = await super().get_repository(owner, repo, request)
        return response.repository

    async def export_repo_snapshot(self, owner: str, repo: str, request: ExportRepoSnapshotRequest) -> ExportRepoSnapshotResponse:
        return await super().export_snapshot(owner, repo, request)
