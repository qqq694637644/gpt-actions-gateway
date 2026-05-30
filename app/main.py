from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router as gateway_router
from app.config.settings import get_settings
from app.errors import register_exception_handlers
from app.github.client import GitHubClient
from app.policy.rules import Policy
from app.storage.audit import AuditStore


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="GPT Actions GitHub Gateway",
        version="1.0.0",
        description=(
            "Task-oriented FastAPI gateway for Custom GPT Actions. "
            "It safely reads repository files, creates gpt/* branches, commits text changes, creates pull requests, "
            "and summarizes GitHub Actions CI status/logs without exposing the raw GitHub API."
        ),
        servers=[{"url": settings.public_base_url.rstrip("/")}],
    )
    app.state.settings = settings
    app.state.github = GitHubClient(settings)
    app.state.policy = Policy(settings)
    app.state.audit = AuditStore(settings.audit_db_url)

    register_exception_handlers(app)
    app.include_router(gateway_router)

    @app.middleware("http")
    async def request_audit_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                app.state.audit.record_event(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    metadata={"duration_ms": duration_ms},
                )
            except Exception:
                # Audit failures must not break GPT Actions requests.
                pass

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "env": settings.app_env})

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await app.state.github.aclose()
        app.state.audit.close()

    return app


app = create_app()
