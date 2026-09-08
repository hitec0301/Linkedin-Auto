"""LinkedIn Posts API client.

Personal profile. Text always; an image is optional, attached through the
separate multi-step upload flow the Posts API requires: register an upload,
PUT the bytes, then reference the returned URN in the post payload. This is
real extra failure surface for a post that would otherwise read the same, so
`upload_image` is called right before the post it will be attached to - never
earlier, since the upload URL LinkedIn hands back is short-lived - and every
caller treats an upload failure as "publish the text without the image"
rather than "fail the post". Documents and article shares are still out of
scope. The source URL goes in the body as plain text.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import requests

from . import log
from .config import Config
from .tokens import TokenSet, USERINFO_URL

logger = log.get(__name__)

# Headers whose values are worth keeping when LinkedIn starts throttling.
QUOTA_HEADER_PREFIXES = ("x-ratelimit", "x-li-throttle", "retry-after", "x-restli-id")


class LinkedInError(Exception):
    pass


class PostNotConfirmed(Exception):
    """Raised when we cannot tell whether a post exists.

    This is deliberately distinct from a failure: not knowing means we must not
    act. A row in this state is left alone for a human to look at.
    """


class LinkedIn:
    def __init__(self, config: Config, tokens: TokenSet, session=None):
        self.config = config
        self.tokens = tokens
        self.session = session or requests.Session()
        self.base = config.get("publish.api_base", "https://api.linkedin.com").rstrip("/")
        self.version = str(config.get("publish.api_version", "202605"))
        self.timeout = int(config.get("publish.request_timeout_seconds", 30))

    # ---- plumbing --------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.tokens.access_token}",
            "LinkedIn-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        headers.update(extra or {})
        return headers

    @staticmethod
    def _log_quota(response: requests.Response) -> None:
        quota = {
            key: value
            for key, value in response.headers.items()
            if key.lower().startswith(QUOTA_HEADER_PREFIXES)
        }
        if quota:
            logger.info("linkedin quota headers", extra={"headers": quota})

    # ---- identity --------------------------------------------------------

    def person_urn(self) -> str:
        """The author URN, from the OIDC userinfo endpoint. Cached on the token."""
        if self.tokens.person_urn:
            return self.tokens.person_urn
        response = self.session.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {self.tokens.access_token}"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise LinkedInError(
                f"userinfo failed ({response.status_code}): {response.text}. "
                "Check that the app has the 'Sign In with LinkedIn using OpenID "
                "Connect' product and the openid and profile scopes."
            )
        sub = response.json().get("sub")
        if not sub:
            raise LinkedInError("userinfo returned no 'sub' claim")
        urn = f"urn:li:person:{sub}"
        self.tokens.person_urn = urn
        logger.info("resolved author urn", extra={"urn": urn})
        return urn

    # ---- posting ---------------------------------------------------------

    def build_payload(self, author: str, commentary: str, image_urn: str = "") -> Dict:
        payload = {
            "author": author,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_urn:
            payload["content"] = {"media": {"id": image_urn}}
        return payload

    def upload_image(
        self, author: str, image_bytes: bytes, mime_type: str = "image/png"
    ) -> str:
        """Register an image upload, PUT the bytes, and return the image URN.

        Two HTTP calls where every other method here makes one: LinkedIn hands
        back a short-lived upload URL rather than accepting bytes directly, so
        this must be called right before the post that will reference the
        result, never at generation time.
        """
        init = self.session.post(
            f"{self.base}/rest/images?action=initializeUpload",
            headers=self._headers(),
            json={"initializeUploadRequest": {"owner": author}},
            timeout=self.timeout,
        )
        if init.status_code >= 400:
            raise LinkedInError(
                f"image upload init failed ({init.status_code}): {init.text[:800]}"
            )
        value = init.json().get("value", {})
        upload_url = value.get("uploadUrl")
        image_urn = value.get("image")
        if not upload_url or not image_urn:
            raise LinkedInError("image upload init returned no uploadUrl/image urn")

        put = self.session.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {self.tokens.access_token}",
                "Content-Type": mime_type,
            },
            data=image_bytes,
            timeout=self.timeout,
        )
        if put.status_code >= 400:
            raise LinkedInError(
                f"image upload failed ({put.status_code}): {put.text[:800]}"
            )
        logger.info(
            "image uploaded", extra={"urn": image_urn, "bytes": len(image_bytes)}
        )
        return image_urn

    def create_post(self, payload: Dict) -> str:
        """POST the payload and return the URN from the x-restli-id header."""
        url = f"{self.base}/rest/posts"
        response = self.session.post(
            url, headers=self._headers(), json=payload, timeout=self.timeout
        )
        self._log_quota(response)
        if response.status_code >= 400:
            raise LinkedInError(
                f"post failed ({response.status_code}): {response.text[:800]}"
            )
        urn = response.headers.get("x-restli-id") or response.headers.get("X-RestLi-Id")
        if not urn:
            # The post may well exist. Say so precisely rather than retrying.
            raise PostNotConfirmed(
                f"post returned {response.status_code} with no x-restli-id header; "
                "the post may have been created"
            )
        logger.info("post created", extra={"urn": urn, "status": response.status_code})
        return urn

    # ---- recovery --------------------------------------------------------

    def find_recent_post(self, author: str, commentary: str, count: int = 20) -> Tuple[bool, Optional[str]]:
        """Look for a post whose commentary matches, to resolve a stuck row.

        Returns (confirmed, urn). Raises PostNotConfirmed when the API cannot
        answer — which is not the same as answering "no", and must never be
        treated as one: publishing again on a maybe is how you double-post.
        """
        url = f"{self.base}/rest/posts"
        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                params={"q": "author", "author": author, "count": count, "sortBy": "LAST_MODIFIED"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PostNotConfirmed(f"posts lookup failed: {exc}") from exc

        self._log_quota(response)
        if response.status_code >= 400:
            raise PostNotConfirmed(
                f"posts lookup returned {response.status_code}: {response.text[:400]}"
            )
        try:
            elements: List[Dict] = response.json().get("elements", [])
        except ValueError as exc:
            raise PostNotConfirmed(f"posts lookup returned non-JSON: {exc}") from exc

        needle = _fingerprint(commentary)
        for element in elements:
            if _fingerprint(element.get("commentary", "")) == needle:
                urn = element.get("id") or element.get("urn")
                logger.info("stuck row confirmed as published", extra={"urn": urn})
                return True, urn
        logger.info(
            "stuck row not found in recent posts",
            extra={"checked": len(elements)},
        )
        return False, None


def _fingerprint(text: str) -> str:
    """Compare post bodies ignoring whitespace LinkedIn may have normalised."""
    return " ".join((text or "").split())[:400]
