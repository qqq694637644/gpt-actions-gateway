from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.authorization import AuthenticatedUser, assert_user_authorized, authenticate_token, token_sha256
from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode

_bearer = HTTPBearer(auto_error=False)
_rate_lock = Lock()
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


async def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    owner: str | None = None,
    repo: str | None = None,
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Missing bearer token.",
            status_code=401,
            suggestion="Send Authorization: Bearer <configured user token>.",
        )
    auth_users = settings.auth_users
    if not auth_users:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Server is missing AUTH_USERS_JSON configuration.",
            status_code=500,
        )
    token = credentials.credentials.strip()
    if not token:
        raise ApiError(ErrorCode.AUTH_FAILED, "Invalid bearer token.", status_code=401)

    user = authenticate_token(token, auth_users=auth_users)
    if user is None:
        raise ApiError(ErrorCode.AUTH_FAILED, "Invalid bearer token.", status_code=401)

    await enforce_rate_limit(request, token, user, settings)
    operation_id = _operation_id(request)
    assert_user_authorized(user, owner=owner, repo=repo, operation_id=operation_id)
    request.state.auth_user = user
    request.state.actor = user.actor
    request.state.operation_id = operation_id
    return user


async def enforce_rate_limit(request: Request, token: str, user: AuthenticatedUser, settings: Settings) -> None:
    limit = max(settings.rate_limit_per_minute, 1)
    now = time.monotonic()
    window_start = now - 60.0
    client_host = request.client.host if request.client else "unknown"
    key = f"{client_host}:{user.rate_limit_identity}:{token_sha256(token)[:12]}"

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
                details={"limit_per_minute": limit},
            )
        bucket.append(now)


def _operation_id(request: Request) -> str | None:
    route = request.scope.get("route")
    operation_id = getattr(route, "operation_id", None)
    if operation_id:
        return str(operation_id)
    return None
