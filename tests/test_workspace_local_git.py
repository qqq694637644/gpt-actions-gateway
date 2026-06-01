from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.workspaces import (
    PrepareWorkspaceFromMirrorRequest,
    PrepareWorkspaceMirrorRequest,
    PrepareWorkspaceRequest,
    WorkspaceApplyPatchRequest,
    WorkspaceCommitAndPushRequest,
    WorkspaceWriteFileRequest,
)
from app.policy.rules import Policy
from app.services.workspaces import WorkspaceService
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager


class LocalGitHub:
    def __init__(self, remote: Path) -> None:
        self.remote = remote

    def git_remote_url(self, owner: str, repo: str) -> str:
        return str(self.remote)

    async def git_auth_config(self) -> list[str]:
        return []

    async def get_repository(self, owner: str, repo: str) -> dict:
        return {"default_branch": "main"}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def make_local_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git("checkout", "-b", "main", cwd=source)
    git("config", "user.name", "tester", cwd=source)
    git("config", "user.email", "tester@example.com", cwd=source)
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "Initial", cwd=source)
    git("checkout", "-b", "gpt/task", cwd=source)
    git("push", "origin", "main", "gpt/task", cwd=source)
    return remote, source


def make_service(
    tmp_path: Path,
    remote: Path,
    *,
    allow_all_repos: bool = True,
    allowed_repos: str = "",
) -> tuple[WorkspaceService, WorkspaceManager]:
    settings = Settings(
        allow_all_repos=allow_all_repos,
        allowed_repos=allowed_repos,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        allow_workflow_edit=True,
    )
    github = LocalGitHub(remote)
    policy = Policy(settings)
    audit = AuditStore(settings.audit_db_url)
    manager = WorkspaceManager(settings, github, policy)  # type: ignore[arg-type]
    service = WorkspaceService(github, policy, settings, manager, audit)  # type: ignore[arg-type]
    return service, manager


def run(coro):
    return asyncio.run(coro)


