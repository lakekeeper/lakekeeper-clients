"""Datasets: versioned collections of files.

A dataset's every change is an immutable snapshot. Branches move as you commit; tags pin a
snapshot for good, so a tag cut for a training run reads the same files next year. The
catalog stores only the file list; the bytes stay in object storage.

::

    ds = client.datasets.create("ml", "images")

    with ds.branch("main").commit(summary={"run": "4711"}) as c:
        c.upload_dir("local/train", prefix="train/")    # writes the bytes, records them
        c.remove("train/stale.jpg")

    ds.create_tag("train-2026-10", from_="main")

    v1 = ds.tag("train-2026-10")
    for f in v1.files(prefix="train/"):
        ...
    img = v1.read("train/0001.jpg")                     # checked against the snapshot
    v1.download("data/")                                # parallel, skips what it has

Reads go through signed URLs, which work for every dataset and storage backend and need no
storage SDK. Writes go through the credentials the catalog vends for a managed dataset's
prefix, so they need the backend's extra (``pylakekeeper[s3]`` and friends).

Uploads never overwrite. The catalog gives each commit a fresh folder below the
location, ``data/<id>/``, with a credential that writes there and nowhere else; the
commit records each file's logical key apart from that path. An object a snapshot
records is therefore never written again: a tag reads the same bytes on any bucket,
versioned or not, and two writers of the same key cannot touch each other's bytes. A
commit deletes what it uploaded and did not publish.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from .dataset_models import (
    AccessGrant,
    CommitFile,
    Constraints,
    ConstraintViolationPolicy,
    DatasetFile,
    DatasetIdentifier,
    DatasetInfo,
    DatasetRef,
    DiffDatasetResponse,
    ExpireResult,
    FileChange,
    ImportMode,
    ImportResult,
    ImportTaskStatus,
    ListDatasetFilesResponse,
    ListDatasetRefsResponse,
    ListDatasetsResponse,
    LoadDatasetCredentialsResponse,
    LoadDatasetResponse,
    Retention,
    SignDatasetFilesResponse,
    SignedFile,
    Snapshot,
    SnapshotMaterialization,
)
from .errors import (
    CommitConflictError,
    ConfigError,
    ConflictError,
    IntegrityError,
    LakekeeperError,
    LakekeeperHTTPError,
)
from .lance import _collect_iceberg_props, iceberg_creds_to_fsspec, iceberg_creds_to_lance
from .models import credential_expiry
from .objects import ObjectStore, _guess_content_type, object_store_for
from .transport import Transport
from .url import NamespaceLike, encode_namespace, parse_namespace

_VENDED_HEADER = {"X-Iceberg-Access-Delegation": "vended-credentials"}
_IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Reload vended credentials, and take a new access grant, this close to their expiry.
REFRESH_MARGIN_SECONDS = 60.0

#: How often a commit rebases onto a branch that keeps moving before giving up.
MAX_REBASES = 10

#: How often a request carrying an idempotency key is resent after a network failure.
#: Safe: the server answers a replay with what the first attempt did.
MAX_RESENDS = 3

#: Statuses worth resending a keyed request on. The request may or may not have landed.
_RESEND_STATUSES = frozenset({502, 503, 504})

#: A downloaded directory records what it holds here, so the next download skips it.
DOWNLOAD_MANIFEST = ".pylakekeeper-download.json"

_log = logging.getLogger(__name__)

SnapshotId = uuid.UUID
#: A version to read or point a ref at: a ref name, or a snapshot id.
Revision = str | uuid.UUID


class Datasets:
    """Datasets in one warehouse: create, load, list, rename and drop."""

    def __init__(
        self,
        transport: Transport,
        warehouse: str,
        storage_overrides: Mapping[str, str] | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._t = transport
        self._warehouse = warehouse
        self._storage_overrides = dict(storage_overrides) if storage_overrides else {}
        # Signed URLs go straight to object storage. A client of their own, so the
        # catalog's bearer token never reaches the object store.
        self._storage_http = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._storage_http.close()

    def _collection_path(self, namespace: NamespaceLike) -> str:
        return (
            f"/lakekeeper/v1/{quote(self._warehouse, safe='')}"
            f"/namespaces/{encode_namespace(namespace)}/datasets"
        )

    def _dataset_path(self, namespace: NamespaceLike, name: str) -> str:
        return f"{self._collection_path(namespace)}/{quote(name, safe='')}"

    def create(
        self,
        namespace: NamespaceLike,
        name: str,
        *,
        location: str | None = None,
        constraints: Constraints | None = None,
    ) -> Dataset:
        """Create a dataset.

        Without ``location`` the dataset is **managed**: the catalog allocates a prefix,
        and :meth:`Branch.commit` writes into it. With ``location`` it is **imported**: it
        borrows a prefix that already holds objects, which :meth:`Dataset.sync` registers
        without copying them. The catalog never writes or deletes objects there.
        """
        body: dict[str, Any] = {"name": name}
        if location is not None:
            body["location"] = location
        if constraints is not None:
            body["constraints"] = constraints.wire()
        resp = self._t.request("POST", self._collection_path(namespace), json=body)
        info = LoadDatasetResponse.model_validate(resp.json()).dataset
        return Dataset(self, parse_namespace(namespace), info)

    def load(self, namespace: NamespaceLike, name: str) -> Dataset:
        """Load a dataset. Raises :class:`~pylakekeeper.NotFoundError` if it is absent."""
        resp = self._t.request("GET", self._dataset_path(namespace, name))
        info = LoadDatasetResponse.model_validate(resp.json()).dataset
        return Dataset(self, parse_namespace(namespace), info)

    def list(
        self, namespace: NamespaceLike, *, page_size: int = 100
    ) -> Iterator[DatasetIdentifier]:
        """Iterate every dataset in a namespace."""
        path = self._collection_path(namespace)
        params: dict[str, Any] = {"pageSize": page_size}
        while True:
            page = ListDatasetsResponse.model_validate(
                self._t.request("GET", path, params=params).json()
            )
            yield from page.identifiers
            if not page.next_page_token:
                return
            params["pageToken"] = page.next_page_token

    def rename(
        self,
        namespace: NamespaceLike,
        name: str,
        *,
        to_name: str | None = None,
        to_namespace: NamespaceLike | None = None,
    ) -> Dataset:
        """Rename a dataset, optionally into another namespace. Returns it under its new name."""
        dest_ns = parse_namespace(to_namespace if to_namespace is not None else namespace)
        dest_name = to_name or name
        self._t.request(
            "POST",
            f"/lakekeeper/v1/{quote(self._warehouse, safe='')}/datasets/rename",
            json={
                "source": {"namespace": list(parse_namespace(namespace)), "name": name},
                "destination": {"namespace": list(dest_ns), "name": dest_name},
            },
        )
        return self.load(dest_ns, dest_name)

    def drop(
        self, namespace: NamespaceLike, name: str, *, purge: bool = False, force: bool = False
    ) -> None:
        """Drop a dataset. ``force`` drops it although it is protected."""
        params = {"purgeRequested": str(purge).lower(), "force": str(force).lower()}
        self._t.request("DELETE", self._dataset_path(namespace, name), params=params)

    def _keyed(self, method: str, path: str, *, json: Any, key: str) -> httpx.Response:
        """Send a request carrying an idempotency key, resending it on network failure."""
        headers = {_IDEMPOTENCY_HEADER: key}
        for attempt in range(MAX_RESENDS + 1):
            try:
                resp = self._t.request(method, path, json=json, headers=headers)
            except httpx.TransportError:
                if attempt == MAX_RESENDS:
                    raise
                continue
            except LakekeeperHTTPError as exc:
                if exc.status_code in _RESEND_STATUSES and attempt < MAX_RESENDS:
                    continue
                raise
            return resp
        raise AssertionError("unreachable")  # pragma: no cover


class Dataset:
    """One dataset: its refs, versions, imports and retention.

    Obtain one from :meth:`Datasets.create` or :meth:`Datasets.load`. ``info`` is the
    state the catalog returned then; :meth:`reload` fetches it again.
    """

    def __init__(self, api: Datasets, namespace: tuple[str, ...], info: DatasetInfo) -> None:
        self._api = api
        self.namespace = namespace
        self.info = info
        self._credentials: DatasetCredentials | None = None

    def __repr__(self) -> str:
        return f"Dataset({'.'.join(self.namespace)}.{self.name}, {self.info.ownership})"

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def id(self) -> uuid.UUID:
        return self.info.id

    @property
    def location(self) -> str:
        return self.info.location

    @property
    def managed(self) -> bool:
        """Whether the catalog allocated this dataset's prefix (and vends credentials for it)."""
        return self.info.ownership == "managed"

    @property
    def _path(self) -> str:
        return self._api._dataset_path(self.namespace, self.name)

    @property
    def _t(self) -> Transport:
        return self._api._t

    def reload(self) -> Dataset:
        """Fetch the dataset's current state into :attr:`info`."""
        self.info = self._api.load(self.namespace, self.name).info
        return self

    # -- refs ------------------------------------------------------------------------

    def refs(self) -> builtins.list[DatasetRef]:
        """Every branch and tag, with the snapshot each points at."""
        resp = self._t.request("GET", f"{self._path}/refs")
        return ListDatasetRefsResponse.model_validate(resp.json()).refs

    def ref(self, name: str) -> Ref:
        """A handle on a branch or tag. Nothing is fetched until it is read."""
        return Ref(self, name)

    def branch(self, name: str = "main") -> Branch:
        """A handle on a branch, for committing to it and moving it."""
        return Branch(self, name)

    def tag(self, name: str) -> Tag:
        """A handle on a tag: every read of it reads the same snapshot."""
        return Tag(self, name)

    def create_branch(self, name: str, from_: Revision = "main") -> Branch:
        """Create a branch at a ref's current snapshot, or at a snapshot id."""
        self._create_ref(name, "branch", from_)
        return Branch(self, name)

    def create_tag(self, name: str, from_: Revision = "main") -> Tag:
        """Create a tag: it points at that snapshot for good, and retention keeps it."""
        self._create_ref(name, "tag", from_)
        return Tag(self, name)

    def _create_ref(self, name: str, typ: str, from_: Revision) -> DatasetRef:
        source: dict[str, Any] = (
            {"type": "snapshot", "snapshot-id": str(from_)}
            if isinstance(from_, uuid.UUID)
            else {"type": "ref", "name": from_}
        )
        resp = self._api._keyed(
            "POST",
            f"{self._path}/refs",
            json={"name": name, "typ": typ, "source": source},
            key=str(uuid.uuid4()),
        )
        return DatasetRef.model_validate(resp.json())

    def _ref_info(self, name: str) -> DatasetRef:
        for ref in self.refs():
            if ref.name == name:
                return ref
        raise LakekeeperError(f"dataset {self.name!r} has no ref {name!r}")

    def _resolve(self, revision: Revision) -> SnapshotId:
        if isinstance(revision, uuid.UUID):
            return revision
        snapshot_id = self._ref_info(revision).snapshot_id
        if snapshot_id is None:
            raise LakekeeperError(f"ref {revision!r} has no commits yet")
        return snapshot_id

    # -- versions --------------------------------------------------------------------

    def diff(
        self, from_: Revision, to: Revision, *, page_size: int | None = None
    ) -> Iterator[FileChange]:
        """The files that differ between two versions, in key order.

        Each side is a ref name or a snapshot id. ``added`` is a file only ``to`` has,
        ``removed`` one only ``from_`` has, ``modified`` one both record differently.
        """
        params: dict[str, Any] = {}
        params["fromSnapshotId" if isinstance(from_, uuid.UUID) else "from"] = str(from_)
        params["toSnapshotId" if isinstance(to, uuid.UUID) else "to"] = str(to)
        if page_size is not None:
            params["pageSize"] = page_size
        # Pages can be short or empty while changes remain: only a missing token ends it.
        while True:
            page = DiffDatasetResponse.model_validate(
                self._t.request("GET", f"{self._path}/diff", params=params).json()
            )
            yield from page.changes
            if not page.next_page_token:
                return
            params["pageToken"] = page.next_page_token

    # -- importing objects already in storage ----------------------------------------

    def import_objects(
        self,
        *,
        mode: ImportMode = "add-only",
        branch: str = "main",
        sub_prefix: str | None = None,
        suffix: str | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        default_excludes: bool | None = None,
        record_versions: bool | None = None,
        check_materialization: bool | None = None,
        max_files: int | None = None,
        on_constraint_violation: ConstraintViolationPolicy | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> ImportResult:
        """List the dataset's prefix and commit what it finds, without copying anything.

        ``add-only`` registers objects under keys the dataset does not have yet. ``sync``
        (see :meth:`sync`) makes the branch mirror the prefix. Runs inside the request,
        so it suits prefixes that list in seconds; use :meth:`queue_import` for larger.
        """
        body = _import_body(locals())
        resp = self._t.request("POST", f"{self._path}/import", json=body)
        return ImportResult.model_validate(resp.json())

    def sync(
        self,
        *,
        branch: str = "main",
        sub_prefix: str | None = None,
        suffix: str | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        default_excludes: bool | None = None,
        record_versions: bool | None = None,
        check_materialization: bool | None = None,
        on_constraint_violation: ConstraintViolationPolicy | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> ImportResult:
        """Make the branch mirror the prefix: new objects added, changed ones modified,
        vanished ones removed. Publishes nothing when nothing changed."""
        return self.import_objects(
            mode="sync",
            branch=branch,
            sub_prefix=sub_prefix,
            suffix=suffix,
            include=include,
            exclude=exclude,
            default_excludes=default_excludes,
            record_versions=record_versions,
            check_materialization=check_materialization,
            on_constraint_violation=on_constraint_violation,
            summary=summary,
        )

    def queue_import(
        self,
        *,
        mode: ImportMode = "add-only",
        branch: str = "main",
        sub_prefix: str | None = None,
        suffix: str | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        default_excludes: bool | None = None,
        record_versions: bool | None = None,
        check_materialization: bool | None = None,
        max_files: int | None = None,
        on_constraint_violation: ConstraintViolationPolicy | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> ImportTask:
        """Run an import on the server's task queue, for prefixes too large for a request.

        Returns at once; :meth:`ImportTask.wait` follows it to the end. At most one queued
        import per dataset runs at a time; queuing a second raises
        :class:`~pylakekeeper.ConflictError`.
        """
        body = _import_body(locals())
        body["queued"] = True
        resp = self._t.request("POST", f"{self._path}/import", json=body)
        task_id = ImportResult.model_validate(resp.json()).task_id
        if task_id is None:  # pragma: no cover - the server always returns one when queued
            raise LakekeeperError("the server queued no task for the import")
        return ImportTask(self._api, task_id)

    def materialization(self, snapshot: Revision) -> SnapshotMaterialization:
        """Whether a snapshot's files were still in storage when an import last checked.

        ``status`` is ``unchecked`` until an import ran with ``check_materialization``.
        ``files`` lists the missing and changed files (the server keeps the first 10,000).
        """
        snapshot_id = self._resolve(snapshot)
        path = f"{self._path}/snapshots/{snapshot_id}/materialization"
        params: dict[str, Any] = {}
        report: SnapshotMaterialization | None = None
        while True:
            page = SnapshotMaterialization.model_validate(
                self._t.request("GET", path, params=params).json()
            )
            if report is None:
                report = page
            else:
                report.files.extend(page.files)
            if not page.next_page_token:
                report.next_page_token = None
                return report
            params["pageToken"] = page.next_page_token

    # -- settings and retention ------------------------------------------------------

    def update_settings(
        self,
        *,
        constraints: Constraints | None = None,
        retention: Retention | None = None,
    ) -> DatasetInfo:
        """Replace the dataset's constraints and/or retention policy.

        ``Constraints()`` lifts every constraint; ``Retention.inherit()`` drops the
        dataset's own policy. An argument left out is left as it is.
        """
        body: dict[str, Any] = {}
        if constraints is not None:
            body["constraints"] = constraints.wire()
        if retention is not None:
            body["retention"] = retention.wire()
        resp = self._t.request("POST", f"{self._path}/settings", json=body)
        self.info = LoadDatasetResponse.model_validate(resp.json()).dataset
        return self.info

    def expire(self, snapshot: Revision) -> ExpireResult:
        """Expire one snapshot now. It stays restorable until ``purge_after``.

        A snapshot a ref points at, or that a live access grant reads, is refused with
        :class:`~pylakekeeper.ConflictError` (``error_type == "DatasetSnapshotHeld"``).
        """
        snapshot_id = self._resolve(snapshot)
        resp = self._t.request("POST", f"{self._path}/snapshots/{snapshot_id}/expire")
        return ExpireResult.model_validate(resp.json())

    def restore(self, snapshot_id: SnapshotId) -> Snapshot:
        """Bring back an expired snapshot before its grace period ends.

        Create a ref to it to keep it, or a later retention pass may expire it again.
        """
        resp = self._t.request("POST", f"{self._path}/snapshots/{snapshot_id}/restore")
        return Snapshot.model_validate(resp.json())

    # -- storage -------------------------------------------------------------------

    def credentials(self) -> DatasetCredentials:
        """Storage credentials, for engines that read the dataset's prefix directly.

        Cached, and fetched again as they near expiry. An imported dataset has none: read
        it with :meth:`Ref.read`.
        """
        cached = self._credentials
        if cached is None or cached.expires_within(REFRESH_MARGIN_SECONDS):
            cached = self._fetch_credentials()
            self._credentials = cached
        return cached

    def _fetch_credentials(self) -> DatasetCredentials:
        try:
            resp = self._t.request("GET", f"{self._path}/credentials", headers=_VENDED_HEADER)
        except ConflictError as exc:
            if exc.error_type == "CannotVendCredentialsForImportedDataset":
                raise ConfigError(
                    f"dataset {self.name!r} is imported: the catalog vends no credentials "
                    "for a prefix it borrows. Write its objects with your own credentials, "
                    "then register them with Dataset.sync() or Commit.add()."
                ) from exc
            raise
        return DatasetCredentials(
            self.location,
            LoadDatasetCredentialsResponse.model_validate(resp.json()),
            self._api._storage_overrides,
        )


class Ref:
    """A branch or tag to read.

    Every call reads the ref as it stands when the call starts, and reads one snapshot
    throughout: a :meth:`download` never mixes two versions. A branch moves on between
    calls; read a :class:`Tag` for a version that never does.
    """

    #: Whether one access grant serves every call. Only a tag's snapshot never changes.
    _keeps_grant = False

    def __init__(self, dataset: Dataset, name: str) -> None:
        self.dataset = dataset
        self.name = name
        self._grant: AccessGrant | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.dataset.name}@{self.name})"

    @property
    def _api(self) -> Datasets:
        return self.dataset._api

    @property
    def _path(self) -> str:
        return f"{self.dataset._path}/refs/{quote(self.name, safe='')}"

    def info(self) -> DatasetRef:
        """The ref as it stands: type, snapshot, protection, materialization."""
        return self.dataset._ref_info(self.name)

    def head(self) -> SnapshotId | None:
        """The snapshot the ref points at now (``None`` before the first commit)."""
        return self.info().snapshot_id

    def delete(self) -> None:
        """Delete the ref. Its snapshots stay until retention expires them."""
        self._api._keyed("DELETE", self._path, json=None, key=str(uuid.uuid4()))

    # -- listing ---------------------------------------------------------------------

    def files(
        self,
        *,
        prefix: str = "",
        content_type: str | None = None,
        page_size: int = 1000,
    ) -> Iterator[DatasetFile]:
        """Iterate the files of the snapshot the ref points at, in key order.

        ``prefix`` narrows by logical key. Keys come back sorted, so the walk stops once it
        passes the prefix, but it still starts at the first key.
        """
        for _, page in self._pages(prefix, content_type, page_size):
            yield from page

    def _pages(
        self, prefix: str, content_type: str | None, page_size: int
    ) -> Iterator[tuple[SnapshotId | None, builtins.list[DatasetFile]]]:
        """Pages of matching files, each with the snapshot they belong to."""
        params: dict[str, Any] = {"pageSize": page_size}
        if content_type is not None:
            params["contentType"] = content_type
        while True:
            page = ListDatasetFilesResponse.model_validate(
                self._api._t.request("GET", f"{self._path}/files", params=params).json()
            )
            # Every page after the first reads the snapshot the first one resolved.
            yield page.snapshot_id, [f for f in page.files if f.logical_key.startswith(prefix)]
            # Keys are sorted: once the last one is past the prefix, nothing later matches.
            last = page.files[-1].logical_key if page.files else ""
            if prefix and last > prefix and not last.startswith(prefix):
                return
            # A page can be short or empty while files remain: only a missing token ends.
            if not page.next_page_token:
                return
            params["pageToken"] = page.next_page_token

    # -- reading ---------------------------------------------------------------------

    def _access_grant(self) -> AccessGrant:
        """A grant on the snapshot the ref points at now; a tag reuses its own."""
        grant = self._grant
        now = datetime.now(tz=timezone.utc)
        expiring = grant is not None and grant.expires_at <= now + timedelta(
            seconds=REFRESH_MARGIN_SECONDS
        )
        if grant is None or expiring or not self._keeps_grant:
            resp = self._api._keyed(
                "POST", f"{self._path}/access-grants", json={}, key=str(uuid.uuid4())
            )
            grant = AccessGrant.model_validate(resp.json())
            self._grant = grant
        return grant

    def sign(self, keys: Sequence[str]) -> builtins.list[SignedFile]:
        """Signed GET URLs for files of the snapshot the ref points at, in the order asked.

        A URL needs no other credential, so hand it to any HTTP reader. It reads the bytes
        the snapshot recorded where the file pins a version, and expires in minutes.
        """
        return self._sign(self._access_grant(), keys)

    def _sign(self, grant: AccessGrant, keys: Sequence[str]) -> builtins.list[SignedFile]:
        signed: builtins.list[SignedFile] = []
        path = f"{self.dataset._path}/snapshots/{grant.snapshot_id}/files/sign"
        for start in range(0, len(keys), grant.max_keys_per_request):
            batch = list(keys[start : start + grant.max_keys_per_request])
            resp = self._api._t.request(
                "POST", path, json={"grant-id": str(grant.grant_id), "keys": batch}
            )
            signed.extend(SignDatasetFilesResponse.model_validate(resp.json()).files)
        return signed

    def url(self, key: str) -> str:
        """A signed GET URL for one file."""
        return self.sign([key])[0].url

    def read(self, key: str) -> bytes:
        """The bytes of one file, checked against what the snapshot recorded."""
        return self._fetch(self.sign([key])[0])

    def _fetch(self, signed: SignedFile) -> bytes:
        resp = self._api._storage_http.get(signed.url)
        if resp.status_code >= 400:
            raise LakekeeperError(
                f"reading {signed.logical_key!r} from storage failed: HTTP {resp.status_code}"
            )
        if signed.etag is not None:
            got = resp.headers.get("etag")
            if got is not None and _bare_etag(got) != _bare_etag(signed.etag):
                raise IntegrityError(
                    f"{signed.logical_key!r} is not the file the snapshot recorded: storage "
                    f"served etag {got}, the snapshot recorded {signed.etag}. The object was "
                    "written again since the commit; pin files to object versions to keep "
                    "reading the recorded bytes."
                )
        return resp.content

    def download(
        self,
        dest: str | Path,
        *,
        prefix: str = "",
        content_type: str | None = None,
        workers: int = 8,
        on_file: Callable[[DatasetFile], None] | None = None,
    ) -> DownloadResult:
        """Copy the files of the snapshot the ref points at into ``dest``, at their keys.

        Files the directory already holds from an earlier download, recorded with the same
        etag and version, are skipped, so downloading another version of the dataset
        fetches only what differs. ``on_file`` is called after each file is written.
        """
        root = Path(dest).resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / DOWNLOAD_MANIFEST
        have = _read_manifest(manifest_path)

        # The listing and the grant must name the same snapshot; a branch that moves
        # between the two is simply read again.
        for _ in range(MAX_REBASES):
            pages = list(self._pages(prefix, content_type, 1000))
            snapshot_id = pages[0][0] if pages else None
            files = [f for _, page in pages for f in page]
            if snapshot_id is None:
                return DownloadResult(snapshot_id=None, path=root, downloaded=0, skipped=0)
            grant = self._access_grant()
            if grant.snapshot_id == snapshot_id:
                break
            self._grant = None
        else:
            raise LakekeeperError(f"{self.name!r} kept moving while being downloaded")

        wanted = {f.logical_key: _fingerprint(f) for f in files}
        todo = [
            f
            for f in files
            if have.get(f.logical_key) != wanted[f.logical_key]
            or not _target(root, f.logical_key).exists()
        ]

        def fetch(signed: SignedFile, file: DatasetFile) -> None:
            data = self._fetch(signed)
            target = _target(root, file.logical_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            if on_file is not None:
                on_file(file)

        # Signed in batches just ahead of the fetches, so no URL outlives its validity.
        batch = 256
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(todo), batch):
                chunk = todo[start : start + batch]
                signed = self._sign(grant, [f.logical_key for f in chunk])
                list(pool.map(fetch, signed, chunk))

        _write_manifest(manifest_path, snapshot_id, wanted)
        return DownloadResult(
            snapshot_id=snapshot_id,
            path=root,
            downloaded=len(todo),
            skipped=len(files) - len(todo),
        )


class Tag(Ref):
    """A tag: a ref that always points at the same snapshot."""

    _keeps_grant = True


class Branch(Ref):
    """A branch: a ref that moves as you commit to it."""

    def commit(
        self,
        *,
        summary: Mapping[str, Any] | None = None,
        on_constraint_violation: ConstraintViolationPolicy | None = None,
        rebase: bool = True,
        workers: int = 8,
    ) -> Commit:
        """Start a commit. Use it as a context manager; it commits on a clean exit::

            with ds.branch("main").commit(summary={"run": run_id}) as c:
                c.put("labels.json", payload)
                c.upload_dir("images/", prefix="images/")

        The commit is based on the branch's head now. If another writer commits first and
        ``rebase`` is set, it is replayed onto their snapshot — unless their commits
        touched a key this one adds or removes, which raises
        :class:`~pylakekeeper.CommitConflictError`.
        """
        return Commit(
            self,
            summary=summary,
            on_constraint_violation=on_constraint_violation,
            rebase=rebase,
            workers=workers,
        )

    def commit_files(
        self,
        added: Iterable[CommitFile | str] = (),
        removed: Iterable[str] = (),
        *,
        summary: Mapping[str, Any] | None = None,
        on_constraint_violation: ConstraintViolationPolicy | None = None,
        rebase: bool = True,
    ) -> Snapshot:
        """Commit files whose bytes are already in storage, in one call.

        A ``str`` is a logical key whose object sits at that key below the location.
        """
        c = self.commit(
            summary=summary, on_constraint_violation=on_constraint_violation, rebase=rebase
        )
        for f in added:
            c.add(f)
        for key in removed:
            c.remove(key)
        return c.commit()

    def fast_forward(self, to: Revision) -> DatasetRef:
        """Move the branch forward to a snapshot that descends from its head.

        The publish step of write-audit-publish, and the only way changes reach a
        protected branch. A target that does not descend is refused with
        :class:`~pylakekeeper.ConflictError`.
        """
        return self._move(to, fast_forward=True)

    def reset(self, to: Revision) -> DatasetRef:
        """Move the branch anywhere, abandoning the commits it leaves. Needs ``manage_refs``."""
        return self._move(to, fast_forward=False)

    def _move(self, to: Revision, *, fast_forward: bool) -> DatasetRef:
        target = self.dataset._resolve(to)
        body: dict[str, Any] = {"snapshot-id": str(target), "fast-forward": fast_forward}
        if (expected := self.head()) is not None:
            body["expected-snapshot-id"] = str(expected)
        resp = self._api._keyed("POST", self._path, json=body, key=str(uuid.uuid4()))
        return DatasetRef.model_validate(resp.json())

    def protect(self, protected: bool = True) -> DatasetRef:
        """Refuse direct commits, resets and deletion; only fast-forwards reach it then."""
        resp = self._api._t.request(
            "POST", f"{self._path}/protection", json={"protected": protected}
        )
        return DatasetRef.model_validate(resp.json())


class Commit:
    """Files to add and keys to remove, published as one snapshot.

    Built by :meth:`Branch.commit`. :meth:`put`, :meth:`put_file` and :meth:`upload_dir`
    write bytes into the fresh folder the catalog names for this commit, and record what
    storage reported (size, etag, version); :meth:`add` records an object already there.

    What the commit uploaded and does not publish — because the server refused it, the
    ``with`` block raised, or the constraints skipped a file — it deletes again. A commit
    whose outcome is unknown, after a network failure, keeps its uploads: it may have been
    published, and calling :meth:`commit` again finds out.
    """

    def __init__(
        self,
        branch: Branch,
        *,
        summary: Mapping[str, Any] | None,
        on_constraint_violation: ConstraintViolationPolicy | None,
        rebase: bool,
        workers: int,
    ) -> None:
        self.branch = branch
        self.summary = dict(summary) if summary else None
        self.on_constraint_violation = on_constraint_violation
        self.rebase = rebase
        self.workers = workers
        self._parent = branch.head()
        self._added: dict[str, CommitFile] = {}
        self._removed: dict[str, None] = {}
        self._lock = threading.Lock()
        self._key = str(uuid.uuid4())
        #: The folders this commit writes to, relative to the location, each with the
        #: store whose credential covers it. One, unless the credential was renewed.
        self._folders: dict[str, ObjectStore] = {}
        self._credentials: DatasetCredentials | None = None
        #: Objects this commit wrote: logical key -> physical path.
        self._uploaded: dict[str, str] = {}
        #: Set once a send failed with no answer: the commit may have been published.
        self._outcome_unknown = False
        #: The published snapshot, once :meth:`commit` has run.
        self.snapshot: Snapshot | None = None

    def __enter__(self) -> Commit:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.snapshot is not None:
            return
        if exc_type is None:
            self.commit()
        elif not self._outcome_unknown:
            self._discard(self._uploaded.values())

    @property
    def parent_snapshot_id(self) -> SnapshotId | None:
        """The snapshot this commit is based on (``None`` for a branch's first commit)."""
        return self._parent

    @property
    def added(self) -> builtins.list[CommitFile]:
        return list(self._added.values())

    @property
    def removed(self) -> builtins.list[str]:
        return list(self._removed)

    def _folder(self) -> tuple[str, ObjectStore]:
        """The folder this commit writes to, relative to the location, and its store.

        A commit holds credentials of its own, so two commits never share a folder.
        """
        with self._lock:
            creds = self._credentials
            if creds is None or creds.expires_within(REFRESH_MARGIN_SECONDS):
                dataset = self.branch.dataset
                creds = dataset._fetch_credentials()
                prefix = creds.write_prefix
                if prefix is None:
                    raise ConfigError(
                        f"the catalog named no write folder for {dataset.name!r}: "
                        "the caller may not commit to it"
                    )
                base = dataset.location.rstrip("/") + "/"
                if not prefix.startswith(base):
                    raise LakekeeperError(f"write folder {prefix} is not below {base}")
                self._folders[prefix[len(base) :].strip("/")] = creds.write_objects()
                self._credentials = creds
            folder = next(reversed(self._folders))
            return folder, self._folders[folder]

    def add(self, file: CommitFile | str, **fields: Any) -> CommitFile:
        """Record a file whose bytes are already in storage.

        ``add("a.jpg")`` names the object at ``a.jpg`` below the location;
        ``add("train/cat.jpg", physical_path="raw/0001.jpg", size=..., etag=...)`` names
        it apart from where its bytes are. Adding a key the branch has replaces its file.
        """
        entry = file if isinstance(file, CommitFile) else CommitFile(logical_key=file, **fields)
        with self._lock:
            self._removed.pop(entry.logical_key, None)
            self._added[entry.logical_key] = entry
        return entry

    def remove(self, key: str) -> None:
        """Remove a file from the branch. Bytes an earlier snapshot records stay in storage."""
        with self._lock:
            self._added.pop(key, None)
            self._removed[key] = None

    def _target(self, key: str) -> tuple[str, ObjectStore]:
        folder, store = self._folder()
        physical = f"{folder}/{key}"
        with self._lock:
            self._uploaded[key] = physical
        return physical, store

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> CommitFile:
        """Upload ``data`` as the file ``key`` and record it."""
        content_type = content_type or _guess_content_type(key)
        physical, store = self._target(key)
        stored = store.put(key, data, content_type=content_type)
        return self.add(
            CommitFile(
                logical_key=key,
                physical_path=physical,
                size=len(data),
                etag=stored.etag,
                version_id=stored.version_id,
                content_type=content_type,
            )
        )

    def put_file(
        self, key: str, path: str | Path, *, content_type: str | None = None
    ) -> CommitFile:
        """Upload a local file as the file ``key`` and record it."""
        content_type = content_type or _guess_content_type(key)
        physical, store = self._target(key)
        stored = store.put_file(key, path, content_type=content_type)
        return self.add(
            CommitFile(
                logical_key=key,
                physical_path=physical,
                size=Path(path).stat().st_size,
                etag=stored.etag,
                version_id=stored.version_id,
                content_type=content_type,
            )
        )

    def upload_dir(
        self, path: str | Path, *, prefix: str = "", pattern: str = "**/*"
    ) -> builtins.list[CommitFile]:
        """Upload every file under ``path`` (matching ``pattern``) to ``prefix`` + its
        relative path, in parallel, and record them."""
        root = Path(path)
        local = sorted(p for p in root.glob(pattern) if p.is_file())
        keys = [prefix + p.relative_to(root).as_posix() for p in local]
        self._folder()  # fetch credentials once, before the workers share them
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(self.put_file, keys, local))

    def commit(self) -> Snapshot:
        """Publish the snapshot. Called by the context manager on a clean exit."""
        if self.snapshot is not None:
            return self.snapshot
        try:
            snapshot = self._publish()
        except (httpx.TransportError, LakekeeperHTTPError) as exc:
            # No answer, or a server error: the commit may have landed. Anything else is
            # a refusal, and nothing will ever read what this commit uploaded.
            unknown = isinstance(exc, httpx.TransportError) or exc.status_code >= 500
            self._outcome_unknown = unknown
            if not unknown:
                self._discard(self._uploaded.values())
            raise
        except LakekeeperError:
            self._discard(self._uploaded.values())
            raise
        self.snapshot = snapshot
        skipped = {s.logical_key for s in snapshot.skipped}
        published = {f.physical_path for f in self._added.values() if f.logical_key not in skipped}
        self._discard(p for p in self._uploaded.values() if p not in published)
        return snapshot

    def _discard(self, paths: Iterable[str]) -> None:
        """Delete objects this commit uploaded and no snapshot records. Best-effort."""
        for path in paths:
            for folder, store in self._folders.items():
                if path.startswith(folder + "/"):
                    try:
                        store.delete(path[len(folder) + 1 :])
                    except Exception as exc:  # noqa: BLE001 - must not mask the real outcome
                        _log.warning("left unpublished upload %s: %s", path, exc)
                    break

    def _publish(self) -> Snapshot:
        dataset = self.branch.dataset
        path = f"{dataset._path}/branches/{quote(self.branch.name, safe='')}/commits"
        for _ in range(MAX_REBASES):
            body: dict[str, Any] = {
                "added": [f.wire() for f in self._added.values()],
                "removed": list(self._removed),
            }
            if self._parent is not None:
                body["parent-snapshot-id"] = str(self._parent)
            if self.summary:
                body["summary"] = self.summary
            if self.on_constraint_violation:
                body["on-constraint-violation"] = self.on_constraint_violation
            try:
                # One key for every attempt: a resend after a lost response is answered with
                # the snapshot the first attempt published, and a rebased retry may reuse it.
                resp = dataset._api._keyed("POST", path, json=body, key=self._key)
            except CommitConflictError as exc:
                head = exc.current_snapshot_id
                if not self.rebase or self._touched_since(head):
                    raise
                self._parent = head
                continue
            return Snapshot.model_validate(resp.json())
        raise LakekeeperError(
            f"branch {self.branch.name!r} kept moving: gave up after {MAX_REBASES} rebases"
        )

    def _touched_since(self, head: SnapshotId | None) -> bool:
        """Whether the commits between this commit's parent and ``head`` changed any key
        this commit adds or removes. Such a commit is not replayed: it would overwrite a
        change its author never saw."""
        mine = set(self._added) | set(self._removed)
        if head is None:
            return False
        dataset = self.branch.dataset
        if self._parent is None:
            # The branch had no commits when this one began: everything is new to it.
            theirs: Iterable[str] = (f.logical_key for f in Ref(dataset, self.branch.name).files())
        else:
            theirs = (c.logical_key for c in dataset.diff(self._parent, head))
        return any(key in mine for key in theirs)


