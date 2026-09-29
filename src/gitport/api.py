"""gitport REST API — FastAPI wrapper around the same engine.

Endpoints
    GET  /healthz              liveness + version (unauthenticated)
    POST /v1/check             gate a diff or a git range
    POST /v1/index             (re)build the rules index
    GET  /v1/schema            the verdict JSON schema clients can rely on
    GET  /v1/checks            summaries of persisted check runs
    GET  /v1/checks/{id}       one full persisted report
    GET  /v1/stats             aggregate stats over persisted checks

Auth: if GITPORT_API_TOKEN is set, all /v1/* routes require ``Authorization:
Bearer <token>``. Without it the service is meant for localhost/CI only.

Errors: every failure returns ``{"error": {"type", "message", "request_id"}}``
— EngineError maps to 400, request validation to 422, anything else to 502.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .engine import EngineError, run_check
from .gate import VERDICT_SCHEMA
from .policy import find_policy_path, load_policy
from .reports import ReportStore
from .server import BodyLimitMiddleware, RateLimitMiddleware, RequestIDMiddleware, json_logging

log = logging.getLogger(__name__)


class CheckRequest(BaseModel):
    diff: str | None = None          # raw unified diff, or…
    repo_path: str | None = None     # …a git range inside a repo
    base_ref: str = "HEAD~1"
    head_ref: str = "HEAD"
    staged: bool = False


class IndexRequest(BaseModel):
    rules_dir: str | None = None
    index_path: str | None = None


def _error(request: Request, status: int, type_: str, message: str,
           headers: dict | None = None) -> JSONResponse:
    """The single error envelope every failure path shares."""
    return JSONResponse(
        status_code=status,
        content={"error": {
            "type": type_,
            "message": message,
            "request_id": request.scope.get("gitport.request_id", ""),
        }},
        headers=headers,
    )


def create_app(default_repo: str | Path = ".", settings=None, client=None) -> FastAPI:
    cfg = settings or get_settings()
    json_logging(cfg)
    app = FastAPI(title="gitport", version=__version__,
                  description="AI-native pre-merge gatekeeper.")

    _store: ReportStore | None = None
    _store_lock = threading.Lock()

    def report_store() -> ReportStore | None:
        """Open the report DB on first use only — deferring it keeps
        ``create_app()`` (and the module-level ``app`` import) free of
        filesystem side effects."""
        nonlocal _store
        if not cfg.store_reports:
            return None
        if _store is None:
            with _store_lock:
                if _store is None:
                    _store = ReportStore(cfg.reports_db)
        return _store

    def _auth(authorization: str = Header(default="")) -> None:
        if cfg.api_token and authorization != f"Bearer {cfg.api_token}":
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # Middleware — added inside-out; the last add_middleware call runs
    # outermost. RequestID must be outermost so every response (including
    # 413/429 rejections and CORS preflights) carries X-Request-ID.
    if cfg.cors_origins:
        origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()]
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RateLimitMiddleware, rpm=cfg.rate_limit_rpm)
    app.add_middleware(BodyLimitMiddleware, max_bytes=cfg.max_request_bytes)
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(EngineError)
    async def _engine_error(request: Request, exc: EngineError):
        return _error(request, 400, "engine_error", str(exc))

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _error(request, exc.status_code, "http_error", msg,
                      headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        msg = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
        return _error(request, 422, "validation_error", msg)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return _error(request, 502, "internal_error",
                      str(exc) or type(exc).__name__)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/v1/schema", dependencies=[Depends(_auth)])
    def schema():
        return VERDICT_SCHEMA

    @app.post("/v1/check", dependencies=[Depends(_auth)])
    async def check(req: CheckRequest) -> dict:
        repo = Path(req.repo_path or default_repo)
        if req.diff is None:
            kwargs = {"repo": repo, "base": req.base_ref,
                      "head": req.head_ref, "staged": req.staged}
        else:
            kwargs = {"diff_text": req.diff}
        # same policy enforcement as the CLI: .gitport/policy.toml under the
        # repo root applies to API checks too.
        pol = load_policy(p) if (p := find_policy_path(cfg, repo)) else None
        # run_check is synchronous and can take seconds — keep it off the
        # event loop so health checks and other requests aren't blocked.
        report = await run_in_threadpool(run_check, cfg, client=client,
                                         policy=pol, **kwargs)
        store = report_store()
        if store is None:
            return report.model_dump()
        check_id = await run_in_threadpool(store.save, report)
        return report.model_dump() | {"id": check_id}

    @app.get("/v1/checks", dependencies=[Depends(_auth)])
    def list_checks(limit: Annotated[int, Query(ge=1, le=500)] = 50,
                    offset: Annotated[int, Query(ge=0)] = 0) -> list[dict]:
        store = report_store()
        return store.list(limit=limit, offset=offset) if store else []

    @app.get("/v1/checks/{check_id}", dependencies=[Depends(_auth)])
    def get_check(check_id: str) -> dict:
        store = report_store()
        row = store.get(check_id) if store else None
        if row is None:
            raise HTTPException(status_code=404, detail="check not found")
        return row

    @app.get("/v1/stats", dependencies=[Depends(_auth)])
    def stats() -> dict:
        store = report_store()
        if store is None:
            return {"total": 0, "by_status": {}, "avg_elapsed_seconds": 0.0}
        return store.stats()

    @app.post("/v1/index", dependencies=[Depends(_auth)])
    def index(req: IndexRequest):
        from .engine import make_client
        from .rules import IndexError_, build_index

        try:
            n = build_index(
                make_client(cfg), cfg,
                req.rules_dir or cfg.rules_dir,
                req.index_path or cfg.index_path,
            )
        except (EngineError, IndexError_) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "chunks": n}

    return app


app = create_app()
