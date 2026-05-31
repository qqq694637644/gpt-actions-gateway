from __future__ import annotations

from app.config.settings import Settings
from app.github.client import GitHubClient
from app.models.search import SearchCodeRequest, SearchCodeResponse
from app.policy.rules import Policy
from app.services.repos import RepoService


class SearchService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings) -> None:
        self._repo_service = RepoService(github, policy, settings)

    async def search_code(self, owner: str, repo: str, request: SearchCodeRequest) -> SearchCodeResponse:
        return await self._repo_service.search_code(owner, repo, request)
