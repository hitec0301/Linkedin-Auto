"""Signing in, and connecting the ability to post. Two different things.

Sign-in identifies the person. Connecting grants this product permission to
publish as them. They are separate endpoints against separate LinkedIn apps,
and a customer can be signed in for days before they connect - which is the
intended shape, because deciding to try something and handing over your
professional identity should not be the same click.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode

import requests
import ulid
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import log
from ..db.schema import Tenant
from ..db.tokens import DbTokenBackend, app_credentials, save_app_credentials
from ..tokens import AUTH_URL, USERINFO_URL, AppCredentials, TokenError, exchange_code
from . import security
from .deps import current_tenant, db
from .schemas import LinkedInAppIn

logger = log.get(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def app_base_url() -> str:
    return os.environ.get("LNP_BASE_URL", "http://localhost:8000").rstrip("/")


def signin_app() -> AppCredentials:
    """The operator's own LinkedIn app, used only to establish identity."""
    client_id = os.environ.get("LNP_AUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LNP_AUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "sign-in is not configured on this deployment",
        )
    return AppCredentials(client_id=client_id, client_secret=client_secret)


def userinfo(access_token: str) -> dict:
    response = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"LinkedIn did not return a profile ({response.status_code})",
        )
    return response.json()


def _authorize(flow: str, client_id: str, scopes: str, redirect_path: str, response: Response) -> str:
    state = security.OAuthState.issue(flow)
    response.set_cookie(
        security.STATE_COOKIE, state.cookie(), max_age=900, **security.cookie_kwargs()
    )
    # quote_via=quote so spaces in `scope` become %20 rather than +, which is
    # the form LinkedIn's documentation uses.
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": f"{app_base_url()}{redirect_path}",
            "state": state.value,
            "scope": scopes,
        },
        quote_via=quote,
    )
    return f"{AUTH_URL}?{query}"


# --------------------------------------------------------------------------
# Sign in
# --------------------------------------------------------------------------


@router.get("/linkedin/start")
def start_signin() -> RedirectResponse:
    response = RedirectResponse("about:blank", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    url = _authorize(
        "signin", signin_app().client_id, security.AUTH_SCOPES,
        "/auth/linkedin/callback", response,
    )
    response.headers["location"] = url
    return response


@router.get("/linkedin/callback")
def finish_signin(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    session: Session = Depends(db),
) -> RedirectResponse:
    if error:
        # The person pressed Cancel. That is an answer, not a failure.
        return RedirectResponse(f"/?signin_error={error}", status_code=303)
    try:
        security.check_state(
            request.cookies.get(security.STATE_COOKIE, ""), state, "signin"
        )
    except security.AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    tokens = exchange_code(
        code, f"{app_base_url()}/auth/linkedin/callback", signin_app()
    )
    profile = userinfo(tokens.access_token)
    sub = profile.get("sub", "")
    if not sub:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "LinkedIn returned no identity")

    tenant = session.scalars(
        select(Tenant).where(Tenant.linkedin_sub == sub)
    ).one_or_none()
    if tenant is None:
        tenant = Tenant(
            id=ulid.new().str,
            linkedin_sub=sub,
            email=profile.get("email", "") or "",
            name=profile.get("name", "") or "",
            picture_url=profile.get("picture", "") or "",
        )
        session.add(tenant)
        logger.info("tenant created", extra={"tenant": tenant.id})
    else:
        # Refresh the display fields; people change their name and their email.
        tenant.email = profile.get("email", "") or tenant.email
        tenant.name = profile.get("name", "") or tenant.name
        tenant.picture_url = profile.get("picture", "") or tenant.picture_url
    session.commit()

    # The sign-in token is discarded here on purpose. It cannot post, and
    # keeping a credential we have no use for is how credentials leak.
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        security.issue_session(tenant.id),
        max_age=security.SESSION_MAX_AGE,
        **security.cookie_kwargs(),
    )
    response.delete_cookie(security.STATE_COOKIE, path="/")
    return response


@router.post("/signout")
def sign_out() -> dict:
    response = {"ok": True}
    return response


# --------------------------------------------------------------------------
# Connect posting: the tenant's own app
# --------------------------------------------------------------------------


@router.put("/linkedin/app")
def set_linkedin_app(
    body: LinkedInAppIn,
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> dict:
    """Store the tenant's own LinkedIn app credentials.

    Their app, not ours: LinkedIn's rate limits and any suspension are per
    app, and one shared app would put every customer behind one ceiling.
    """
    redirect_uri = f"{app_base_url()}/auth/linkedin/connect/callback"
    save_app_credentials(
        session, tenant.id, body.client_id.strip(), body.client_secret.strip(), redirect_uri
    )
    return {"ok": True, "redirect_uri": redirect_uri}


@router.get("/linkedin/connect")
def start_connect(
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> RedirectResponse:
    try:
        app = app_credentials(session, tenant.id)
    except TokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    response = RedirectResponse("about:blank", status_code=307)
    url = _authorize(
        "connect", app.client_id, security.POSTING_SCOPES,
        "/auth/linkedin/connect/callback", response,
    )
    response.headers["location"] = url
    return response


@router.get("/linkedin/connect/callback")
def finish_connect(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/setup?connect_error={error}", status_code=303)
    try:
        security.check_state(
            request.cookies.get(security.STATE_COOKIE, ""), state, "connect"
        )
    except security.AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    app = app_credentials(session, tenant.id)
    tokens = exchange_code(
        code, f"{app_base_url()}/auth/linkedin/connect/callback", app
    )
    profile = userinfo(tokens.access_token)
    tokens.person_urn = f"urn:li:person:{profile.get('sub', '')}"
    DbTokenBackend(session, tenant.id).save(tokens)
    logger.info("linkedin connected", extra={"tenant": tenant.id})

    response = RedirectResponse("/setup?connected=1", status_code=303)
    response.delete_cookie(security.STATE_COOKIE, path="/")
    return response


@router.delete("/linkedin/connect")
def disconnect(
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> dict:
    """Revoke this product's ability to post. Drafts are untouched."""
    DbTokenBackend(session, tenant.id).forget()
    return {"ok": True}
