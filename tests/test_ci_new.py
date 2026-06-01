from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.ci import (
    CIStatusQueryRequest,
    DeleteCacheRequest,
    DispatchWorkflowRequest,
    GetCiJobsRequest,
    GetCiRunRequest,
    GetJobLogRequest,
    GetRunLogRequest,
    ListArtifactsRequest,
    ListCachesRequest,
    ReadArtifactTextRequest,
    RerunWorkflowJobRequest,
    RerunWorkflowRunRequest,
)
from app.policy.rules import Policy
from app.services.ci import CIService


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class CIGitHubStub:
    def __init__(self) -> None:
        self.dispatched: tuple[str, str, dict] | None = None
        self.rerun_runs: list[tuple[int, bool]] = []
        self.rerun_jobs: list[tuple[int, bool]] = []
        self.deleted_caches: list[int] = []
        self.run_zip = make_zip({"job1/1_build.txt": "build ok\nwarning\n", "job2/test.log": "tests passed\n"})
        self.artifact_zip = make_zip({"junit.xml": "<testsuite tests='1'/>\n", "image.png": "not really png"})

    async def resolve_ref_sha(self, owner: str, repo: str, ref: str) -> str:
        if ref in {"refs/heads/feature/android-ci-fix", "heads/feature/android-ci-fix", "feature/android-ci-fix"}:
            return "4444444444444444444444444444444444444444"
        if ref in {"refs/tags/v1.0.0", "tags/v1.0.0"}:
            return "5555555555555555555555555555555555555555"
        return "2222222222222222222222222222222222222222"

    async def get_workflow(self, owner: str, repo: str, workflow_id: str) -> dict:
        return {"id": workflow_id, "path": f".github/workflows/{workflow_id}", "state": "active"}

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict:
        return {
            "id": run_id,
            "run_attempt": 2,
            "workflow_id": 123,
            "name": "CI",
            "event": "pull_request",
            "head_branch": "gpt/fix",
            "head_sha": "2222222222222222222222222222222222222222",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.test/run",
            "created_at": "2026-05-30T00:00:00Z",
            "updated_at": "2026-05-30T00:01:00Z",
        }

    async def list_jobs_for_run(self, owner: str, repo: str, run_id: int, *, run_attempt: int | None = None) -> dict:
        return {
            "jobs": [
                {
                    "id": 10,
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.test/job/10",
                    "steps": [{"name": "compile", "number": 1, "status": "completed", "conclusion": "success"}],
                }
            ]
        }

    async def list_workflow_runs(self, owner: str, repo: str, *, workflow_id: str | None = None, params: dict | None = None) -> dict:
        assert params is not None
        if params.get("head_sha") == "4444444444444444444444444444444444444444":
            assert params.get("branch") == "feature/android-ci-fix"
            event = "push"
            head_branch = "feature/android-ci-fix"
        else:
            assert workflow_id == "ci.yml"
            assert params.get("event") == "workflow_dispatch"
            assert "created" in params
            assert "branch" not in params
            assert "head_sha" not in params
            event = "workflow_dispatch"
            head_branch = None
        return {
            "workflow_runs": [
                {
                    "id": 88,
                    "run_attempt": 1,
                    "workflow_id": workflow_id,
                    "name": "CI",
                    "event": event,
                    "head_branch": head_branch,
                    "head_sha": params.get("head_sha") or "3333333333333333333333333333333333333333",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.test/run/88",
                    "created_at": "2026-05-30T00:00:00Z",
                    "updated_at": "2026-05-30T00:01:00Z",
                }
            ]
        }

    async def get_workflow_job(self, owner: str, repo: str, job_id: int) -> dict:
        return {
            "id": job_id,
            "run_id": 77,
            "run_attempt": 1,
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "steps": [{"name": "pytest", "number": 1, "status": "completed", "conclusion": "failure"}],
        }

    async def download_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        return "setup\n##[group]pytest\nFAILED test_example.py\n##[group]cleanup\ndone\n"

    async def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        return self.run_zip

    async def dispatch_workflow(self, owner: str, repo: str, workflow_id: str, *, ref: str, inputs: dict | None = None) -> None:
        self.dispatched = (workflow_id, ref, inputs or {})

    async def rerun_workflow_run(self, owner: str, repo: str, run_id: int, *, enable_debug_logging: bool = False) -> None:
        self.rerun_runs.append((run_id, enable_debug_logging))

    async def rerun_workflow_job(self, owner: str, repo: str, job_id: int, *, enable_debug_logging: bool = False) -> None:
        self.rerun_jobs.append((job_id, enable_debug_logging))

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int, *, per_page: int = 100) -> dict:
        return {
            "artifacts": [
                {
                    "id": 55,
                    "name": "reports",
                    "size_in_bytes": 123,
                    "archive_download_url": "https://github.test/artifacts/55/zip",
                    "expired": False,
                    "created_at": "2026-05-30T00:00:00Z",
                    "expires_at": "2026-06-30T00:00:00Z",
                }
            ]
        }

    async def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        return self.artifact_zip

    async def list_actions_caches(self, owner: str, repo: str, *, params: dict | None = None) -> dict:
        key = (params or {}).get("key")
        caches = [
            {
                "id": 101,
                "key": "ce-lib-windows-x64",
                "ref": "refs/heads/gpt/fix",
                "version": "v1",
                "size_in_bytes": 456,
                "created_at": "2026-05-29T00:00:00Z",
                "last_accessed_at": "2026-05-30T00:00:00Z",
            }
        ]
        if key:
            caches = [item for item in caches if item["key"].startswith(key)]
        return {"total_count": len(caches), "actions_caches": caches}

    async def delete_actions_cache(self, owner: str, repo: str, cache_id: int) -> None:
        if cache_id == 999:
            raise ApiError(ErrorCode.GITHUB_NOT_FOUND, "not found", status_code=404)
        self.deleted_caches.append(cache_id)


