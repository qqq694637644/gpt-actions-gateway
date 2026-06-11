from __future__ import annotations

import fnmatch
import json

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

import app.config.settings as settings_module
from app.api.routes import router
from app.auth.authorization import CLASSIFIED_OPERATION_PATTERNS, token_sha256
from app.auth.dependencies import require_auth
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode


class RouteStub:
    def __init__(self, operation_id: str | None) -> None:
        self.operation_id = operation_id


def make_request(operation_id: str | None) -> Request:
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


def auth_users_json(*users: dict) -> str:
    return json.dumps(list(users))


@pytest.mark.anyio
async def test_gpt_action_secret_no_longer_authenticates(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        gpt_action_secret="legacy-admin-secret",
        auth_users_json=auth_users_json(
            {
                "username": "actual-admin",
                "token": "actual-admin-token",
                "roles": ["admin"],
            }
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await require_auth(make_request("workspaceCommitAndPush"), owner="acme", repo="demo", credentials=bearer("legacy-admin-secret"), settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_code == ErrorCode.AUTH_FAILED


@pytest.mark.anyio
async def test_configured_admin_can_write_any_repo_when_repo_scope_omitted(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=auth_users_json(
            {
                "username": "admin",
                "token": "admin-token-123",
                "roles": ["admin"],
            }
        ),
    )
    request = make_request("workspaceCommitAndPush")

    user = await require_auth(request, owner="any", repo="repo", credentials=bearer("admin-token-123"), settings=settings)

    assert user.is_admin
    assert user.username == "admin"
    assert request.state.actor == "admin"


@pytest.mark.anyio
async def test_configured_reader_can_read_allowed_repo(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=auth_users_json(
            {
                "username": "alice",
                "token": "alice-token",
                "roles": ["reader"],
                "repos": ["acme/*"],
            }
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
        auth_users_json=auth_users_json(
            {
                "username": "alice",
                "token": "alice-write-denied-token",
                "roles": ["reader"],
                "repos": ["acme/demo"],
            }
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
        auth_users_json=auth_users_json(
            {
                "username": "bob",
                "token": "bob-repo-token",
                "roles": ["writer"],
                "repos": ["acme/demo"],
            }
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
        auth_users_json=auth_users_json(
            {
                "username": "carol",
                "token_sha256": token_sha256("carol-secret-token"),
                "roles": ["writer"],
                "repos": ["acme/demo"],
            }
        ),
    )

    user = await require_auth(make_request("workspaceWriteFile"), owner="acme", repo="demo", credentials=bearer("carol-secret-token"), settings=settings)

    assert user.username == "carol"


@pytest.mark.anyio
async def test_missing_operation_id_is_denied_even_for_admin(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=auth_users_json(
            {
                "username": "admin",
                "token": "admin-token-123",
                "roles": ["admin"],
            }
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await require_auth(make_request(None), owner="acme", repo="demo", credentials=bearer("admin-token-123"), settings=settings)

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.AUTHZ_DENIED


@pytest.mark.anyio
async def test_unknown_operation_id_is_denied(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auth_users_json=auth_users_json(
            {
                "username": "dana",
                "token": "dana-token-123",
                "roles": ["maintainer"],
                "repos": ["acme/demo"],
            }
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await require_auth(make_request("newUnclassifiedOperation"), owner="acme", repo="demo", credentials=bearer("dana-token-123"), settings=settings)

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.AUTHZ_DENIED


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


def test_settings_caches_parsed_auth_users(monkeypatch, tmp_path) -> None:
    calls = 0
    real_parse = settings_module.parse_auth_users_json

    def counting_parse(raw: str | None):
        nonlocal calls
        calls += 1
        return real_parse(raw)

    monkeypatch.setattr(settings_module, "parse_auth_users_json", counting_parse)
    settings = settings_module.Settings(
        workspace_root=str(tmp_path / "workspaces"),
        workspace_mirror_root=str(tmp_path / "mirrors"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        auth_users_json=auth_users_json(
            {
                "username": "cached-user",
                "token": "cached-token-123",
                "roles": ["reader"],
                "repos": ["acme/demo"],
            }
        ),
    )

    assert calls == 1
    assert [user.username for user in settings.auth_users] == ["cached-user"]
    assert [user.username for user in settings.auth_users] == ["cached-user"]
    assert calls == 1


def test_all_api_routes_have_classified_operation_ids() -> None:
    missing_operation_id: list[str] = []
    operation_ids: list[str] = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.operation_id:
            missing_operation_id.append(f"{sorted(route.methods)} {route.path}")
            continue
        operation_ids.append(route.operation_id)

    assert missing_operation_id == []
    assert len(operation_ids) == len(set(operation_ids))

    unclassified = sorted(
        operation_id
        for operation_id in operation_ids
        if not any(fnmatch.fnmatchcase(operation_id, pattern) for pattern in CLASSIFIED_OPERATION_PATTERNS)
    )
    assert unclassified == []
