from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode


@dataclass
class GitCredentials:
    username: str
    password: str


class GiteaAuthProvider:
    """Token-based Gitea API and Git authentication.

    Gitea personal access tokens are sent to the REST API with the `token`
    authorization scheme and to Git over HTTPS as Basic auth credentials.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_api_token(self, client: httpx.AsyncClient) -> str:
        del client
        token = self.settings.effective_gitea_token
        if not token:
            raise ApiError(ErrorCode.GITEA_AUTH_FAILED, "GITEA_TOKEN is required.", status_code=500)
        return token

    async def get_git_credentials(self, client: httpx.AsyncClient) -> GitCredentials:
        del client
        token = self.settings.effective_gitea_token
        if not token:
            raise ApiError(ErrorCode.GITEA_AUTH_FAILED, "GITEA_TOKEN is required.", status_code=500)
        username = self.settings.effective_gitea_git_username
        if not username:
            raise ApiError(
                ErrorCode.GITEA_AUTH_FAILED,
                "GITEA_GIT_USERNAME is required for authenticated Gitea Git remotes.",
                status_code=500,
            )
        return GitCredentials(username=username, password=token)
