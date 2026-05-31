from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass, field

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.common import ChangedFile
from app.models.commits import ApplyPatchAndCommitRequest, ApplyPatchAndCommitResponse, CommitFilesRequest, CommitFilesResponse, PatchChangedFile
from app.policy.rules import Policy
from app.storage.audit import AuditStore

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_MODE_RE = re.compile(r"^(?:old mode|new mode|new file mode|deleted file mode) (\d{6})$")
_INDEX_MODE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: (\d{6}))?$")
_SUPPORTED_BLOB_MODES = {"100644", "100755", "120000"}


@dataclass
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class BinaryPatchBlock:
    kind: str
    size: int
    lines: list[str] = field(default_factory=list)


@dataclass
class ParsedPatchFile:
    old_path: str
    new_path: str
    operation: str
    hunks: list[PatchHunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    binary_forward: BinaryPatchBlock | None = None

    @property
    def result_path(self) -> str:
        return self.old_path if self.operation == "deleted" else self.new_path


class CommitService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, audit: AuditStore) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.audit = audit

    async def commit_files(self, owner: str, repo: str, request: CommitFilesRequest) -> CommitFilesResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.branch)
        scope = f"{owner}/{repo}:commit_files:{request.branch}"
        request_payload = request.model_dump()
        if request.idempotency_key:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload)
            if cached:
                return CommitFilesResponse(**cached)

        if len(request.files) > self.settings.max_files_per_commit:
            raise ApiError(
                ErrorCode.TOO_MANY_FILES,
                "Too many files in one commit.",
                status_code=413,
                details={"max_files_per_commit": self.settings.max_files_per_commit, "actual_files": len(request.files)},
            )

        total_bytes = 0
        seen_paths: set[str] = set()
        tree_entries: list[dict] = []
        changed_files: list[ChangedFile] = []
        normalized_changes = []
        for change in request.files:
            normalized_path = self.policy.assert_write_path_allowed(change.path, operation=change.operation)
            if normalized_path in seen_paths:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Duplicate path in commit_files request.", status_code=422, details={"path": normalized_path})
            seen_paths.add(normalized_path)
            if change.operation == "upsert":
                content = change.content or ""
                encoded = content.encode("utf-8")
                if self.policy.looks_binary(encoded):
                    raise ApiError(ErrorCode.BINARY_FILE_NOT_ALLOWED, "Binary-looking content cannot be committed.", status_code=403, details={"path": normalized_path})
                total_bytes += len(encoded)
                tree_entries.append({"path": normalized_path, "mode": "100644", "type": "blob", "content": content})
            else:
                tree_entries.append({"path": normalized_path, "mode": "100644", "type": "blob", "sha": None})
            normalized_changes.append((normalized_path, change))
            changed_files.append(ChangedFile(path=normalized_path, operation=change.operation, previous_sha=change.previous_sha))

        if total_bytes > self.settings.max_total_commit_size:
            raise ApiError(
                ErrorCode.TOTAL_SIZE_TOO_LARGE,
                "Commit payload exceeds MAX_TOTAL_COMMIT_SIZE.",
                status_code=413,
                details={"actual_bytes": total_bytes, "max_total_commit_size": self.settings.max_total_commit_size},
            )

        current_head = await self.github.get_branch_head(owner, repo, request.branch)
        if current_head != request.expected_head_sha:
            raise ApiError(
                ErrorCode.BRANCH_HEAD_CHANGED,
                "The branch head has changed since the client last read it.",
                status_code=409,
                suggestion="Read the latest branch head / files, then retry commit_files with the new expected_head_sha.",
                details={"expected_head_sha": request.expected_head_sha, "actual_head_sha": current_head},
            )

        base_commit = await self.github.get_commit_object(owner, repo, current_head)
        base_tree_sha = base_commit["tree"]["sha"]

        await self._assert_previous_sha_matches(owner, repo, base_tree_sha, normalized_changes)

        tree = await self.github.create_tree(owner, repo, base_tree_sha, tree_entries)
        commit = await self.github.create_commit(owner, repo, request.commit_message, tree["sha"], [current_head])
        new_head = commit["sha"]
        await self._update_ref_or_raise(owner, repo, request.branch, current_head, new_head)

        for item in changed_files:
            if item.operation == "upsert":
                item.new_sha = None

        response = CommitFilesResponse(
            commit_sha=new_head,
            previous_head_sha=current_head,
            new_head_sha=new_head,
            changed_files=changed_files,
            commit_url=f"https://github.com/{owner}/{repo}/commit/{new_head}",
        )
        if request.idempotency_key:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
        return response

    async def apply_patch_and_commit(self, owner: str, repo: str, request: ApplyPatchAndCommitRequest) -> ApplyPatchAndCommitResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.branch)
        scope = f"{owner}/{repo}:apply_patch:{request.branch}"
        request_payload = request.model_dump()
        if request.idempotency_key:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload)
            if cached:
                return ApplyPatchAndCommitResponse(**cached)

        parsed_files = parse_git_patch(request.patch)
        if not parsed_files:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Patch does not contain any git diff file sections.", status_code=422)
        if len(parsed_files) > self.settings.max_files_per_commit:
            raise ApiError(
                ErrorCode.TOO_MANY_FILES,
                "Patch changes too many files in one commit.",
                status_code=413,
                details={"max_files_per_commit": self.settings.max_files_per_commit, "actual_files": len(parsed_files)},
            )

        current_head = await self.github.get_branch_head(owner, repo, request.branch)
        if current_head != request.expected_head_sha:
            raise ApiError(
                ErrorCode.BRANCH_HEAD_CHANGED,
                "The branch head has changed since the client last read it.",
                status_code=409,
                suggestion="Read the latest branch head, regenerate the patch if needed, then retry applyPatchAndCommit.",
                details={"expected_head_sha": request.expected_head_sha, "actual_head_sha": current_head},
            )
        base_commit = await self.github.get_commit_object(owner, repo, current_head)
        base_tree_sha = base_commit["tree"]["sha"]
        tree_payload = await self.github.get_tree(owner, repo, base_tree_sha, recursive=True)
        tree_map = {entry.get("path"): entry for entry in tree_payload.get("tree", []) if entry.get("type") == "blob"}

        tree_entries: list[dict] = []
        changed_files: list[PatchChangedFile] = []
        total_bytes = 0
        seen_paths: set[str] = set()
        for file_patch in parsed_files:
            entries, changed, output_bytes = await self._tree_entries_for_patch_file(owner, repo, tree_map, file_patch)
            for entry in entries:
                path = entry["path"]
                if path in seen_paths:
                    raise ApiError(ErrorCode.VALIDATION_ERROR, "Patch changes the same path more than once.", status_code=422, details={"path": path})
                seen_paths.add(path)
                tree_entries.append(entry)
            total_bytes += output_bytes
            changed_files.append(changed)

        if total_bytes > self.settings.max_total_commit_size:
            raise ApiError(
                ErrorCode.TOTAL_SIZE_TOO_LARGE,
                "Patched content exceeds MAX_TOTAL_COMMIT_SIZE.",
                status_code=413,
                details={"actual_bytes": total_bytes, "max_total_commit_size": self.settings.max_total_commit_size},
            )

        if request.dry_run:
            response = ApplyPatchAndCommitResponse(
                commit_sha=None,
                previous_head_sha=current_head,
                new_head_sha=current_head,
                changed_files=changed_files,
                commit_url=None,
                dry_run=True,
            )
            if request.idempotency_key:
                self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
            return response

        tree = await self.github.create_tree(owner, repo, base_tree_sha, tree_entries)
        commit = await self.github.create_commit(owner, repo, request.commit_message, tree["sha"], [current_head])
        new_head = commit["sha"]
        await self._update_ref_or_raise(owner, repo, request.branch, current_head, new_head)
        response = ApplyPatchAndCommitResponse(
            commit_sha=new_head,
            previous_head_sha=current_head,
            new_head_sha=new_head,
            changed_files=changed_files,
            commit_url=f"https://github.com/{owner}/{repo}/commit/{new_head}",
            dry_run=False,
        )
        if request.idempotency_key:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=request_payload, response_payload=response.model_dump())
        return response

    async def _tree_entries_for_patch_file(
        self,
        owner: str,
        repo: str,
        tree_map: dict[str, dict],
        file_patch: ParsedPatchFile,
    ) -> tuple[list[dict], PatchChangedFile, int]:
        entries: list[dict] = []
        previous_path = None
        output_bytes = 0

        if file_patch.operation == "added":
            new_path = self.policy.assert_write_path_allowed(file_patch.new_path, operation="upsert")
            new_mode = _resolve_new_mode(file_patch, None)
            new_bytes = await self._build_output_bytes(owner, repo, file_patch, None, new_path)
            entries.append(await self._build_blob_tree_entry(owner, repo, new_path, new_mode, new_bytes, previous_sha=None, previous_bytes=None))
            output_bytes = len(new_bytes)
            result_path = new_path
        elif file_patch.operation == "deleted":
            old_path = self.policy.assert_write_path_allowed(file_patch.old_path, operation="delete")
            old_entry = tree_map.get(old_path)
            if old_entry is None:
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Patch deletes a file that does not exist at the branch head.", status_code=422, details={"path": old_path})
            new_bytes = await self._build_output_bytes(owner, repo, file_patch, old_entry, old_path)
            if new_bytes:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary or text delete patch must result in empty content.", status_code=422, details={"path": old_path})
            entries.append({"path": old_path, "mode": old_entry.get("mode") or file_patch.old_mode or "100644", "type": "blob", "sha": None})
            output_bytes = 0
            result_path = old_path
        elif file_patch.operation == "renamed":
            old_path = self.policy.assert_write_path_allowed(file_patch.old_path, operation="delete")
            new_path = self.policy.assert_write_path_allowed(file_patch.new_path, operation="upsert")
            old_entry = tree_map.get(old_path)
            if old_entry is None:
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Patch renames a file that does not exist at the branch head.", status_code=422, details={"path": old_path})
            old_bytes = await self._read_existing_bytes(owner, repo, old_path, old_entry["sha"])
            new_mode = _resolve_new_mode(file_patch, old_entry.get("mode"))
            new_bytes = await self._build_output_bytes(owner, repo, file_patch, old_entry, new_path)
            entries.append({"path": old_path, "mode": old_entry.get("mode") or file_patch.old_mode or "100644", "type": "blob", "sha": None})
            entries.append(await self._build_blob_tree_entry(owner, repo, new_path, new_mode, new_bytes, previous_sha=old_entry.get("sha"), previous_bytes=old_bytes))
            previous_path = old_path
            output_bytes = len(new_bytes) if new_bytes != old_bytes else 0
            result_path = new_path
        else:
            path = self.policy.assert_write_path_allowed(file_patch.new_path, operation="upsert")
            old_entry = tree_map.get(path)
            if old_entry is None:
                raise ApiError(ErrorCode.PATH_NOT_ALLOWED, "Patch modifies a file that does not exist at the branch head.", status_code=422, details={"path": path})
            old_bytes = await self._read_existing_bytes(owner, repo, path, old_entry["sha"])
            new_mode = _resolve_new_mode(file_patch, old_entry.get("mode"))
            new_bytes = await self._build_output_bytes(owner, repo, file_patch, old_entry, path)
            entries.append(await self._build_blob_tree_entry(owner, repo, path, new_mode, new_bytes, previous_sha=old_entry.get("sha"), previous_bytes=old_bytes))
            output_bytes = len(new_bytes) if new_bytes != old_bytes else 0
            result_path = path

        changed = PatchChangedFile(
            path=result_path,
            operation=file_patch.operation,  # type: ignore[arg-type]
            previous_path=previous_path,
            additions=file_patch.additions,
            deletions=file_patch.deletions,
        )
        return entries, changed, output_bytes

    async def _build_output_bytes(self, owner: str, repo: str, file_patch: ParsedPatchFile, old_entry: dict | None, path: str) -> bytes:
        old_bytes = b""
        if old_entry is not None:
            old_bytes = await self._read_existing_bytes(owner, repo, file_patch.old_path, old_entry["sha"])
        if file_patch.binary:
            return _apply_binary_patch_block(file_patch, old_bytes)
        old_text = old_bytes.decode("utf-8", errors="replace")
        if file_patch.operation == "added":
            old_text = ""
        output_text = apply_hunks(old_text, file_patch.hunks, path) if file_patch.hunks else old_text
        self._assert_text_safe(path, output_text)
        return output_text.encode("utf-8")

    async def _build_blob_tree_entry(
        self,
        owner: str,
        repo: str,
        path: str,
        mode: str,
        content: bytes,
        *,
        previous_sha: str | None,
        previous_bytes: bytes | None,
    ) -> dict:
        _assert_supported_mode(mode, path)
        if previous_sha and previous_bytes == content:
            return {"path": path, "mode": mode, "type": "blob", "sha": previous_sha}
        if mode != "120000" and not self.policy.looks_binary(content):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                return {"path": path, "mode": mode, "type": "blob", "content": text}
        blob = await self.github.create_blob(owner, repo, content)
        return {"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]}

    async def _read_existing_bytes(self, owner: str, repo: str, path: str, sha: str) -> bytes:
        return await self.github.get_blob(owner, repo, sha)

    def _assert_text_safe(self, path: str, text: str) -> None:
        encoded = text.encode("utf-8")
        if self.policy.looks_binary(encoded):
            raise ApiError(ErrorCode.BINARY_FILE_NOT_ALLOWED, "Patched content looks binary and cannot be committed.", status_code=403, details={"path": path})

    async def _assert_previous_sha_matches(self, owner: str, repo: str, base_tree_sha: str, normalized_changes: list[tuple[str, object]]) -> None:
        needs_check = {path: change for path, change in normalized_changes if getattr(change, "previous_sha", None)}
        if not needs_check:
            return
        tree = await self.github.get_tree(owner, repo, base_tree_sha, recursive=True)
        shas = {entry.get("path"): entry.get("sha") for entry in tree.get("tree", [])}
        for path, change in needs_check.items():
            expected = getattr(change, "previous_sha", None)
            actual = shas.get(path)
            if actual != expected:
                raise ApiError(
                    ErrorCode.BRANCH_HEAD_CHANGED,
                    "A file changed since it was last read.",
                    status_code=409,
                    suggestion="Read the file again and retry with the latest previous_sha and expected_head_sha.",
                    details={"path": path, "expected_previous_sha": expected, "actual_sha": actual},
                )

    async def _update_ref_or_raise(self, owner: str, repo: str, branch: str, expected_head: str, new_head: str) -> None:
        try:
            await self.github.update_ref(owner, repo, branch, new_head, force=False)
        except ApiError as exc:
            if exc.error_code == ErrorCode.GITHUB_CONFLICT:
                latest = await self.github.get_branch_head(owner, repo, branch)
                raise ApiError(
                    ErrorCode.BRANCH_HEAD_CHANGED,
                    "The branch head changed while creating the commit.",
                    status_code=409,
                    suggestion="Read the latest branch head / files, then retry with the new expected_head_sha.",
                    details={"expected_head_sha": expected_head, "actual_head_sha": latest},
                ) from exc
            raise


def parse_git_patch(patch: str) -> list[ParsedPatchFile]:
    lines = patch.splitlines(keepends=True)
    files: list[ParsedPatchFile] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("diff --git "):
            i += 1
            continue
        header = lines[i].rstrip("\n")
        match = _DIFF_RE.match(header)
        if not match:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported diff --git header format.", status_code=422, details={"header": header})
        old_path, new_path = match.group(1), match.group(2)
        i += 1
        section: list[str] = []
        while i < len(lines) and not lines[i].startswith("diff --git "):
            section.append(lines[i])
            i += 1
        files.append(_parse_file_section(old_path, new_path, section))
    return files


def _parse_file_section(old_path: str, new_path: str, section: list[str]) -> ParsedPatchFile:
    operation = "modified"
    hunks: list[PatchHunk] = []
    binary = False
    old_mode: str | None = None
    new_mode: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    additions = 0
    deletions = 0
    current_hunk: PatchHunk | None = None
    binary_forward: BinaryPatchBlock | None = None

    i = 0
    while i < len(section):
        raw_line = section[i]
        line = raw_line.rstrip("\n")
        if line.startswith("new file mode"):
            operation = "added"
            new_mode = _extract_mode(line)
        elif line.startswith("deleted file mode"):
            operation = "deleted"
            old_mode = _extract_mode(line)
        elif line.startswith("old mode"):
            old_mode = _extract_mode(line)
        elif line.startswith("new mode"):
            new_mode = _extract_mode(line)
        elif line.startswith("rename from "):
            rename_from = line[len("rename from ") :]
        elif line.startswith("rename to "):
            rename_to = line[len("rename to ") :]
        elif line.startswith("index "):
            index_mode = _extract_index_mode(line)
            if index_mode:
                old_mode = old_mode or index_mode
                new_mode = new_mode or index_mode
        elif line.startswith("Binary files "):
            binary = True
        elif line.startswith("GIT binary patch"):
            binary = True
            binary_forward, i = _parse_binary_forward_block(section, i + 1)
            continue
        elif line.startswith("--- "):
            marker = line[4:].strip()
            if marker == "/dev/null":
                operation = "added"
            elif marker.startswith("a/"):
                old_path = marker[2:]
        elif line.startswith("+++ "):
            marker = line[4:].strip()
            if marker == "/dev/null":
                operation = "deleted"
            elif marker.startswith("b/"):
                new_path = marker[2:]
        elif line.startswith("@@ "):
            match = _HUNK_RE.match(line)
            if not match:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported hunk header format.", status_code=422, details={"header": line})
            current_hunk = PatchHunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2) or "1"),
                new_start=int(match.group(3)),
                new_count=int(match.group(4) or "1"),
            )
            hunks.append(current_hunk)
        elif current_hunk is not None:
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                additions += 1
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                deletions += 1
            current_hunk.lines.append(raw_line)
        i += 1

    if rename_from and rename_to:
        operation = "renamed"
        old_path = rename_from
        new_path = rename_to
    return ParsedPatchFile(
        old_path=old_path,
        new_path=new_path,
        operation=operation,
        hunks=hunks,
        additions=additions,
        deletions=deletions,
        binary=binary,
        old_mode=old_mode,
        new_mode=new_mode,
        binary_forward=binary_forward,
    )


