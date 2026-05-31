from __future__ import annotations

import fnmatch
import io
import zipfile
from typing import Any

from app.ci.logs import parse_failed_log
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.ci import (
    Annotation,
    Artifact,
    ArtifactTextFile,
    CIJob,
    CIRun,
    CIStep,
    CIStatusQueryRequest,
    CIStatusResponse,
    FailedCILogResponse,
    FailedJobLog,
    FailedStep,
    GetCiJobsRequest,
    GetCiJobsResponse,
    GetCiRunRequest,
    GetCiRunResponse,
    GetJobLogRequest,
    GetRunLogRequest,
    JobLogResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    ReadArtifactTextRequest,
    ReadArtifactTextResponse,
    RunLogFile,
    RunLogResponse,
)
from app.policy.rules import Policy

_TEXT_ARTIFACT_PATTERNS = [
    "*.txt",
    "*.log",
    "*.xml",
    "*.json",
    "*.html",
    "*.htm",
    "*.md",
    "*.csv",
    "*.lcov",
    "lcov.info",
    "coverage/**",
    "junit*.xml",
    "**/junit*.xml",
]


class CIService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings

    async def get_ci_status(self, owner: str, repo: str, request: CIStatusQueryRequest) -> CIStatusResponse:
        self.policy.assert_repo_allowed(owner, repo)
        matched_by = "branch"
        target_sha = request.commit_sha
        branch = request.branch
        warning = None
        if request.pr_number is not None:
            pr = await self.github.get_pull_request(owner, repo, request.pr_number)
            target_sha = pr["head"]["sha"]
            branch = pr["head"]["ref"]
            matched_by = "pr_number"
        elif request.commit_sha:
            matched_by = "commit_sha"
        elif branch:
            self.policy.assert_read_ref_allowed(branch)
            target_sha = await self.github.get_branch_head(owner, repo, branch)
            matched_by = "branch"
            warning = "Branch query was resolved to current branch head SHA."
        else:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Provide commit_sha, pr_number, or branch.", status_code=422)

        params: dict[str, Any] = {"per_page": 100}
        if target_sha:
            params["head_sha"] = target_sha
        if branch:
            params["branch"] = branch
        if request.event:
            params["event"] = request.event
        if request.created_after:
            params["created"] = f">={request.created_after}"

        payload = await self.github.list_workflow_runs(owner, repo, workflow_id=request.workflow_id, params=params)
        raw_runs = payload.get("workflow_runs", [])
        if target_sha:
            raw_runs = [run for run in raw_runs if run.get("head_sha") == target_sha]
        if not raw_runs:
            raise ApiError(
                ErrorCode.CI_RUN_NOT_FOUND,
                "No matching workflow runs were found.",
                status_code=404,
                suggestion="Check that CI has started for the commit, or query again with the exact commit_sha.",
                details={"commit_sha": target_sha, "branch": branch, "workflow_id": request.workflow_id, "event": request.event},
            )

        runs: list[CIRun] = []
        for raw_run in raw_runs[:20]:
            jobs_payload = await self.github.list_jobs_for_run(owner, repo, raw_run["id"], run_attempt=raw_run.get("run_attempt"))
            jobs = [self._job_from_github(job) for job in jobs_payload.get("jobs", [])]
            runs.append(self._run_from_github(raw_run, jobs=jobs))
        status, conclusion = self._aggregate(runs)
        return CIStatusResponse(matched_by=matched_by, status=status, conclusion=conclusion, workflow_runs=runs, warning=warning)

    async def get_ci_run(self, owner: str, repo: str, request: GetCiRunRequest) -> GetCiRunResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw_run = await self.github.get_workflow_run(owner, repo, request.run_id)
        jobs: list[CIJob] = []
        if request.include_jobs:
            jobs_payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=raw_run.get("run_attempt"))
            jobs = [self._job_from_github(job) for job in jobs_payload.get("jobs", [])]
        run = self._run_from_github(raw_run, jobs=jobs)
        return GetCiRunResponse(workflow_run=run, run=run)

    async def get_ci_jobs(self, owner: str, repo: str, request: GetCiJobsRequest) -> GetCiJobsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=request.run_attempt)
        jobs = [self._job_from_github(job) for job in payload.get("jobs", [])]
        return GetCiJobsResponse(run_id=request.run_id, run_attempt=request.run_attempt, jobs=jobs, total_count=int(payload.get("total_count") or len(jobs)))

    async def get_failed_ci_log(self, owner: str, repo: str, request: FailedLogQueryRequest) -> FailedCILogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        jobs_payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=request.run_attempt)
        jobs = jobs_payload.get("jobs", [])
        if request.job_id is not None:
            jobs = [job for job in jobs if int(job["id"]) == request.job_id]
        else:
            jobs = [job for job in jobs if job.get("conclusion") not in {None, "success", "skipped", "neutral"}]
        if not jobs:
            raise ApiError(ErrorCode.CI_RUN_NOT_FOUND, "No failed jobs matched this workflow run.", status_code=404, details={"run_id": request.run_id, "job_id": request.job_id})

        max_lines = min(request.max_lines or self.settings.max_log_lines, self.settings.max_log_lines)
        failed_jobs: list[FailedJobLog] = []
        for job in jobs[:10]:
            raw_log = await self.github.download_job_logs(owner, repo, int(job["id"]))
            parsed = parse_failed_log(raw_log, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
            failed_step = None
            for step in job.get("steps") or []:
                conclusion = step.get("conclusion")
                if conclusion and conclusion not in {"success", "skipped", "neutral"}:
                    failed_step = step.get("name")
                    break
            failed_jobs.append(
                FailedJobLog(
                    job_id=int(job["id"]),
                    job_name=job.get("name") or str(job["id"]),
                    failed_step=failed_step,
                    error_summary=parsed.error_summary,
                    annotations=[Annotation(**item) for item in parsed.annotations],
                    log_excerpt=parsed.log_excerpt,
                    last_lines=parsed.last_lines,
                    truncated=parsed.truncated,
                )
            )
        return FailedCILogResponse(run_id=request.run_id, run_attempt=request.run_attempt, failed_jobs=failed_jobs)

    async def get_job_log(self, owner: str, repo: str, request: GetJobLogRequest) -> JobLogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw_log = await self.github.download_job_logs(owner, repo, request.job_id)
        if request.step_name:
            raw_log = _extract_step_log(raw_log, request.step_name)
        max_lines = min(request.max_lines or self.settings.max_log_lines, self.settings.max_log_lines)
        excerpt, last_lines, total_lines, truncated = _trim_log(raw_log, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
        return JobLogResponse(job_id=request.job_id, step_name=request.step_name, log_excerpt=excerpt, log=excerpt, last_lines=last_lines, total_lines=total_lines, truncated=truncated)

    async def get_run_log(self, owner: str, repo: str, request: GetRunLogRequest) -> RunLogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        data = await self.github.download_run_logs(owner, repo, request.run_id)
        max_lines = min(request.max_lines_per_file or self.settings.max_log_lines, self.settings.max_log_lines)
        files: list[RunLogFile] = []
        truncated = False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if request.path_contains and request.path_contains not in info.filename:
                        continue
                    if len(files) >= request.max_files:
                        truncated = True
                        break
                    raw = archive.read(info.filename)
                    if b"\x00" in raw[:4096]:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    excerpt, last_lines, total_lines, file_truncated = _trim_log(text, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
                    files.append(RunLogFile(path=info.filename, name=info.filename, log_excerpt=excerpt, log=excerpt, last_lines=last_lines, total_lines=total_lines, truncated=file_truncated))
        except zipfile.BadZipFile as exc:
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "Workflow run log archive is not a valid zip file.", status_code=502) from exc
        return RunLogResponse(run_id=request.run_id, files=files, entries=files, truncated=truncated)

    async def list_artifacts(self, owner: str, repo: str, request: ListArtifactsRequest) -> ListArtifactsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        payload = await self.github.list_artifacts_for_run(owner, repo, request.run_id, per_page=request.max_results)
        artifacts = [
            Artifact(
                artifact_id=item["id"],
                name=item.get("name", ""),
                size_in_bytes=item.get("size_in_bytes"),
                archive_download_url=item.get("archive_download_url"),
                expired=item.get("expired"),
                created_at=item.get("created_at"),
                expires_at=item.get("expires_at"),
                updated_at=item.get("updated_at"),
            )
            for item in payload.get("artifacts", [])[: request.max_results]
        ]
        return ListArtifactsResponse(run_id=request.run_id, artifacts=artifacts, total_count=int(payload.get("total_count") or len(artifacts)))

    async def read_artifact_text(self, owner: str, repo: str, request: ReadArtifactTextRequest) -> ReadArtifactTextResponse:
        self.policy.assert_repo_allowed(owner, repo)
        data = await self.github.download_artifact(owner, repo, request.artifact_id)
        max_bytes = min(request.max_bytes_per_file or self.settings.max_log_bytes, self.settings.max_log_bytes)
        files: list[ArtifactTextFile] = []
        truncated = False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not _artifact_path_matches(info.filename, request.path):
                        continue
                    if not _is_text_artifact_path(info.filename):
                        continue
                    if len(files) >= request.max_files:
                        truncated = True
                        break
                    raw = archive.read(info.filename)
                    if b"\x00" in raw[:4096]:
                        continue
                    content = raw[:max_bytes].decode("utf-8", errors="replace")
                    files.append(ArtifactTextFile(path=info.filename, name=info.filename, content=content, size=info.file_size, truncated=info.file_size > max_bytes))
        except zipfile.BadZipFile as exc:
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "Artifact archive is not a valid zip file.", status_code=502) from exc
        return ReadArtifactTextResponse(artifact_id=request.artifact_id, files=files, entries=files, total_files=len(files), truncated=truncated)

    @staticmethod
    def _job_from_github(job: dict[str, Any]) -> CIJob:
        steps: list[CIStep] = []
        failed_steps: list[FailedStep] = []
        for step in job.get("steps") or []:
            mapped = CIStep(
                name=step.get("name"),
                number=step.get("number"),
                status=step.get("status"),
                conclusion=step.get("conclusion"),
                started_at=step.get("started_at"),
                completed_at=step.get("completed_at"),
            )
            steps.append(mapped)
            conclusion = step.get("conclusion")
            if conclusion and conclusion not in {"success", "skipped", "neutral"}:
                failed_steps.append(FailedStep(**mapped.model_dump()))
        return CIJob(
            job_id=job["id"],
            run_id=job.get("run_id"),
            name=job.get("name") or str(job["id"]),
            status=job.get("status"),
            conclusion=job.get("conclusion"),
            html_url=job.get("html_url"),
            started_at=job.get("started_at"),
            completed_at=job.get("completed_at"),
            runner_name=job.get("runner_name"),
            steps=steps,
            failed_steps=failed_steps,
        )

    @staticmethod
    def _run_from_github(raw_run: dict[str, Any], *, jobs: list[CIJob] | None = None) -> CIRun:
        return CIRun(
            run_id=raw_run["id"],
            run_attempt=raw_run.get("run_attempt"),
            workflow_id=raw_run.get("workflow_id"),
            workflow_name=raw_run.get("name"),
            event=raw_run.get("event"),
            head_branch=raw_run.get("head_branch"),
            head_sha=raw_run.get("head_sha"),
            status=raw_run.get("status"),
            conclusion=raw_run.get("conclusion"),
            run_url=raw_run.get("html_url"),
            created_at=raw_run.get("created_at"),
            updated_at=raw_run.get("updated_at"),
            jobs=jobs or [],
        )

    @staticmethod
    def _aggregate(runs: list[CIRun]) -> tuple[str, str | None]:
        statuses = {run.status for run in runs}
        conclusions = [run.conclusion for run in runs if run.conclusion]
        if "queued" in statuses:
            return "queued", None
        if "in_progress" in statuses or any(status not in {"completed", None} for status in statuses):
            return "in_progress", None
        if not conclusions:
            return "completed", None
        if any(conclusion in {"failure", "timed_out", "cancelled", "action_required"} for conclusion in conclusions):
            return "completed", "failure"
        if all(conclusion in {"success", "skipped", "neutral"} for conclusion in conclusions):
            return "completed", "success"
        return "completed", conclusions[0]


def _trim_log(raw_log: str, *, max_lines: int, max_bytes: int) -> tuple[str, str, int, bool]:
    encoded = raw_log.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        raw_log = encoded[:max_bytes].decode("utf-8", errors="replace")
    lines = raw_log.splitlines()
    total_lines = len(lines)
    if len(lines) > max_lines:
        truncated = True
    selected = lines[:max_lines]
    tail = lines[-max_lines:] if lines else []
    return "\n".join(selected), "\n".join(tail), total_lines, truncated


def _extract_step_log(raw_log: str, step_name: str) -> str:
    lines = raw_log.splitlines()
    needle = step_name.lower()
    matches = [idx for idx, line in enumerate(lines) if needle in line.lower()]
    if not matches:
        return raw_log
    start = max(matches[0] - 5, 0)
    end = min(matches[-1] + 80, len(lines))
    return "\n".join(lines[start:end])


def _is_text_artifact_path(path: str) -> bool:
    lower = path.lower()
    return any(fnmatch.fnmatchcase(lower, pattern.lower()) for pattern in _TEXT_ARTIFACT_PATTERNS)


def _artifact_path_matches(filename: str, requested: str | None) -> bool:
    if not requested:
        return True
    requested = requested.strip()
    if not requested:
        return True
    return filename == requested or filename.startswith(requested.rstrip("/") + "/") or fnmatch.fnmatchcase(filename, requested)
