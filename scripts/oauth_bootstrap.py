#!/usr/bin/env python3
"""Run the LinkedIn OAuth flow once and store the resulting tokens.

Run this on your own machine, not in CI — it opens a browser.

    python scripts/oauth_bootstrap.py

It starts a temporary HTTP server on localhost:8765, sends you to LinkedIn,
catches the redirect, validates the state parameter, exchanges the code, and
writes the tokens to the configured backend.

Before running, add this exact redirect URL to your LinkedIn app under
Auth -> Authorized redirect URLs:

    http://localhost:8765/callback
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import tokens as token_mod
from lnp.config import load_config, require_env
from lnp.tokens import AUTH_URL, SCOPES, TokenError, make_backend

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"

_result: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = (params.get("code") or [""])[0]
        _result["state"] = (params.get("state") or [""])[0]
        _result["error"] = (params.get("error_description") or params.get("error") or [""])[0]

        body = (
            b"<h2>LinkedIn authorisation received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            if _result["code"]
            else b"<h2>Authorisation failed.</h2><p>Check the terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):  # keep the console clean
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true",
                        help="print the URL instead of opening a browser")
    args = parser.parse_args()

    config = load_config()
    client_id = require_env("LINKEDIN_CLIENT_ID")
    require_env("LINKEDIN_CLIENT_SECRET")

    state = secrets.token_urlsafe(24)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
        }
    )

    print(f"Redirect URL this script expects: {REDIRECT_URI}")
    print("It must be listed in your LinkedIn app under Auth -> Authorized redirect URLs.\n")
    print(f"Open this URL to authorise:\n\n{auth_url}\n")
    if not args.no_browser:
        webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print(f"Waiting for the redirect on localhost:{PORT} ...")
    server.serve_forever()
    server.server_close()

    if _result.get("error"):
        print(f"\nLinkedIn returned an error: {_result['error']}", file=sys.stderr)
        return 1
    if not _result.get("code"):
        print("\nNo authorisation code received.", file=sys.stderr)
        return 1
    # A mismatched state means the response did not come from the request we
    # made. Nothing about it can be trusted, so nothing about it is used.
    if not secrets.compare_digest(_result.get("state", ""), state):
        print("\nState parameter mismatch. Aborting without exchanging the code.",
              file=sys.stderr)
        return 1

    try:
        tokens = token_mod.exchange_code(_result["code"], REDIRECT_URI)
    except TokenError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    backend = make_backend(config)
    backend.save(tokens)

    print("\nTokens stored.")
    print(f"  access token expires:  {tokens.expires_at}")
    print(f"  refresh token expires: {tokens.refresh_expires_at or 'unknown'}")

    if not tokens.refresh_token:
        print(
            "\n!!! LinkedIn returned NO REFRESH TOKEN.\n"
            "    Your access token will die in about 60 days and the pipeline\n"
            "    will stop with no way to recover automatically.\n"
            "    Refresh tokens are only issued to apps approved for them: check\n"
            "    that your app has both 'Sign In with LinkedIn using OpenID\n"
            "    Connect' and 'Share on LinkedIn' added under Products, then run\n"
            "    this script again.",
            file=sys.stderr,
        )
        return 2

    if isinstance(backend, token_mod.FileBackend):
        print(
            f"\nStored locally at {backend.path} (chmod 600).\n"
            "For GitHub Actions, paste that file's contents into a private Gist "
            "and set the GIST_ID and GIST_TOKEN secrets. CI cannot write back to "
            "repository secrets, which is why the Gist exists."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
