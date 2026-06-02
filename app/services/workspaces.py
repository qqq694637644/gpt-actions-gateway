from __future__ import annotations

from dataclasses import asdict

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.workspaces import (
    PrepareWorkspaceFromMirrorRequest,
    PrepareWorkspaceMirrorRequest,
    PrepareWorkspaceMirrorResponse,
    PrepareWorkspaceRequest,
    PrepareWorkspaceResponse,
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceCommitAndPushRequest,
    WorkspaceCommitAndPushResponse,
    WorkspaceDiffRequest,
    WorkspaceDiffResponse,
    WorkspaceExecPwshRequest,
    WorkspaceExecPwshResponse,
    WorkspaceResetRequest,
    WorkspaceResetResponse,
    WorkspaceStatusRequest,
    WorkspaceStatusResponse,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
    WorkspacePrepareDiagnostics,
)
from app.policy.rules import Policy
from app.storage.audit import AuditStore
from app.workspace.exec import PwshExecutor
from app.workspace.manager import WorkspaceManager, command_hash
from app.workspace.text_ops import (
    apply_text_patch,
    assert_payload_size,
    assert_text_bytes,
    normalize_line_endings,
    parse_codex_patch,
    restore_files,
    sha256_hex,
    snapshot_files,
    validate_write_target,
)


