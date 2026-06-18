from __future__ import annotations

from dataclasses import dataclass

from app.gitea.auth import GitCredentials, GiteaAuthProvider


@dataclass
class InstallationToken:
    """Deprecated compatibility placeholder for the former GitHub App flow."""

    token: str
    expires_at_epoch: float


GitHubAuthProvider = GiteaAuthProvider

__all__ = ["GitCredentials", "GitHubAuthProvider", "InstallationToken"]
