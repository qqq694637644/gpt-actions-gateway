from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.auth import GitHubAuthProvider


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
                "User-Agent": "gpt-actions-gateway/1.0",
            },
            follow_redirects=False,
        )
        self._auth = GitHubAuthProvider(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        follow_redirects: bool = False,
        raw_text: bool = False,
    ) -> Any:
        token = await self._auth.get_token(self._client)
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=follow_redirects,
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request timed out.",
                status_code=502,
                suggestion="Check outbound network access to GitHub, or increase REQUEST_TIMEOUT_SECONDS if the network is slow.",
                details={"method": method, "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request failed before receiving a valid response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and GitHub API reachability.",
                details={"method": method, "path": path, "error": str(exc)},
            ) from exc
        if response.status_code >= 400:
            self._raise_for_github_error(response)
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
        if status == 409:
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "GitHub reported a conflict.", status_code=409, details=details)
        if status in (410, 425):
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "GitHub Actions log is not ready or no longer available.", status_code=404, details=details)
        raise ApiError(ErrorCode.GITHUB_ERROR, "GitHub API request failed.", status_code=502, details=details)

    @staticmethod
    def _q(value: str) -> str:
        return quote(value, safe="")

    @staticmethod
    def _path_q(path: str) -> str:
        return quote(path, safe="/")

    async def get_ref(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/git/ref/{self._path_q(ref)}")

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        ref = await self.get_ref(owner, repo, f"heads/{branch}")
        return ref["object"]["sha"]

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def update_ref(self, owner: str, repo: str, branch: str, sha: str, *, force: bool = False) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/git/refs/heads/{self._path_q(branch)}",
            json={"sha": sha, "force": force},
        )

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/git/commits/{commit_sha}")

    async def create_tree(self, owner: str, repo: str, base_tree: str | None, tree: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {"tree": tree}
        if base_tree:
            payload["base_tree"] = base_tree
        return await self._request("POST", f"/repos/{owner}/{repo}/git/trees", json=payload)

    async def create_commit(self, owner: str, repo: str, message: str, tree_sha: str, parents: list[str]) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": parents},
        )

    async def get_tree(self, owner: str, repo: str, tree_sha: str, *, recursive: bool = True) -> dict[str, Any]:
        params = {"recursive": "1"} if recursive else None
        return await self._request("GET", f"/repos/{owner}/{repo}/git/trees/{tree_sha}", params=params)

    async def get_contents(self, owner: str, repo: str, path: str, *, ref: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{self._path_q(path)}",
            params={"ref": ref},
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        message: str,
        content_base64: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": message, "content": content_base64}
        if branch:
            payload["branch"] = branch
        if sha:
            payload["sha"] = sha
        return await self._request("PUT", f"/repos/{owner}/{repo}/contents/{self._path_q(path)}", json=payload)

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes:
        payload = await self._request("GET", f"/repos/{owner}/{repo}/git/blobs/{blob_sha}")
        if payload.get("encoding") != "base64":
            raise ApiError(ErrorCode.GITHUB_ERROR, "Unsupported GitHub blob encoding.", status_code=502, details={"encoding": payload.get("encoding")})
        return base64.b64decode(payload.get("content", ""), validate=False)

    async def list_pull_requests(self, owner: str, repo: str, *, head: str | None = None, base: str | None = None, state: str = "open") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "per_page": 50}
        if head:
            params["head"] = head
        if base:
            params["base"] = base
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params)

    async def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json={"head": head, "base": base, "title": title, "body": body})

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        merge_method: str,
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            payload["commit_title"] = commit_title
        if commit_message is not None:
            payload["commit_message"] = commit_message
        return await self._request("PUT", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", json=payload)

    async def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        *,
        workflow_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if workflow_id:
            path = f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/runs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs"
        return await self._request("GET", path, params=params)

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    async def list_jobs_for_run(self, owner: str, repo: str, run_id: int, *, run_attempt: int | None = None) -> dict[str, Any]:
        if run_attempt:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        return await self._request("GET", path, params={"per_page": 100})

    async def download_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            follow_redirects=True,
            raw_text=True,
        )

    async def rerun_failed_jobs(self, owner: str, repo: str, run_id: int) -> None:
        await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
