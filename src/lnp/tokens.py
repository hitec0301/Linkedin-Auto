"""LinkedIn OAuth token rotation.

Token expiry is what silently kills a product like this in month two. Access
tokens last 60 days, refresh tokens 365, and both die quietly: the jobs keep
running, every publish fails, and nobody notices until a customer asks why
there have been no posts.

So: refresh proactively (7 days before expiry, checked on every publish run),
persist the rotated refresh token, and warn 30 days before the refresh token
itself expires — because recovering from that needs the customer at a browser,
and it cannot be automated on their behalf.

Storage is per tenant and lives in `db.tokens`. Nothing here touches a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import requests

from . import log
from .config import Config
from .util import iso, parse_dt, utcnow

logger = log.get(__name__)

TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
SCOPES = "openid profile w_member_social"


class TokenError(Exception):
    pass


@dataclass
class AppCredentials:
    """The LinkedIn app a token exchange is performed against.

    Always passed in, never read from the environment. Every tenant has their
    own app, and a default that silently fell back to a process-wide one would
    be a way to post as the wrong person.
    """

    client_id: str
    client_secret: str


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: str = ""
    refresh_expires_at: str = ""
    person_urn: str = ""
    obtained_at: str = field(default_factory=iso)

    @classmethod
    def from_response(cls, payload: dict, previous: Optional["TokenSet"] = None) -> "TokenSet":
        now = utcnow()
        refresh_token = payload.get("refresh_token") or (previous.refresh_token if previous else "")
        refresh_expires = payload.get("refresh_token_expires_in")
        if refresh_expires:
            refresh_expires_at = iso(now + timedelta(seconds=int(refresh_expires)))
        else:
            refresh_expires_at = previous.refresh_expires_at if previous else ""
        return cls(
            access_token=payload.get("access_token", ""),
            refresh_token=refresh_token,
            expires_at=iso(now + timedelta(seconds=int(payload.get("expires_in", 0)))),
            refresh_expires_at=refresh_expires_at,
            person_urn=previous.person_urn if previous else "",
            obtained_at=iso(now),
        )

    def days_until_expiry(self) -> Optional[float]:
        due = parse_dt(self.expires_at)
        return None if due is None else (due - utcnow()).total_seconds() / 86400

    def days_until_refresh_expiry(self) -> Optional[float]:
        due = parse_dt(self.refresh_expires_at)
        return None if due is None else (due - utcnow()).total_seconds() / 86400

    def needs_refresh(self, within_days: int) -> bool:
        remaining = self.days_until_expiry()
        return remaining is None or remaining <= within_days


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def exchange_code(code: str, redirect_uri: str, app: AppCredentials) -> TokenSet:
    """Authorization code -> tokens."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise TokenError(f"code exchange failed ({response.status_code}): {response.text}")
    return TokenSet.from_response(response.json())


def refresh(tokens: TokenSet, app: AppCredentials) -> TokenSet:
    if not tokens.refresh_token:
        raise TokenError(
            "no refresh token stored; this account has to authorise posting again"
        )
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise TokenError(
            f"token refresh failed ({response.status_code}): {response.text}. "
            "If the refresh token has expired, this account has to authorise "
            "posting again from the Setup screen."
        )
    return TokenSet.from_response(response.json(), previous=tokens)


def load_fresh(
    config: Config, backend, app: AppCredentials, alerter=None
) -> TokenSet:
    """Return a usable access token, refreshing and persisting if needed.

    Called on every publish run. The refresh-token warning is deliberately loud
    and repeated: it is the one failure only the customer can fix, at a browser,
    and it has a month of warning before it becomes an outage.
    """
    tokens = backend.load()
    if tokens is None or not tokens.access_token:
        raise TokenError(
            "this account has not connected LinkedIn yet, so there is nothing "
            "to publish with. Finish the setup step that authorises posting."
        )

    within = int(config.get("tokens.refresh_within_days", 7))
    if tokens.needs_refresh(within):
        logger.info(
            "refreshing access token",
            extra={"days_left": tokens.days_until_expiry()},
        )
        rotated = refresh(tokens, app)
        rotated.person_urn = tokens.person_urn
        backend.save(rotated)
        tokens = rotated

    warn_days = int(config.get("tokens.refresh_token_warn_days", 30))
    remaining = tokens.days_until_refresh_expiry()
    if alerter and remaining is not None and remaining <= warn_days:
        alerter(
            f"LinkedIn access expires in {remaining:.0f} days",
            "Open Setup and press Authorise posting again. Once it expires, "
            "every publish fails until you do, and it is not something that "
            "can be renewed on your behalf.",
        )
    return tokens


def cache_person_urn(config: Config, tokens: TokenSet, urn: str, backend) -> None:
    """Persist the member URN so publishes stop calling /userinfo."""
    if not urn or tokens.person_urn == urn:
        return
    tokens.person_urn = urn
    backend.save(tokens)