def test_workspace_commit_and_push_updates_local_remote(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("after\n", encoding="utf-8")

    response = run(
        service.commit_and_push(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceCommitAndPushRequest(branch="gpt/task", expected_head_sha=prepared.head_sha, commit_message="Update README"),
        )
    )

    assert response.pushed is True
    assert response.previous_head_sha == prepared.head_sha
    assert response.new_head_sha != prepared.head_sha
    assert response.changed_files[0].path == "README.md"
    assert git("rev-parse", "gpt/task", cwd=remote) == response.new_head_sha


def test_workspace_prepare_explicit_ws_id_reports_created_true(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task", workspace_id="ws_custom_1")))

    assert prepared.workspace_id == "ws_custom_1"
    assert prepared.created is True
    assert prepared.diagnostics.mirror_stage in {"clone", "fetch"}
    assert prepared.diagnostics.total_duration_ms >= prepared.diagnostics.mirror_duration_ms


def test_workspace_prepare_mirror_then_from_mirror(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    mirror = run(service.prepare_mirror("acme", "demo", PrepareWorkspaceMirrorRequest(refresh=True)))
    assert mirror.diagnostics.mirror_stage == "clone"
    assert mirror.diagnostics.workspace_stage == "skip"

    prepared = run(
        service.prepare_from_mirror(
            "acme",
            "demo",
            PrepareWorkspaceFromMirrorRequest(branch="gpt/task", workspace_id="ws_custom_2"),
        )
    )

    assert prepared.created is True
    assert prepared.diagnostics.mirror_stage == "reuse"
    assert prepared.diagnostics.workspace_stage in {"clone", "reuse"}
    assert prepared.head_sha


def test_workspace_prepare_mirror_rejects_disallowed_repo(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote, allow_all_repos=False, allowed_repos="acme/allowed")

    with pytest.raises(ApiError) as exc:
        run(service.prepare_mirror("evil", "repo", PrepareWorkspaceMirrorRequest(refresh=True)))

    assert exc.value.error_code == ErrorCode.REPO_NOT_ALLOWED
    assert not (tmp_path / "mirrors" / "evil" / "repo.git").exists()


def test_workspace_prepare_mirror_refresh_false_clones_missing_mirror(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    mirror = run(service.prepare_mirror("acme", "demo", PrepareWorkspaceMirrorRequest(refresh=False)))

    assert mirror.diagnostics.mirror_stage == "clone"
    assert mirror.refreshed is False
    assert mirror.diagnostics.mirror_pack_files >= 0


def test_workspace_prepare_mirror_refresh_false_reuses_existing_mirror(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    run(service.prepare_mirror("acme", "demo", PrepareWorkspaceMirrorRequest(refresh=False)))
    mirror = run(service.prepare_mirror("acme", "demo", PrepareWorkspaceMirrorRequest(refresh=False)))

    assert mirror.diagnostics.mirror_stage == "reuse"
    assert mirror.refreshed is False


def test_workspace_prepare_from_mirror_requires_existing_mirror(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)

    with pytest.raises(ApiError) as exc:
        run(
            service.prepare_from_mirror(
                "acme",
                "demo",
                PrepareWorkspaceFromMirrorRequest(branch="gpt/task", workspace_id="ws_missing_mirror"),
            )
        )

    assert exc.value.error_code == ErrorCode.WORKSPACE_NOT_FOUND
    assert "prepareWorkspaceMirror" in exc.value.message


def test_workspace_prepare_from_mirror_checks_out_exact_sha(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    expected_sha = git("rev-parse", "main", cwd=remote)
    service, _ = make_service(tmp_path, remote)

    run(service.prepare_mirror("acme", "demo", PrepareWorkspaceMirrorRequest(refresh=True)))
    prepared = run(
        service.prepare_from_mirror(
            "acme",
            "demo",
            PrepareWorkspaceFromMirrorRequest(base_ref=expected_sha, workspace_id="ws_exact_sha"),
        )
    )

    assert prepared.branch == expected_sha
    assert prepared.head_sha == expected_sha


def test_workspace_prepare_read_only_base_ref_is_not_limited_to_read_allowlist(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    git("checkout", "main", cwd=source)
    git("checkout", "-b", "feature/read-only", cwd=source)
    (source / "README.md").write_text("feature branch\n", encoding="utf-8")
    git("commit", "-am", "Feature read-only branch", cwd=source)
    git("push", "origin", "feature/read-only", cwd=source)
    expected_sha = git("rev-parse", "feature/read-only", cwd=remote)
    service, _ = make_service(tmp_path, remote)

    prepared = run(
        service.prepare(
            "acme",
            "demo",
            PrepareWorkspaceRequest(base_ref="refs/heads/feature/read-only", workspace_id="ws_feature_read_only"),
        )
    )

    assert prepared.branch == "refs/heads/feature/read-only"
    assert prepared.head_sha == expected_sha


def test_workspace_prepare_read_only_tag_ref_checks_out_tag_target(tmp_path: Path):
    remote, source = make_local_repo(tmp_path)
    git("checkout", "main", cwd=source)
    (source / "README.md").write_text("tagged release\n", encoding="utf-8")
    git("commit", "-am", "Tagged release", cwd=source)
    git("tag", "v1.0.0", cwd=source)
    git("push", "origin", "main", "refs/tags/v1.0.0", cwd=source)
    expected_sha = git("rev-parse", "refs/tags/v1.0.0", cwd=remote)
    service, _ = make_service(tmp_path, remote)

    prepared = run(
        service.prepare(
            "acme",
            "demo",
            PrepareWorkspaceRequest(base_ref="refs/tags/v1.0.0", workspace_id="ws_release_tag"),
        )
    )

    assert prepared.branch == "refs/tags/v1.0.0"
    assert prepared.head_sha == expected_sha


def test_workspace_apply_patch_dry_run_and_apply_do_not_push(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)

    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    original_remote_head = git("rev-parse", "gpt/task", cwd=remote)
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-before\n+after\n*** End Patch\n"

    dry = run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch, dry_run=True)))
    assert dry.applied is False
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "before\n"

    applied = run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert applied.applied is True
    assert applied.changed_files[0].path == "README.md"
    assert applied.changed_files[0].operation == "modified"
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "after\n"
    assert git("rev-parse", "gpt/task", cwd=remote) == original_remote_head


def test_workspace_apply_patch_rejects_delete_by_default(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    patch = "*** Begin Patch\n*** Delete File: README.md\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_DELETE_NOT_ALLOWED


def test_workspace_apply_patch_context_mismatch_leaves_file_unchanged(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-missing\n+after\n*** End Patch\n"

    with pytest.raises(ApiError) as exc:
        run(service.apply_patch("acme", "demo", prepared.workspace_id, WorkspaceApplyPatchRequest(patch=patch)))
    assert exc.value.error_code == ErrorCode.WORKSPACE_PATCH_CONTEXT_MISMATCH
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "before\n"


def test_workspace_write_file_create_only_and_sha_guard(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, manager = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    original_remote_head = git("rev-parse", "gpt/task", cwd=remote)

    dry = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(path="docs/ci.md", content="# CI\n", dry_run=True),
        )
    )
    assert dry.written is False
    assert not (repo_dir / "docs/ci.md").exists()

    written = run(
        service.write_file(
            "acme",
            "demo",
            prepared.workspace_id,
            WorkspaceWriteFileRequest(path="docs/ci.md", content="# CI\n", line_ending="lf"),
        )
    )
    assert written.written is True
    assert written.operation == "added"
    assert written.previous_sha256 is None
    assert (repo_dir / "docs/ci.md").read_text(encoding="utf-8") == "# CI\n"
    assert git("rev-parse", "gpt/task", cwd=remote) == original_remote_head

    with pytest.raises(ApiError) as exc:
        run(service.write_file("acme", "demo", prepared.workspace_id, WorkspaceWriteFileRequest(path="docs/ci.md", content="again\n")))
    assert exc.value.error_code == ErrorCode.WORKSPACE_FILE_EXISTS

    with pytest.raises(ApiError) as exc:
        run(
            service.write_file(
                "acme",
                "demo",
                prepared.workspace_id,
                WorkspaceWriteFileRequest(path="docs/ci.md", content="again\n", mode="overwrite_if_sha256_matches", expected_sha256="0" * 64),
            )
        )
    assert exc.value.error_code == ErrorCode.WORKSPACE_SHA_MISMATCH


def test_workspace_write_file_rejects_sensitive_path(tmp_path: Path):
    remote, _ = make_local_repo(tmp_path)
    service, _ = make_service(tmp_path, remote)
    prepared = run(service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task")))

    with pytest.raises(ApiError) as exc:
        run(service.write_file("acme", "demo", prepared.workspace_id, WorkspaceWriteFileRequest(path=".env", content="SECRET=x\n")))
    assert exc.value.error_code == ErrorCode.WORKSPACE_POLICY_VIOLATION