def _extract_mode(line: str) -> str:
    match = _MODE_RE.match(line)
    if not match:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported mode header format.", status_code=422, details={"header": line})
    return match.group(1)


def _extract_index_mode(line: str) -> str | None:
    match = _INDEX_MODE_RE.match(line)
    if not match:
        return None
    return match.group(1)


def _parse_binary_forward_block(section: list[str], start_index: int) -> tuple[BinaryPatchBlock, int]:
    i = start_index
    while i < len(section) and not section[i].strip():
        i += 1
    if i >= len(section):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch is missing its forward block.", status_code=422)
    header = section[i].strip()
    parts = header.split()
    if len(parts) != 2 or parts[0] not in {"literal", "delta"}:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported binary patch block header.", status_code=422, details={"header": header})
    try:
        size = int(parts[1])
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch size is invalid.", status_code=422, details={"header": header}) from exc
    i += 1
    lines: list[str] = []
    while i < len(section):
        current = section[i].rstrip("\n")
        if not current:
            break
        lines.append(current)
        i += 1
    return BinaryPatchBlock(kind=parts[0], size=size, lines=lines), i


def _resolve_new_mode(file_patch: ParsedPatchFile, current_mode: str | None) -> str:
    mode = file_patch.new_mode or current_mode or "100644"
    _assert_supported_mode(mode, file_patch.result_path)
    return mode


