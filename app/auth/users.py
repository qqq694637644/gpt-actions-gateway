from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode

ROLE_OPERATION_PATTERNS: dict[str, tuple[str, ...]] = {
    "admin": ("*",),
    "maintainer": (
        "prepareWorkspace",
        "prepareWorkspaceFromMirror",
        "workspaceExecPwsh",
        "workspaceStatus",
        "workspaceDiff",
        "workspaceApplyPatch",
        "workspaceWriteFile",
        "workspaceCommitAndPush",
        "workspaceReset",
        "createWorkBranch",
        "continueWorkBranch",
        "createPullRequest",
        "getPullRequest",
        "listPullRequests",
        "getPullRequestFiles",
        "updatePullRequest",
        "commentPullRequest",
        "queryCiStatus",
        "queryFailedCiLog",
        "getCiRun",
        "getCiJobs",
        "getJobLog",
        "getRunLog",
        "listArtifacts",
        "readArtifactText",
        "listCaches",
        "rerunWorkflowRun",
        "rerunWorkflowJob",
    ),
    "reader": (
        "prepareWorkspace",
        "prepareWorkspaceFromMirror",
        "workspaceExecPwsh",
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
    ),
    "ci": (
        "queryCiStatus",
        "queryFailedCiLog",
        "getCiRun",
        "getCiJobs",
        "getJobLog",
        "getRunLog",
        "listArtifacts",
        "readArtifactText",
        "listCaches",
        "dispatchWorkflow",
        "rerunWorkflowRun",
        "rerunWorkflowJob",
    ),
}

DEFAULT_ROLE = "maintainer"
_current_user: ContextVar[AuthUser | None] = ContextVar("current_auth_user", default=None)


@dataclass(frozen=True, slots=True)
class AuthUser:
    name: str
    token: str = field(repr=False)
    role: str = DEFAULT_ROLE
    allowed_repos: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    denied_operations: tuple[str, ...] = ()
    rate_limit_per_minute: int | None = None
    disabled: bool = False
    legacy: bool = False

    def public_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "allowed_repos": list(self.allowed_repos),
            "allowed_operations": list(self.allowed_operations),
            "denied_operations": list(self.denied_operations),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "legacy": self.legacy,
        }


def set_current_user(user: AuthUser) -> None:
    _current_user.set(user)


def get_current_user() -> AuthUser | None:
    return _current_user.get()


def get_current_actor() -> str | None:
    user = get_current_user()
    return user.name if user else None


def load_auth_users(settings: Settings) -> list[AuthUser]:
    users: list[AuthUser] = []
    if settings.gateway_users_json.strip():
        users.extend(_parse_users_json(settings.gateway_users_json))
    for index, secret in enumerate(settings.secrets, start=1):
        users.append(AuthUser(name=f"legacy-gpt-action-secret-{index}", token=secret, role="admin", legacy=True))
    _validate_unique_users(users)
    return users


def authenticate_token(token: str, users: list[AuthUser]) -> AuthUser | None:
    for user in users:
        if hmac.compare_digest(token, user.token):
            return user
    return None


def assert_user_authorized(user: AuthUser, *, owner: str | None, repo: str | None, operation_id: str | None) -> None:
    if user.disabled:
        raise ApiError(
            ErrorCode.AUTH_FORBIDDEN,
            "User is disabled.",
            status_code=403,
            details={"actor": user.name, "role": user.role},
        )
    if owner and repo and not is_repo_allowed(user, owner=owner, repo=repo):
        full_name = f"{owner}/{repo}".lower()
        raise ApiError(
            ErrorCode.AUTH_FORBIDDEN,
            "User is not allowed to access this repository.",
            status_code=403,
            suggestion="Add the repository to the user's allowed_repos or use a token for an authorized user.",
            details={"actor": user.name, "role": user.role, "repo": full_name},
        )
    if operation_id and not is_operation_allowed(user, operation_id):
        raise ApiError(
            ErrorCode.AUTH_FORBIDDEN,
            "User is not allowed to call this operation.",
            status_code=403,
            suggestion="Grant this operation through role, allowed_operations, or use a token for an authorized user.",
            details={"actor": user.name, "role": user.role, "operation_id": operation_id},
        )


