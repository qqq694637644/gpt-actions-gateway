from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.errors import ApiError, ErrorCode

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AuthorizationRole(StrEnum):
    READER = "reader"
    WRITER = "writer"
    CI = "ci"
    MAINTAINER = "maintainer"
    ADMIN = "admin"


READ_OPERATIONS = {
    "workspaceStatus",
    "workspaceDiff",
    "getPullRequest",
    "listPullRequests",
    "getPullRequestFiles",
    "queryCiStatus",
    "queryFailedCiLog",
    "getCiRun",
    "getCiJobs",
    "getJobLog",
    "getRunLog",
    "listArtifacts",
    "readArtifactText",
    "listCaches",
}

WORKSPACE_WRITE_OPERATIONS = {
    "prepareWorkspace",
    "prepareWorkspaceFromMirror",
    "workspaceExecPwsh",
    "workspaceApplyPatch",
    "workspaceWriteFile",
    "workspaceCommitAndPush",
    "workspaceReset",
    "createWorkBranch",
    "continueWorkBranch",
    "createPullRequest",
    "updatePullRequest",
    "commentPullRequest",
}

CI_WRITE_OPERATIONS = {
    "dispatchWorkflow",
    "rerunWorkflowRun",
    "rerunWorkflowJob",
}

MAINTAINER_OPERATIONS = {
    "prepareWorkspaceMirror",
    "mergePullRequest",
    "deleteCache",
}

ROLE_OPERATION_PATTERNS: dict[AuthorizationRole, set[str]] = {
    AuthorizationRole.READER: READ_OPERATIONS,
    AuthorizationRole.WRITER: READ_OPERATIONS | WORKSPACE_WRITE_OPERATIONS,
    AuthorizationRole.CI: READ_OPERATIONS | CI_WRITE_OPERATIONS,
    AuthorizationRole.MAINTAINER: READ_OPERATIONS | WORKSPACE_WRITE_OPERATIONS | CI_WRITE_OPERATIONS | MAINTAINER_OPERATIONS,
    AuthorizationRole.ADMIN: {"*"},
}

CLASSIFIED_OPERATION_PATTERNS = frozenset(
    pattern
    for role, patterns in ROLE_OPERATION_PATTERNS.items()
    if role != AuthorizationRole.ADMIN
    for pattern in patterns
)


class ConfiguredAuthUser(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    token: str | None = Field(default=None, min_length=8)
    token_sha256: str | None = None
    roles: list[AuthorizationRole] = Field(default_factory=lambda: [AuthorizationRole.READER])
    repos: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    denied_operations: list[str] = Field(default_factory=list)
    disabled: bool = False

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        username = value.strip()
        if not username:
            raise ValueError("username must not be empty")
        return username

    @field_validator("token_sha256")
    @classmethod
    def _validate_token_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("token_sha256 must be a 64-character SHA-256 hex digest")
        return normalized

    @field_validator("repos", "operations", "denied_operations", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("roles", mode="before")
    @classmethod
    def _coerce_roles(cls, value: Any) -> Any:
        if value is None or value == "":
            return [AuthorizationRole.READER]
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _require_one_token_source(self) -> ConfiguredAuthUser:
        if bool(self.token) == bool(self.token_sha256):
            raise ValueError("each auth user must set exactly one of token or token_sha256")
        return self

    @property
    def token_hash(self) -> str:
        if self.token_sha256:
            return self.token_sha256
        assert self.token is not None
        return token_sha256(self.token)


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    roles: tuple[AuthorizationRole, ...]
    repo_patterns: tuple[str, ...] = ()
    operation_patterns: tuple[str, ...] = ()
    denied_operation_patterns: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return AuthorizationRole.ADMIN in self.roles

    @property
    def actor(self) -> str:
        return self.username

    @property
    def rate_limit_identity(self) -> str:
        return self.username

    def can_access_repo(self, owner: str, repo: str) -> bool:
        if self.is_admin and not self.repo_patterns:
            return True
        full_name = f"{owner}/{repo}".lower()
        return any(fnmatch.fnmatchcase(full_name, pattern.lower()) for pattern in self.repo_patterns)

    def can_run_operation(self, operation_id: str | None) -> bool:
        if not operation_id:
            return False
        if _matches_any(operation_id, self.denied_operation_patterns):
            return False
        if self.is_admin:
            return True
        role_patterns = set[str]()
        for role in self.roles:
            role_patterns.update(ROLE_OPERATION_PATTERNS[role])
        return _matches_any(operation_id, tuple(role_patterns)) or _matches_any(operation_id, self.operation_patterns)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_auth_users_json(raw: str | None) -> list[ConfiguredAuthUser]:
    if not raw or not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AUTH_USERS_JSON must be valid JSON: {exc.msg}") from exc
    if isinstance(payload, dict):
        payload = payload.get("users")
    if not isinstance(payload, list):
        raise ValueError("AUTH_USERS_JSON must be a JSON array or an object with a 'users' array")

    users: list[ConfiguredAuthUser] = []
    names: set[str] = set()
    token_hashes: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"AUTH_USERS_JSON user at index {index} must be an object")
        user = ConfiguredAuthUser.model_validate(item)
        normalized_name = user.username.lower()
        if normalized_name in names:
            raise ValueError(f"AUTH_USERS_JSON contains duplicate username {user.username!r}")
        names.add(normalized_name)
        current_token_hash = user.token_hash
        if current_token_hash in token_hashes:
            raise ValueError("AUTH_USERS_JSON contains duplicate user tokens")
        token_hashes.add(current_token_hash)
        users.append(user)
    return users


def authenticate_token(token: str, *, auth_users: list[ConfiguredAuthUser]) -> AuthenticatedUser | None:
    candidate_hash = token_sha256(token)
    for user in auth_users:
        if user.disabled:
            continue
        token_matches = False
        if user.token is not None:
            token_matches = hmac.compare_digest(token, user.token)
        elif user.token_sha256 is not None:
            token_matches = hmac.compare_digest(candidate_hash, user.token_sha256)
        if token_matches:
            return AuthenticatedUser(
                username=user.username,
                roles=tuple(user.roles),
                repo_patterns=tuple(user.repos),
                operation_patterns=tuple(user.operations),
                denied_operation_patterns=tuple(user.denied_operations),
            )
    return None


def assert_user_authorized(user: AuthenticatedUser, *, owner: str | None, repo: str | None, operation_id: str | None) -> None:
    if owner and repo and not user.can_access_repo(owner, repo):
        raise ApiError(
            ErrorCode.AUTHZ_DENIED,
            "User is not authorized for this repository.",
            status_code=403,
            suggestion="Grant the user a matching repo pattern in AUTH_USERS_JSON or use a configured admin user token.",
            details={"user": user.username, "repo": f"{owner}/{repo}"},
        )
    if not user.can_run_operation(operation_id):
        suggestion = "Grant a role or explicit operation pattern in AUTH_USERS_JSON."
        if not operation_id:
            suggestion = "Protected routes must define an explicit operation_id before they can be authorized."
        raise ApiError(
            ErrorCode.AUTHZ_DENIED,
            "User is not authorized to run this operation.",
            status_code=403,
            suggestion=suggestion,
            details={"user": user.username, "operation_id": operation_id},
        )


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)
