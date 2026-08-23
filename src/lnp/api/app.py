"""The application.

Serves the API and, in production, the built front end from the same origin —
which is why the session cookie can be SameSite=Lax and there is no CORS
configuration to get wrong.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import log, publish_loop
from ..config import REPO_ROOT
from ..llm import UsageCapExceeded
from ..db.store import StoreError
from ..tokens import TokenError
from . import security
from .deps import PUBLISH_NOW_LOCK, config as get_config
from .routes_account import router as account_router
from .routes_auth import router as auth_router
from .routes_pipeline import router as pipeline_router

logger = log.get(__name__)

WEB_DIST = Path(os.environ.get("LNP_WEB_DIST") or (REPO_ROOT / "web" / "dist"))

BOOTED_AT = datetime.now(timezone.utc)

# Every variable a deployment needs, and whether the process can do its job
# without it. Presence only is ever reported - a deployment that is missing a
# key should be able to find that out without anyone pasting a secret into a
# chat window to prove it is set.
REQUIRED_ENV = (
    "DATABASE_URL",
    "LNP_SECRET_KEY",
    "LNP_ENCRYPTION_KEY",
    "LNP_AUTH_CLIENT_ID",
    "LNP_AUTH_CLIENT_SECRET",
)
OPTIONAL_ENV = ("ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL")

# Opt-in, not opt-out: create_app() runs on every test and on every import of
# this module (see `app = create_app()` below), and a background thread that
# fired off real publish attempts in that context would be a hazard, not a
# feature. Only scripts/serve.sh - the actual web server's start command -
# sets this before the process starts.
PUBLISH_CHECKER_ENV = "LNP_PUBLISH_CHECKER"


def config_report() -> dict:
    """What this process can actually see in its environment.

    The dashboard says what you typed; this says what the running container
    got, which is the only one of the two that decides whether sign-in works.
    """
    present = {name: bool(os.environ.get(name, "").strip()) for name in REQUIRED_ENV + OPTIONAL_ENV}
    return {
        "booted_at": BOOTED_AT.isoformat(),
        # Not a secret - it is the domain in the address bar - and seeing it is
        # how you catch a trailing slash or an http:// that the redirect URL
        # registered with LinkedIn does not match.
        "base_url": os.environ.get("LNP_BASE_URL", "").strip() or None,
        "env": present,
        "missing": [name for name in REQUIRED_ENV if not present[name]],
        "signin_configured": present["LNP_AUTH_CLIENT_ID"] and present["LNP_AUTH_CLIENT_SECRET"],
    }


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
        return {"ok": True, **config_report()}

    @app.post("/auth/signout", include_in_schema=False)
    def signout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(security.SESSION_COOKIE, path="/")
        return response

    if os.environ.get(PUBLISH_CHECKER_ENV, "").strip().lower() in {"1", "true", "yes"}:
        publish_loop.start(get_config(), PUBLISH_NOW_LOCK)

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

    # Printed once at boot so the deploy log answers "did this container get
    # the variables?" without anyone having to reproduce the failure first.
    report = config_report()
    logger.info(
        "config: base_url=%s signin_configured=%s missing=%s",
        report["base_url"],
        report["signin_configured"],
        ",".join(report["missing"]) or "none",
    )

    return app


app = create_app()
