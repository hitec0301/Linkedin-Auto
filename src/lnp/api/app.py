"""The application.

Serves the API and, in production, the built front end from the same origin —
which is why the session cookie can be SameSite=Lax and there is no CORS
configuration to get wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import log
from ..config import REPO_ROOT
from ..llm import UsageCapExceeded
from ..db.store import StoreError
from ..tokens import TokenError
from . import security
from .routes_account import router as account_router
from .routes_auth import router as auth_router
from .routes_pipeline import router as pipeline_router

logger = log.get(__name__)

WEB_DIST = Path(os.environ.get("LNP_WEB_DIST") or (REPO_ROOT / "web" / "dist"))


def create_app() -> FastAPI:
    app = FastAPI(
        title="LinkedIn publishing pipeline",
        description="The human decides, the model drafts.",
        version="1.0.0",
    )

    app.include_router(auth_router)
    app.include_router(pipeline_router)
    app.include_router(account_router)

    @app.exception_handler(UsageCapExceeded)
    def _cap(request: Request, exc: UsageCapExceeded) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=402)

    @app.exception_handler(TokenError)
    def _token(request: Request, exc: TokenError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(StoreError)
    def _store(request: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(security.AuthError)
    def _auth(request: Request, exc: security.AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/auth/signout", include_in_schema=False)
    def signout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(security.SESSION_COOKIE, path="/")
        return response

    if WEB_DIST.is_dir():
        app.mount(
            "/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets"
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            """Hand any unmatched path to the front end.

            Routing lives in one place — the browser — so a deep link like
            /setup works on a fresh load instead of 404ing.
            """
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()
