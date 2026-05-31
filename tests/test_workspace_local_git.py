from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.models.workspaces import PrepareWorkspaceRequest, WorkspaceCommitAndPushRequest
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


@pytest.mark.asyncio
async def test_workspace_commit_and_push_updates_local_remote(tmp_path: Path):
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

    settings = Settings(
        allow_all_repos=True,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
    )
    github = LocalGitHub(remote)
    policy = Policy(settings)
    audit = AuditStore(settings.audit_db_url)
    manager = WorkspaceManager(settings, github, policy)  # type: ignore[arg-type]
    service = WorkspaceService(github, policy, settings, manager, audit)  # type: ignore[arg-type]

    prepared = await service.prepare("acme", "demo", PrepareWorkspaceRequest(branch="gpt/task"))
    repo_dir = manager.repo_dir(prepared.workspace_id)
    (repo_dir / "README.md").write_text("after\n", encoding="utf-8")

    response = await service.commit_and_push(
        "acme",
        "demo",
        prepared.workspace_id,
        WorkspaceCommitAndPushRequest(branch="gpt/task", expected_head_sha=prepared.head_sha, commit_message="Update README"),
    )

    assert response.pushed is True
    assert response.previous_head_sha == prepared.head_sha
    assert response.new_head_sha != prepared.head_sha
    assert response.changed_files[0].path == "README.md"
    assert git("rev-parse", "gpt/task", cwd=remote) == response.new_head_sha
