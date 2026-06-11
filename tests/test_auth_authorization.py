from __future__ import annotations

import json

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from app.auth.authorization import token_sha256
from app.auth.dependencies import require_auth
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode


class RouteStub:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id


def make_request(operation_id: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/repos/acme/demo/test",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope)
    request.scope["route"] = RouteStub(operation_id)
    return request


def bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def make_settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        rate_limit_per_minute=1000,
        **kwargs,
    )


@pytest.mark.anyio
async def test_legacy_gpt_action_secret_authenticates_as_admin(tmp_path) -> None:
    settings = make_settings(tmp_path, gpt_action_secret="admin-secret")
    request = make_request("workspaceCommitAndPush")

    user = await require_auth(request, owner="any", repo="repo", credentials=bearer("admin-secret"), settings=settings)

    assert user.is_admin
    assert user.username == "legacy-admin"
    assert request.state.actor == "legacy-admin"


@pytest.mark.anyio
async def test_configured_reader_can_read_allowed_repo(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=json.dumps(
            [
                {
                    "username": "alice",
                    "token": "alice-token",
                    "roles": ["reader"],
                    "repos": ["acme/*"],
                }
            ]
        ),
    )
    request = make_request("getPullRequest")

    user = await require_auth(request, owner="acme", repo="demo", credentials=bearer("alice-token"), settings=settings)

    assert user.username == "alice"
    assert not user.is_admin


@pytest.mark.anyio
async def test_configured_reader_is_denied_write_operation(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=json.dumps(
            [
                {
                    "username": "alice",
                    "token": "alice-write-denied-token",
                    "roles": ["reader"],
                    "repos": ["acme/demo"],
                }
            ]
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await require_auth(make_request("workspaceCommitAndPush"), owner="acme", repo="demo", credentials=bearer("alice-write-denied-token"), settings=settings)

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.AUTHZ_DENIED


@pytest.mark.anyio
async def test_configured_user_repo_scope_denies_other_repo(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=json.dumps(
            [
                {
                    "username": "bob",
                    "token": "bob-repo-token",
                    "roles": ["writer"],
                    "repos": ["acme/demo"],
                }
            ]
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await require_auth(make_request("workspaceWriteFile"), owner="other", repo="demo", credentials=bearer("bob-repo-token"), settings=settings)

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.AUTHZ_DENIED


@pytest.mark.anyio
async def test_configured_user_can_authenticate_with_token_hash(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=json.dumps(
            [
                {
                    "username": "carol",
                    "token_sha256": token_sha256("carol-secret-token"),
                    "roles": ["writer"],
                    "repos": ["acme/demo"],
                }
            ]
        ),
    )

    user = await require_auth(make_request("workspaceWriteFile"), owner="acme", repo="demo", credentials=bearer("carol-secret-token"), settings=settings)

    assert user.username == "carol"


def test_settings_rejects_invalid_auth_users_json(tmp_path) -> None:
    with pytest.raises(ValidationError, match="AUTH_USERS_JSON must be valid JSON"):
        make_settings(tmp_path, auth_users_json="not json")


def test_settings_rejects_duplicate_auth_user_tokens(tmp_path) -> None:
    payload = [
        {"username": "alice", "token": "shared-token", "repos": ["acme/demo"]},
        {"username": "bob", "token": "shared-token", "repos": ["acme/demo"]},
    ]

    with pytest.raises(ValidationError, match="duplicate user tokens"):
        make_settings(tmp_path, auth_users_json=json.dumps(payload))
