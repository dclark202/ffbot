#!/usr/bin/env python3
"""One-time Yahoo authorization. Run locally, never in CI.

Produces the long-lived refresh token that every later run uses. The token is
written straight to .env and never printed, so it does not end up in terminal
scrollback or a screen share.

    python scripts/authorize.py

Requires YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in .env first.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.auth import (  # noqa: E402
    DEFAULT_REDIRECT_URI,
    AuthError,
    authorization_url,
    env_file_persister,
    exchange_code,
    load_env_file,
)


def extract_code(pasted: str) -> str:
    """Accept either a bare code or the whole failed-to-load redirect URL."""
    pasted = pasted.strip()
    if "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        codes = qs.get("code")
        if codes:
            return codes[0]
    return pasted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--redirect-uri",
        default=os.environ.get("YAHOO_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        help="Must exactly match a Redirect URI registered on your Yahoo app.",
    )
    args = ap.parse_args()

    load_env_file()
    client_id = os.environ.get("YAHOO_CLIENT_ID", "")
    client_secret = os.environ.get("YAHOO_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print(
            "Missing credentials.\n"
            "Put YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in .env first "
            "(see docs/SETUP.md).",
            file=sys.stderr,
        )
        return 1

    print("\n1. Open this URL in a browser:\n")
    print("   " + authorization_url(client_id, args.redirect_uri))
    print(
        "\n2. Sign in as the Yahoo account that OWNS YOUR FANTASY TEAM, then "
        "approve.\n"
        "   (A different Yahoo account will authorize fine and then find zero "
        "leagues.)\n"
        f"\n3. The browser will fail to load {args.redirect_uri} — that is "
        "expected,\n"
        "   nothing is running there. Copy the whole address bar and paste it "
        "below.\n"
    )

    pasted = input("Pasted URL (or just the code): ").strip()
    if not pasted:
        print("Nothing pasted.", file=sys.stderr)
        return 1

    code = extract_code(pasted)

    try:
        tokens = exchange_code(client_id, client_secret, code, args.redirect_uri)
    except AuthError as e:
        print(f"\nAuthorization failed: {e}", file=sys.stderr)
        return 1

    if not tokens.refresh_token:
        print(
            "\nYahoo returned no refresh token. Without one the agent cannot run "
            "unattended.",
            file=sys.stderr,
        )
        return 1

    env_file_persister(".env")(tokens.refresh_token)

    print("\nAuthorized. Refresh token saved to .env (not printed on purpose).")
    print("\nVerify it works:")
    print("    python scripts/whoami.py")
    print("\nThen publish it to GitHub Actions:")
    print("    gh secret set YAHOO_REFRESH_TOKEN < <(grep '^YAHOO_REFRESH_TOKEN=' "
          ".env | cut -d= -f2-)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
