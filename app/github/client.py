from __future__ import annotations

import base64
import logging
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.auth import GitHubAuthProvider

logger = logging.getLogger(__name__)

_ARTIFACT_DOWNLOAD_PROGRESS_BYTES = 10 * 1024 * 1024
_ARTIFACT_DOWNLOAD_PROGRESS_SECONDS = 10.0


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.github_api_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            trust_env=settings.github_use_env_proxy,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": settings.github_api_version,
                "User-Agent": "gpt-actions-gateway-v2/2.0",
            },
            follow_redirects=False,
        )
        self._auth = GitHubAuthProvider(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_api_token(self) -> str:
        return await self._auth.get_api_token(self._client)

    def git_remote_url(self, owner: str, repo: str) -> str:
        parsed = urlparse(self.settings.github_api_base_url)
        if parsed.netloc == "api.github.com":
            return f"https://github.com/{owner}/{repo}.git"
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/api/v3"):
            base_path = base_path[: -len("/api/v3")]
        host_base = f"{parsed.scheme}://{parsed.netloc}{base_path}".rstrip("/")
        return f"{host_base}/{owner}/{repo}.git"

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
        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)
        started = time.perf_counter()
        try:
            response = await self._client.request(method, path, params=params, json=json, headers=request_headers, follow_redirects=follow_redirects)
        except httpx.TimeoutException as exc:
            logger.warning(
                "github_request.timeout method=%s path=%s raw_bytes=%s raw_text=%s follow_redirects=%s timeout_seconds=%s duration_ms=%.2f",
                method,
                path,
                raw_bytes,
                raw_text,
                follow_redirects,
                self.settings.request_timeout_seconds,
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request timed out.",
                status_code=502,
                suggestion="Check outbound network access to GitHub, or increase REQUEST_TIMEOUT_SECONDS if the network is slow.",
                details={"method": method, "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception(
                "github_request.http_error method=%s path=%s raw_bytes=%s raw_text=%s follow_redirects=%s duration_ms=%.2f",
                method,
                path,
                raw_bytes,
                raw_text,
                follow_redirects,
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request failed before receiving a valid response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and GitHub API reachability.",
                details={"method": method, "path": path, "error": str(exc)},
            ) from exc
        duration_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            logger.warning(
                "github_request.error_response method=%s path=%s status_code=%s content_length=%s raw_bytes=%s raw_text=%s follow_redirects=%s duration_ms=%.2f",
                method,
                path,
                response.status_code,
                response.headers.get("content-length"),
                raw_bytes,
                raw_text,
                follow_redirects,
                duration_ms,
            )
            self._raise_for_github_error(response)
        if raw_bytes:
            data = response.content
            logger.warning(
                "github_request.raw_bytes_done method=%s path=%s status_code=%s bytes=%s content_length=%s duration_ms=%.2f",
                method,
                path,
                response.status_code,
                len(data),
                response.headers.get("content-length"),
                duration_ms,
            )
            return data
        if raw_text:
            return response.text
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _raise_for_github_error(self, response: httpx.Response) -> None:
        status = response.status_code
        body = response.text[:4000]
        details = {"github_status": status, "body": body}
        if status in (401, 403) and "rate limit" in body.lower():
            raise ApiError(ErrorCode.GITHUB_RATE_LIMITED, "GitHub API rate limit exceeded.", status_code=429, details=details)
        if status in (401, 403):
            raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GitHub authentication or permission failed.", status_code=502, details=details)
        if status == 404:
            raise ApiError(ErrorCode.GITHUB_NOT_FOUND, "GitHub resource was not found.", status_code=404, details=details)
        if status in (405, 409):
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "GitHub reported a conflict.", status_code=409, details=details)
        if status in (410, 425):
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "GitHub Actions log is not ready or no longer available.", status_code=404, details=details)
        raise ApiError(ErrorCode.GITHUB_ERROR, "GitHub API request failed.", status_code=502, details=details)

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
        return await self._request("GET", f"/repos/{owner}/{repo}/git/ref/{self._path_q(ref)}")

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        ref = await self.get_ref(owner, repo, f"heads/{branch}")
        return ref["object"]["sha"]

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

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
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        if base:
            params["base"] = base
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params)

    async def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json={"head": head, "base": base, "title": title, "body": body})

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, *, per_page: int = 100) -> list[dict[str, Any]]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params={"per_page": per_page})

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
                "commit_title": commit_title,
                "commit_message": commit_message,
                "sha": sha,
                "merge_method": merge_method,
            }.items()
            if value is not None
        }
        return await self._request("PUT", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", json=payload)

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})

    async def list_workflow_runs(self, owner: str, repo: str, *, workflow_id: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if workflow_id:
            path = f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/runs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs"
        return await self._request("GET", path, params=params)

    async def get_workflow(self, owner: str, repo: str, workflow_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}")

    async def dispatch_workflow(self, owner: str, repo: str, workflow_id: str, *, ref: str, inputs: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        await self._request("POST", f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/dispatches", json=payload)

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    async def rerun_workflow_run(self, owner: str, repo: str, run_id: int, *, enable_debug_logging: bool = False) -> None:
        await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun", json={"enable_debug_logging": enable_debug_logging})

    async def list_jobs_for_run(self, owner: str, repo: str, run_id: int, *, run_attempt: int | None = None) -> dict[str, Any]:
        if run_attempt:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        return await self._request("GET", path, params={"per_page": 100})

    async def get_workflow_job(self, owner: str, repo: str, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}")

    async def rerun_workflow_job(self, owner: str, repo: str, job_id: int, *, enable_debug_logging: bool = False) -> None:
        await self._request("POST", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun", json={"enable_debug_logging": enable_debug_logging})

    async def download_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True, raw_text=True)

    async def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs", follow_redirects=True, raw_bytes=True)

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int, *, per_page: int = 100, page: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": per_page}
        if page is not None:
            params["page"] = page
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", params=params)

    async def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        path = f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
        started = time.perf_counter()
        logger.warning(
            "github_artifact_download.start owner=%s repo=%s artifact_id=%s api_path=%s timeout_seconds=%s",
            owner,
            repo,
            artifact_id,
            path,
            self.settings.request_timeout_seconds,
        )
        try:
            redirect_url = await self._resolve_artifact_download_url(owner, repo, artifact_id, path)
            data = await self._download_artifact_redirect_url(owner, repo, artifact_id, redirect_url, started)
        except Exception:
            logger.exception(
                "github_artifact_download.error owner=%s repo=%s artifact_id=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                (time.perf_counter() - started) * 1000,
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        mib = len(data) / (1024 * 1024)
        seconds = max(duration_ms / 1000, 0.001)
        logger.warning(
            "github_artifact_download.done owner=%s repo=%s artifact_id=%s bytes=%s duration_ms=%.2f mib_per_second=%.2f",
            owner,
            repo,
            artifact_id,
            len(data),
            duration_ms,
            mib / seconds,
        )
        return data

    async def _resolve_artifact_download_url(self, owner: str, repo: str, artifact_id: int, path: str) -> str:
        token = await self.get_api_token()
        started = time.perf_counter()
        try:
            response = await self._client.request(
                "GET",
                path,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "github_artifact_download.redirect_timeout owner=%s repo=%s artifact_id=%s path=%s timeout_seconds=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                path,
                self.settings.request_timeout_seconds,
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub artifact download redirect request timed out.",
                status_code=502,
                suggestion="Check outbound network access to GitHub, or increase REQUEST_TIMEOUT_SECONDS if the network is slow.",
                details={"method": "GET", "path": path, "artifact_id": artifact_id},
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception(
                "github_artifact_download.redirect_http_error owner=%s repo=%s artifact_id=%s path=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                path,
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub artifact download redirect request failed before receiving a valid response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and GitHub API reachability.",
                details={"method": "GET", "path": path, "artifact_id": artifact_id, "error": str(exc)},
            ) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            logger.warning(
                "github_artifact_download.redirect_error_response owner=%s repo=%s artifact_id=%s path=%s status_code=%s content_length=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                path,
                response.status_code,
                response.headers.get("content-length"),
                duration_ms,
            )
            self._raise_for_github_error(response)
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub artifact download endpoint did not return a redirect.",
                status_code=502,
                details={"artifact_id": artifact_id, "status_code": response.status_code, "path": path},
            )
        redirect_url = response.headers.get("location")
        if not redirect_url:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub artifact download redirect did not include a Location header.",
                status_code=502,
                details={"artifact_id": artifact_id, "status_code": response.status_code, "path": path},
            )
        parsed = urlparse(redirect_url)
        logger.warning(
            "github_artifact_download.redirect_resolved owner=%s repo=%s artifact_id=%s status_code=%s redirect_host=%s redirect_scheme=%s duration_ms=%.2f",
            owner,
            repo,
            artifact_id,
            response.status_code,
            parsed.netloc,
            parsed.scheme,
            duration_ms,
        )
        return redirect_url

    async def _download_artifact_redirect_url(self, owner: str, repo: str, artifact_id: int, redirect_url: str, started: float) -> bytes:
        parsed = urlparse(redirect_url)
        data = bytearray()
        last_progress_time = time.perf_counter()
        last_progress_bytes = 0
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        headers = {"Accept": "application/octet-stream", "User-Agent": "gpt-actions-gateway-v2/2.0"}
        logger.warning(
            "github_artifact_download.stream_start owner=%s repo=%s artifact_id=%s redirect_host=%s timeout_seconds=%s",
            owner,
            repo,
            artifact_id,
            parsed.netloc,
            self.settings.request_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=self.settings.github_use_env_proxy, follow_redirects=True, headers=headers) as client:
                async with client.stream("GET", redirect_url) as response:
                    content_length = response.headers.get("content-length")
                    logger.warning(
                        "github_artifact_download.stream_response owner=%s repo=%s artifact_id=%s status_code=%s content_length=%s final_url_host=%s",
                        owner,
                        repo,
                        artifact_id,
                        response.status_code,
                        content_length,
                        response.url.host,
                    )
                    if response.status_code >= 400:
                        body = (await response.aread())[:4000]
                        logger.warning(
                            "github_artifact_download.stream_error_response owner=%s repo=%s artifact_id=%s status_code=%s body=%s",
                            owner,
                            repo,
                            artifact_id,
                            response.status_code,
                            body.decode("utf-8", errors="replace"),
                        )
                        raise ApiError(
                            ErrorCode.GITHUB_ERROR,
                            "Artifact storage download request failed.",
                            status_code=502,
                            details={"artifact_id": artifact_id, "storage_status": response.status_code, "body": body.decode("utf-8", errors="replace")},
                        )
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        data.extend(chunk)
                        now = time.perf_counter()
                        if len(data) - last_progress_bytes >= _ARTIFACT_DOWNLOAD_PROGRESS_BYTES or now - last_progress_time >= _ARTIFACT_DOWNLOAD_PROGRESS_SECONDS:
                            elapsed_seconds = max(now - started, 0.001)
                            logger.warning(
                                "github_artifact_download.progress owner=%s repo=%s artifact_id=%s bytes=%s content_length=%s duration_ms=%.2f mib_per_second=%.2f",
                                owner,
                                repo,
                                artifact_id,
                                len(data),
                                content_length,
                                elapsed_seconds * 1000,
                                (len(data) / (1024 * 1024)) / elapsed_seconds,
                            )
                            last_progress_bytes = len(data)
                            last_progress_time = now
        except httpx.TimeoutException as exc:
            logger.warning(
                "github_artifact_download.stream_timeout owner=%s repo=%s artifact_id=%s bytes=%s timeout_seconds=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                len(data),
                self.settings.request_timeout_seconds,
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "Artifact storage download timed out.",
                status_code=502,
                suggestion="Check artifact storage download bandwidth or increase REQUEST_TIMEOUT_SECONDS if the storage path is slow.",
                details={"artifact_id": artifact_id, "downloaded_bytes": len(data)},
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception(
                "github_artifact_download.stream_http_error owner=%s repo=%s artifact_id=%s bytes=%s duration_ms=%.2f",
                owner,
                repo,
                artifact_id,
                len(data),
                (time.perf_counter() - started) * 1000,
            )
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "Artifact storage download failed before receiving a complete response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and GitHub artifact storage reachability.",
                details={"artifact_id": artifact_id, "downloaded_bytes": len(data), "error": str(exc)},
            ) from exc
        return bytes(data)

    async def list_actions_caches(self, owner: str, repo: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/caches", params=params)

    async def delete_actions_cache(self, owner: str, repo: str, cache_id: int) -> None:
        await self._request("DELETE", f"/repos/{owner}/{repo}/actions/caches/{cache_id}")
