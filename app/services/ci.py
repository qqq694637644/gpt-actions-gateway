from __future__ import annotations

from typing import Any

from app.ci.logs import parse_failed_log
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.ci import (
    Annotation,
    CIJob,
    CIRun,
    CIStatusResponse,
    FailedCILogResponse,
    FailedJobLog,
    FailedStep,
    RerunCIRequest,
    RerunCIResponse,
)
from app.policy.rules import Policy


class CIService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings

    async def get_ci_status(
        self,
        owner: str,
        repo: str,
        *,
        commit_sha: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        workflow_id: str | None = None,
        event: str | None = None,
        created_after: str | None = None,
    ) -> CIStatusResponse:
        self.policy.assert_repo_allowed(owner, repo)
        matched_by = "branch"
        target_sha = commit_sha
        warning = None
        if pr_number is not None:
            pr = await self.github.get_pull_request(owner, repo, pr_number)
            target_sha = pr["head"]["sha"]
            branch = pr["head"]["ref"]
            matched_by = "pr_number"
        elif commit_sha:
            matched_by = "commit_sha"
        elif branch:
            self.policy.assert_read_ref_allowed(branch)
            # Avoid stale branch-only matches by resolving current branch head and matching that SHA.
            target_sha = await self.github.get_branch_head(owner, repo, branch)
            matched_by = "branch"
            warning = "Branch query was resolved to the current branch head SHA to avoid stale workflow runs."
        else:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Provide commit_sha, pr_number, or branch.", status_code=422)

        params: dict[str, Any] = {"per_page": 100}
        if target_sha:
            params["head_sha"] = target_sha
        if branch:
            params["branch"] = branch
        if event:
            params["event"] = event
        if created_after:
            params["created"] = f">={created_after}"

        payload = await self.github.list_workflow_runs(owner, repo, workflow_id=workflow_id, params=params)
        raw_runs = payload.get("workflow_runs", [])
        if target_sha:
            raw_runs = [run for run in raw_runs if run.get("head_sha") == target_sha]
        if not raw_runs:
            raise ApiError(
                ErrorCode.CI_RUN_NOT_FOUND,
                "No matching workflow runs were found.",
                status_code=404,
                suggestion="Check that CI has started for the commit, or query again with the exact commit_sha.",
                details={"commit_sha": target_sha, "branch": branch, "workflow_id": workflow_id, "event": event},
            )

        runs: list[CIRun] = []
        for raw_run in raw_runs[:20]:
            run_attempt = raw_run.get("run_attempt")
            jobs_payload = await self.github.list_jobs_for_run(owner, repo, raw_run["id"], run_attempt=run_attempt)
            jobs = [self._job_from_github(job) for job in jobs_payload.get("jobs", [])]
            runs.append(
                CIRun(
                    run_id=raw_run["id"],
                    run_attempt=run_attempt,
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
                    jobs=jobs,
                )
            )

        status, conclusion = self._aggregate(runs)
        return CIStatusResponse(matched_by=matched_by, status=status, conclusion=conclusion, workflow_runs=runs, warning=warning)

    def _job_from_github(self, job: dict[str, Any]) -> CIJob:
        failed_steps = []
        for step in job.get("steps") or []:
            conclusion = step.get("conclusion")
            if conclusion and conclusion not in {"success", "skipped", "neutral"}:
                failed_steps.append(
                    FailedStep(
                        name=step.get("name"),
                        number=step.get("number"),
                        status=step.get("status"),
                        conclusion=conclusion,
                        started_at=step.get("started_at"),
                        completed_at=step.get("completed_at"),
                    )
                )
        return CIJob(
            job_id=job["id"],
            name=job.get("name") or str(job["id"]),
            status=job.get("status"),
            conclusion=job.get("conclusion"),
            html_url=job.get("html_url"),
            failed_steps=failed_steps,
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

    async def get_failed_ci_log(
        self,
        owner: str,
        repo: str,
        *,
        run_id: int,
        run_attempt: int | None = None,
        job_id: int | None = None,
        max_lines: int | None = None,
    ) -> FailedCILogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        jobs_payload = await self.github.list_jobs_for_run(owner, repo, run_id, run_attempt=run_attempt)
        jobs = jobs_payload.get("jobs", [])
        if job_id is not None:
            jobs = [job for job in jobs if int(job["id"]) == job_id]
        else:
            jobs = [job for job in jobs if job.get("conclusion") not in {None, "success", "skipped", "neutral"}]
        if not jobs:
            raise ApiError(ErrorCode.CI_RUN_NOT_FOUND, "No failed jobs matched this workflow run.", status_code=404, details={"run_id": run_id, "job_id": job_id})

        max_lines = min(max_lines or self.settings.max_log_lines, self.settings.max_log_lines)
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
        return FailedCILogResponse(run_id=run_id, run_attempt=run_attempt, failed_jobs=failed_jobs)

    async def rerun_failed_ci(self, owner: str, repo: str, request: RerunCIRequest) -> RerunCIResponse:
        self.policy.assert_repo_allowed(owner, repo)
        if not self.settings.allow_rerun_ci:
            raise ApiError(
                ErrorCode.NOT_IMPLEMENTED,
                "CI rerun is disabled by configuration.",
                status_code=403,
                suggestion="Set ALLOW_RERUN_CI=true and grant Actions: Write only after reviewing the risk.",
            )
        run = await self.github.get_workflow_run(owner, repo, request.run_id)
        head_branch = run.get("head_branch") or ""
        self.policy.assert_write_branch_allowed(head_branch)
        await self.github.rerun_failed_jobs(owner, repo, request.run_id)
        return RerunCIResponse(run_id=request.run_id, accepted=True, message="GitHub accepted rerun_failed_jobs request.")
