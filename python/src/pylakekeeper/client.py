"""The top-level Lakekeeper client."""

from __future__ import annotations

from types import TracebackType

from .auth import Auth
from .errors import ConfigError
from .generic_tables import GenericTables
from .transport import Transport


class Client:
    """Entry point for talking to a Lakekeeper server.

    Args:
        base_url: server base URL, e.g. ``http://localhost:8181``.
        warehouse: warehouse name or UUID (the ``{prefix}`` path segment).
        auth: an :class:`~pylakekeeper.auth.Auth` (``StaticToken`` or ``ClientCredentials``).
        project_id: optional; sets the ``x-project-id`` header.
        timeout: per-request timeout in seconds.

    Use as a context manager (or call :meth:`close`) to release the HTTP connection pool.
    """

    def __init__(
        self,
        base_url: str,
        warehouse: str,
        auth: Auth,
        *,
        project_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ConfigError("base_url is required")
        if not warehouse:
            raise ConfigError("warehouse is required")
        self.base_url = base_url
        self.warehouse = warehouse
        self._transport = Transport(base_url, auth, project_id=project_id, timeout=timeout)
        #: Generic-tables API surface.
        self.generic_tables = GenericTables(self._transport, warehouse)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
