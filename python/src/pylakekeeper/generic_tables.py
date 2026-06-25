"""The generic-tables API surface — the methods that replace hand-rolled requests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

from .formats import GenericTableFormat, normalize_format
from .models import (
    GenericTableIdentifier,
    ListGenericTablesResponse,
    LoadGenericTableResponse,
)
from .transport import Transport
from .url import NamespaceLike, encode_namespace

_VENDED_HEADER = {"X-Iceberg-Access-Delegation": "vended-credentials"}


class GenericTables:
    """CRUD for Lakekeeper generic (non-Iceberg) tables, scoped to one warehouse."""

    def __init__(self, transport: Transport, warehouse: str) -> None:
        self._t = transport
        self._warehouse = warehouse

    def _collection_path(self, namespace: NamespaceLike) -> str:
        return (
            f"/lakekeeper/v1/{quote(self._warehouse, safe='')}"
            f"/namespaces/{encode_namespace(namespace)}/generic-tables"
        )

    def _table_path(self, namespace: NamespaceLike, name: str) -> str:
        return f"{self._collection_path(namespace)}/{quote(name, safe='')}"

    def create(
        self,
        namespace: NamespaceLike,
        name: str,
        *,
        format: str | GenericTableFormat,
        base_location: str | None = None,
        doc: str | None = None,
        properties: Mapping[str, str] | None = None,
    ) -> LoadGenericTableResponse:
        """Create a generic table. Returns the load response for the new table.

        ``format`` accepts a :class:`~pylakekeeper.GenericTableFormat` or any string
        matching the server's shape rule (e.g. ``"lance"``); it is validated client-side.
        """
        body: dict[str, Any] = {"name": name, "format": normalize_format(format)}
        if base_location is not None:
            body["base-location"] = base_location
        if doc is not None:
            body["doc"] = doc
        if properties:
            body["properties"] = dict(properties)
        resp = self._t.request("POST", self._collection_path(namespace), json=body)
        return LoadGenericTableResponse.model_validate(resp.json())

    def load(
        self,
        namespace: NamespaceLike,
        name: str,
        *,
        vended: bool = False,
    ) -> LoadGenericTableResponse:
        """Load a generic table. With ``vended=True``, request inline STS credentials."""
        headers = _VENDED_HEADER if vended else None
        resp = self._t.request("GET", self._table_path(namespace, name), headers=headers)
        return LoadGenericTableResponse.model_validate(resp.json())

    def list(
        self,
        namespace: NamespaceLike,
        *,
        page_size: int = 100,
    ) -> Iterator[GenericTableIdentifier]:
        """Iterate every table identifier in a namespace, following ``next-page-token``."""
        path = self._collection_path(namespace)
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            resp = self._t.request("GET", path, params=params)
            page = ListGenericTablesResponse.model_validate(resp.json())
            yield from page.identifiers
            page_token = page.next_page_token
            if not page_token:
                return

    def drop(self, namespace: NamespaceLike, name: str) -> None:
        """Drop a generic table."""
        self._t.request("DELETE", self._table_path(namespace, name))
