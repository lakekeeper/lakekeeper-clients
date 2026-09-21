"""The top-level Lakekeeper client."""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any

from .auth import Auth
from .errors import ConfigError
from .generic_tables import GenericTables
from .transport import Transport

if TYPE_CHECKING:  # pragma: no cover
    from pyiceberg.catalog.rest import RestCatalog


class Client:
    """Entry point for talking to a Lakekeeper server.

    Args:
        base_url: server base URL, e.g. ``http://localhost:8181``.
        warehouse: warehouse name or UUID (the ``{prefix}`` path segment).
        auth: an :class:`~pylakekeeper.auth.Auth` (``StaticToken`` or ``ClientCredentials``).
        project_id: optional; sets the ``x-project-id`` header.
        timeout: per-request timeout in seconds.
        storage_overrides: storage properties to override on every load response, e.g.
            ``{"s3.endpoint": "http://localhost:8333"}``. For when the endpoint the server
            vends is not reachable under that name from here — a storage service published
            under one name inside a cluster and another outside it, a port-forward, or a
            local compose stack. Applied to ``lance_storage_options``, ``fsspec_kwargs``
            and ``objects()`` alike.

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
        storage_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if not base_url:
            raise ConfigError("base_url is required")
        if not warehouse:
            raise ConfigError("warehouse is required")
        self.base_url = base_url
        self.warehouse = warehouse
        #: The configured auth strategy, shared with :meth:`iceberg_catalog`.
        self.auth = auth
        #: Project header value, or None.
        self.project_id = project_id
        self._transport = Transport(base_url, auth, project_id=project_id, timeout=timeout)
        #: Generic-tables API surface.
        self.generic_tables = GenericTables(self._transport, warehouse, storage_overrides)

    def whoami(self) -> dict[str, Any]:
        """The catalog user for the current token.

        Returns the ``whoami`` payload — ``id``, ``name``, ``user-type`` and friends.
        The ``id`` is the authenticated principal as the server sees it, so it is the
        right thing to attribute an action to: a caller cannot pass someone else's.
        """
        resp = self._transport.request("GET", "/management/v1/whoami")
        body: dict[str, Any] = resp.json()
        return body

    def iceberg_catalog(
        self,
        *,
        name: str | None = None,
        warehouse: str | None = None,
        **properties: Any,
    ) -> RestCatalog:
        """A PyIceberg ``RestCatalog`` for this warehouse, sharing this client's auth.

        Lakekeeper serves Iceberg tables and generic tables from one server, so a program
        using both would otherwise configure authentication twice and refresh it twice.
        This routes PyIceberg through the same :class:`~pylakekeeper.auth.Auth`: one
        login, one refresh, both surfaces.

        ::

            table = client.iceberg_catalog().load_table("agent_memory.index")

        Needs the ``[iceberg]`` extra (``pip install 'pylakekeeper[iceberg]'``).
        """
        from .iceberg import iceberg_catalog  # noqa: PLC0415 - optional dependency

        return iceberg_catalog(self, name=name, warehouse=warehouse, **properties)

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