def is_repo_allowed(user: AuthUser, *, owner: str, repo: str) -> bool:
    if not user.allowed_repos:
        return True
    full_name = f"{owner}/{repo}".lower()
    return _matches_any(full_name, user.allowed_repos)


def is_operation_allowed(user: AuthUser, operation_id: str) -> bool:
    if _matches_any(operation_id, user.denied_operations):
        return False
    return _matches_any(operation_id, (*ROLE_OPERATION_PATTERNS[user.role], *user.allowed_operations))


def token_cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _parse_users_json(raw: str) -> list[AuthUser]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(
            ErrorCode.AUTH_CONFIG_INVALID,
            "GATEWAY_USERS_JSON is not valid JSON.",
            status_code=500,
            details={"error": str(exc)},
        ) from exc

    if isinstance(payload, dict):
        items = []
        for name, value in payload.items():
            if not isinstance(value, dict):
                raise _invalid_user_config("Each GATEWAY_USERS_JSON object value must be a user object.")
            items.append({"name": name, **value})
    elif isinstance(payload, list):
        items = payload
    else:
        raise _invalid_user_config("GATEWAY_USERS_JSON must be a list of users or an object keyed by user name.")

    users = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise _invalid_user_config(f"User entry at index {index} must be an object.")
        users.append(_parse_user(item, index=index))
    return users


def _parse_user(item: dict[str, Any], *, index: int) -> AuthUser:
    name = str(item.get("name") or item.get("username") or "").strip()
    token = str(item.get("token") or item.get("secret") or "").strip()
    if not name:
        raise _invalid_user_config(f"User entry at index {index} is missing name.")
    if not token:
        raise _invalid_user_config(f"User {name!r} is missing token.")

    role = str(item.get("role") or DEFAULT_ROLE).strip().lower()
    if role not in ROLE_OPERATION_PATTERNS:
        raise _invalid_user_config(f"User {name!r} has unsupported role {role!r}.")

    rate_limit = _parse_optional_positive_int(item.get("rate_limit_per_minute"), field_name=f"users[{index}].rate_limit_per_minute")
    return AuthUser(
        name=name,
        token=token,
        role=role,
        allowed_repos=_parse_string_list(item.get("allowed_repos"), field_name=f"users[{index}].allowed_repos", lowercase=True),
        allowed_operations=_parse_string_list(item.get("allowed_operations") or item.get("operations"), field_name=f"users[{index}].allowed_operations"),
        denied_operations=_parse_string_list(item.get("denied_operations"), field_name=f"users[{index}].denied_operations"),
        rate_limit_per_minute=rate_limit,
        disabled=_parse_bool(item.get("disabled", False), field_name=f"users[{index}].disabled"),
    )


def _validate_unique_users(users: list[AuthUser]) -> None:
    names: set[str] = set()
    token_hashes: set[str] = set()
    for user in users:
        normalized_name = user.name.casefold()
        if normalized_name in names:
            raise _invalid_user_config(f"Duplicate auth user name {user.name!r}.")
        names.add(normalized_name)
        digest = hashlib.sha256(user.token.encode("utf-8")).hexdigest()
        if digest in token_hashes:
            raise _invalid_user_config("Duplicate auth user token configured.")
        token_hashes.add(digest)


def _parse_string_list(value: Any, *, field_name: str, lowercase: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list | tuple | set):
        parts = value
    else:
        raise _invalid_user_config(f"{field_name} must be a string list or comma-separated string.")
    normalized = []
    for part in parts:
        item = str(part).strip()
        if item:
            normalized.append(item.lower() if lowercase else item)
    return tuple(dict.fromkeys(normalized))


def _parse_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_user_config(f"{field_name} must be a positive integer.") from exc
    if parsed < 1:
        raise _invalid_user_config(f"{field_name} must be a positive integer.")
    return parsed


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise _invalid_user_config(f"{field_name} must be a boolean.")


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    return any(pattern == "*" or fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _invalid_user_config(message: str) -> ApiError:
    return ApiError(
        ErrorCode.AUTH_CONFIG_INVALID,
        message,
        status_code=500,
        suggestion="Fix GATEWAY_USERS_JSON and restart the gateway.",
    )
