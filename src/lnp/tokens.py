"""LinkedIn OAuth token storage and rotation.

Token expiry is what silently kills a project like this in month two. Access
tokens last 60 days, refresh tokens 365, and both die quietly: the pipeline
keeps running, every publish fails, and nobody notices until someone asks why
there have been no posts.

So: refresh proactively (7 days before expiry, checked on every publish run),
persist the rotated refresh token, and alert 30 days before the refresh token
itself expires — because recovering from that needs a human at a browser.

Two backends. A chmod-600 file locally, and a private Gist in CI, because a
GitHub Actions run cannot write back to its own repository secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

import requests

from . import log
from .config import REPO_ROOT, Config, env, require_env
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

    Passed in rather than read from the environment, because in the hosted
    product every tenant has their own app. When it is omitted the environment
    supplies it, which is the single-tenant install.
    """

    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> "AppCredentials":
        return cls(
            client_id=require_env("LINKEDIN_CLIENT_ID"),
            client_secret=require_env("LINKEDIN_CLIENT_SECRET"),
        )


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
# Backends
# --------------------------------------------------------------------------


class FileBackend:
    """Local storage. The file holds a live credential, so it is chmod 600."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[TokenSet]:
        if not self.path.exists():
            return None
        return TokenSet(**json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
        os.chmod(self.path, 0o600)
        logger.info("tokens saved", extra={"backend": "file", "path": str(self.path)})


class GistBackend:
    """Private Gist storage for CI.

    A workflow run cannot write a rotated refresh token back into repository
    secrets, so the rotation has to live somewhere the run can PATCH. A private
    Gist is the smallest thing that works.
    """

    def __init__(self, gist_id: str, token: str, filename: str = "linkedin_tokens.json"):
        self.gist_id = gist_id
        self.token = token
        self.filename = filename
        self.api = f"https://api.github.com/gists/{gist_id}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def load(self) -> Optional[TokenSet]:
        response = requests.get(self.api, headers=self._headers(), timeout=20)
        response.raise_for_status()
        files = response.json().get("files", {})
        entry = files.get(self.filename)
        if not entry:
            return None
        content = entry.get("content")
        if entry.get("truncated") and entry.get("raw_url"):
            content = requests.get(entry["raw_url"], timeout=20).text
        return TokenSet(**json.loads(content)) if content else None

    def save(self, tokens: TokenSet) -> None:
        response = requests.patch(
            self.api,
            headers=self._headers(),
            json={"files": {self.filename: {"content": json.dumps(asdict(tokens), indent=2)}}},
            timeout=20,
        )
        response.raise_for_status()
        logger.info("tokens saved", extra={"backend": "gist", "gist_id": self.gist_id})


class SeededFileBackend(FileBackend):
    """A file that starts life from an environment variable.

    Tokens rotate: the access token is refreshed every sixty days and the store
    has to keep what comes back. A process cannot write to its own environment,
    so a variable alone loses every rotation. A file alone needs someone to put
    the first tokens there, which on a container means a deploy step.

    So: read the variable when the file is missing or empty, and write every
    rotation to the file. One paste on first deploy, rotations persist across
    restarts, and if the disk is ever wiped it re-seeds from the variable and
    carries on - at worst costing one extra refresh.
    """

    def __init__(self, path: Path, seed_var: str = "LINKEDIN_TOKENS_JSON"):
        super().__init__(path)
        self.seed_var = seed_var

    def load(self) -> Optional[TokenSet]:
        stored = super().load()
        if stored and stored.access_token:
            return stored
        raw = env(self.seed_var)
        if not raw:
            return stored
        try:
            seeded = TokenSet(**json.loads(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            raise TokenError(
                f"{self.seed_var} is set but is not the JSON that "
                f"scripts/oauth_bootstrap.py produced: {exc}"
            ) from exc
        logger.info("seeded tokens from environment", extra={"var": self.seed_var})
        try:
            self.save(seeded)  # so the next rotation has somewhere to land
        except OSError as exc:
            logger.warning(
                "token store is not writable, so refreshes will not persist",
                extra={"path": str(self.path), "error": str(exc)},
            )
        return seeded


def make_backend(config: Config):
    """Pick the backend from config, or from the environment it is running in.

    Detection beats configuration here: the same image runs on a laptop and on a
    host, and a container that has to be told where it is is a container that
    will one day be told wrong.
    """
    backend = (config.get("tokens.backend", "file") or "file").lower()
    if os.environ.get("GITHUB_ACTIONS") == "true" and env("GIST_ID"):
        backend = "gist"
    elif env("LINKEDIN_TOKENS_JSON") or env("RAILWAY_ENVIRONMENT"):
        backend = "seeded"

    if backend == "gist":
        return GistBackend(
            require_env("GIST_ID"),
            require_env("GIST_TOKEN"),
            config.get("tokens.gist_filename", "linkedin_tokens.json"),
        )

    configured = config.get("tokens.file_path", ".secrets/linkedin_tokens.json")
    if backend == "seeded":
        # The mounted volume, so a refresh outlives the container that made it.
        data_dir = env("LNP_DATA_DIR") or "/data"
        path = Path(data_dir) / "linkedin_tokens.json"
        return SeededFileBackend(path)

    path = Path(configured)
    return FileBackend(path if path.is_absolute() else REPO_ROOT / path)


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def exchange_code(
    code: str, redirect_uri: str, app: Optional[AppCredentials] = None
) -> TokenSet:
    """Authorization code -> tokens."""
    app = app or AppCredentials.from_env()
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


def refresh(tokens: TokenSet, app: Optional[AppCredentials] = None) -> TokenSet:
    if not tokens.refresh_token:
        raise TokenError(
            "no refresh token stored; re-run `python scripts/oauth_bootstrap.py`"
        )
    app = app or AppCredentials.from_env()
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
            "If the refresh token has expired, re-run scripts/oauth_bootstrap.py."
        )
    return TokenSet.from_response(response.json(), previous=tokens)


def load_fresh(
    config: Config, backend=None, alerter=None, app: Optional[AppCredentials] = None
) -> TokenSet:
    """Return a usable access token, refreshing and persisting if needed.

    Called on every publish run. The refresh-token warning is deliberately loud
    and repeated: it is the one failure a human must fix at a browser, and it
    has a month of warning before it becomes an outage.
    """
    backend = backend or make_backend(config)
    tokens = backend.load()
    if tokens is None or not tokens.access_token:
        raise TokenError(
            "no LinkedIn tokens stored; run `python scripts/oauth_bootstrap.py` "
            "locally and copy the result to the configured backend"
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
            f"LinkedIn refresh token expires in {remaining:.0f} days",
            "Run `python scripts/oauth_bootstrap.py` on your machine and update "
            "the token store. Once the refresh token expires, every publish "
            "fails until you do this, and it cannot be automated.",
        )
    return tokens


def cache_person_urn(config: Config, tokens: TokenSet, urn: str, backend=None) -> None:
    """Persist the member URN so publishes stop calling /userinfo."""
    if not urn or tokens.person_urn == urn:
        return
    tokens.person_urn = urn
    (backend or make_backend(config)).save(tokens)