def _assert_supported_mode(mode: str, path: str) -> None:
    if mode not in _SUPPORTED_BLOB_MODES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Unsupported file mode in patch.",
            status_code=422,
            details={"path": path, "mode": mode, "supported_modes": sorted(_SUPPORTED_BLOB_MODES)},
        )


def _apply_binary_patch_block(file_patch: ParsedPatchFile, old_bytes: bytes) -> bytes:
    block = file_patch.binary_forward
    if block is None:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch is missing its forward payload.", status_code=422, details={"path": file_patch.result_path})
    decoded = _decode_git_binary_payload(block.lines)
    if block.kind == "literal":
        try:
            result = zlib.decompress(decoded)
        except zlib.error as exc:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch literal payload could not be decompressed.", status_code=422, details={"path": file_patch.result_path}) from exc
    elif block.kind == "delta":
        try:
            delta = zlib.decompress(decoded)
        except zlib.error as exc:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch delta payload could not be decompressed.", status_code=422, details={"path": file_patch.result_path}) from exc
        result = _apply_git_delta(old_bytes, delta, file_patch.result_path)
    else:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported binary patch block kind.", status_code=422, details={"path": file_patch.result_path, "kind": block.kind})
    if len(result) != block.size:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Binary patch output size does not match its declared size.",
            status_code=422,
            details={"path": file_patch.result_path, "declared_size": block.size, "actual_size": len(result)},
        )
    return result


