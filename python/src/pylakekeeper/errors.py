"""Exception hierarchy for the Lakekeeper client.

All errors raised by this package derive from :class:`LakekeeperError`, so callers
can ``except LakekeeperError`` to catch everything we throw. HTTP-level failures
raise :class:`LakekeeperHTTPError` (or a status-specific subclass) carrying the
response status and body for diagnostics.
"""

from __future__ import annotations

import json
import re
import uuid


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
        error_type: The server's error ``type`` (e.g. ``"DatasetRefProtected"``), or
            ``None`` when the body is not a Lakekeeper error. Branch on this, not on the
            message, which is prose and may change.
        error_message: The server's error ``message``, or ``None``.
        stack: The server's error ``stack`` — detail lines, some machine-readable.
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
        self.error_type, self.error_message, self.stack = _parse_error_body(body)
        where = f" ({method} {url})" if method and url else ""
        detail = f": {body}" if body else ""
        super().__init__(f"HTTP {status_code}{where}{detail}")


class NotFoundError(LakekeeperHTTPError):
    """The requested resource does not exist (HTTP 404)."""


class ConflictError(LakekeeperHTTPError):
    """The resource already exists or conflicts with current state (HTTP 409)."""


class CommitConflictError(ConflictError):
    """A dataset branch moved between reading its head and committing (HTTP 409).

    Nothing was written. ``current_snapshot_id`` is the branch's head now, so the change
    can be rebased onto it and sent again.
    """

    @property
    def current_snapshot_id(self) -> uuid.UUID | None:
        for line in self.stack:
            if match := _CURRENT_HEAD.fullmatch(line.strip()):
                return uuid.UUID(match.group(1))
        return None


class IntegrityError(LakekeeperError):
    """Bytes read for a dataset file are not the bytes its snapshot recorded.

    Raised when a download's ``ETag`` differs from the etag the snapshot recorded: the
    object was written again since the commit, and the file was not pinned to a version.
    """


#: How a commit conflict names the branch's current head in the error stack.
_CURRENT_HEAD = re.compile(r"current-snapshot-id: ([0-9a-fA-F-]{36})")


def _parse_error_body(body: str) -> tuple[str | None, str | None, list[str]]:
    """Pull ``type``, ``message`` and ``stack`` out of an Iceberg-style error body."""
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return None, None, []
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return None, None, []
    error_type = error.get("type")
    message = error.get("message")
    stack = error.get("stack")
    return (
        error_type if isinstance(error_type, str) else None,
        message if isinstance(message, str) else None,
        [line for line in stack if isinstance(line, str)] if isinstance(stack, list) else [],
    )


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
        error_type, _, _ = _parse_error_body(body)
        cls = CommitConflictError if error_type == "DatasetCommitConflict" else ConflictError
    else:
        cls = LakekeeperHTTPError
    return cls(status_code, body, method=method, url=url)
