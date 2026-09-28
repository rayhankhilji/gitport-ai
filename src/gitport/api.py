"""gitport REST API — FastAPI wrapper around the same engine.

Endpoints
    GET  /healthz          liveness + version
    POST /v1/check         gate a diff or a git range
    POST /v1/index         (re)build the rules index
    GET  /v1/schema        the verdict JSON schema clients can rely on

Auth: if GITPORT_API_TOKEN is set, all /v1/* routes require ``Authorization:
Bearer <token>``. Without it the service is meant for localhost/CI only.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .engine import EngineError, error_verdict, run_check
from .gate import VERDICT_SCHEMA
from .models import CheckReport


class CheckRequest(BaseModel):
    diff: str | None = None          # raw unified diff, or…
    repo_path: str | None = None     # …a git range inside a repo
    base_ref: str = "HEAD~1"
    head_ref: str = "HEAD"
    staged: bool = False


class IndexRequest(BaseModel):
    rules_dir: str | None = None
    index_path: str | None = None


def create_app(default_repo: str | Path = ".", settings=None, client=None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(title="gitport", version=__version__,
                  description="AI-native pre-merge gatekeeper.")

    def _auth(authorization: str = Header(default="")) -> None:
        if cfg.api_token and authorization != f"Bearer {cfg.api_token}":
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/v1/schema", dependencies=[Depends(_auth)])
    def schema():
        return VERDICT_SCHEMA

    @app.post("/v1/check", dependencies=[Depends(_auth)])
    def check(req: CheckRequest) -> CheckReport:
        if req.diff is None:
            repo = Path(req.repo_path or default_repo)
            kwargs = {"repo": repo, "base": req.base_ref,
                      "head": req.head_ref, "staged": req.staged}
        else:
            kwargs = {"diff_text": req.diff}
        try:
            return run_check(cfg, client=client, **kwargs)
        except EngineError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            report = CheckReport(verdict=error_verdict(str(e), cfg.fail_open))
            if report.verdict.status == "FAILED":
                raise HTTPException(status_code=502,
                                    detail=report.model_dump()) from e
            return report

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