def _decode_git_binary_payload(lines: list[str]) -> bytes:
    chunks = bytearray()
    for line in lines:
        if not line:
            continue
        expected = _decode_git_binary_line_length(line[0])
        payload = line[1:]
        try:
            chunk = base64.b85decode(payload.encode("ascii"))
        except ValueError as exc:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch line contains invalid base85 data.", status_code=422, details={"line": line[:120]}) from exc
        if len(chunk) < expected:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Binary patch line length prefix exceeds decoded payload size.",
                status_code=422,
                details={"line": line[:120], "expected_bytes": expected, "actual_bytes": len(chunk)},
            )
        chunks.extend(chunk[:expected])
    return bytes(chunks)


def _decode_git_binary_line_length(prefix: str) -> int:
    if "A" <= prefix <= "Z":
        return ord(prefix) - ord("A") + 1
    if "a" <= prefix <= "z":
        return ord(prefix) - ord("a") + 27
    raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary patch line prefix is invalid.", status_code=422, details={"prefix": prefix})


def _read_git_delta_size(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    size = 0
    while True:
        if offset >= len(data):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta payload ended unexpectedly while reading size.", status_code=422)
        byte = data[offset]
        offset += 1
        size |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return size, offset
        shift += 7


def _apply_git_delta(source: bytes, delta: bytes, path: str) -> bytes:
    source_size, offset = _read_git_delta_size(delta, 0)
    if source_size != len(source):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Binary delta source size does not match the current blob.",
            status_code=409,
            suggestion="Read the latest branch head and regenerate the patch from the current file content.",
            details={"path": path, "expected_source_size": source_size, "actual_source_size": len(source)},
        )
    target_size, offset = _read_git_delta_size(delta, offset)
    output = bytearray()
    while offset < len(delta):
        command = delta[offset]
        offset += 1
        if command & 0x80:
            copy_offset = 0
            copy_size = 0
            for shift, mask in enumerate((0x01, 0x02, 0x04, 0x08)):
                if command & mask:
                    if offset >= len(delta):
                        raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta copy command is truncated.", status_code=422, details={"path": path})
                    copy_offset |= delta[offset] << (shift * 8)
                    offset += 1
            for shift, mask in enumerate((0x10, 0x20, 0x40)):
                if command & mask:
                    if offset >= len(delta):
                        raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta copy size is truncated.", status_code=422, details={"path": path})
                    copy_size |= delta[offset] << (shift * 8)
                    offset += 1
            if copy_size == 0:
                copy_size = 0x10000
            end = copy_offset + copy_size
            if end > len(source):
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta copy command exceeds source blob size.", status_code=422, details={"path": path})
            output.extend(source[copy_offset:end])
            continue
        if command == 0:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta contains an invalid zero-length command.", status_code=422, details={"path": path})
        end = offset + command
        if end > len(delta):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Binary delta insert command is truncated.", status_code=422, details={"path": path})
        output.extend(delta[offset:end])
        offset = end
    if len(output) != target_size:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Binary delta output size does not match its declared target size.",
            status_code=422,
            details={"path": path, "declared_target_size": target_size, "actual_size": len(output)},
        )
    return bytes(output)


