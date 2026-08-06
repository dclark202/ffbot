from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import auth  # noqa: E402
from scripts.authorize import extract_code  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def token_payload():
    return {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
    }


class TestAuthorizationUrl:
    def test_contains_the_required_parameters(self):
        url = auth.authorization_url("my-client", "https://localhost:8080")
        assert url.startswith(auth.AUTH_URL)
        assert "client_id=my-client" in url
        assert "response_type=code" in url
        assert "localhost%3A8080" in url


class TestTokenRequest:
    def test_parses_a_successful_response(self, monkeypatch, token_payload):
        monkeypatch.setattr(
            auth.requests, "post", lambda *a, **k: FakeResponse(200, token_payload)
        )
        tokens = auth.refresh_tokens("id", "secret", "refresh-0")
        assert tokens.access_token == "access-1"
        assert tokens.refresh_token == "refresh-1"
        assert tokens.expires_at > time.time()

    def test_falls_back_to_basic_auth_when_body_credentials_are_rejected(
        self, monkeypatch, token_payload
    ):
        """Yahoo documents body credentials but some apps require Basic.

        Untestable against the live API until access is granted, so the code
        tries both rather than guessing — this pins that behaviour down.
        """
        calls = []

        def fake_post(url, data=None, headers=None, timeout=None):
            calls.append(headers or {})
            if len(calls) == 1:
                return FakeResponse(401, text="invalid_client")
            return FakeResponse(200, token_payload)

        monkeypatch.setattr(auth.requests, "post", fake_post)
        tokens = auth.refresh_tokens("id", "secret", "refresh-0")

        assert tokens.access_token == "access-1"
        assert len(calls) == 2
        assert calls[0].get("Authorization") is None
        assert calls[1]["Authorization"].startswith("Basic ")

    def test_raises_with_yahoo_detail_on_hard_failure(self, monkeypatch):
        monkeypatch.setattr(
            auth.requests,
            "post",
            lambda *a, **k: FakeResponse(500, text="upstream exploded"),
        )
        with pytest.raises(auth.AuthError, match="upstream exploded"):
            auth.refresh_tokens("id", "secret", "refresh-0")

    def test_keeps_the_existing_refresh_token_when_yahoo_omits_it(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            auth.requests,
            "post",
            lambda *a, **k: FakeResponse(200, {"access_token": "a", "expires_in": 3600}),
        )
        assert auth.refresh_tokens("id", "s", "refresh-0").refresh_token == "refresh-0"


class TestTokenSecrecy:
    def test_str_does_not_leak_the_token(self):
        t = auth.TokenSet("super-secret", "also-secret", time.time() + 3600)
        assert "secret" not in str(t)

    def test_repr_does_not_leak_the_token(self):
        t = auth.TokenSet("super-secret", "also-secret", time.time() + 3600)
        assert "secret" not in repr(t)

    def test_expiry_accounts_for_skew(self):
        assert auth.TokenSet("a", "r", time.time() + 30).is_expired()
        assert not auth.TokenSet("a", "r", time.time() + 3600).is_expired()


class TestRotation:
    """Losing a rotated refresh token locks the agent out for the season."""

    def _session(self, monkeypatch, payload, on_refresh=None):
        monkeypatch.setattr(
            auth.requests, "post", lambda *a, **k: FakeResponse(200, payload)
        )
        return auth.YahooSession("id", "secret", "refresh-0", on_refresh=on_refresh)

    def test_rotated_token_is_persisted(self, monkeypatch, token_payload):
        seen = []
        self._session(monkeypatch, token_payload, on_refresh=seen.append)
        assert seen == ["refresh-1"]

    def test_unchanged_token_is_not_rewritten(self, monkeypatch):
        seen = []
        payload = {
            "access_token": "a",
            "refresh_token": "refresh-0",
            "expires_in": 3600,
        }
        self._session(monkeypatch, payload, on_refresh=seen.append)
        assert seen == []

    def test_session_carries_the_bearer_header(self, monkeypatch, token_payload):
        sc = self._session(monkeypatch, token_payload)
        assert sc.session.headers["Authorization"] == "Bearer access-1"

    def test_subsequent_rotation_is_also_persisted(self, monkeypatch):
        """A second rotation must not be missed because the first was handled."""
        seen = []
        payloads = iter(
            [
                {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600},
                {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600},
            ]
        )
        monkeypatch.setattr(
            auth.requests, "post", lambda *a, **k: FakeResponse(200, next(payloads))
        )
        sc = auth.YahooSession("id", "secret", "refresh-0", on_refresh=seen.append)
        sc.refresh_access_token()
        assert seen == ["r1", "r2"]

    def test_missing_credentials_fail_loudly(self):
        with pytest.raises(auth.AuthError, match="Missing credentials"):
            auth.YahooSession("", "", "")


class TestPersisters:
    def test_env_persister_replaces_an_existing_value(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("YAHOO_CLIENT_ID=abc\nYAHOO_REFRESH_TOKEN=old\n")
        auth.env_file_persister(p)("new")
        assert "YAHOO_REFRESH_TOKEN=new" in p.read_text()
        assert "YAHOO_CLIENT_ID=abc" in p.read_text()
        assert "old" not in p.read_text()

    def test_env_persister_appends_when_absent(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("YAHOO_CLIENT_ID=abc\n")
        auth.env_file_persister(p)("new")
        assert "YAHOO_REFRESH_TOKEN=new" in p.read_text()

    def test_github_persister_raises_when_the_secret_cannot_be_written(
        self, monkeypatch
    ):
        class Proc:
            returncode = 1
            stderr = "HTTP 403: Resource not accessible"

        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: Proc())
        with pytest.raises(auth.AuthError, match="next run will"):
            auth.github_secret_persister()("token")

    def test_github_persister_succeeds_quietly(self, monkeypatch):
        class Proc:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: Proc())
        auth.github_secret_persister()("token")  # no exception


class TestEnvLoading:
    def test_loads_values_from_file(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        p.write_text("# comment\n\nYAHOO_CLIENT_ID='quoted'\nOTHER=plain\n")
        monkeypatch.delenv("YAHOO_CLIENT_ID", raising=False)
        monkeypatch.delenv("OTHER", raising=False)
        auth.load_env_file(p)
        import os

        assert os.environ["YAHOO_CLIENT_ID"] == "quoted"
        assert os.environ["OTHER"] == "plain"

    def test_does_not_shadow_real_environment(self, tmp_path, monkeypatch):
        """Actions secrets must win over a stray .env in the checkout."""
        p = tmp_path / ".env"
        p.write_text("YAHOO_CLIENT_ID=from-file\n")
        monkeypatch.setenv("YAHOO_CLIENT_ID", "from-ci")
        auth.load_env_file(p)
        import os

        assert os.environ["YAHOO_CLIENT_ID"] == "from-ci"

    def test_missing_file_is_not_an_error(self, tmp_path):
        auth.load_env_file(tmp_path / "nope.env")


class TestCodeExtraction:
    def test_accepts_a_full_redirect_url(self):
        url = "https://localhost:8080/?code=abc123&state=x"
        assert extract_code(url) == "abc123"

    def test_accepts_a_bare_code(self):
        assert extract_code("  abc123 ") == "abc123"
