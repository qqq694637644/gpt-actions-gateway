from __future__ import annotations

import asyncio
import io
import zipfile

from app.config.settings import Settings
from app.models.ci import (
    DispatchWorkflowRequest,
    GetCiJobRequest,
    GetCiJobsRequest,
    GetCiRunRequest,
    GetJobLogRequest,
    GetRunLogRequest,
    ListArtifactsRequest,
    ReadArtifactTextRequest,
    RerunFailedJobsRequest,
    RerunJobRequest,
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
        self.rerun_runs: list[int] = []
        self.rerun_failed: list[int] = []
        self.rerun_jobs: list[int] = []
        self.run_zip = make_zip({"job1/1_build.txt": "build ok\nwarning\n", "job2/test.log": "tests passed\n"})
        self.artifact_zip = make_zip({"junit.xml": "<testsuite tests='1'/>\n", "image.png": "not really png"})

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

    async def rerun_workflow_run(self, owner: str, repo: str, run_id: int) -> None:
        self.rerun_runs.append(run_id)

    async def rerun_failed_jobs(self, owner: str, repo: str, run_id: int) -> None:
        self.rerun_failed.append(run_id)

    async def rerun_job(self, owner: str, repo: str, job_id: int) -> None:
        self.rerun_jobs.append(job_id)

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int) -> dict:
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


def make_service(github: CIGitHubStub) -> CIService:
    settings = Settings(gpt_action_secret="secret", allowed_repos="acme/demo", allow_rerun_ci=True, max_log_lines=10)
    return CIService(github, Policy(settings), settings)


def test_ci_run_jobs_job_and_logs() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    run = asyncio.run(service.get_ci_run("acme", "demo", GetCiRunRequest(run_id=77)))
    jobs = asyncio.run(service.get_ci_jobs("acme", "demo", GetCiJobsRequest(run_id=77, run_attempt=2)))
    job = asyncio.run(service.get_ci_job("acme", "demo", GetCiJobRequest(job_id=10)))
    job_log = asyncio.run(service.get_job_log("acme", "demo", GetJobLogRequest(job_id=10, step_name="pytest")))
    run_log = asyncio.run(service.get_run_log("acme", "demo", GetRunLogRequest(run_id=77, path_contains="job1")))

    assert run.run.run_id == 77
    assert jobs.jobs[0].name == "build"
    assert job.job.failed_steps[0].name == "pytest"
    assert "FAILED test_example.py" in job_log.log
    assert run_log.entries[0].name == "job1/1_build.txt"


def test_ci_dispatch_rerun_and_artifacts() -> None:
    github = CIGitHubStub()
    service = make_service(github)

    dispatch = asyncio.run(
        service.dispatch_workflow("acme", "demo", DispatchWorkflowRequest(workflow_id="ci.yml", ref="main", inputs={"suite": "unit"}))
    )
    rerun = asyncio.run(service.rerun_workflow_run("acme", "demo", RerunWorkflowRunRequest(run_id=77)))
    failed = asyncio.run(service.rerun_failed_jobs("acme", "demo", RerunFailedJobsRequest(run_id=77)))
    job = asyncio.run(service.rerun_job("acme", "demo", RerunJobRequest(job_id=10)))
    artifacts = asyncio.run(service.list_artifacts("acme", "demo", ListArtifactsRequest(run_id=77)))
    artifact_text = asyncio.run(service.read_artifact_text("acme", "demo", ReadArtifactTextRequest(artifact_id=55)))

    assert dispatch.accepted is True
    assert github.dispatched == ("ci.yml", "main", {"suite": "unit"})
    assert rerun.accepted is True and github.rerun_runs == [77]
    assert failed.accepted is True and github.rerun_failed == [77]
    assert job.accepted is True and github.rerun_jobs == [10]
    assert artifacts.artifacts[0].name == "reports"
    assert artifact_text.entries[0].name == "junit.xml"
    assert "testsuite" in artifact_text.entries[0].content
