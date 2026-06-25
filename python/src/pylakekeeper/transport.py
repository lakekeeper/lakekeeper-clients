"""HTTP transport: bearer-token injection, ``x-project-id`` plumbing, and 401-retry.

A thin wrapper over ``httpx.Client`` used by the generic-tables surface. It does not
re-encode paths: callers pass URL paths with namespace segments already percent-encoded
(``%1F``), and httpx preserves existing percent-escapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .auth import Auth
from .errors import http_error_for


class Transport:
    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        project_id: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._auth = auth
        self._project_id = project_id
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request, retrying once on 401 after invalidating the token."""
        resp = self._send(method, path, params=params, json=json, headers=headers)
        if resp.status_code == 401:
            self._auth.invalidate()
            resp = self._send(method, path, params=params, json=json, headers=headers)
        if resp.status_code >= 400:
            raise http_error_for(
                resp.status_code, resp.text, method=method, url=str(resp.request.url)
            )
        return resp

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        merged: dict[str, str] = {"Authorization": self._auth.auth_header()}
        if self._project_id:
            merged["x-project-id"] = self._project_id
        if headers:
            merged.update(headers)
        return self._client.request(method, path, params=params, json=json, headers=merged)

    def close(self) -> None:
        self._client.close()
