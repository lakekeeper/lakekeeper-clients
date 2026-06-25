"""Official Lakekeeper client.

A small, standalone client for Lakekeeper's generic-tables API with built-in auth
(static token or client_credentials with automatic refresh). It is not a general
Iceberg REST client.

    from pylakekeeper import Client, ClientCredentials, StaticToken

    c = Client(base_url="http://localhost:8181", warehouse="demo",
               auth=ClientCredentials(token_url=..., client_id=..., client_secret=...))
    t = c.generic_tables.load("ai.test", "image_embeddings", vended=True)
    import lance
    ds = lance.dataset(t.location, storage_options=t.lance_storage_options)
"""

from __future__ import annotations

from .auth import Auth, ClientCredentials, StaticToken
from .client import Client
from .errors import (
    AuthError,
    ConfigError,
    ConflictError,
    LakekeeperError,
    LakekeeperHTTPError,
    NotFoundError,
)
from .formats import GenericTableFormat
from .lance import ICEBERG_TO_LANCE, iceberg_creds_to_lance
from .models import (
    GenericTableData,
    GenericTableIdentifier,
    ListGenericTablesResponse,
    LoadGenericTableResponse,
    StorageCredential,
)
from .url import encode_namespace, join_namespace, parse_namespace

__all__ = [
    # client + auth
    "Client",
    "Auth",
    "StaticToken",
    "ClientCredentials",
    # errors
    "LakekeeperError",
    "LakekeeperHTTPError",
    "NotFoundError",
    "ConflictError",
    "AuthError",
    "ConfigError",
    # models
    "LoadGenericTableResponse",
    "GenericTableData",
    "GenericTableIdentifier",
    "ListGenericTablesResponse",
    "StorageCredential",
    "GenericTableFormat",
    # helpers
    "ICEBERG_TO_LANCE",
    "iceberg_creds_to_lance",
    "encode_namespace",
    "join_namespace",
    "parse_namespace",
]
