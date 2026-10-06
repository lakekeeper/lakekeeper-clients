"""Typed models for the Datasets API (pydantic).

Same conventions as :mod:`pylakekeeper.models`: snake_case fields, kebab-case on the wire,
unknown fields ignored so the models tolerate server additions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .models import StorageCredential, _kebab

DatasetOwnership = Literal["managed", "imported"]
RefType = Literal["branch", "tag"]
AccessMode = Literal["storage-credentials", "presigned"]
ChangeKind = Literal["added", "removed", "modified"]
MaterializationStatus = Literal["unchecked", "fully-materialized", "partially-degraded"]
ImportMode = Literal["add-only", "sync"]
ConstraintViolationPolicy = Literal["reject", "skip"]


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, populate_by_name=True, extra="ignore")

    def wire(self) -> dict[str, Any]:
        """The request body: kebab-case keys, unset fields left out."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def _iso_duration(value: str | timedelta) -> str:
    """An ISO 8601 duration for the server, from a string or a ``timedelta``."""
    if isinstance(value, str):
        return value
    seconds = int(value.total_seconds())
    days, rest = divmod(seconds, 86400)
    if rest == 0:
        return f"P{days}D"
    return f"P{days}DT{rest}S" if days else f"PT{rest}S"


class Constraints(_Wire):
    """What a dataset admits: content types and a size bound, checked on commit."""

    allowed_content_types: list[str] | None = None
    max_file_size: int | None = None


class Retention(_Wire):
    """A dataset's retention policy. Build one with the constructors below.

    Durations are ISO 8601 strings (``"P7D"``) or ``timedelta``.
    """

    mode: Literal["inherit", "manual", "ttl", "max-count"]
    grace_period: str | None = None
    max_snapshot_age: str | None = None
    min_snapshots_to_keep: int | None = None
    max_snapshots: int | None = None

    @classmethod
    def inherit(cls) -> Retention:
        """Follow the warehouse's policy (drops the dataset's own)."""
        return cls(mode="inherit")

    @classmethod
    def manual(cls, *, grace_period: str | timedelta | None = None) -> Retention:
        """Expire nothing automatically; only :meth:`Dataset.expire` expires."""
        return cls(mode="manual", grace_period=_opt_duration(grace_period))

    @classmethod
    def ttl(
        cls,
        max_snapshot_age: str | timedelta,
        *,
        min_snapshots_to_keep: int | None = None,
        grace_period: str | timedelta | None = None,
    ) -> Retention:
        """Expire snapshots older than ``max_snapshot_age``, keeping the newest few."""
        return cls(
            mode="ttl",
            max_snapshot_age=_iso_duration(max_snapshot_age),
            min_snapshots_to_keep=min_snapshots_to_keep,
            grace_period=_opt_duration(grace_period),
        )

    @classmethod
    def max_count(
        cls, max_snapshots: int, *, grace_period: str | timedelta | None = None
    ) -> Retention:
        """Keep the newest ``max_snapshots`` of each branch, expire the rest."""
        return cls(
            mode="max-count", max_snapshots=max_snapshots, grace_period=_opt_duration(grace_period)
        )


def _opt_duration(value: str | timedelta | None) -> str | None:
    return None if value is None else _iso_duration(value)


class DatasetInfo(_Wire):
    """A dataset as the catalog describes it."""

    id: uuid.UUID
    name: str
    location: str
    ownership: DatasetOwnership
    protected: bool = False
    constraints: Constraints | None = None
    retention: Retention | None = None


class LoadDatasetResponse(_Wire):
    dataset: DatasetInfo


class DatasetIdentifier(_Wire):
    id: uuid.UUID
    name: str
    namespace: list[str]
    ownership: DatasetOwnership


class ListDatasetsResponse(_Wire):
    identifiers: list[DatasetIdentifier] = []
    next_page_token: str | None = None


class DatasetFile(_Wire):
    """One file of a snapshot.

    ``physical_path`` is where the bytes are: relative to the dataset's location, or a
    full URI inside it. ``version_id`` pins the object version a reader gets.
    """

    logical_key: str
    physical_path: str
    size: int | None = None
    etag: str | None = None
    content_type: str | None = None
    checksum: str | None = None
    version_id: str | None = None
    last_modified: datetime | None = None


class ListDatasetFilesResponse(_Wire):
    access_mode: AccessMode
    files: list[DatasetFile] = []
    next_page_token: str | None = None
    snapshot_id: uuid.UUID | None = None


class RefMaterialization(_Wire):
    status: MaterializationStatus
    checked_at: datetime
    missing_files: int
    changed_files: int


