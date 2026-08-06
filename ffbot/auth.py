"""Yahoo OAuth 2.0, shaped for CI rather than a developer's laptop.

`yahoo_oauth` is deliberately not used here. It is built around a token file on
disk, which is the wrong shape for GitHub Actions where the refresh token
arrives as an environment variable and any rotation has to be written back to a
repository secret. It also pulls in `rauth` and `myql`, both unmaintained.

What we need instead is small: Yahoo's token endpoint, plus a session object
satisfying the four members `yahoo_fantasy_api.yhandler.YHandler` touches on its
session context (`.session`, `.access_token`, `.refresh_access_token()`, and
`.oauth.get_session(token=...)`).

The one genuinely dangerous failure mode is losing a rotated refresh token.
Yahoo's docs state a refresh response *may* carry a new refresh token that the
client must store, discarding the old one. Drop it and the agent is locked out
mid-season with no way back in but a manual re-authorization. So persistence
runs the moment a new token is seen, before any other work.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import requests

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

# Must match a Redirect URI registered on the Yahoo app. Nothing is ever served
# here — the browser fails to load it and we read the code out of the address
# bar — so no local server or TLS certificate is involved.
DEFAULT_REDIRECT_URI = "https://localhost:8080"

# Refresh this long before nominal expiry, so a slow run cannot straddle it.
EXPIRY_SKEW_SECONDS = 120


class AuthError(RuntimeError):
    pass


@dataclass
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float

    def is_expired(self, skew: int = EXPIRY_SKEW_SECONDS) -> bool:
        return time.time() >= (self.expires_at - skew)

    def __str__(self) -> str:  # never let a token reach a log
        return f"TokenSet(expires_in={int(self.expires_at - time.time())}s)"


def authorization_url(client_id: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    """The URL the user visits once to grant access."""
    return f"{AUTH_URL}?" + urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code"}
    )


def _token_request(client_id: str, client_secret: str, payload: dict) -> TokenSet:
    """POST to Yahoo's token endpoint.

    Yahoo documents the client credentials as body parameters but also accepts
    HTTP Basic. Which one a given app requires is not something we can verify
    without live credentials, so try the documented form and fall back on an
    auth failure rather than guessing.
    """
    body = dict(payload, client_id=client_id, client_secret=client_secret)
    resp = requests.post(TOKEN_URL, data=body, timeout=30)

    if resp.status_code in (400, 401):
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            data=payload,
            headers={"Authorization": f"Basic {basic}"},
            timeout=30,
        )

    if resp.status_code != 200:
        raise AuthError(f"Yahoo token request failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    if "access_token" not in data:
        raise AuthError(f"No access_token in Yahoo response: {data}")

    return TokenSet(
        access_token=data["access_token"],
        # Yahoo usually echoes the existing refresh token; keep whatever it sends.
        refresh_token=data.get("refresh_token", payload.get("refresh_token", "")),
        expires_at=time.time() + float(data.get("expires_in", 3600)),
    )


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> TokenSet:
    """Trade the one-time authorization code for a token pair."""
    return _token_request(
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )


def refresh_tokens(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> TokenSet:
    return _token_request(
        client_id,
        client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        },
    )


# --- Persistence of a rotated refresh token ---------------------------------


def github_secret_persister(
    secret_name: str = "YAHOO_REFRESH_TOKEN", repo: str | None = None
) -> Callable[[str], None]:
    """Store a rotated refresh token as a repository secret.

    Needs a PAT with Secrets: read and write — the built-in Actions token
    cannot write secrets.
    """

    def persist(new_refresh_token: str) -> None:
        cmd = ["gh", "secret", "set", secret_name]
        if repo:
            cmd += ["--repo", repo]
        proc = subprocess.run(
            cmd, input=new_refresh_token, text=True, capture_output=True
        )
        if proc.returncode != 0:
            raise AuthError(
                "Failed to persist rotated refresh token — the next run will "
                f"fail to authenticate: {proc.stderr.strip()}"
            )

    return persist


def env_file_persister(path: str | Path = ".env") -> Callable[[str], None]:
    """Update YAHOO_REFRESH_TOKEN in a local .env, for development."""

    def persist(new_refresh_token: str) -> None:
        p = Path(path)
        lines = p.read_text().splitlines() if p.exists() else []
        out, replaced = [], False
        for line in lines:
            if line.startswith("YAHOO_REFRESH_TOKEN="):
                out.append(f"YAHOO_REFRESH_TOKEN={new_refresh_token}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"YAHOO_REFRESH_TOKEN={new_refresh_token}")
        p.write_text("\n".join(out) + "\n")

    return persist


def load_env_file(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs into os.environ without overwriting what is set.

    Avoids a python-dotenv dependency for something this small. Existing
    environment variables win, so GitHub Actions secrets are never shadowed.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# --- Session context for yahoo_fantasy_api ----------------------------------


def _bearer_session(access_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {access_token}"})
    return s


class _OAuthShim:
    """Satisfies `sc.oauth.get_session(token=...)`, the one indirect call
    YHandler makes when it refreshes after a 401."""

    def get_session(self, token: str) -> requests.Session:
        return _bearer_session(token)


class YahooSession:
    """The `sc` object `yahoo_fantasy_api` expects.

    Constructed from a refresh token alone — we never persist an access token,
    since it lives an hour and CI runs are far apart.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        on_refresh: Callable[[str], None] | None = None,
    ):
        if not (client_id and client_secret and refresh_token):
            raise AuthError(
                "Missing credentials — set YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET "
                "and YAHOO_REFRESH_TOKEN."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._refresh_token = refresh_token
        self._on_refresh = on_refresh

        self.oauth = _OAuthShim()
        self.access_token: str = ""
        self.session: requests.Session = requests.Session()
        self.refresh_access_token()

    @classmethod
    def from_env(cls, on_refresh: Callable[[str], None] | None = None) -> "YahooSession":
        load_env_file()
        return cls(
            client_id=os.environ.get("YAHOO_CLIENT_ID", ""),
            client_secret=os.environ.get("YAHOO_CLIENT_SECRET", ""),
            refresh_token=os.environ.get("YAHOO_REFRESH_TOKEN", ""),
            redirect_uri=os.environ.get("YAHOO_REDIRECT_URI", DEFAULT_REDIRECT_URI),
            on_refresh=on_refresh,
        )

    def refresh_access_token(self) -> dict:
        """Mint a fresh access token, persisting the refresh token if it rotated.

        Returns the dict shape YHandler expects. Persistence happens before this
        returns, so a crash later in the run cannot strand a rotated token.
        """
        tokens = refresh_tokens(
            self._client_id, self._client_secret, self._refresh_token, self._redirect_uri
        )

        rotated = tokens.refresh_token and tokens.refresh_token != self._refresh_token
        if rotated:
            self._refresh_token = tokens.refresh_token
            if self._on_refresh is not None:
                self._on_refresh(tokens.refresh_token)

        self.access_token = tokens.access_token
        self.session = _bearer_session(tokens.access_token)
        self._tokens = tokens
        return {"access_token": tokens.access_token}

    def token_is_valid(self) -> bool:
        return not self._tokens.is_expired()
