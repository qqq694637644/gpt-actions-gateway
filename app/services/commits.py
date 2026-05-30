from __future__ import annotations

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.common import ChangedFile
from app.models.commits import CommitFilesRequest, CommitFilesResponse
from app.policy.rules import Policy
from app.storage.audit import AuditStore


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
        try:
            await self.github.update_ref(owner, repo, request.branch, new_head, force=False)
        except ApiError as exc:
            if exc.error_code == ErrorCode.GITHUB_CONFLICT:
                latest = await self.github.get_branch_head(owner, repo, request.branch)
                raise ApiError(
                    ErrorCode.BRANCH_HEAD_CHANGED,
                    "The branch head changed while creating the commit.",
                    status_code=409,
                    suggestion="Read the latest branch head / files, then retry commit_files with the new expected_head_sha.",
                    details={"expected_head_sha": current_head, "actual_head_sha": latest},
                ) from exc
            raise

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
