from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.files import (
    FileContentResponse,
    FileRangeResponse,
    ListTreeResponse,
    ReadFileRangeRequest,
    ReadFileRequest,
    ReadFilesRequest,
    ReadFilesResponse,
    TreeEntry,
)
from app.policy.rules import Policy, is_sha


def _entry_type(entry: dict) -> str:
    mode = entry.get("mode")
    typ = entry.get("type")
    if mode == "120000":
        return "symlink"
    if typ == "commit" or mode == "160000":
        return "submodule"
    if typ == "tree":
        return "dir"
    return "file"


def _parse_extensions(extensions: Iterable[str] | None) -> set[str]:
    result: set[str] = set()
    for value in extensions or []:
        for part in str(value).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if not part.startswith("."):
                part = "." + part
            result.add(part)
    return result


class FileService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings

    async def _resolve_tree_sha(self, owner: str, repo: str, ref: str) -> tuple[str, str]:
        self.policy.assert_read_ref_allowed(ref)
        commit_sha = ref if is_sha(ref) else await self.github.get_branch_head(owner, repo, ref)
        commit = await self.github.get_commit_object(owner, repo, commit_sha)
        return commit_sha, commit["tree"]["sha"]

    async def list_tree(
        self,
        owner: str,
        repo: str,
        *,
        ref: str,
        path: str | None = None,
        extensions: Iterable[str] | None = None,
        max_results: int = 200,
    ) -> ListTreeResponse:
        self.policy.assert_repo_allowed(owner, repo)
        normalized_path = self.policy.assert_tree_path_allowed(path)
        _, tree_sha = await self._resolve_tree_sha(owner, repo, ref)
        payload = await self.github.get_tree(owner, repo, tree_sha, recursive=True)
        requested_extensions = _parse_extensions(extensions)

        entries: list[TreeEntry] = []
        max_results = max(1, min(max_results, 200))
        prefix = f"{normalized_path}/" if normalized_path else None
        for raw in payload.get("tree", []):
            raw_path = raw.get("path", "")
            if self.policy.is_excluded_tree_entry(raw_path):
                continue
            if normalized_path and raw_path != normalized_path and not raw_path.startswith(prefix or ""):
                continue
            typ = _entry_type(raw)
            if requested_extensions and typ == "file" and PurePosixPath(raw_path).suffix.lower() not in requested_extensions:
                continue
            entries.append(TreeEntry(path=raw_path, type=typ, size=raw.get("size"), sha=raw.get("sha")))
            if len(entries) >= max_results:
                break

        return ListTreeResponse(
            ref=ref,
            path=normalized_path,
            entries=entries,
            truncated=len(entries) >= max_results or bool(payload.get("truncated")),
            max_results=max_results,
        )

    async def _read_content_bytes(self, owner: str, repo: str, *, ref: str, path: str) -> tuple[str, int, bytes]:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_read_ref_allowed(ref)
        normalized = self.policy.assert_read_path_allowed(path)
        metadata = await self.github.get_contents(owner, repo, normalized, ref=ref)
        if isinstance(metadata, list) or metadata.get("type") != "file":
            raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Path is not a file.", status_code=400, details={"path": normalized})
        size = int(metadata.get("size") or 0)
        if size > self.settings.max_blob_read_bytes:
            raise ApiError(
                ErrorCode.FILE_TOO_LARGE,
                "File is too large to read through the gateway.",
                status_code=413,
                suggestion="Reduce MAX_BLOB_READ_BYTES only if you accept the memory cost, or inspect the file manually.",
                details={"path": normalized, "size": size, "max_blob_read_bytes": self.settings.max_blob_read_bytes},
            )
        sha = metadata["sha"]
        content = await self.github.get_blob(owner, repo, sha)
        return sha, size, content

    async def read_file(self, owner: str, repo: str, request: ReadFileRequest) -> FileContentResponse:
        normalized = self.policy.assert_read_path_allowed(request.path)
        sha, size, content_bytes = await self._read_content_bytes(owner, repo, ref=request.ref, path=normalized)
        binary = self.policy.has_binary_extension(normalized) or self.policy.looks_binary(content_bytes)
        if binary:
            return FileContentResponse(
                path=normalized,
                ref=request.ref,
                sha=sha,
                size=size,
                content="",
                truncated=False,
                binary=True,
                suggestion="Binary files are not returned as text. Inspect or modify them outside this gateway.",
            )
        text = content_bytes.decode("utf-8", errors="replace")
        truncated = False
        suggestion = None
        if len(content_bytes) > self.settings.max_file_size:
            truncated = True
            head_bytes = self.settings.max_file_size // 2
            tail_bytes = self.settings.max_file_size - head_bytes
            text = (
                content_bytes[:head_bytes].decode("utf-8", errors="replace")
                + "\n\n...[middle truncated; use files/read-range for specific lines]...\n\n"
                + content_bytes[-tail_bytes:].decode("utf-8", errors="replace")
            )
            suggestion = "Use /files/read-range to read specific line ranges."
        return FileContentResponse(path=normalized, ref=request.ref, sha=sha, size=size, content=text, truncated=truncated, binary=False, suggestion=suggestion)

    async def read_file_range(self, owner: str, repo: str, request: ReadFileRangeRequest) -> FileRangeResponse:
        if request.end_line < request.start_line:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "end_line must be >= start_line.", status_code=422)
        normalized = self.policy.assert_read_path_allowed(request.path)
        _, _, content_bytes = await self._read_content_bytes(owner, repo, ref=request.ref, path=normalized)
        if self.policy.has_binary_extension(normalized) or self.policy.looks_binary(content_bytes):
            raise ApiError(ErrorCode.BINARY_FILE_NOT_ALLOWED, "Binary files cannot be read as line ranges.", status_code=400)
        lines = content_bytes.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start_idx = max(0, request.start_line - 1)
        end_idx = min(total, request.end_line)
        content = "\n".join(lines[start_idx:end_idx])
        return FileRangeResponse(
            path=normalized,
            ref=request.ref,
            start_line=request.start_line,
            end_line=min(request.end_line, total),
            total_lines=total,
            content=content,
            truncated=request.end_line > total,
        )

    async def read_files(self, owner: str, repo: str, request: ReadFilesRequest) -> ReadFilesResponse:
        if len(request.paths) > 20:
            raise ApiError(ErrorCode.TOO_MANY_FILES, "Too many files requested.", status_code=413, details={"max_files": 20})
        files: list[FileContentResponse] = []
        total = 0
        for path in request.paths:
            file_response = await self.read_file(owner, repo, ReadFileRequest(ref=request.ref, path=path))
            total += len(file_response.content.encode("utf-8"))
            if total > self.settings.max_total_read_size:
                raise ApiError(
                    ErrorCode.TOTAL_SIZE_TOO_LARGE,
                    "Total read response would exceed MAX_TOTAL_READ_SIZE.",
                    status_code=413,
                    details={"max_total_read_size": self.settings.max_total_read_size, "actual_bytes": total},
                )
            files.append(file_response)
        return ReadFilesResponse(ref=request.ref, files=files, total_bytes=total, truncated=any(item.truncated for item in files))