def make_service(github: CIGitHubStub) -> CIService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo", max_log_lines=10)
    return CIService(github, Policy(settings), settings)


def test_ci_run_jobs_job_and_logs() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    run = asyncio.run(service.get_ci_run("acme", "demo", GetCiRunRequest(run_id=77)))
    jobs = asyncio.run(service.get_ci_jobs("acme", "demo", GetCiJobsRequest(run_id=77, run_attempt=2)))
    job_log = asyncio.run(service.get_job_log("acme", "demo", GetJobLogRequest(job_id=10, step_name="pytest")))
    run_log = asyncio.run(service.get_run_log("acme", "demo", GetRunLogRequest(run_id=77, path_contains="job1")))

    assert run.run.run_id == 77
    assert jobs.jobs[0].name == "build"
    assert "FAILED test_example.py" in job_log.log
    assert run_log.entries[0].name == "job1/1_build.txt"


def test_ci_dispatch_rerun_artifacts_and_caches() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    dispatch = asyncio.run(
        service.dispatch_workflow("acme", "demo", DispatchWorkflowRequest(workflow_id="ci.yml", ref="gpt/fix", inputs={"suite": "unit"}))
    )
    rerun = asyncio.run(service.rerun_workflow_run("acme", "demo", RerunWorkflowRunRequest(run_id=77, enable_debug_logging=True)))
    job = asyncio.run(service.rerun_workflow_job("acme", "demo", RerunWorkflowJobRequest(job_id=10)))
    artifacts = asyncio.run(service.list_artifacts("acme", "demo", ListArtifactsRequest(run_id=77)))
    artifact_text = asyncio.run(service.read_artifact_text("acme", "demo", ReadArtifactTextRequest(artifact_id=55)))
    caches = asyncio.run(service.list_caches("acme", "demo", ListCachesRequest(key="ce-lib-")))
    dry_run = asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(cache_id=101, dry_run=True)))
    delete = asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(key="ce-lib-", ref="refs/heads/gpt/fix", dry_run=False)))

    assert dispatch.accepted is True
    assert dispatch.event == "workflow_dispatch"
    assert dispatch.query_hint["event"] == "workflow_dispatch"
    assert github.dispatched == ("ci.yml", "gpt/fix", {"suite": "unit"})
    assert rerun.accepted is True and github.rerun_runs == [(77, True)]
    assert job.accepted is True and github.rerun_jobs == [(10, False)]
    assert artifacts.artifacts[0].name == "reports"
    assert artifact_text.entries[0].name == "junit.xml"
    assert "testsuite" in artifact_text.entries[0].content
    assert caches.caches[0].key == "ce-lib-windows-x64"
    assert dry_run.deleted is False and dry_run.matched_count == 1
    assert delete.deleted is True and github.deleted_caches == [101]


