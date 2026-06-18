import asyncio
import base64

import httpx
import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.gitea.auth import GiteaAuthProvider
from app.gitea.client import GiteaClient


def test_git_auth_config_uses_basic_auth_for_gitea_token():
    settings = Settings(
        gitea_token="gitea-token",
        gitea_git_username="gitea-bot",
    )
    config = asyncio.run(_git_auth_config(settings))

    assert config == [
        "-c",
        f"http.extraHeader=Authorization: Basic {base64.b64encode(b'gitea-bot:gitea-token').decode('ascii')}",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "credential.interactive=never",
    ]


def test_gitea_git_credentials_require_gitea_username():
    settings = Settings(gitea_token="gitea-token")

    with pytest.raises(ApiError) as exc:
        asyncio.run(_require_git_credentials(settings))

    assert exc.value.error_code == ErrorCode.GITEA_AUTH_FAILED
    assert exc.value.message == "GITEA_GIT_USERNAME is required for authenticated Gitea Git remotes."


def test_gitea_auth_requires_gitea_environment_names():
    settings = Settings()

    with pytest.raises(ApiError) as exc:
        asyncio.run(_require_api_token(settings))

    assert exc.value.error_code == ErrorCode.GITEA_AUTH_FAILED
    assert exc.value.message == "GITEA_TOKEN is required."


async def _git_auth_config(settings: Settings) -> list[str]:
    client = GiteaClient(settings)
    try:
        return await client.git_auth_config()
    finally:
        await client.aclose()


async def _require_git_credentials(settings: Settings) -> None:
    provider = GiteaAuthProvider(settings)
    client = httpx.AsyncClient()
    try:
        await provider.get_git_credentials(client)
    finally:
        await client.aclose()


async def _require_api_token(settings: Settings) -> None:
    provider = GiteaAuthProvider(settings)
    client = httpx.AsyncClient()
    try:
        await provider.get_api_token(client)
    finally:
        await client.aclose()
