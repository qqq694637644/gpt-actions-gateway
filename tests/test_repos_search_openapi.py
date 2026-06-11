from __future__ import annotations

import asyncio

from app.config.settings import Settings
from app.main import app
from app.models.repos import CompareRefsRequest, ExportRepoSnapshotRequest
from app.models.search import SearchCodeRequest
from app.policy.rules import Policy
from app.services.repos import RepositoryService
from app.services.search import SearchService
from scripts.export_openapi import PUBLIC_OPERATION_IDS

HEAD = "1111111111111111111111111111111111111111"


class RepoGitHubStub:
    async def get_repository(self, owner: str, repo: str) -> dict:
        return {
            "full_name": f"{owner}/{repo}",
            "default_branch": "main",
            "private": True,
            "html_url": "https://github.test/acme/demo",
            "permissions": {"push": True},
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }

    async def compare_refs(self, owner: str, repo: str, base: str, head: str) -> dict:
        return {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
            "base_commit": {"sha": "base"},
            "merge_base_commit": {"sha": "merge-base"},
            "files": [{"filename": "app.py", "status": "modified", "additions": 2, "deletions": 1, "changes": 3, "patch": "@@"}],
        }

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        return HEAD

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict:
        return {"tree": {"sha": "tree"}}

    async def get_tree(self, owner: str, repo: str, tree_sha: str, *, recursive: bool = True) -> dict:
        return {
            "tree": [
                {"path": "app.py", "type": "blob", "sha": "sha-app", "size": 30},
                {"path": "node_modules/pkg/index.js", "type": "blob", "sha": "sha-vendor", "size": 10},
            ]
        }

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes:
        return b"def handler():\n    return 'needle'\n"


def make_services() -> tuple[RepositoryService, SearchService]:
    settings = Settings(allowed_repos="acme/demo")
    policy = Policy(settings)
    github = RepoGitHubStub()
    return RepositoryService(github, policy, settings), SearchService(github, policy, settings)


def test_repository_compare_snapshot_and_search() -> None:
    repo_service, search_service = make_services()

    repo = asyncio.run(repo_service.get_repository("acme", "demo"))
    default_branch = asyncio.run(repo_service.get_default_branch("acme", "demo"))
    compare = asyncio.run(repo_service.compare_refs("acme", "demo", CompareRefsRequest(base="main", head="gpt/fix")))
    snapshot = asyncio.run(repo_service.export_repo_snapshot("acme", "demo", ExportRepoSnapshotRequest(ref="main")))
    search = asyncio.run(search_service.search_code("acme", "demo", SearchCodeRequest(ref="main", query="needle", extensions=[".py"])))

    assert repo.full_name == "acme/demo"
    assert default_branch.default_branch == "main"
    assert compare.files[0].filename == "app.py"
    assert snapshot.head_sha == HEAD
    assert snapshot.file_count == 1
    assert search.matches[0].path == "app.py"
    assert search.matches[0].line_number == 2


def test_openapi_contains_current_public_operation_ids_and_no_legacy_routes() -> None:
    operation_ids = {
        operation.get("operationId")
        for path_item in app.openapi()["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
    }

    assert operation_ids == PUBLIC_OPERATION_IDS
    assert "validatePatch" not in operation_ids
    assert "continueWorkBranch" not in operation_ids
    assert "prepareWorkspaceMirror" not in operation_ids