def test_dispatch_workflow_tag_query_hint_can_query_ci_status() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    dispatch = asyncio.run(service.dispatch_workflow("acme", "demo", DispatchWorkflowRequest(workflow_id="ci.yml", ref="refs/tags/v1.0.0")))
    status = asyncio.run(service.get_ci_status("acme", "demo", CIStatusQueryRequest(**dispatch.query_hint)))

    assert "branch" not in dispatch.query_hint
    assert "commit_sha" not in dispatch.query_hint
    assert dispatch.query_hint["workflow_id"] == "ci.yml"
    assert status.matched_by == "workflow_id"
    assert status.conclusion == "success"


def test_tag_refs_use_common_ref_format_validation() -> None:
    service = make_service(CIGitHubStub())

    for ref in ["refs/tags/foo..bar", "refs/tags/.bad", "tags/foo..bar", "tags/.bad"]:
        with pytest.raises(ApiError) as dispatch_exc:
            asyncio.run(service.dispatch_workflow("acme", "demo", DispatchWorkflowRequest(workflow_id="ci.yml", ref=ref)))
        assert dispatch_exc.value.error_code == ErrorCode.VALIDATION_ERROR

        with pytest.raises(ApiError) as cache_exc:
            asyncio.run(service.list_caches("acme", "demo", ListCachesRequest(ref=ref)))
        assert cache_exc.value.error_code == ErrorCode.VALIDATION_ERROR


def test_short_tag_cache_ref_is_normalized_to_full_tag_ref() -> None:
    service = make_service(CIGitHubStub())

    params = service._cache_query_params(ListCachesRequest(ref="tags/v1.0.0"))

    assert params["ref"] == "refs/tags/v1.0.0"


def test_ci_status_branch_ref_is_resolved_without_double_heads_prefix() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    status = asyncio.run(service.get_ci_status("acme", "demo", CIStatusQueryRequest(branch="refs/heads/feature/android-ci-fix")))

    assert status.matched_by == "branch"
    assert status.workflow_runs[0].head_sha == "4444444444444444444444444444444444444444"
    assert status.workflow_runs[0].head_branch == "feature/android-ci-fix"


def test_delete_cache_defaults_to_dry_run_and_does_not_fake_missing_cache_id() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    default_dry_run = asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(cache_id=101)))
    missing = asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(cache_id=999, dry_run=False)))

    assert default_dry_run.dry_run is True
    assert default_dry_run.deleted is False
    assert github.deleted_caches == []
    assert missing.matched_count == 0
    assert missing.deleted_caches == []
    assert github.deleted_caches == []

    direct_delete = asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(cache_id=202, dry_run=False)))

    assert direct_delete.deleted is True
    assert direct_delete.deleted_caches[0].cache_id == 202
    assert github.deleted_caches == [202]


def test_delete_cache_rejects_short_or_broad_keys() -> None:
    service = make_service(CIGitHubStub())

    with pytest.raises(ApiError) as exc:
        asyncio.run(service.delete_cache("acme", "demo", DeleteCacheRequest(key="c")))

    assert exc.value.error_code == ErrorCode.VALIDATION_ERROR
