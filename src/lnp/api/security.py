"""Sessions and the two OAuth flows, which are not the same flow.

LinkedIn appears twice in this product and conflating them would be a security
bug, so they are named apart everywhere:

  *sign-in*   the operator's own LinkedIn app, scopes `openid profile email`.
              It establishes who is logged in. It cannot post.

  *posting*   the tenant's own LinkedIn app, scopes `openid profile
              w_member_social`. It is the permission to publish as them, and
              it is granted separately, later, once they have set their app up.

A tenant who has signed in has not thereby granted anything. That separation is
deliberate: the moment somebody creates an account should not be the moment
they hand over the ability to post.
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "lnp_session"
STATE_COOKIE = "lnp_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # two weeks

AUTH_SCOPES = "openid profile email"
POSTING_SCOPES = "openid profile w_member_social"


class AuthError(Exception):
    pass


def secret_key() -> str:
    key = os.environ.get("LNP_SECRET_KEY", "").strip()
    if not key:
        raise AuthError(
            "LNP_SECRET_KEY is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`. "
            "Without it, sessions cannot be signed."
        )
    return key


def serializer(salt: str = "session") -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt=salt)


def issue_session(tenant_id: str) -> str:
    return serializer().dumps({"t": tenant_id})


def read_session(token: str, max_age: int = SESSION_MAX_AGE) -> Optional[str]:
    """The tenant id in a session cookie, or None if it is not valid.

    Returns None rather than raising for every failure mode - expired, forged,
    truncated - because the caller's response to all of them is the same, and
    a caller that has to enumerate them is a caller that will miss one.
    """
    if not token:
        return None
    try:
        data = serializer().loads(token, max_age=max_age)
    except BadSignature:
        return None
    except Exception:  # noqa: BLE001 - a malformed cookie is just not a session
        return None
    tenant_id = (data or {}).get("t")
    return tenant_id or None


@dataclass
class OAuthState:
    """The `state` parameter, kept in a cookie so the callback can check it.

    Signed and compared with a constant-time equality: without this an attacker
    can complete the flow in somebody else's browser and attach their own
    LinkedIn account to it.
    """

    value: str
    flow: str = "signin"

    @classmethod
    def issue(cls, flow: str = "signin") -> "OAuthState":
        return cls(value=secrets.token_urlsafe(24), flow=flow)

    def cookie(self) -> str:
        return serializer("oauth-state").dumps({"v": self.value, "f": self.flow})


def check_state(cookie_value: str, returned_state: str, flow: str) -> None:
    """Raise unless the state we issued is the state that came back."""
    if not cookie_value or not returned_state:
        raise AuthError("the sign-in attempt is missing its state; start again")
    try:
        data = serializer("oauth-state").loads(cookie_value, max_age=900)
    except Exception as exc:  # noqa: BLE001
        raise AuthError("the sign-in attempt expired; start again") from exc
    if not hmac.compare_digest(str(data.get("v", "")), returned_state):
        raise AuthError("the sign-in attempt did not match; start again")
    if data.get("f") != flow:
        raise AuthError("that authorisation was for a different step")


def cookie_kwargs(secure: Optional[bool] = None) -> dict:
    """Cookie flags. Secure everywhere except a plain-HTTP local run."""
    if secure is None:
        secure = os.environ.get("LNP_INSECURE_COOKIES", "").lower() not in {"1", "true"}
    return {
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }
