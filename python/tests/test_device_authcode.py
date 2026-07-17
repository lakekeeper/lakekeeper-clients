"""Tests for the interactive flows: Device Code (RFC 8628) and Authorization Code + PKCE.

The interactive step (browser / device approval) is simulated, but every network hop and
the loopback redirect server are exercised for real. The emphasis is the requirement that
the session survives past the access token's lifetime via silent ``refresh_token`` renewal.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import urllib.parse
import urllib.request

from pylakekeeper.auth import (
    AuthorizationCodeFlow,
    DeviceCodeFlow,
    DeviceCodePrompt,
    _code_challenge,
    _generate_verifier,
)

DEVICE_URL = "https://idp.example.com/device"
AUTH_URL = "https://idp.example.com/authorize"
TOKEN_URL = "https://idp.example.com/token"


# --- PKCE --------------------------------------------------------------------------------


def test_pkce_challenge_is_s256_of_verifier():
    verifier = _generate_verifier()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert _code_challenge(verifier) == expected
    assert "=" not in _code_challenge(verifier)  # unpadded base64url


def test_pkce_verifier_is_random():
    assert _generate_verifier() != _generate_verifier()


# --- Device Code flow --------------------------------------------------------------------


def test_device_flow_polls_until_approved(httpx_mock, monkeypatch):
    monkeypatch.setattr("pylakekeeper.auth.time.sleep", lambda _s: None)

    httpx_mock.add_response(
        url=DEVICE_URL,
        json={
            "device_code": "dev-123",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://idp.example.com/activate",
            "verification_uri_complete": "https://idp.example.com/activate?code=WDJB-MJHT",
            "interval": 5,
            "expires_in": 600,
        },
    )
    # Pending twice, then approved.
    httpx_mock.add_response(url=TOKEN_URL, status_code=400, json={"error": "authorization_pending"})
    httpx_mock.add_response(url=TOKEN_URL, status_code=400, json={"error": "slow_down"})
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}
    )

    shown: list[DeviceCodePrompt] = []
    flow = DeviceCodeFlow(
        device_authorization_url=DEVICE_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        scope="lakekeeper offline_access",
        prompt=shown.append,
    )

    assert flow.auth_header() == "Bearer acc-1"
    # Cached after acquisition.
    assert flow.auth_header() == "Bearer acc-1"

    # The user was prompted with the completion URL.
    assert shown and shown[0].verification_uri_complete.endswith("code=WDJB-MJHT")

    # Device grant was sent to the token endpoint.
    poll = httpx_mock.get_requests(url=TOKEN_URL)[0]
    body = poll.read().decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in body
    assert "device_code=dev-123" in body


def test_device_flow_renews_via_refresh_token(httpx_mock, monkeypatch):
    """After login, the session must renew silently (no second device approval)."""
    monkeypatch.setattr("pylakekeeper.auth.time.sleep", lambda _s: None)

    httpx_mock.add_response(
        url=DEVICE_URL,
        json={"device_code": "dev-123", "verification_uri": "u", "interval": 1, "expires_in": 600},
    )
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}
    )
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600}
    )

    flow = DeviceCodeFlow(
        device_authorization_url=DEVICE_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        prompt=lambda _p: None,
    )

    assert flow.auth_header() == "Bearer acc-1"
    flow.invalidate()  # simulate a 401 / expiry
    assert flow.auth_header() == "Bearer acc-2"

    # The device endpoint was hit exactly once — renewal used the refresh token.
    assert len(httpx_mock.get_requests(url=DEVICE_URL)) == 1
    refresh_req = httpx_mock.get_requests(url=TOKEN_URL)[-1]
    body = refresh_req.read().decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=ref-1" in body


def test_device_flow_reauthenticates_when_refresh_fails(httpx_mock, monkeypatch):
    """If the refresh token is dead, fall back to a fresh interactive login."""
    monkeypatch.setattr("pylakekeeper.auth.time.sleep", lambda _s: None)

    httpx_mock.add_response(
        url=DEVICE_URL,
        json={"device_code": "d1", "verification_uri": "u", "interval": 1, "expires_in": 600},
    )
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}
    )
    httpx_mock.add_response(url=TOKEN_URL, status_code=400, json={"error": "invalid_grant"})
    httpx_mock.add_response(
        url=DEVICE_URL,
        json={"device_code": "d2", "verification_uri": "u", "interval": 1, "expires_in": 600},
    )
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "acc-3", "expires_in": 3600})

    flow = DeviceCodeFlow(
        device_authorization_url=DEVICE_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        prompt=lambda _p: None,
    )

    assert flow.auth_header() == "Bearer acc-1"
    flow.invalidate()
    assert flow.auth_header() == "Bearer acc-3"
    assert len(httpx_mock.get_requests(url=DEVICE_URL)) == 2  # re-login happened


# --- Authorization Code + PKCE flow ------------------------------------------------------


def _browser_that_completes_login(code: str = "auth-code-xyz"):
    """Return an open_browser callback that plays the IdP: redirects back with ``code``."""

    def open_browser(url: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]

        def hit_redirect() -> None:
            # Use urllib (not httpx) so this bypasses httpx_mock and reaches the real
            # loopback server the flow started.
            urllib.request.urlopen(  # noqa: S310 — localhost loopback under test
                f"{redirect_uri}?code={code}&state={urllib.parse.quote(state)}", timeout=5
            ).read()

        threading.Thread(target=hit_redirect, daemon=True).start()

    return open_browser


def test_authorization_code_flow_roundtrip(httpx_mock):
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}
    )

    flow = AuthorizationCodeFlow(
        authorization_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        scope="lakekeeper offline_access",
        open_browser=_browser_that_completes_login(),
    )

    assert flow.auth_header() == "Bearer acc-1"

    # The authorization code was exchanged with the PKCE verifier.
    exchange = httpx_mock.get_requests(url=TOKEN_URL)[0]
    body = exchange.read().decode()
    assert "grant_type=authorization_code" in body
    assert "code=auth-code-xyz" in body
    assert "code_verifier=" in body


def test_authorization_code_flow_renews_via_refresh_token(httpx_mock):
    httpx_mock.add_response(
        url=TOKEN_URL, json={"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}
    )
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "acc-2", "expires_in": 3600})

    opened: list[str] = []

    def counting_browser(url: str) -> None:
        opened.append(url)
        _browser_that_completes_login()(url)

    flow = AuthorizationCodeFlow(
        authorization_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        open_browser=counting_browser,
    )

    assert flow.auth_header() == "Bearer acc-1"
    flow.invalidate()
    assert flow.auth_header() == "Bearer acc-2"

    # Browser opened once; the second token came from the refresh grant.
    assert len(opened) == 1
    assert "grant_type=refresh_token" in httpx_mock.get_requests(url=TOKEN_URL)[-1].read().decode()


def test_authorization_code_flow_builds_pkce_authorization_url(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "a", "expires_in": 3600})

    captured: list[str] = []

    def capture_and_complete(url: str) -> None:
        captured.append(url)
        _browser_that_completes_login()(url)

    flow = AuthorizationCodeFlow(
        authorization_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id="cli",
        open_browser=capture_and_complete,
    )
    flow.auth_header()

    query = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0]).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["cli"]
    assert query["redirect_uri"][0].startswith("http://localhost:")
    assert query["code_challenge"]  # present
