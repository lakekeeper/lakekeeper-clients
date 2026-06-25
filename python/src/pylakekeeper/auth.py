"""Authentication for the Lakekeeper client.

Two flows for v1:

- :class:`StaticToken` — a fixed bearer token.
- :class:`ClientCredentials` — OAuth2 ``client_credentials`` grant with automatic refresh
  before expiry. Refresh is single-flight: under concurrent callers exactly one token
  request is made.

Both implement :class:`Auth`: ``auth_header()`` returns the ``Authorization`` value and
``invalidate()`` forces the next call to re-acquire (used by the transport's 401-retry).
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import httpx

from .errors import AuthError


class Auth(ABC):
    """Strategy that supplies (and refreshes) the ``Authorization`` header value."""

    @abstractmethod
    def auth_header(self) -> str:
        """Return the full header value, e.g. ``"Bearer <token>"``."""

    def invalidate(self) -> None:  # noqa: B027 — intentional no-op default (e.g. StaticToken)
        """Drop any cached token so the next :meth:`auth_header` re-acquires."""


class StaticToken(Auth):
    """A fixed bearer token. No refresh."""

    def __init__(self, token: str) -> None:
        if not token:
            raise AuthError("StaticToken requires a non-empty token")
        self._token = token

    def auth_header(self) -> str:
        return f"Bearer {self._token}"


class ClientCredentials(Auth):
    """OAuth2 ``client_credentials`` grant with single-flight, expiry-aware refresh.

    Args:
        token_url: the IdP token endpoint.
        client_id, client_secret: the client credentials (sent as form params).
        scope: optional space-delimited scopes.
        refresh_margin: seconds before ``expires_in`` to refresh proactively (default 60).
        timeout: token-request timeout in seconds.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str | None = None,
        refresh_margin: int = 60,
        timeout: float = 30.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._refresh_margin = refresh_margin
        self._timeout = timeout

        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0  # monotonic deadline

    def _valid(self) -> bool:
        return self._token is not None and time.monotonic() < (
            self._expires_at - self._refresh_margin
        )

    def auth_header(self) -> str:
        if not self._valid():
            with self._lock:
                # Double-check: another thread may have refreshed while we waited.
                if not self._valid():
                    self._refresh()
        assert self._token is not None
        return f"Bearer {self._token}"

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _refresh(self) -> None:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope
        try:
            resp = httpx.post(self._token_url, data=data, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AuthError(f"token request to {self._token_url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(f"token endpoint returned HTTP {resp.status_code}: {resp.text}")
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError(f"token response missing access_token: {payload!r}")
        # Default to a short lifetime if the IdP omits expires_in.
        expires_in = float(payload.get("expires_in", 300))
        self._token = token
        self._expires_at = time.monotonic() + expires_in
