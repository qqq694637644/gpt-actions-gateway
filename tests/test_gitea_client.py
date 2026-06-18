from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.gitea.client import GiteaClient


class Recorder:
    def __init__(self, payloads: dict[tuple[str, str], object]) -> None:
        self.payloads = payloads
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            self.bodies.append(json.loads(request.content.decode()))
        payload = self.payloads.get((request.method, request.url.path))
        if payload is None:
            return httpx.Response(404, json={"message": f"not found: {request.method} {request.url.path}"})
        return httpx.Response(200, json=payload)


def run(coro):
    return asyncio.run(coro)


async def _client(recorder: Recorder) -> GiteaClient:
    client = GiteaClient(
        Settings(
            gitea_api_base_url="https://gitea.test/api/v1",
            gitea_token="token",
            gitea_git_username="bot",
        )
    )
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(base_url="https://gitea.test/api/v1/", transport=httpx.MockTransport(recorder))  # noqa: SLF001
    return client


def test_gitea_ref_and_branch_creation_endpoints() -> None:
    async def scenario() -> tuple[dict, Recorder]:
        recorder = Recorder(
            {
                ("GET", "/api/v1/repos/acme/demo/git/refs/heads/main"): {"ref": "refs/heads/main", "object": {"sha": "abc", "type": "commit"}},
                ("POST", "/api/v1/repos/acme/demo/branches"): {"name": "gpt/fix"},
            }
        )
        client = await _client(recorder)
        try:
            ref = await client.get_ref("acme", "demo", "heads/main")
            await client.create_ref("acme", "demo", "gpt/fix", "abc")
        finally:
            await client.aclose()
        return ref, recorder

    ref, recorder = run(scenario())

    assert ref["object"]["sha"] == "abc"
    assert recorder.requests[0].url.path == "/api/v1/repos/acme/demo/git/refs/heads/main"
    assert recorder.requests[1].method == "POST"
    assert recorder.requests[1].url.path == "/api/v1/repos/acme/demo/branches"
    assert recorder.bodies[0] == {"new_branch_name": "gpt/fix", "old_ref_name": "abc"}


def test_gitea_merge_uses_post_and_gitea_payload() -> None:
    async def scenario() -> tuple[dict, Recorder]:
        recorder = Recorder({("POST", "/api/v1/repos/acme/demo/pulls/7/merge"): {"merge_commit_id": "def"}})
        client = await _client(recorder)
        try:
            result = await client.merge_pull_request(
                "acme",
                "demo",
                7,
                commit_title="merge title",
                commit_message="merge body",
                sha="abc",
                merge_method="squash",
            )
        finally:
            await client.aclose()
        return result, recorder

    result, recorder = run(scenario())

    assert result == {"merged": True, "message": "Pull request merged.", "sha": "def"}
    assert recorder.requests[0].method == "POST"
    assert recorder.bodies[0] == {
        "do": "squash",
        "merge_title_field": "merge title",
        "merge_message_field": "merge body",
        "head_commit_id": "abc",
    }


def test_gitea_list_pulls_filters_head_and_base_client_side() -> None:
    async def scenario() -> tuple[list[dict], Recorder]:
        recorder = Recorder(
            {
                ("GET", "/api/v1/repos/acme/demo/pulls"): [
                    {"number": 1, "head": {"ref": "gpt/one"}, "base": {"ref": "main"}},
                    {"number": 2, "head": {"ref": "gpt/two"}, "base": {"ref": "main"}},
                    {"number": 3, "head": {"ref": "gpt/one"}, "base": {"ref": "release"}},
                ]
            }
        )
        client = await _client(recorder)
        try:
            pulls = await client.list_pull_requests("acme", "demo", head="acme:gpt/one", base="main", state="open", per_page=25)
        finally:
            await client.aclose()
        return pulls, recorder

    pulls, recorder = run(scenario())

    assert [pull["number"] for pull in pulls] == [1]
    assert recorder.requests[0].url.params["limit"] == "25"
    assert "head" not in recorder.requests[0].url.params


def test_gitea_dispatch_requests_run_details_and_stringifies_inputs() -> None:
    async def scenario() -> tuple[dict | None, Recorder]:
        recorder = Recorder(
            {
                ("POST", "/api/v1/repos/acme/demo/actions/workflows/build.yml/dispatches"): {
                    "workflow_run_id": 123,
                    "run_url": "https://gitea.test/api/v1/repos/acme/demo/actions/runs/123",
                }
            }
        )
        client = await _client(recorder)
        try:
            result = await client.dispatch_workflow("acme", "demo", "build.yml", ref="main", inputs={"count": 3})
        finally:
            await client.aclose()
        return result, recorder

    result, recorder = run(scenario())

    assert result and result["workflow_run_id"] == 123
    assert recorder.requests[0].url.params["return_run_details"] == "true"
    assert recorder.bodies[0] == {"ref": "main", "inputs": {"count": "3"}}


def test_gitea_actions_cache_api_is_reported_unsupported() -> None:
    async def scenario() -> None:
        client = GiteaClient(Settings(gitea_token="token", gitea_git_username="bot"))
        try:
            with pytest.raises(ApiError) as raised:
                await client.list_actions_caches("acme", "demo")
        finally:
            await client.aclose()
        assert raised.value.error_code == ErrorCode.GITEA_UNSUPPORTED

    run(scenario())