class WorkspaceService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, manager: WorkspaceManager, audit: AuditStore) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.manager = manager
        self.audit = audit
        self.executor = PwshExecutor(settings)

    async def prepare(self, owner: str, repo: str, request: PrepareWorkspaceRequest) -> PrepareWorkspaceResponse:
        result = await self.manager.prepare(
            owner=owner,
            repo=repo,
            branch=request.branch,
            source_pr_number=request.source_pr_number,
            base_ref=request.base_ref,
            workspace_id=request.workspace_id,
            refresh=request.refresh,
            clean=request.clean,
        )
        self._audit(
            operation_id="prepareWorkspace",
            owner=owner,
            repo=repo,
            workspace_id=result.meta.workspace_id,
            branch=result.meta.branch,
            head_sha_after=result.meta.head_sha,
            changed_files=[item.model_dump() for item in result.changed_files],
            metadata=self._prepare_metadata(result),
        )
        return self._response_from_prepare_result(owner, repo, result)

    async def prepare_from_mirror(self, owner: str, repo: str, request: PrepareWorkspaceFromMirrorRequest) -> PrepareWorkspaceResponse:
        result = await self.manager.prepare_from_mirror(
            owner=owner,
            repo=repo,
            branch=request.branch,
            source_pr_number=request.source_pr_number,
            base_ref=request.base_ref,
            workspace_id=request.workspace_id,
            clean=request.clean,
        )
        self._audit(
            operation_id="prepareWorkspaceFromMirror",
            owner=owner,
            repo=repo,
            workspace_id=result.meta.workspace_id,
            branch=result.meta.branch,
            head_sha_after=result.meta.head_sha,
            changed_files=[item.model_dump() for item in result.changed_files],
            metadata=self._prepare_metadata(result),
        )
        return self._response_from_prepare_result(owner, repo, result)

    async def prepare_mirror(self, owner: str, repo: str, request: PrepareWorkspaceMirrorRequest) -> PrepareWorkspaceMirrorResponse:
        result = await self.manager.prepare_mirror(owner, repo, refresh=request.refresh)
        diagnostics = WorkspacePrepareDiagnostics(
            mirror_stage=result.stage,
            mirror_duration_ms=result.duration_ms,
            mirror_pack_bytes=result.pack_bytes,
            mirror_pack_files=result.pack_files,
            workspace_stage="skip",
            workspace_duration_ms=0,
            total_duration_ms=result.duration_ms,
        )
        self._audit(
            operation_id="prepareWorkspaceMirror",
            owner=owner,
            repo=repo,
            metadata={"mirror": asdict(result)},
        )
        return PrepareWorkspaceMirrorResponse(owner=owner, repo=repo, refreshed=result.refreshed, diagnostics=diagnostics)

    async def exec_pwsh(self, owner: str, repo: str, workspace_id: str, request: WorkspaceExecPwshRequest) -> WorkspaceExecPwshResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        timeout = min(request.timeout_seconds or self.settings.workspace_default_timeout_seconds, self.settings.workspace_max_timeout_seconds)
        max_output = min(request.max_output_bytes or self.settings.workspace_max_output_bytes, self.settings.workspace_max_output_bytes)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            result = await self.executor.execute(
                repo_dir,
                script=request.script,
                timeout_seconds=timeout,
                max_output_bytes=max_output,
                allow_network=request.allow_network,
                plain_output=request.plain_output,
                utf8_output=request.utf8_output,
            )
            changed, _, _ = await self.manager.changed_files(repo_dir)
            diff_stat = await self.manager.diff_stat(repo_dir)
        self._audit(
            operation_id="workspaceExecPwsh",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in changed],
            command_hash=command_hash(request.script),
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )
        return WorkspaceExecPwshResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            truncated=result.truncated,
            duration_ms=result.duration_ms,
            changed_files=changed,
            diff_stat=diff_stat,
        )

    async def status(self, owner: str, repo: str, workspace_id: str, request: WorkspaceStatusRequest) -> WorkspaceStatusResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            if request.refresh:
                await self.manager.fetch_branch(repo_dir, meta.branch)
            head_sha = await self.manager.head_sha(repo_dir)
            remote_head = await self.manager.remote_head_sha(repo_dir, meta.branch)
            changed, untracked, conflicts = await self.manager.changed_files(repo_dir)
            ahead, behind = await self.manager.ahead_behind(repo_dir, meta.branch)
        return WorkspaceStatusResponse(
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha=head_sha,
            remote_head_sha=remote_head,
            dirty=bool(changed),
            ahead=ahead,
            behind=behind,
            changed_files=changed,
            untracked_files=untracked,
            conflicts=conflicts,
        )

    async def diff(self, owner: str, repo: str, workspace_id: str, request: WorkspaceDiffRequest) -> WorkspaceDiffResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_bytes = min(request.max_bytes or self.settings.workspace_max_diff_bytes, self.settings.workspace_max_diff_bytes)
        with self.manager.lock(workspace_id):
            diff_text, truncated = await self.manager.diff_text(repo_dir, paths=request.paths, stat_only=request.stat_only, max_bytes=max_bytes)
            diff_stat = diff_text if request.stat_only else await self.manager.diff_stat(repo_dir)
            changed, _, _ = await self.manager.changed_files(repo_dir)
        self._audit(
            operation_id="workspaceDiff",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in changed],
        )
        return WorkspaceDiffResponse(workspace_id=workspace_id, diff=diff_text, diff_stat=diff_stat, changed_files=changed, truncated=truncated)

    async def apply_patch(self, owner: str, repo: str, workspace_id: str, request: WorkspaceApplyPatchRequest) -> WorkspaceApplyPatchResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        max_patch_bytes = min(request.max_patch_bytes or self.settings.workspace_max_patch_bytes, self.settings.workspace_max_patch_bytes)
        patch_bytes = request.patch.encode("utf-8")
        assert_payload_size(patch_bytes, max_bytes=max_patch_bytes, error_code=ErrorCode.WORKSPACE_PATCH_TOO_LARGE, label="Patch")
        max_changed_files = min(request.max_changed_files or self.settings.workspace_max_changed_files, self.settings.workspace_max_changed_files)
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            operations = parse_codex_patch(request.patch, self.policy, repo_dir, allow_delete=request.allow_delete, max_changed_files=max_changed_files)
            target_paths = list(dict.fromkeys(item.path for item in operations))
            snapshots = snapshot_files(repo_dir, target_paths)
            should_restore = True
            try:
                apply_text_patch(repo_dir, operations)
                changed = await self.manager.changed_files_for_paths(repo_dir, target_paths)
                if len(changed) > max_changed_files:
                    raise ApiError(ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES, "Patch changes too many files.", status_code=413, details={"count": len(changed), "max": max_changed_files})
                await self.manager.validate_changed_paths(repo_dir, changed)
                diff_stat = await self.manager.diff_stat_for_paths(repo_dir, target_paths)
                response = WorkspaceApplyPatchResponse(
                    applied=not request.dry_run,
                    dry_run=request.dry_run,
                    changed_files=changed,
                    diff_stat=diff_stat,
                    truncated=False,
                )
                should_restore = request.dry_run
            except Exception:
                restore_files(repo_dir, snapshots)
                raise
            finally:
                if should_restore:
                    restore_files(repo_dir, snapshots)
        self._audit(
            operation_id="workspaceApplyPatch",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
            command_hash=command_hash(request.patch),
        )
        return response

    async def write_file(self, owner: str, repo: str, workspace_id: str, request: WorkspaceWriteFileRequest) -> WorkspaceWriteFileResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        repo_dir = self.manager.repo_dir(workspace_id)
        max_bytes = min(request.max_bytes or self.settings.workspace_max_write_bytes, self.settings.workspace_max_write_bytes)
        with self.manager.lock(workspace_id):
            path, resolved = validate_write_target(self.policy, repo_dir, request.path, operation="modified", error_code=ErrorCode.WORKSPACE_WRITE_INVALID_PATH)
            previous_bytes: bytes | None = None
            if resolved.exists():
                if not resolved.is_file():
                    raise ApiError(ErrorCode.WORKSPACE_WRITE_INVALID_PATH, "Target path exists but is not a file.", status_code=400, details={"path": path})
                previous_bytes = resolved.read_bytes()
                assert_text_bytes(previous_bytes, path=path)
                previous_sha = sha256_hex(previous_bytes)
            else:
                previous_sha = None

            if request.mode == "create_only" and previous_bytes is not None:
                raise ApiError(ErrorCode.WORKSPACE_FILE_EXISTS, "File already exists; create_only refused to overwrite it.", status_code=409, details={"path": path})
            if request.mode == "overwrite_if_sha256_matches":
                if previous_bytes is None:
                    raise ApiError(ErrorCode.WORKSPACE_FILE_NOT_FOUND, "File does not exist; cannot verify expected_sha256.", status_code=404, details={"path": path})
                if not request.expected_sha256:
                    raise ApiError(ErrorCode.VALIDATION_ERROR, "expected_sha256 is required for overwrite_if_sha256_matches.", status_code=422, details={"path": path})
                if previous_sha != request.expected_sha256:
                    raise ApiError(ErrorCode.WORKSPACE_SHA_MISMATCH, "Current file SHA-256 does not match expected_sha256.", status_code=409, details={"path": path, "expected_sha256": request.expected_sha256, "actual_sha256": previous_sha})

            normalized_content = normalize_line_endings(request.content, line_ending=request.line_ending, previous_bytes=previous_bytes)
            data = normalized_content.encode("utf-8")
            assert_text_bytes(data, path=path)
            assert_payload_size(data, max_bytes=max_bytes, error_code=ErrorCode.WORKSPACE_CONTENT_TOO_LARGE, label="Content")
            new_sha = sha256_hex(data)
            if previous_bytes is None:
                operation = "added"
            elif previous_bytes == data:
                operation = "unchanged"
            else:
                operation = "modified"

            changed = []
            diff_stat = ""
            if operation != "unchanged":
                snapshots = snapshot_files(repo_dir, [path])
                should_restore = True
                try:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_bytes(data)
                    changed = await self.manager.changed_files_for_paths(repo_dir, [path])
                    if len(changed) > 1:
                        raise ApiError(ErrorCode.WORKSPACE_TOO_MANY_CHANGED_FILES, "write-file unexpectedly changed more than one file.", status_code=409, details={"count": len(changed)})
                    await self.manager.validate_changed_paths(repo_dir, changed)
                    diff_stat = await self.manager.diff_stat_for_paths(repo_dir, [path])
                    should_restore = request.dry_run
                except Exception as exc:
                    restore_files(repo_dir, snapshots)
                    if isinstance(exc, ApiError):
                        raise
                    raise ApiError(ErrorCode.WORKSPACE_WRITE_FAILED, "Failed to write workspace file.", status_code=500, details={"path": path, "error": str(exc)}) from exc
                finally:
                    if should_restore:
                        restore_files(repo_dir, snapshots)
            response = WorkspaceWriteFileResponse(
                written=bool(operation != "unchanged" and not request.dry_run),
                dry_run=request.dry_run,
                path=path,
                operation=operation,
                previous_sha256=previous_sha,
                new_sha256=new_sha,
                bytes=len(data),
                changed_files=changed,
                diff_stat=diff_stat,
            )
        self._audit(
            operation_id="workspaceWriteFile",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=meta.branch,
            head_sha_before=meta.head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
            command_hash=command_hash(path + "\n" + new_sha),
        )
        return response

    async def commit_and_push(self, owner: str, repo: str, workspace_id: str, request: WorkspaceCommitAndPushRequest) -> WorkspaceCommitAndPushResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        self.policy.assert_write_branch_allowed(request.branch)
        if request.branch != meta.branch:
            raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Commit branch must match prepared workspace branch.", status_code=403, details={"workspace_branch": meta.branch, "request_branch": request.branch})
        scope = f"{owner}/{repo}:{workspace_id}:commit_and_push"
        payload = request.model_dump()
        if request.idempotency_key:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return WorkspaceCommitAndPushResponse(**cached)

        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            await self.manager.fetch_branch(repo_dir, request.branch)
            remote_head = await self.manager.remote_head_sha(repo_dir, request.branch)
            if remote_head != request.expected_head_sha:
                raise ApiError(
                    ErrorCode.WORKSPACE_HEAD_CHANGED,
                    "Remote branch head changed before commit.",
                    status_code=409,
                    suggestion="Refresh the workspace and retry with the latest expected_head_sha.",
                    details={"expected_head_sha": request.expected_head_sha, "actual_head_sha": remote_head, "branch": request.branch},
                )
            current_branch = await self.manager.current_branch(repo_dir)
            if current_branch != request.branch:
                raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Current workspace checkout is not the requested branch.", status_code=409, details={"current_branch": current_branch, "request_branch": request.branch})
            changed, _, conflicts = await self.manager.changed_files(repo_dir)
            if conflicts:
                raise ApiError(ErrorCode.WORKSPACE_DIRTY, "Workspace has unresolved conflicts.", status_code=409, details={"conflicts": conflicts})
            if not changed:
                raise ApiError(ErrorCode.WORKSPACE_NO_CHANGES, "Workspace has no changes to commit.", status_code=409)
            await self.manager.validate_changed_paths(repo_dir, changed)
            paths = [self.policy.assert_workspace_path_allowed(path) for path in request.paths]
            await self.manager.git.run(["git", "add", "--", *paths], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
            staged = await self.manager.staged_changed_files(repo_dir)
            if not staged:
                await self.manager.git.run(["git", "reset", "--mixed"], cwd=repo_dir)
                raise ApiError(ErrorCode.WORKSPACE_NO_CHANGES, "Selected paths have no staged changes.", status_code=409)
            await self.manager.validate_changed_paths(repo_dir, staged)
            diff_stat_result = await self.manager.git.run(["git", "diff", "--cached", "--stat"], cwd=repo_dir)
            diff_stat = diff_stat_result.stdout.strip()
            previous_head = await self.manager.head_sha(repo_dir)
            if request.dry_run:
                await self.manager.git.run(["git", "reset", "--mixed"], cwd=repo_dir)
                response = WorkspaceCommitAndPushResponse(
                    previous_head_sha=previous_head,
                    new_head_sha=previous_head,
                    commit_sha=None,
                    commit_url=None,
                    changed_files=staged,
                    diff_stat=diff_stat,
                    pushed=False,
                    dry_run=True,
                )
            else:
                await self.manager.git.run(["git", "config", "user.name", self.settings.workspace_git_user_name], cwd=repo_dir)
                await self.manager.git.run(["git", "config", "user.email", self.settings.workspace_git_user_email], cwd=repo_dir)
                await self.manager.git.run(["git", "commit", "-m", request.commit_message], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds)
                new_head = await self.manager.head_sha(repo_dir)
                auth_config = await self.github.git_auth_config()
                push = await self.manager.git.run(["git", *auth_config, "push", "origin", f"HEAD:{request.branch}"], cwd=repo_dir, timeout=self.settings.workspace_max_timeout_seconds, check=False, allowed_exit_codes=(0,), max_output_bytes=self.settings.workspace_max_output_bytes)
                if push.exit_code != 0:
                    raise ApiError(ErrorCode.WORKSPACE_PUSH_FAILED, "Git push failed; local commit was not force-pushed.", status_code=502, details={"stdout": push.stdout, "stderr": push.stderr})
                meta.head_sha = new_head
                from app.workspace.models import save_meta

                save_meta(self.manager.workspace_dir(workspace_id), meta)
                response = WorkspaceCommitAndPushResponse(
                    previous_head_sha=previous_head,
                    new_head_sha=new_head,
                    commit_sha=new_head,
                    commit_url=f"https://github.com/{owner}/{repo}/commit/{new_head}",
                    changed_files=staged,
                    diff_stat=diff_stat,
                    pushed=True,
                    dry_run=False,
                )
        self._audit(
            operation_id="workspaceCommitAndPush",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=request.branch,
            head_sha_before=request.expected_head_sha,
            head_sha_after=response.new_head_sha,
            changed_files=[item.model_dump() for item in response.changed_files],
        )
        if request.idempotency_key:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def reset(self, owner: str, repo: str, workspace_id: str, request: WorkspaceResetRequest) -> WorkspaceResetResponse:
        meta = self._assert_workspace(owner, repo, workspace_id)
        if request.branch != meta.branch:
            raise ApiError(ErrorCode.WORKSPACE_POLICY_VIOLATION, "Reset branch must match prepared workspace branch.", status_code=403, details={"workspace_branch": meta.branch, "request_branch": request.branch})
        repo_dir = self.manager.repo_dir(workspace_id)
        with self.manager.lock(workspace_id):
            removed = await self.manager.reset_to_remote(repo_dir, request.branch, clean_untracked=request.clean_untracked)
            head_sha = await self.manager.head_sha(repo_dir)
            changed, _, _ = await self.manager.changed_files(repo_dir)
        self._audit(
            operation_id="workspaceReset",
            owner=owner,
            repo=repo,
            workspace_id=workspace_id,
            branch=request.branch,
            head_sha_after=head_sha,
            changed_files=[],
        )
        return WorkspaceResetResponse(workspace_id=workspace_id, branch=request.branch, head_sha=head_sha, dirty=bool(changed), removed_untracked_files=removed)

    def _assert_workspace(self, owner: str, repo: str, workspace_id: str):
        self.policy.assert_repo_allowed(owner, repo)
        meta = self.manager.get_meta(workspace_id)
        if meta.owner != owner or meta.repo != repo:
            raise ApiError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace was not found for this repository.", status_code=404, details={"workspace_id": workspace_id})
        return meta

    def _audit(self, **kwargs) -> None:
        try:
            self.audit.record_workspace_operation(**kwargs)
        except Exception:
            pass

    @staticmethod
    def _diagnostics_model(result) -> WorkspacePrepareDiagnostics:
        return WorkspacePrepareDiagnostics(
            mirror_stage=result.mirror.stage,
            mirror_duration_ms=result.mirror.duration_ms,
            mirror_pack_bytes=result.mirror.pack_bytes,
            mirror_pack_files=result.mirror.pack_files,
            workspace_stage=result.workspace_stage,
            workspace_duration_ms=result.workspace_duration_ms,
            total_duration_ms=result.total_duration_ms,
        )

    def _response_from_prepare_result(self, owner: str, repo: str, result) -> PrepareWorkspaceResponse:
        return PrepareWorkspaceResponse(
            workspace_id=result.meta.workspace_id,
            owner=owner,
            repo=repo,
            branch=result.meta.branch,
            source_pr_number=result.meta.source_pr_number,
            head_sha=result.meta.head_sha,
            default_branch=result.meta.default_branch,
            dirty=result.dirty,
            changed_files=result.changed_files,
            created=result.created,
            refreshed=result.refreshed,
            diagnostics=self._diagnostics_model(result),
        )

    @staticmethod
    def _prepare_metadata(result) -> dict:
        return {
            "created": result.created,
            "refreshed": result.refreshed,
            "dirty": result.dirty,
            "mirror": asdict(result.mirror),
            "workspace_stage": result.workspace_stage,
            "workspace_duration_ms": result.workspace_duration_ms,
            "total_duration_ms": result.total_duration_ms,
        }
