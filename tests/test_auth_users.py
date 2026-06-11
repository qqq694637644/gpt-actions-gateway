import json

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.auth.dependencies import _rate_windows, require_auth
from app.config.settings import Settings, get_settings
from app.errors import ErrorCode, register_exception_handlers


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(**kwargs) -> Settings:
    kwargs.setdefault("rate_limit_per_minute", 1000)
    return Settings(**kwargs)


def make_app(settings: Settings, *, operation_id: str = "workspaceStatus") -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/repos/{owner}/{repo}/authorized", operation_id=operation_id, dependencies=[Depends(require_auth)])
    async def authorized() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def post_authorized(app: FastAPI, *, token: str, owner: str = "acme", repo: str = "demo") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(f"/repos/{owner}/{repo}/authorized", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.anyio
async def test_legacy_gpt_action_secret_still_authenticates_as_admin() -> None:
    app = make_app(make_settings(gpt_action_secret="legacy-secret"), operation_id="deleteCache")

    response = await post_authorized(app, token="legacy-secret")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.anyio
async def test_configured_reader_can_call_read_operation_for_allowed_repo() -> None:
    settings = make_settings(
        gateway_users_json=json.dumps(
            [
                {
                    "name": "reader",
                    "token": "reader-token",
                    "role": "reader",
                    "allowed_repos": ["acme/demo"],
                }
            ]
        )
    )
    app = make_app(settings, operation_id="workspaceStatus")

    response = await post_authorized(app, token="reader-token")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.anyio
async def test_user_repository_allowlist_is_enforced_before_route_handler() -> None:
    settings = make_settings(
        gateway_users_json=json.dumps([{"name": "reader", "token": "reader-token", "role": "reader", "allowed_repos": ["acme/demo"]}])
    )
    app = make_app(settings, operation_id="workspaceStatus")

    response = await post_authorized(app, token="reader-token", owner="acme", repo="other")

    assert response.status_code == 403
    assert response.json()["error_code"] == ErrorCode.AUTH_FORBIDDEN
    assert response.json()["details"] == {"actor": "reader", "role": "reader", "repo": "acme/other"}


@pytest.mark.anyio
async def test_role_operation_permissions_are_enforced() -> None:
    settings = make_settings(gateway_users_json=json.dumps([{"name": "reader", "token": "reader-token", "role": "reader"}]))
    app = make_app(settings, operation_id="workspaceApplyPatch")

    response = await post_authorized(app, token="reader-token")

    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == ErrorCode.AUTH_FORBIDDEN
    assert body["details"]["operation_id"] == "workspaceApplyPatch"


@pytest.mark.anyio
async def test_allowed_operations_can_extend_role_permissions() -> None:
    settings = make_settings(
        gateway_users_json=json.dumps(
            [
                {
                    "name": "reader-plus",
                    "token": "reader-plus-token",
                    "role": "reader",
                    "allowed_operations": ["workspaceApplyPatch"],
                }
            ]
        )
    )
    app = make_app(settings, operation_id="workspaceApplyPatch")

    response = await post_authorized(app, token="reader-plus-token")

    assert response.status_code == 200


@pytest.mark.anyio
async def test_denied_operations_override_role_permissions() -> None:
    settings = make_settings(
        gateway_users_json=json.dumps(
            [
                {
                    "name": "maintainer",
                    "token": "maintainer-token",
                    "role": "maintainer",
                    "denied_operations": ["workspaceExecPwsh"],
                }
            ]
        )
    )
    app = make_app(settings, operation_id="workspaceExecPwsh")

    response = await post_authorized(app, token="maintainer-token")

    assert response.status_code == 403
    assert response.json()["details"]["operation_id"] == "workspaceExecPwsh"


@pytest.mark.anyio
async def test_disabled_user_is_rejected() -> None:
    settings = make_settings(gateway_users_json=json.dumps([{"name": "disabled", "token": "disabled-token", "disabled": True}]))
    app = make_app(settings)

    response = await post_authorized(app, token="disabled-token")

    assert response.status_code == 403
    assert response.json()["error_code"] == ErrorCode.AUTH_FORBIDDEN
    assert response.json()["message"] == "User is disabled."


@pytest.mark.anyio
async def test_user_specific_rate_limit_overrides_global_limit() -> None:
    _rate_windows.clear()
    settings = make_settings(
        rate_limit_per_minute=1000,
        gateway_users_json=json.dumps([{"name": "limited", "token": "limited-token", "role": "reader", "rate_limit_per_minute": 1}]),
    )
    app = make_app(settings)

    first = await post_authorized(app, token="limited-token")
    second = await post_authorized(app, token="limited-token")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error_code"] == ErrorCode.RATE_LIMITED
    assert second.json()["details"] == {"actor": "limited", "limit_per_minute": 1}
