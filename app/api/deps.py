from __future__ import annotations

from fastapi import Request

from app.gitea.client import GiteaClient
from app.policy.rules import Policy
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager


def gitea_client(request: Request) -> GiteaClient:
    return request.app.state.gitea


def github_client(request: Request) -> GiteaClient:
    """Deprecated dependency name retained for unchanged route signatures."""

    return gitea_client(request)


def policy(request: Request) -> Policy:
    return request.app.state.policy


def audit_store(request: Request) -> AuditStore:
    return request.app.state.audit


def workspace_manager(request: Request) -> WorkspaceManager:
    return request.app.state.workspace_manager

