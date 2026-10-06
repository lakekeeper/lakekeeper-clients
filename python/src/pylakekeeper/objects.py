"""Object I/O against a generic table's storage location.

``dataset``-format tables catalog *files* rather than a columnar table, so the natural
operation is "write this object under the table's location" — not "mount a filesystem".
This module provides that in four calls (:meth:`put`, :meth:`get`, :meth:`list`,
:meth:`delete`), keyed by a path **relative to the table's base location**. Callers never
handle a bucket name, a container, or an absolute URI.

The point is that the per-backend branch lives here once instead of in every caller: the
same four calls work whether the warehouse is on S3, ADLS or GCS, and only the vended
credential shape differs underneath.

Storage SDKs are imported lazily, so the core install stays ``httpx`` + ``pydantic``::

    pip install 'pylakekeeper[s3]'      # boto3
    pip install 'pylakekeeper[azure]'   # azure-storage-blob
    pip install 'pylakekeeper[gcs]'     # google-cloud-storage
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .errors import ConfigError

#: URI scheme -> the extra that provides its SDK, for the error message.
_EXTRA_FOR_SCHEME = {
    "s3": "s3",
    "s3a": "s3",
    "abfs": "azure",
    "abfss": "azure",
    "gs": "gcs",
}


def _guess_content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def _missing_dep(scheme: str, package: str, exc: ImportError) -> ConfigError:
    extra = _EXTRA_FOR_SCHEME.get(scheme, scheme)
    return ConfigError(
        f"object I/O on {scheme}:// needs {package}. "
        f"Install it with: pip install 'pylakekeeper[{extra}]'"
    )


@dataclass(frozen=True)
class StoredObject:
    """What storage reported about an object it just stored.

    A dataset commit records both, so a later read can be pinned to these exact bytes
    (``version_id``) or checked against them (``etag``). Either is ``None`` where the
    backend does not report it — ``version_id`` needs a versioned bucket.
    """

    etag: str | None = None
    version_id: str | None = None


class ObjectStore(Protocol):
    """The operations a ``dataset`` table needs, relative to its base location."""

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject: ...

    def put_file(
        self, key: str, path: str | Path, *, content_type: str | None = None
    ) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None: ...


class _S3Objects:
    """S3 and every S3-compatible store (MinIO, SeaweedFS, R2, StackIT, ...)."""

    def __init__(self, bucket: str, prefix: str, props: dict[str, str]) -> None:
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised by the error path test
            raise _missing_dep("s3", "boto3", exc) from exc

        self._bucket = bucket
        self._prefix = prefix
        config: Any = None
        if props.get("s3.path-style-access", "").lower() == "true":
            from botocore.config import Config  # noqa: PLC0415

            config = Config(s3={"addressing_style": "path"})

        self._client: Any = boto3.client(
            "s3",
            aws_access_key_id=props.get("s3.access-key-id"),
            aws_secret_access_key=props.get("s3.secret-access-key"),
            aws_session_token=props.get("s3.session-token"),
            region_name=props.get("s3.region") or props.get("client.region"),
            # None on real AWS; set for MinIO/SeaweedFS and other compatible stores.
            endpoint_url=props.get("s3.endpoint"),
            config=config,
        )

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key.lstrip('/')}" if self._prefix else key.lstrip("/")

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        resp = self._client.put_object(
            Bucket=self._bucket,
            Key=self._full(key),
            Body=data,
            ContentType=content_type or _guess_content_type(key),
        )
        return _s3_stored(resp)

    def put_file(
        self, key: str, path: str | Path, *, content_type: str | None = None
    ) -> StoredObject:
        # One PUT streamed from the file, so S3's etag stays the MD5 a reader can check.
        with open(path, "rb") as body:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=self._full(key),
                Body=body,
                ContentType=content_type or _guess_content_type(key),
            )
        return _s3_stored(resp)

    def get(self, key: str) -> bytes:
        body = self._client.get_object(Bucket=self._bucket, Key=self._full(key))["Body"]
        data: bytes = body.read()
        return data

    def list(self, prefix: str = "") -> list[str]:
        base = self._full(prefix)
        cut = len(self._prefix) + 1 if self._prefix else 0
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=base):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][cut:])
        return keys

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._full(key))


class _AzureObjects:
    """ADLS Gen2 / Blob storage, authenticated with the vended account SAS token."""

    def __init__(self, container: str, account: str, prefix: str, props: dict[str, str]) -> None:
        try:
            from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise _missing_dep("abfss", "azure-storage-blob", exc) from exc

        # The trailing dot matters: `adls.sas-token.<account>.<suffix>` carries the token,
        # while `adls.sas-token-expires-at-ms.<account>.<suffix>` carries its expiry. A
        # prefix match without the dot picks up the timestamp and signs nothing.
        sas = next((v for k, v in props.items() if k.startswith("adls.sas-token.")), None)
        if sas is None:
            raise ConfigError(
                "no ADLS SAS token in the vended credentials — "
                "load the table with vended=True and read access"
            )
        self._prefix = prefix
        service: Any = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=sas)
        self._container: Any = service.get_container_client(container)

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key.lstrip('/')}" if self._prefix else key.lstrip("/")

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        resp = self._container.upload_blob(
            name=self._full(key),
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type or _guess_content_type(key)),
        )
        return _azure_stored(resp)

    def put_file(
        self, key: str, path: str | Path, *, content_type: str | None = None
    ) -> StoredObject:
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        with open(path, "rb") as data:
            resp = self._container.upload_blob(
                name=self._full(key),
                data=data,
                overwrite=True,
                content_settings=ContentSettings(
                    content_type=content_type or _guess_content_type(key)
                ),
            )
        return _azure_stored(resp)

    def get(self, key: str) -> bytes:
        data: bytes = self._container.download_blob(self._full(key)).readall()
        return data

    def list(self, prefix: str = "") -> list[str]:
        base = self._full(prefix)
        cut = len(self._prefix) + 1 if self._prefix else 0
        return [b.name[cut:] for b in self._container.list_blobs(name_starts_with=base)]

    def delete(self, key: str) -> None:
        self._container.delete_blob(self._full(key))


class _GcsObjects:
    """GCS, authenticated with the vended OAuth2 bearer token."""

    def __init__(self, bucket: str, prefix: str, props: dict[str, str]) -> None:
        try:
            from google.cloud import storage  # noqa: PLC0415
            from google.oauth2.credentials import Credentials  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise _missing_dep("gs", "google-cloud-storage", exc) from exc

        token = props.get("gcs.oauth2.token")
        if not token:
            raise ConfigError(
                "no GCS OAuth2 token in the vended credentials — "
                "load the table with vended=True and read access"
            )
        self._prefix = prefix
        client: Any = storage.Client(credentials=Credentials(token=token))
        self._bucket: Any = client.bucket(bucket)

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key.lstrip('/')}" if self._prefix else key.lstrip("/")

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        blob = self._bucket.blob(self._full(key))
        blob.upload_from_string(data, content_type=content_type or _guess_content_type(key))
        return _gcs_stored(blob)

    def put_file(
        self, key: str, path: str | Path, *, content_type: str | None = None
    ) -> StoredObject:
        blob = self._bucket.blob(self._full(key))
        blob.upload_from_filename(str(path), content_type=content_type or _guess_content_type(key))
        return _gcs_stored(blob)

    def get(self, key: str) -> bytes:
        data: bytes = self._bucket.blob(self._full(key)).download_as_bytes()
        return data

    def list(self, prefix: str = "") -> list[str]:
        base = self._full(prefix)
        cut = len(self._prefix) + 1 if self._prefix else 0
        return [b.name[cut:] for b in self._bucket.list_blobs(prefix=base)]

    def delete(self, key: str) -> None:
        self._bucket.blob(self._full(key)).delete()


def _s3_stored(resp: Any) -> StoredObject:
    resp = resp if isinstance(resp, dict) else {}
    return StoredObject(etag=resp.get("ETag"), version_id=resp.get("VersionId"))


def _azure_stored(resp: Any) -> StoredObject:
    resp = resp if isinstance(resp, dict) else {}
    return StoredObject(etag=resp.get("etag"), version_id=resp.get("version_id"))


def _gcs_stored(blob: Any) -> StoredObject:
    # GCS reports a different etag to a download than to an upload, so only the
    # generation is worth recording: it pins the bytes outright.
    generation = blob.generation
    return StoredObject(version_id=str(generation) if generation is not None else None)


def object_store_for(location: str, props: dict[str, str]) -> ObjectStore:
    """Build an :class:`ObjectStore` for a table location and its vended credentials.

    Args:
        location: the table's base location — ``s3://bucket/prefix``,
            ``abfss://container@account.dfs.core.windows.net/prefix`` or ``gs://bucket/prefix``.
        props: the merged vended credentials and config (see
            :func:`~pylakekeeper.lance.iceberg_creds_to_lance` for the same input).

    Raises:
        ConfigError: the scheme is unsupported, or its SDK is not installed.
    """
    parts = urlsplit(location)
    scheme = parts.scheme.lower()
    prefix = parts.path.strip("/")

    if scheme in ("s3", "s3a"):
        return _S3Objects(parts.netloc, prefix, props)
    if scheme in ("abfs", "abfss"):
        container = parts.username
        host = parts.hostname or ""
        if not container:
            raise ConfigError(
                f"malformed ADLS location {location!r} — "
                "expected abfss://<container>@<account>.dfs.core.windows.net/<prefix>"
            )
        account = props.get("adls.account-name") or host.split(".", 1)[0]
        return _AzureObjects(container, account, prefix, props)
    if scheme == "gs":
        return _GcsObjects(parts.netloc, prefix, props)

    raise ConfigError(
        f"unsupported storage scheme {scheme!r} for object I/O (location {location!r}). "
        "Supported: s3, s3a, abfs, abfss, gs."
    )
