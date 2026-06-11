from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.users import AuthUser, assert_user_authorized, authenticate_token, load_auth_users, set_current_user, token_cache_key
from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode

_bearer = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(_bearer)
_settings_dependency = Depends(get_settings)
_rate_lock = Lock()
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
    settings: Settings = _settings_dependency,
) -> AuthUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Missing bearer token.",
            status_code=401,
            suggestion="Send Authorization: Bearer <GPT_ACTION_SECRET>.",
        )
    users = load_auth_users(settings)
    if not users:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Server is missing GPT_ACTION_SECRET or GATEWAY_USERS_JSON configuration.",
            status_code=500,
        )
    token = credentials.credentials.strip()
    user = authenticate_token(token, users)
    if user is None:
        raise ApiError(ErrorCode.AUTH_FAILED, "Invalid bearer token.", status_code=401)

    operation_id = _operation_id(request)
    owner = request.path_params.get("owner")
    repo = request.path_params.get("repo")
    assert_user_authorized(user, owner=owner, repo=repo, operation_id=operation_id)

    set_current_user(user)
    request.state.auth_actor = user.name
    request.state.auth_user = user.public_metadata()

    await enforce_rate_limit(request, token, user, settings)
    return user


async def enforce_rate_limit(request: Request, token: str, user: AuthUser, settings: Settings) -> None:
    limit = max(user.rate_limit_per_minute or settings.rate_limit_per_minute, 1)
    now = time.monotonic()
    window_start = now - 60.0
    client_host = request.client.host if request.client else "unknown"
    key = f"{client_host}:{user.name}:{token_cache_key(token)}"

    with _rate_lock:
        bucket = _rate_windows[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit:
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                "Rate limit exceeded.",
                status_code=429,
                suggestion="Retry after the current one-minute window or increase RATE_LIMIT_PER_MINUTE.",
                details={"actor": user.name, "limit_per_minute": limit},
            )
        bucket.append(now)


def _operation_id(request: Request) -> str | None:
    route = request.scope.get("route")
    operation_id = getattr(route, "operation_id", None)
    return str(operation_id) if operation_id else None
