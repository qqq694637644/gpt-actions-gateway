from __future__ import annotations

import base64
import io
import re
import zipfile
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.gitea.auth import GiteaAuthProvider


class GiteaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.effective_gitea_api_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            trust_env=settings.effective_gitea_use_env_proxy,
            headers={
                "Accept": "application/json",
                "User-Agent": "gpt-actions-gitea-gateway-v2/2.0",
            },
            follow_redirects=False,
        )
        self._auth = GiteaAuthProvider(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_api_token(self) -> str:
        return await self._auth.get_api_token(self._client)

    def git_remote_url(self, owner: str, repo: str) -> str:
        return f"{self.web_base_url()}/{owner}/{repo}.git"

    def web_base_url(self) -> str:
        parsed = urlparse(self.settings.effective_gitea_api_base_url)
        base_path = re.sub(r"/api/v1/?$", "", parsed.path.rstrip("/"))
        return f"{parsed.scheme}://{parsed.netloc}{base_path}".rstrip("/")

    def commit_url(self, owner: str, repo: str, sha: str) -> str:
        return f"{self.web_base_url()}/{owner}/{repo}/commit/{sha}"

    async def git_auth_config(self) -> list[str]:
        credentials = await self._auth.get_git_credentials(self._client)
        basic_token = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode("ascii")
        return [
            "-c",
            f"http.extraHeader=Authorization: Basic {basic_token}",
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "credential.interactive=never",
        ]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        follow_redirects: bool = False,
        raw_text: bool = False,
        raw_bytes: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        token = await self.get_api_token()
        request_headers = {"Authorization": f"token {token}"}
        if headers:
            request_headers.update(headers)
        request_path = path.lstrip("/")
        try:
            response = await self._client.request(method, request_path, params=params, json=json, headers=request_headers, follow_redirects=follow_redirects)
        except httpx.TimeoutException as exc:
            raise ApiError(
                ErrorCode.GITEA_ERROR,
                "Gitea API request timed out.",
                status_code=502,
                suggestion="Check outbound network access to Gitea, or increase REQUEST_TIMEOUT_SECONDS if the network is slow.",
                details={"method": method, "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.GITEA_ERROR,
                "Gitea API request failed before receiving a valid response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and Gitea API reachability.",
                details={"method": method, "path": path, "error": str(exc)},
            ) from exc
        if response.status_code >= 400:
            self._raise_for_gitea_error(response)
        if raw_bytes:
            return response.content
        if raw_text:
            return response.text
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _raise_for_gitea_error(self, response: httpx.Response) -> None:
        status = response.status_code
        body = response.text[:4000]
        details = {"gitea_status": status, "body": body}
        if status == 429 or (status in (401, 403) and "rate limit" in body.lower()):
            raise ApiError(ErrorCode.GITEA_RATE_LIMITED, "Gitea API rate limit exceeded.", status_code=429, details=details)
        if status in (401, 403):
            raise ApiError(ErrorCode.GITEA_AUTH_FAILED, "Gitea authentication or permission failed.", status_code=502, details=details)
        if status == 404:
            raise ApiError(ErrorCode.GITEA_NOT_FOUND, "Gitea resource was not found.", status_code=404, details=details)
        if status in (405, 409, 422):
            raise ApiError(ErrorCode.GITEA_CONFLICT, "Gitea reported a conflict.", status_code=409, details=details)
        if status in (410, 425):
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "Gitea Actions log is not ready or no longer available.", status_code=404, details=details)
        raise ApiError(ErrorCode.GITEA_ERROR, "Gitea API request failed.", status_code=502, details=details)

    @staticmethod
    def _q(value: str | int) -> str:
        return quote(str(value), safe="")

    @staticmethod
    def _path_q(path: str) -> str:
        return quote(path, safe="/")

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}")

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/branches/{self._path_q(branch)}")

    async def get_ref(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/repos/{owner}/{repo}/git/refs/{self._path_q(ref)}")
        if isinstance(payload, list):
            wanted = f"refs/{ref}"
            for item in payload:
                if item.get("ref") == wanted or item.get("ref") == ref:
                    return item
            if len(payload) == 1:
                return payload[0]
            raise ApiError(ErrorCode.GITEA_NOT_FOUND, "Gitea git ref was not found.", status_code=404, details={"ref": ref})
        return payload

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        ref = await self.get_ref(owner, repo, f"heads/{branch}")
        return ref["object"]["sha"]

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/branches",
            json={"new_branch_name": branch, "old_ref_name": sha},
        )

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/git/commits/{commit_sha}")

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        head: str | None = None,
        base: str | None = None,
        state: str = "open",
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "limit": max(1, min(per_page, 100))}
        payload = await self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params)
        pulls = payload.get("pull_requests", []) if isinstance(payload, dict) else list(payload or [])
        head_ref = head.split(":", 1)[-1] if head else None
        if head_ref:
            pulls = [pr for pr in pulls if ((pr.get("head") or {}).get("ref") == head_ref or (pr.get("head") or {}).get("label") == head)]
        if base:
            pulls = [pr for pr in pulls if (pr.get("base") or {}).get("ref") == base]
        return pulls

    async def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json={"head": head, "base": base, "title": title, "body": body})

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, *, per_page: int = 100) -> list[dict[str, Any]]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params={"limit": max(1, min(per_page, 100))})

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        base: str | None = None,
    ) -> dict[str, Any]:
        payload = {k: v for k, v in {"title": title, "body": body, "state": state, "base": base}.items() if v is not None}
        return await self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", json=payload)

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        commit_title: str | None = None,
        commit_message: str | None = None,
        sha: str | None = None,
        merge_method: str = "merge",
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "do": _gitea_merge_method(merge_method),
                "merge_title_field": commit_title,
                "merge_message_field": commit_message,
                "head_commit_id": sha,
            }.items()
            if value is not None
        }
        raw = await self._request("POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", json=payload)
        result: dict[str, Any] = {"merged": True, "message": "Pull request merged."}
        if isinstance(raw, dict):
            result["message"] = raw.get("message") or raw.get("merge_message") or result["message"]
            result["sha"] = raw.get("sha") or raw.get("merge_commit_id") or raw.get("merged_commit_id")
        return result

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})

    async def list_workflow_runs(self, owner: str, repo: str, *, workflow_id: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_params = _gitea_list_params(params)
        if workflow_id:
            path = f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/runs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs"
        return await self._request("GET", path, params=normalized_params)

    async def get_workflow(self, owner: str, repo: str, workflow_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}")

    async def dispatch_workflow(self, owner: str, repo: str, workflow_id: str, *, ref: str, inputs: dict[str, Any] | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = {str(key): str(value) for key, value in inputs.items()}
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/dispatches",
            params={"return_run_details": "true"},
            json=payload,
        )

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    async def rerun_workflow_run(self, owner: str, repo: str, run_id: int, *, enable_debug_logging: bool = False) -> None:
        del enable_debug_logging
        await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun")

    async def list_jobs_for_run(self, owner: str, repo: str, run_id: int, *, run_attempt: int | None = None) -> dict[str, Any]:
        if run_attempt:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        return await self._request("GET", path, params={"limit": 100})

    async def get_workflow_job(self, owner: str, repo: str, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}")

    async def rerun_workflow_job(self, owner: str, repo: str, job_id: int, *, enable_debug_logging: bool = False) -> None:
        del enable_debug_logging
        job = await self.get_workflow_job(owner, repo, job_id)
        run_id = job.get("run_id")
        if not run_id:
            raise ApiError(ErrorCode.GITEA_ERROR, "Gitea workflow job payload did not include run_id.", status_code=502, details={"job_id": job_id})
        await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{int(run_id)}/jobs/{job_id}/rerun")

    async def download_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True, raw_text=True)

    async def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        jobs_payload = await self.list_jobs_for_run(owner, repo, run_id)
        jobs = jobs_payload.get("jobs", [])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for job in jobs:
                job_id = int(job["id"])
                name = _safe_log_name(job.get("name") or str(job_id))
                try:
                    log_text = await self.download_job_logs(owner, repo, job_id)
                except ApiError as exc:
                    log_text = f"Unable to download job log: {exc.message}\n"
                archive.writestr(f"{job_id}-{name}.log", log_text)
        return buffer.getvalue()

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int, *, per_page: int = 100, page: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(per_page, 100))}
        if page is not None:
            params["page"] = page
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", params=params)

    async def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip", follow_redirects=True, raw_bytes=True)

    async def list_actions_caches(self, owner: str, repo: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise ApiError(
            ErrorCode.GITEA_UNSUPPORTED,
            "Gitea API does not expose GitHub-compatible Actions cache listing endpoints.",
            status_code=501,
            suggestion="Use Gitea server-side cache maintenance or runner storage cleanup instead.",
            details={"owner": owner, "repo": repo, "params": params or {}},
        )

    async def delete_actions_cache(self, owner: str, repo: str, cache_id: int) -> None:
        raise ApiError(
            ErrorCode.GITEA_UNSUPPORTED,
            "Gitea API does not expose GitHub-compatible Actions cache deletion endpoints.",
            status_code=501,
            suggestion="Use Gitea server-side cache maintenance or runner storage cleanup instead.",
            details={"owner": owner, "repo": repo, "cache_id": cache_id},
        )


def _gitea_merge_method(merge_method: str) -> str:
    mapping = {
        "merge": "merge",
        "squash": "squash",
        "rebase": "rebase",
        "rebase-merge": "rebase-merge",
        "fast-forward-only": "fast-forward-only",
    }
    return mapping.get(merge_method, merge_method)


def _gitea_list_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    normalized = dict(params)
    if "per_page" in normalized and "limit" not in normalized:
        normalized["limit"] = normalized.pop("per_page")
    return normalized


def _safe_log_name(name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-_") or "job")[:80]
