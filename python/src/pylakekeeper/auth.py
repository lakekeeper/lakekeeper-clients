"""Authentication for the Lakekeeper client.

Flows:

- :class:`StaticToken` — a fixed bearer token.
- :class:`ClientCredentials` — OAuth2 ``client_credentials`` grant (machine-to-machine),
  auto-refreshed before expiry.
- :class:`DeviceCodeFlow` — OAuth2 Device Authorization Grant (RFC 8628): the user is
  shown a URL + code to approve in a browser on any device. For CLIs / headless hosts.
- :class:`AuthorizationCodeFlow` — OAuth2 Authorization Code + PKCE (RFC 7636): opens a
  browser and captures the redirect on a loopback socket. For interactive desktop use.

The two interactive flows keep the session alive well past the ~1h access-token lifetime:
the initial login is interactive, but afterwards the ``refresh_token`` is used to renew
silently (``grant_type=refresh_token``). Only if the refresh token itself is
expired/revoked does a new interactive login occur.

All flows implement :class:`Auth`: ``auth_header()`` returns the ``Authorization`` value
and ``invalidate()`` forces the next call to re-acquire (used by the transport's
401-retry). Refresh is single-flight: under concurrent callers exactly one token request
is in flight at a time.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class _TokenBundle:
    """A parsed OAuth2 token response."""

    access_token: str
    expires_in: float
    refresh_token: str | None


def _parse_token_response(payload: dict[str, Any]) -> _TokenBundle:
    token = payload.get("access_token")
    if not token:
        raise AuthError(f"token response missing access_token: {payload!r}")
    # Default to a short lifetime if the IdP omits expires_in.
    expires_in = float(payload.get("expires_in", 300))
    return _TokenBundle(
        access_token=token,
        expires_in=expires_in,
        refresh_token=payload.get("refresh_token"),
    )


class _RefreshableAuth(Auth):
    """Base for interactive flows that acquire a token bundle once, then keep it fresh.

    Subclasses implement :meth:`_acquire` (the interactive first login). This base handles
    caching, expiry-aware single-flight renewal, and — crucially — silent renewal via the
    ``refresh_token`` so the session survives long past the access token's ~1h lifetime.
    An interactive re-login happens only when there is no usable refresh token.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str | None,
        scope: str | None,
        refresh_margin: int,
        timeout: float,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._refresh_margin = refresh_margin
        self._timeout = timeout

        self._lock = threading.Lock()
        self._token: str | None = None
        self._refresh_token: str | None = None
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
                    self._ensure_token()
        assert self._token is not None
        return f"Bearer {self._token}"

    def invalidate(self) -> None:
        # Drop the access token but KEEP the refresh token, so the next call renews
        # silently rather than forcing a fresh interactive login.
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _ensure_token(self) -> None:
        """Acquire a valid access token. Caller holds the lock."""
        if self._refresh_token is not None:
            try:
                self._apply_bundle(self._refresh())
                return
            except AuthError:
                # Refresh token expired/revoked — fall back to interactive login.
                self._refresh_token = None
        self._apply_bundle(self._acquire())

    def _apply_bundle(self, bundle: _TokenBundle) -> None:
        self._token = bundle.access_token
        self._expires_at = time.monotonic() + bundle.expires_in
        # Honour refresh-token rotation, but keep the old one if none was returned.
        if bundle.refresh_token:
            self._refresh_token = bundle.refresh_token

    def _refresh(self) -> _TokenBundle:
        assert self._refresh_token is not None
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        if self._scope:
            data["scope"] = self._scope
        return self._token_request(data)

    def _token_request(self, data: dict[str, str]) -> _TokenBundle:
        """POST a grant to the token endpoint and parse the response."""
        try:
            resp = httpx.post(self._token_url, data=data, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AuthError(f"token request to {self._token_url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(f"token endpoint returned HTTP {resp.status_code}: {resp.text}")
        return _parse_token_response(resp.json())

    @abstractmethod
    def _acquire(self) -> _TokenBundle:
        """Run the interactive first login and return the initial token bundle."""


@dataclass(frozen=True)
class DeviceCodePrompt:
    """What to show the user so they can approve a :class:`DeviceCodeFlow` login.

    ``verification_uri_complete`` (if the IdP provides it) already embeds ``user_code`` —
    prefer it for a one-click / QR experience; otherwise show ``verification_uri`` and ask
    the user to enter ``user_code``.
    """

    verification_uri: str | None
    user_code: str | None
    verification_uri_complete: str | None
    expires_in: float


def _default_device_prompt(prompt: DeviceCodePrompt) -> None:
    target = prompt.verification_uri_complete or prompt.verification_uri
    lines = ["", "To sign in, open the following URL in a browser:", f"    {target}"]
    if not prompt.verification_uri_complete and prompt.user_code:
        lines.append(f"and enter the code:  {prompt.user_code}")
    lines.append("")
    print("\n".join(lines), flush=True)


# RFC 8628 device-flow grant type.
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceCodeFlow(_RefreshableAuth):
    """OAuth2 Device Authorization Grant (RFC 8628).

    The user approves the login in a browser (on this or any other device); this client
    polls the token endpoint until they do. Ideal for CLIs and headless hosts where you
    cannot capture a redirect. After the first login, the token is renewed silently via
    the refresh token.

    Args:
        device_authorization_url: the IdP device-authorization endpoint.
        token_url: the IdP token endpoint.
        client_id: the OAuth2 client id.
        client_secret: optional; only for confidential clients (most device-flow clients
            are public and omit it).
        scope: optional space-delimited scopes. Include ``offline_access`` if your IdP
            requires it to issue a refresh token.
        prompt: callback that shows :class:`DeviceCodePrompt` to the user. Defaults to
            printing to stdout.
        refresh_margin: seconds before expiry to renew proactively.
        timeout: per-HTTP-request timeout in seconds.
        poll_timeout: overall seconds to wait for the user to approve.
    """

    def __init__(
        self,
        *,
        device_authorization_url: str,
        token_url: str,
        client_id: str,
        client_secret: str | None = None,
        scope: str | None = None,
        prompt: Callable[[DeviceCodePrompt], None] | None = None,
        refresh_margin: int = 60,
        timeout: float = 30.0,
        poll_timeout: float = 300.0,
    ) -> None:
        super().__init__(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            refresh_margin=refresh_margin,
            timeout=timeout,
        )
        self._device_auth_url = device_authorization_url
        self._prompt = prompt or _default_device_prompt
        self._poll_timeout = poll_timeout

    def _acquire(self) -> _TokenBundle:
        device = self._request_device_code()
        device_code = device["device_code"]
        interval = float(device.get("interval", 5))
        expires_in = float(device.get("expires_in", self._poll_timeout))

        self._prompt(
            DeviceCodePrompt(
                verification_uri=device.get("verification_uri"),
                user_code=device.get("user_code"),
                verification_uri_complete=device.get("verification_uri_complete"),
                expires_in=expires_in,
            )
        )

        deadline = time.monotonic() + min(expires_in, self._poll_timeout)
        while time.monotonic() < deadline:
            time.sleep(interval)
            done, bundle, interval = self._poll_once(device_code, interval)
            if done:
                assert bundle is not None
                return bundle
        raise AuthError("device authorization timed out before the user approved")

    def _request_device_code(self) -> dict[str, Any]:
        data = {"client_id": self._client_id}
        if self._client_secret:
            data["client_secret"] = self._client_secret
        if self._scope:
            data["scope"] = self._scope
        try:
            resp = httpx.post(self._device_auth_url, data=data, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AuthError(
                f"device-authorization request to {self._device_auth_url} failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise AuthError(
                f"device-authorization endpoint returned HTTP {resp.status_code}: {resp.text}"
            )
        device: dict[str, Any] = resp.json()
        if "device_code" not in device:
            raise AuthError(f"device-authorization response missing device_code: {device!r}")
        return device

    def _poll_once(
        self, device_code: str, interval: float
    ) -> tuple[bool, _TokenBundle | None, float]:
        """Poll the token endpoint once. Returns (done, bundle, next_interval)."""
        data = {
            "grant_type": _DEVICE_GRANT,
            "device_code": device_code,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        try:
            resp = httpx.post(self._token_url, data=data, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AuthError(f"token request to {self._token_url} failed: {exc}") from exc

        if resp.status_code == 200:
            return True, _parse_token_response(resp.json()), interval

        error = _error_code(resp)
        if error == "authorization_pending":
            return False, None, interval
        if error == "slow_down":
            # RFC 8628: back off by 5s and keep polling.
            return False, None, interval + 5
        raise AuthError(f"device authorization failed ({error or resp.status_code}): {resp.text}")


def _error_code(resp: httpx.Response) -> str | None:
    try:
        error = resp.json().get("error")
    except ValueError:
        return None
    return error if isinstance(error, str) else None


def _generate_verifier() -> str:
    """A high-entropy PKCE ``code_verifier`` (RFC 7636 §4.1; 43–128 unreserved chars)."""
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    """The S256 ``code_challenge`` for ``verifier`` (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single OAuth redirect on the loopback socket."""

    def do_GET(self) -> None:  # noqa: N802 — name mandated by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.expected_path:  # type: ignore[attr-defined]
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.returned_state = params.get("state", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]
        body = b"<html><body>Login complete. You may close this tab.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence default stderr logging
        pass


class _RedirectServer(http.server.HTTPServer):
    def __init__(self, address: tuple[str, int], expected_path: str) -> None:
        super().__init__(address, _CallbackHandler)
        self.expected_path = expected_path
        self.auth_code: str | None = None
        self.returned_state: str | None = None
        self.auth_error: str | None = None

    def wait_for_code(self, *, login_timeout: float, expected_state: str) -> str:
        deadline = time.monotonic() + login_timeout
        # Poll in short slices so we can honour the overall deadline and skip stray
        # requests (e.g. the browser's /favicon.ico) without giving up.
        self.timeout = 1.0
        while self.auth_code is None and self.auth_error is None:
            if time.monotonic() >= deadline:
                raise AuthError("timed out waiting for the authorization redirect")
            self.handle_request()
        if self.auth_error is not None:
            raise AuthError(f"authorization failed: {self.auth_error}")
        if self.returned_state != expected_state:
            raise AuthError("state mismatch in authorization redirect (possible CSRF)")
        assert self.auth_code is not None
        return self.auth_code


def _default_open_browser(url: str) -> None:
    print(
        f"\nOpening your browser to sign in. If it does not open, visit:\n    {url}\n",
        flush=True,
    )
    webbrowser.open(url)


class AuthorizationCodeFlow(_RefreshableAuth):
    """OAuth2 Authorization Code grant with PKCE (RFC 7636).

    Opens a browser for the user to sign in and captures the redirect on a short-lived
    loopback HTTP server, so no client secret needs to live on the machine (PKCE proves
    possession instead). For interactive desktop / notebook use. After the first login the
    token is renewed silently via the refresh token.

    Args:
        authorization_url: the IdP authorization endpoint.
        token_url: the IdP token endpoint.
        client_id: the OAuth2 client id.
        client_secret: optional; supply only for confidential clients. PKCE means public
            clients can omit it.
        scope: optional space-delimited scopes. Include ``offline_access`` if your IdP
            requires it to issue a refresh token.
        redirect_host: loopback host to bind (default ``localhost``).
        redirect_port: loopback port; ``0`` (default) lets the OS pick a free one. The
            resulting ``redirect_uri`` must be registered on the IdP client (register
            ``http://localhost`` / the exact port your IdP requires).
        redirect_path: path the IdP redirects back to (default ``/callback``).
        open_browser: callback that opens ``authorization_url``. Defaults to
            :func:`webbrowser.open`. Must not block.
        refresh_margin: seconds before expiry to renew proactively.
        timeout: per-HTTP-request timeout in seconds.
        login_timeout: overall seconds to wait for the browser redirect.
    """

    def __init__(
        self,
        *,
        authorization_url: str,
        token_url: str,
        client_id: str,
        client_secret: str | None = None,
        scope: str | None = None,
        redirect_host: str = "localhost",
        redirect_port: int = 0,
        redirect_path: str = "/callback",
        open_browser: Callable[[str], None] | None = None,
        refresh_margin: int = 60,
        timeout: float = 30.0,
        login_timeout: float = 300.0,
    ) -> None:
        super().__init__(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            refresh_margin=refresh_margin,
            timeout=timeout,
        )
        self._authorization_url = authorization_url
        self._redirect_host = redirect_host
        self._redirect_port = redirect_port
        self._redirect_path = redirect_path
        self._open_browser = open_browser or _default_open_browser
        self._login_timeout = login_timeout

    def _acquire(self) -> _TokenBundle:
        verifier = _generate_verifier()
        challenge = _code_challenge(verifier)
        state = secrets.token_urlsafe(24)

        server = _RedirectServer((self._redirect_host, self._redirect_port), self._redirect_path)
        try:
            port = server.server_address[1]
            redirect_uri = f"http://{self._redirect_host}:{port}{self._redirect_path}"
            self._open_browser(self._build_authorization_url(redirect_uri, state, challenge))
            code = server.wait_for_code(login_timeout=self._login_timeout, expected_state=state)
        finally:
            server.server_close()

        return self._exchange_code(code, redirect_uri, verifier)

    def _build_authorization_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self._scope:
            params["scope"] = self._scope
        sep = "&" if urllib.parse.urlparse(self._authorization_url).query else "?"
        return self._authorization_url + sep + urllib.parse.urlencode(params)

    def _exchange_code(self, code: str, redirect_uri: str, verifier: str) -> _TokenBundle:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "code_verifier": verifier,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        return self._token_request(data)
