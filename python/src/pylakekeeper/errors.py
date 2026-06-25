"""Exception hierarchy for the Lakekeeper client.

All errors raised by this package derive from :class:`LakekeeperError`, so callers
can ``except LakekeeperError`` to catch everything we throw. HTTP-level failures
raise :class:`LakekeeperHTTPError` (or a status-specific subclass) carrying the
response status and body for diagnostics.
"""

from __future__ import annotations


class LakekeeperError(Exception):
    """Base class for every error raised by the pylakekeeper package."""


class ConfigError(LakekeeperError):
    """Invalid or missing client configuration (bad URL, missing warehouse, etc.)."""


class AuthError(LakekeeperError):
    """Authentication or token-acquisition failure (token endpoint, device flow, etc.)."""


class LakekeeperHTTPError(LakekeeperError):
    """A non-success HTTP response from the Lakekeeper server.

    Attributes:
        status_code: The HTTP status code.
        body: The response body as text (may be empty).
        method: The HTTP method of the originating request, if known.
        url: The request URL, if known.
    """

    def __init__(
        self,
        status_code: int,
        body: str = "",
        *,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.method = method
        self.url = url
        where = f" ({method} {url})" if method and url else ""
        detail = f": {body}" if body else ""
        super().__init__(f"HTTP {status_code}{where}{detail}")


class NotFoundError(LakekeeperHTTPError):
    """The requested resource does not exist (HTTP 404)."""


class ConflictError(LakekeeperHTTPError):
    """The resource already exists or conflicts with current state (HTTP 409)."""


def http_error_for(
    status_code: int,
    body: str = "",
    *,
    method: str | None = None,
    url: str | None = None,
) -> LakekeeperHTTPError:
    """Construct the most specific :class:`LakekeeperHTTPError` for ``status_code``."""
    cls: type[LakekeeperHTTPError]
    if status_code == 404:
        cls = NotFoundError
    elif status_code == 409:
        cls = ConflictError
    else:
        cls = LakekeeperHTTPError
    return cls(status_code, body, method=method, url=url)