def apply_hunks(old_text: str, hunks: list[PatchHunk], path: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        target = max(hunk.old_start - 1, 0)
        if target < cursor:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Patch hunks overlap or are out of order.", status_code=422, details={"path": path})
        output.extend(old_lines[cursor:target])
        cursor = target
        for raw_line in hunk.lines:
            if raw_line.startswith("\\ No newline at end of file"):
                continue
            if not raw_line:
                continue
            prefix = raw_line[0]
            content = raw_line[1:]
            if prefix == " ":
                _assert_patch_line_matches(old_lines, cursor, content, path)
                output.append(old_lines[cursor])
                cursor += 1
            elif prefix == "-":
                _assert_patch_line_matches(old_lines, cursor, content, path)
                cursor += 1
            elif prefix == "+":
                output.append(content)
            else:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Unsupported patch line prefix.", status_code=422, details={"path": path, "line": raw_line[:120]})
    output.extend(old_lines[cursor:])
    return "".join(output)


def _assert_patch_line_matches(old_lines: list[str], cursor: int, expected: str, path: str) -> None:
    if cursor >= len(old_lines):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Patch context exceeds file length.", status_code=422, details={"path": path})
    actual = old_lines[cursor]
    if actual.rstrip("\r\n") != expected.rstrip("\r\n"):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Patch context does not match the current branch head.",
            status_code=409,
            suggestion="Regenerate the patch from the latest branch head.",
            details={"path": path, "expected": expected.rstrip("\r\n"), "actual": actual.rstrip("\r\n")},
        )
