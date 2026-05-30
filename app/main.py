from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import debug_router, router as gateway_router
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
            "面向 Custom GPT Actions 的任务型 FastAPI 网关。"
            "它会以受限方式读取仓库文件、创建 gpt/* 工作分支、提交文本修改、创建拉取请求、按需合并已审核的 GPT PR，"
            "并汇总 GitHub Actions CI 状态与日志，而不会把原始 GitHub API 直接暴露给 GPT。"
        ),
        servers=[{"url": settings.public_base_url.rstrip("/")}],
    )
    app.state.settings = settings
    app.state.github = GitHubClient(settings)
    app.state.policy = Policy(settings)
    app.state.audit = AuditStore(settings.audit_db_url)

    register_exception_handlers(app)
    app.include_router(debug_router)
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
