"""Auth tests: client_credentials refresh, single-flight, invalidate."""

from __future__ import annotations

import threading

import pytest

from pylakekeeper.auth import ClientCredentials, StaticToken
from pylakekeeper.errors import AuthError

TOKEN_URL = "https://idp.example.com/token"


def test_static_token_header():
    assert StaticToken("abc").auth_header() == "Bearer abc"


def test_static_token_rejects_empty():
    with pytest.raises(AuthError):
        StaticToken("")


def test_client_credentials_fetches_and_caches(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok-1", "expires_in": 3600})
    cc = ClientCredentials(TOKEN_URL, "cid", "secret", scope="lakekeeper")

    assert cc.auth_header() == "Bearer tok-1"
    # Cached: second call must not hit the token endpoint again.
    assert cc.auth_header() == "Bearer tok-1"
    assert len(httpx_mock.get_requests(url=TOKEN_URL)) == 1

    # The grant is sent as form params.
    sent = httpx_mock.get_requests(url=TOKEN_URL)[0]
    body = sent.read().decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "scope=lakekeeper" in body


def test_client_credentials_refreshes_when_expired(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok-1", "expires_in": 3600})
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok-2", "expires_in": 3600})
    cc = ClientCredentials(TOKEN_URL, "cid", "secret")

    assert cc.auth_header() == "Bearer tok-1"
    cc.invalidate()  # simulate expiry / 401
    assert cc.auth_header() == "Bearer tok-2"
    assert len(httpx_mock.get_requests(url=TOKEN_URL)) == 2


def test_client_credentials_auto_refreshes_near_expiry(httpx_mock, monkeypatch):
    """Refresh fires automatically once the token is within refresh_margin of exp.

    Drives a fake monotonic clock so the expiry path is exercised deterministically,
    with no real waiting — this is what 'does token refresh happen' actually means.
    """
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok-1", "expires_in": 3600})
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok-2", "expires_in": 3600})

    now = {"t": 1000.0}
    monkeypatch.setattr("pylakekeeper.auth.time.monotonic", lambda: now["t"])

    cc = ClientCredentials(TOKEN_URL, "cid", "secret", refresh_margin=60)

    # First call fetches tok-1; it expires at 1000 + 3600 = 4600.
    assert cc.auth_header() == "Bearer tok-1"

    # Comfortably inside validity -> served from cache, no second fetch.
    now["t"] = 4000.0
    assert cc.auth_header() == "Bearer tok-1"
    assert len(httpx_mock.get_requests(url=TOKEN_URL)) == 1

    # Cross the refresh threshold (>= 4600 - 60 = 4540) -> auto-refresh to tok-2.
    now["t"] = 4550.0
    assert cc.auth_header() == "Bearer tok-2"
    assert len(httpx_mock.get_requests(url=TOKEN_URL)) == 2


def test_client_credentials_single_flight(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "tok", "expires_in": 3600})
    cc = ClientCredentials(TOKEN_URL, "cid", "secret")

    headers: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()  # maximise contention on the cold cache
        headers.append(cc.auth_header())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert headers == ["Bearer tok"] * 8
    # Single-flight: 8 concurrent cold callers => exactly one token request.
    assert len(httpx_mock.get_requests(url=TOKEN_URL)) == 1


def test_client_credentials_raises_on_error_status(httpx_mock):
    httpx_mock.add_response(url=TOKEN_URL, status_code=401, text="bad client")
    cc = ClientCredentials(TOKEN_URL, "cid", "secret")
    with pytest.raises(AuthError):
        cc.auth_header()
