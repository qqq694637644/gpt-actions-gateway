from __future__ import annotations

import asyncio

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.commits import ApplyPatchRequest
from app.policy.rules import Policy
from app.services.commits import CommitService
from app.storage.audit import AuditStore

HEAD = "1111111111111111111111111111111111111111"


class PatchGitHubStub:
    def __init__(self) -> None:
        self.head = HEAD
        self.blobs = {
            "sha-app": b"hello\nold\n",
            "sha-delete": b"bye\n",
        }
        self.created_tree_entries: list[dict] = []
        self.updated_refs: list[tuple[str, str, bool]] = []

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        return self.head

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict:
        return {"tree": {"sha": "tree-base"}}

    async def get_tree(self, owner: str, repo: str, tree_sha: str, *, recursive: bool = True) -> dict:
        return {
            "tree": [
                {"path": "app.txt", "type": "blob", "sha": "sha-app", "size": 10},
                {"path": "delete.txt", "type": "blob", "sha": "sha-delete", "size": 4},
            ]
        }

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes:
        return self.blobs[blob_sha]

    async def create_tree(self, owner: str, repo: str, base_tree: str | None, tree: list[dict]) -> dict:
        self.created_tree_entries = tree
        return {"sha": "tree-new"}

    async def create_commit(self, owner: str, repo: str, message: str, tree_sha: str, parents: list[str]) -> dict:
        assert message == "apply patch"
        assert tree_sha == "tree-new"
        assert parents == [HEAD]
        return {"sha": "2222222222222222222222222222222222222222"}

    async def update_ref(self, owner: str, repo: str, branch: str, sha: str, *, force: bool = False) -> dict:
        self.updated_refs.append((branch, sha, force))
        self.head = sha
        return {"object": {"sha": sha}}


def make_service(tmp_path, github: PatchGitHubStub, *, allow_delete_files: bool = True) -> CommitService:
    settings = Settings(
        gpt_action_secret="secret",
        allowed_repos="acme/demo",
        allow_delete_files=allow_delete_files,
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
    )
    return CommitService(github, Policy(settings), settings, AuditStore(settings.audit_db_url))


def test_apply_patch_and_commit_supports_modify_add_delete(tmp_path) -> None:
    github = PatchGitHubStub()
    service = make_service(tmp_path, github)
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1,2 +1,2 @@
 hello
-old
+new
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+created
diff --git a/delete.txt b/delete.txt
deleted file mode 100644
--- a/delete.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""

    response = asyncio.run(
        service.apply_patch_and_commit(
            "acme",
            "demo",
            ApplyPatchRequest(branch="gpt/fix", expected_head_sha=HEAD, patch=patch, commit_message="apply patch"),
        )
    )

    assert response.commit_sha == "2222222222222222222222222222222222222222"
    assert [item.operation for item in response.changed_files] == ["modified", "added", "deleted"]
    assert {entry["path"]: entry for entry in github.created_tree_entries}["app.txt"]["content"] == "hello\nnew\n"
    assert {entry["path"]: entry for entry in github.created_tree_entries}["new.txt"]["content"] == "created\n"
    assert {entry["path"]: entry for entry in github.created_tree_entries}["delete.txt"]["sha"] is None
    assert github.updated_refs == [("gpt/fix", response.commit_sha, False)]


def test_apply_patch_dry_run_does_not_commit(tmp_path) -> None:
    github = PatchGitHubStub()
    service = make_service(tmp_path, github)
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1,2 +1,2 @@
 hello
-old
+new
"""

    response = asyncio.run(
        service.apply_patch_and_commit(
            "acme",
            "demo",
            ApplyPatchRequest(branch="gpt/fix", expected_head_sha=HEAD, patch=patch, commit_message="apply patch", dry_run=True),
        )
    )

    assert response.dry_run is True
    assert response.commit_sha is None
    assert github.created_tree_entries == []
    assert github.updated_refs == []


def test_apply_patch_rejects_stale_head(tmp_path) -> None:
    github = PatchGitHubStub()
    github.head = "9999999999999999999999999999999999999999"
    service = make_service(tmp_path, github)
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1,2 +1,2 @@
 hello
-old
+new
"""

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.apply_patch_and_commit(
                "acme",
                "demo",
                ApplyPatchRequest(branch="gpt/fix", expected_head_sha=HEAD, patch=patch, commit_message="apply patch"),
            )
        )

    assert exc.value.error_code == ErrorCode.BRANCH_HEAD_CHANGED