class DatasetRef(_Wire):
    """A branch or tag, and the snapshot it points at (``None`` before the first commit)."""

    name: str
    typ: RefType
    snapshot_id: uuid.UUID | None = None
    protected: bool = False
    materialization: RefMaterialization | None = None


class ListDatasetRefsResponse(_Wire):
    refs: list[DatasetRef] = []


class SkippedFile(_Wire):
    logical_key: str
    reason: str


class Snapshot(_Wire):
    """A published snapshot: what a commit or restore answers with."""

    snapshot_id: uuid.UUID
    parent_snapshot_id: uuid.UUID | None = None
    created_at: datetime
    location: str
    summary: dict[str, Any] | None = None
    #: Files the dataset's constraints left out of the commit, each with its reason.
    skipped: list[SkippedFile] = []


class CommitFile(_Wire):
    """A file to add to a snapshot. Only ``logical_key`` is required.

    ``physical_path`` defaults to the logical key: the object at that key below the
    dataset's location. Set it to name a file apart from where its bytes are.
    """

    logical_key: str
    physical_path: str | None = None
    size: int | None = None
    etag: str | None = None
    content_type: str | None = None
    checksum: str | None = None
    version_id: str | None = None
    last_modified: datetime | None = None


class FileChange(_Wire):
    """One difference between two versions, with the file as each side records it."""

    logical_key: str
    change: ChangeKind
    from_: DatasetFile | None = None
    to: DatasetFile | None = None

    model_config = ConfigDict(
        alias_generator=lambda name: "from" if name == "from_" else _kebab(name),
        populate_by_name=True,
        extra="ignore",
    )


class DiffDatasetResponse(_Wire):
    changes: list[FileChange] = []
    from_snapshot_id: uuid.UUID | None = None
    to_snapshot_id: uuid.UUID | None = None
    next_page_token: str | None = None


class MaterializationReport(_Wire):
    checked_snapshots: int
    degraded_snapshots: int


class ImportResult(_Wire):
    """What an import found and published.

    ``snapshot_id`` is the new head, or the unchanged head when nothing changed.
    """

    imported: int = 0
    modified: int = 0
    removed: int = 0
    skipped: int = 0
    skipped_files: list[SkippedFile] = []
    truncated: bool = False
    snapshot_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    materialization: MaterializationReport | None = None


class AccessGrant(_Wire):
    grant_id: uuid.UUID
    snapshot_id: uuid.UUID
    access_mode: AccessMode
    expires_at: datetime
    max_keys_per_request: int
    url_validity_seconds: int
    content_type: str | None = None


class SignedFile(_Wire):
    logical_key: str
    url: str
    expires_at: datetime
    etag: str | None = None


class SignDatasetFilesResponse(_Wire):
    files: list[SignedFile] = []


class LoadDatasetCredentialsResponse(_Wire):
    storage_credentials: list[StorageCredential] = []
    #: A fresh folder below the location for a caller who may commit; its credential
    #: writes there and nowhere else.
    write_prefix: str | None = None


class DegradedFile(_Wire):
    logical_key: str
    physical_path: str
    problem: Literal["missing", "changed"]


class SnapshotMaterialization(_Wire):
    """Whether a snapshot's files were still in storage when an import last checked."""

    status: MaterializationStatus
    checked_at: datetime | None = None
    missing_files: int | None = None
    changed_files: int | None = None
    files: list[DegradedFile] = []
    next_page_token: str | None = None


class ExpireResult(_Wire):
    snapshot_id: uuid.UUID
    #: The earliest a purge deletes the snapshot; :meth:`Dataset.restore` works until then.
    purge_after: datetime


class ImportTaskStatus(_Wire):
    """A queued import's task, as the management API reports it."""

    task_id: uuid.UUID
    status: Literal["RUNNING", "SCHEDULED", "STOPPING", "CANCELLED", "SUCCESS", "FAILED"]
    progress: float = 0.0
    message: str | None = None
    execution_details: dict[str, Any] | None = None

    @property
    def done(self) -> bool:
        return self.status in ("SUCCESS", "FAILED", "CANCELLED")

    @property
    def phase(self) -> str | None:
        """``listing``, ``merging``, ``publishing``, ``checking`` or ``done``."""
        phase = (self.execution_details or {}).get("phase")
        return phase if isinstance(phase, str) else None

    @property
    def objects_listed(self) -> int:
        value = (self.execution_details or {}).get("objects_listed", 0)
        return int(value) if isinstance(value, int) else 0
