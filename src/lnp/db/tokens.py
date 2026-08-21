"""Per-tenant LinkedIn credentials and tokens.

Tokens belong to a customer, so they live in their row, encrypted, and the
rotation logic in `lnp.tokens` writes back through this backend.

The tenant's *app* credentials live here too. One LinkedIn app per tenant is a
deliberate cost: it means an extra setup step, and it means LinkedIn's per-app
rate limits and any suspension land on one customer rather than all of them.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .. import log
from ..tokens import AppCredentials, TokenError, TokenSet
from .schema import LinkedInApp, LinkedInToken

logger = log.get(__name__)


class DbTokenBackend:
    """`lnp.tokens` backend backed by one tenant's row."""

    def __init__(self, session: Session, tenant_id: str):
        self.session = session
        self.tenant_id = tenant_id

    def load(self) -> Optional[TokenSet]:
        found = self.session.get(LinkedInToken, self.tenant_id)
        if found is None:
            return None
        return TokenSet(
            access_token=found.access_token,
            refresh_token=found.refresh_token,
            expires_at=found.expires_at,
            refresh_expires_at=found.refresh_expires_at,
            person_urn=found.person_urn,
            obtained_at=found.obtained_at,
        )

    def save(self, tokens: TokenSet) -> None:
        found = self.session.get(LinkedInToken, self.tenant_id)
        if found is None:
            found = LinkedInToken(tenant_id=self.tenant_id)
            self.session.add(found)
        found.access_token = tokens.access_token
        found.refresh_token = tokens.refresh_token
        found.expires_at = tokens.expires_at
        found.refresh_expires_at = tokens.refresh_expires_at
        found.person_urn = tokens.person_urn
        found.obtained_at = tokens.obtained_at
        self.session.commit()
        logger.info("tokens saved", extra={"backend": "db", "tenant": self.tenant_id})

    def forget(self) -> None:
        """Drop the tokens — on disconnect, and on cancellation.

        A cancelled account keeps its drafts and loses its ability to post,
        which is the right way round: the data is theirs, the permission to
        act as them is not something we should hold once they have left.
        """
        found = self.session.get(LinkedInToken, self.tenant_id)
        if found is not None:
            self.session.delete(found)
            self.session.commit()


def app_credentials(session: Session, tenant_id: str) -> AppCredentials:
    """The tenant's own LinkedIn app. Every OAuth call needs these."""
    found = session.get(LinkedInApp, tenant_id)
    if found is None or not found.client_id or not found.client_secret:
        raise TokenError(
            "this account has not connected a LinkedIn app yet. Finish the "
            "setup step that creates one at developer.linkedin.com."
        )
    return AppCredentials(client_id=found.client_id, client_secret=found.client_secret)


def save_app_credentials(
    session: Session, tenant_id: str, client_id: str, client_secret: str, redirect_uri: str
) -> None:
    found = session.get(LinkedInApp, tenant_id)
    if found is None:
        found = LinkedInApp(tenant_id=tenant_id)
        session.add(found)
    found.client_id = client_id
    found.client_secret = client_secret
    found.redirect_uri = redirect_uri
    session.commit()
    logger.info("linkedin app saved", extra={"tenant": tenant_id, "client_id": client_id})