class ImportTask:
    """An import running on the server's task queue."""

    def __init__(self, api: Datasets, task_id: uuid.UUID) -> None:
        self._api = api
        self.task_id = task_id

    def __repr__(self) -> str:
        return f"ImportTask({self.task_id})"

    @property
    def _path(self) -> str:
        return f"/management/v1/warehouse/{quote(self._api._warehouse, safe='')}/task"

    def status(self) -> ImportTaskStatus:
        """The task as it stands: status, phase, objects listed so far."""
        resp = self._api._t.request("GET", f"{self._path}/by-id/{self.task_id}")
        return ImportTaskStatus.model_validate(resp.json())

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        on_progress: Callable[[ImportTaskStatus], None] | None = None,
    ) -> ImportResult:
        """Block until the import ends, and return what it published.

        ``on_progress`` sees every status polled, e.g. to show ``phase`` and
        ``objects_listed``. Raises :class:`~pylakekeeper.LakekeeperError` if the import
        failed or was cancelled, and :class:`TimeoutError` past ``timeout`` seconds.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.status()
            if on_progress is not None:
                on_progress(status)
            if status.status == "SUCCESS":
                return ImportResult.model_validate(status.execution_details or {})
            if status.status in ("FAILED", "CANCELLED"):
                raise LakekeeperError(
                    f"import {self.task_id} {status.status.lower()}: {status.message or ''}"
                )
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"import {self.task_id} still {status.status} after {timeout}s")
            time.sleep(poll_interval)

    def cancel(self) -> None:
        """Stop the import for good. One that has not published yet publishes nothing."""
        self._api._t.request(
            "POST",
            f"{self._path}/control",
            json={"action": {"action-type": "cancel"}, "task-ids": [str(self.task_id)]},
        )


class DatasetCredentials:
    """Vended storage credentials for a dataset, in the shapes engines expect.

    A reader holds one, read-only, for the dataset's prefix. A writer holds one more, for
    :attr:`write_prefix` alone: a fresh folder that only it writes to.
    """

    def __init__(
        self,
        location: str,
        response: LoadDatasetCredentialsResponse,
        overrides: Mapping[str, str],
    ) -> None:
        self.location = location
        #: Where this holder writes, or ``None`` if it may not commit.
        self.write_prefix = response.write_prefix
        self._creds = [c.model_dump() for c in response.storage_credentials]
        self._overrides = dict(overrides)

    def _covering(self, path: str) -> builtins.list[dict[str, Any]]:
        """The credential with the longest prefix that covers ``path``."""
        target = path.rstrip("/") + "/"
        covering = [c for c in self._creds if target.startswith(c["prefix"].rstrip("/") + "/")]
        return sorted(covering, key=lambda c: len(c["prefix"]))[-1:]

    def _props(self, path: str) -> dict[str, str]:
        creds = self._covering(path)
        if not creds:
            raise ConfigError(f"no credential covers {path}: the caller may not access it")
        return _collect_iceberg_props(creds, self._overrides)

    @property
    def props(self) -> dict[str, str]:
        """The read credential's properties, as flat Iceberg-style keys."""
        return self._props(self.location)

    @property
    def expires_at(self) -> datetime | None:
        """When the first of the credentials expires."""
        expiries = [
            e
            for c in self._creds
            if (e := credential_expiry(_collect_iceberg_props([c], self._overrides)))
        ]
        return min(expiries, default=None)

    def expires_within(self, seconds: float) -> bool:
        deadline = self.expires_at
        return deadline is not None and deadline <= datetime.now(tz=timezone.utc) + timedelta(
            seconds=seconds
        )

    @property
    def fsspec_kwargs(self) -> dict[str, object]:
        """For ``fsspec.filesystem("s3", **kwargs)`` — and so pandas, pyarrow, DuckDB."""
        return iceberg_creds_to_fsspec(self._covering(self.location), self._overrides)

    @property
    def lance_storage_options(self) -> dict[str, str]:
        return iceberg_creds_to_lance(self._covering(self.location), self._overrides)

    def objects(self) -> ObjectStore:
        """Read objects below the dataset's location."""
        return object_store_for(self.location, self.props)

    def write_objects(self) -> ObjectStore:
        """Write objects below :attr:`write_prefix`, the only place this holder may write."""
        if self.write_prefix is None:
            raise ConfigError("these credentials name no write folder: the caller may not commit")
        return object_store_for(self.write_prefix, self._props(self.write_prefix))


