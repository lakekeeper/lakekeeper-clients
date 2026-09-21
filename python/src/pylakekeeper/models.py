"""Typed response models for the generic-tables API (pydantic).

Field names are snake_case; the wire format uses kebab-case (``base-location``,
``storage-credentials``, ``next-page-token``), handled by the alias generator. Unknown
fields are ignored so the models tolerate server additions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict

from .lance import _collect_iceberg_props, iceberg_creds_to_fsspec, iceberg_creds_to_lance
from .objects import ObjectStore, object_store_for

#: Credential-expiry keys, most general first. All carry milliseconds since the epoch.
#: `expiration-time` is backend-agnostic (S3 and GCS both emit it); the rest are fallbacks.
_EXPIRY_KEYS = (
    "expiration-time",
    "s3.session-token-expires-at-ms",
    "gcs.oauth2.token-expires-at",
)

#: ADLS keys its SAS expiry per storage account, so it can only be matched by prefix.
_EXPIRY_KEY_PREFIX = "adls.sas-token-expires-at-ms."


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, populate_by_name=True, extra="ignore")


class StorageCredential(_WireModel):
    """A vended, prefix-scoped storage credential."""

    prefix: str
    config: dict[str, str] = {}


class GenericTableData(_WireModel):
    """The table metadata portion of a load/create response."""

    name: str
    format: str
    base_location: str
    doc: str | None = None
    properties: dict[str, str] = {}
    protected: bool = False


class LoadGenericTableResponse(_WireModel):
    """Response from create/load: table metadata plus (optional) vended credentials."""

    table: GenericTableData
    config: dict[str, str] | None = None
    storage_credentials: list[StorageCredential] | None = None

    @property
    def location(self) -> str:
        """The table's base location (e.g. ``s3://bucket/prefix``)."""
        return self.table.base_location

    @property
    def lance_storage_options(self) -> dict[str, str]:
        """Vended credentials translated to Lance ``storage_options``.

        Empty if the table was loaded without ``vended=True``.
        """
        creds = [c.model_dump() for c in (self.storage_credentials or [])]
        return iceberg_creds_to_lance(creds, self.config)

    @property
    def credentials(self) -> dict[str, str]:
        """Vended credentials and config merged into one flat dict of Iceberg-style keys.

        The raw material behind :attr:`lance_storage_options` and :attr:`expires_at`, for
        backends this client has no ready-made shape for. Empty without ``vended=True``.
        """
        creds = [c.model_dump() for c in (self.storage_credentials or [])]
        return _collect_iceberg_props(creds, self.config)

    @property
    def expires_at(self) -> datetime | None:
        """When the **vended storage credentials** expire, or ``None`` if not stated.

        Distinct from the OAuth2 token used to reach Lakekeeper, which the configured
        :class:`~pylakekeeper.auth.Auth` refreshes on its own. These are the short-lived
        S3/ADLS/GCS credentials in this response: once they lapse, reload the table.

        ``None`` means the server sent no expiry (it always does for vended S3, ADLS and
        GCS credentials) — not that the credentials last forever.
        """
        props = self.credentials
        raw: str | None = None
        for key in _EXPIRY_KEYS:
            if value := props.get(key):
                raw = value
                break
        else:
            for key, value in props.items():
                if key.startswith(_EXPIRY_KEY_PREFIX) and value:
                    raw = value
                    break
        if raw is None:
            return None
        try:
            ms = int(raw)
        except ValueError:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    def expires_within(self, seconds: float) -> bool:
        """Whether the vended credentials lapse within ``seconds`` from now.

        ``False`` when no expiry was stated, so an unknown expiry never forces a reload
        loop. Use it to decide whether to reload before a long write::

            if resp.expires_within(60):
                resp = client.generic_tables.load(ns, name, vended=True)
        """
        deadline = self.expires_at
        if deadline is None:
            return False
        return deadline <= datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)

    @property
    def is_expired(self) -> bool:
        """Whether the vended credentials have already lapsed (``False`` if unstated)."""
        return self.expires_within(0)

    def objects(self) -> ObjectStore:
        """Read and write objects under this table's location with the vended credentials.

        For ``dataset``-format tables, whose content is files rather than a columnar
        table. Keys are relative to the table's base location — no bucket, container or
        absolute URI reaches the caller::

            t = client.generic_tables.load("ai.memory", "agent_a", vended=True)
            store = t.objects()
            store.put("memories/preferences.md", body)
            store.list(prefix="memories/")

        The storage SDK is imported lazily; install the matching extra
        (``pylakekeeper[s3]``, ``[azure]`` or ``[gcs]``).

        Raises:
            ConfigError: unsupported scheme, missing SDK, or no vended credentials.
        """
        return object_store_for(self.location, self.credentials)

    @property
    def fsspec_kwargs(self) -> dict[str, object]:
        """Vended credentials translated to ``fsspec.filesystem("s3", **kwargs)``.

        Pass directly to ``fsspec.filesystem()``::

            resp = lk.generic_tables.load(ns, name, vended=True)
            fs = fsspec.filesystem("s3", **resp.fsspec_kwargs)
            files = fs.ls(resp.location)

        Empty if the table was loaded without ``vended=True``.
        """
        creds = [c.model_dump() for c in (self.storage_credentials or [])]
        return iceberg_creds_to_fsspec(creds, self.config)


class GenericTableIdentifier(_WireModel):
    """An entry in a list response."""

    namespace: list[str]
    name: str
    format: str | None = None
    id: str | None = None


class ListGenericTablesResponse(_WireModel):
    identifiers: list[GenericTableIdentifier] = []
    next_page_token: str | None = None
