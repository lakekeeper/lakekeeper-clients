"""Iceberg interop: a PyIceberg ``RestCatalog`` that shares this client's authentication.

Lakekeeper serves two surfaces from one server — generic tables under ``/lakekeeper/v1``
and the Iceberg REST catalog under ``/catalog``. A program that uses both (agent memory is
one: markdown objects in a ``dataset`` table, an index in an Iceberg table) otherwise
authenticates twice: once through this client's :class:`~pylakekeeper.auth.Auth` and again
through PyIceberg's own OAuth2 configuration, each refreshing on its own schedule.

:meth:`pylakekeeper.Client.iceberg_catalog` builds the catalog against the *same* ``Auth``,
so one login, one refresh, both surfaces::

    with Client(base_url=..., warehouse=..., auth=DeviceCodeFlow(...)) as client:
        catalog = client.iceberg_catalog()
        table = catalog.load_table("agent_memory.index")

Needs the ``[iceberg]`` extra::

    pip install 'pylakekeeper[iceberg]'
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.catalog.rest.auth import AuthManager

from .auth import Auth

if TYPE_CHECKING:  # pragma: no cover
    from .client import Client

#: Live ``Auth`` objects, keyed by an opaque handle.
#:
#: PyIceberg instantiates a custom ``AuthManager`` from a *string* class path with a
#: dict of constructor kwargs, so a live object cannot be handed over directly. The
#: handle travels through that config instead, and this table resolves it back. Entries
#: are kept for the process lifetime: a ``RestCatalog`` may rebuild its session, and a
#: resolve that fails later would surface as a confusing 401.
_AUTH_REGISTRY: dict[str, Auth] = {}

_MANAGER_PATH = "pylakekeeper.iceberg.SharedAuthManager"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def warehouse_name_for(client: Client, warehouse: str) -> str:
    """Resolve a warehouse identifier to the name the Iceberg REST surface expects.

    The two surfaces identify a warehouse differently: generic tables take the **UUID** as
    a URL path prefix, while the Iceberg catalog resolves ``?warehouse=`` by **name** and
    answers 404 for a UUID. A `Client` is configured with the UUID, so handing that
    straight to PyIceberg fails with `NoSuchWarehouseException` — papering over exactly
    this is the point of `Client.iceberg_catalog`.

    A value that is not a UUID is assumed to be a name already and passed through. A
    lookup that fails falls back to the original value rather than raising, so an
    unexpected deployment still gets PyIceberg's own error rather than ours.
    """
    if not _UUID_RE.match(warehouse):
        return warehouse
    try:
        resp = client._transport.request("GET", f"/management/v1/warehouse/{warehouse}")
        name = resp.json().get("name")
    except Exception:  # noqa: BLE001 - fall through to PyIceberg's error
        return warehouse
    return str(name) if name else warehouse


def register_auth(auth: Auth) -> str:
    """Register ``auth`` for use by :class:`SharedAuthManager`, returning its handle."""
    handle = uuid.uuid4().hex
    _AUTH_REGISTRY[handle] = auth
    return handle


class SharedAuthManager(AuthManager):
    """PyIceberg ``AuthManager`` delegating to a pylakekeeper :class:`~pylakekeeper.auth.Auth`.

    Both interfaces are ``auth_header() -> str | None``, so this forwards. The value is
    read per request rather than captured, which is what makes refresh work: when the
    underlying ``Auth`` renews its token, PyIceberg picks up the new one with no
    reconfiguration.
    """

    def __init__(self, auth_ref: str) -> None:
        try:
            self._auth = _AUTH_REGISTRY[auth_ref]
        except KeyError as exc:  # pragma: no cover - only on a handle from another process
            raise ValueError(
                "unknown pylakekeeper auth handle — build the catalog with "
                "Client.iceberg_catalog() rather than by passing auth properties yourself"
            ) from exc

    def auth_header(self) -> str | None:
        return self._auth.auth_header()


def iceberg_catalog(
    client: Client,
    *,
    name: str | None = None,
    warehouse: str | None = None,
    **properties: Any,
) -> RestCatalog:
    """Build a ``RestCatalog`` for ``client``'s warehouse, sharing its authentication.

    Args:
        client: the configured :class:`~pylakekeeper.Client`.
        name: catalog name; defaults to the warehouse identifier.
        warehouse: override the warehouse (name or UUID); defaults to the client's.
        **properties: extra PyIceberg catalog properties, merged last so a caller can
            override anything set here.

    Returns:
        A ``RestCatalog`` pointed at Lakekeeper's Iceberg REST endpoint.
    """
    target = warehouse or client.warehouse
    # The Iceberg surface wants the name, not the UUID the client is configured with.
    iceberg_warehouse = warehouse_name_for(client, target)
    handle = register_auth(client.auth)

    config: dict[str, Any] = {
        "uri": f"{client.base_url.rstrip('/')}/catalog/",
        "warehouse": iceberg_warehouse,
        # Route PyIceberg's auth through this client's Auth (see SharedAuthManager).
        "auth": {
            "type": "custom",
            "impl": _MANAGER_PATH,
            "custom": {"auth_ref": handle},
        },
    }
    if client.project_id:
        config["header.x-project-id"] = client.project_id
    config.update(properties)

    return RestCatalog(name=name or iceberg_warehouse, **config)