@dataclass(frozen=True)
class DownloadResult:
    """What :meth:`Ref.download` did."""

    snapshot_id: SnapshotId | None
    path: Path
    downloaded: int
    skipped: int


def _import_body(args: Mapping[str, Any]) -> dict[str, Any]:
    names = {
        "mode": "mode",
        "branch": "branch",
        "sub_prefix": "sub-prefix",
        "suffix": "suffix",
        "include": "include",
        "exclude": "exclude",
        "default_excludes": "default-excludes",
        "record_versions": "record-versions",
        "check_materialization": "check-materialization",
        "max_files": "max-files",
        "on_constraint_violation": "on-constraint-violation",
        "summary": "summary",
    }
    body: dict[str, Any] = {}
    for arg, wire in names.items():
        value = args.get(arg)
        if value is not None:
            body[wire] = list(value) if arg in ("include", "exclude") else value
    if isinstance(body.get("summary"), Mapping):
        body["summary"] = dict(body["summary"])
    return body


def _bare_etag(etag: str) -> str:
    etag = etag.strip()
    if etag.startswith("W/"):
        etag = etag[2:]
    return etag.strip('"')


def _fingerprint(f: DatasetFile) -> dict[str, Any]:
    return {"etag": f.etag, "version-id": f.version_id, "size": f.size}


def _target(root: Path, key: str) -> Path:
    target = (root / key).resolve()
    if root not in target.parents:
        raise LakekeeperError(f"file key {key!r} resolves outside the download directory")
    return target


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        files = json.loads(path.read_text()).get("files", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}


def _write_manifest(path: Path, snapshot_id: SnapshotId | None, files: dict[str, Any]) -> None:
    payload = {"snapshot-id": str(snapshot_id) if snapshot_id else None, "files": files}
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    os.replace(tmp, path)
