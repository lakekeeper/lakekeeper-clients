"""Typed response models for the generic-tables API (pydantic).

Field names are snake_case; the wire format uses kebab-case (``base-location``,
``storage-credentials``, ``next-page-token``), handled by the alias generator. Unknown
fields are ignored so the models tolerate server additions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .lance import iceberg_creds_to_fsspec, iceberg_creds_to_lance


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
