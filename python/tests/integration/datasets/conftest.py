"""Fixtures for the Datasets scenarios: a Lakekeeper server you run, and a warehouse on it.

The compose stack in ``tests/integration`` pins a published Lakekeeper image, which does
not serve Datasets yet. These scenarios run against a server you start yourself — a
build of the catalog with Datasets — on S3-compatible storage with STS (MinIO, Silo)::

    export PYLK_ITEST_LAKEKEEPER_URL=http://localhost:8181   # no auth, allow-all authz
    export PYLK_ITEST_S3_ENDPOINT=http://127.0.0.1:9000
    export PYLK_ITEST_S3_ACCESS_KEY=... PYLK_ITEST_S3_SECRET_KEY=...
    export PYLK_ITEST_S3_BUCKET=pylk          # created; plus pylk-versioned, with versioning
    pytest tests/integration/datasets -m integration

The storage endpoint must be reachable under the same name from the server and from
here: signed URLs carry its host in their signature, so it cannot be rewritten.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

from pylakekeeper import Client, StaticToken

DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Server:
    url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    #: A bucket that keeps no object versions: what most buckets are. The scenarios run
    #: here unless they are about versions, so nothing passes by leaning on versioning.
    s3_bucket: str
    s3_region: str
    warehouse_id: str
    key_prefix: str
    #: The same, on a bucket with versioning on.
    versioned_bucket: str = ""
    versioned_warehouse_id: str = ""

    def stored(self, location: str) -> set[str]:
        """Every object below a dataset's location, relative to it."""
        bucket, _, prefix = location.removeprefix("s3://").partition("/")
        pages = (
            self.s3()
            .get_paginator("list_objects_v2")
            .paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
        )
        cut = len(prefix.rstrip("/")) + 1
        return {o["Key"][cut:] for page in pages for o in page.get("Contents", [])}

    def s3(self):  # type: ignore[no-untyped-def]
        """A boto3 client with the storage's own credentials: the world outside the catalog."""
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.s3_endpoint,
            aws_access_key_id=self.s3_access_key,
            aws_secret_access_key=self.s3_secret_key,
            region_name=self.s3_region,
            config=Config(s3={"addressing_style": "path"}),
        )


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; see tests/integration/datasets/conftest.py")
    return value


@pytest.fixture(scope="session")
def server() -> Server:
    url = _env("PYLK_ITEST_LAKEKEEPER_URL").rstrip("/")
    endpoint = _env("PYLK_ITEST_S3_ENDPOINT")
    access_key = _env("PYLK_ITEST_S3_ACCESS_KEY")
    secret_key = _env("PYLK_ITEST_S3_SECRET_KEY")
    bucket = os.environ.get("PYLK_ITEST_S3_BUCKET", "pylk")
    versioned_bucket = f"{bucket}-versioned"
    region = os.environ.get("PYLK_ITEST_S3_REGION", "local")
    headers = {"x-project-id": DEFAULT_PROJECT_ID}

    r = httpx.post(
        f"{url}/management/v1/bootstrap",
        headers=headers,
        json={"accept-terms-of-use": True, "is-operator": True},
        timeout=15,
    )
    if r.status_code not in (200, 204, 400):  # 400: already bootstrapped
        r.raise_for_status()

    probe = Server(url, endpoint, access_key, secret_key, bucket, region, "", "")
    s3 = probe.s3()
    for name in (bucket, versioned_bucket):
        try:
            s3.create_bucket(Bucket=name)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
    if s3.get_bucket_versioning(Bucket=bucket).get("Status"):
        pytest.fail(f"bucket {bucket!r} keeps versions; the scenarios need one that does not")
    # Versioning lets a snapshot pin object versions: a tag then keeps reading the bytes
    # it recorded after another tool writes the key again.
    s3.put_bucket_versioning(Bucket=versioned_bucket, VersioningConfiguration={"Status": "Enabled"})

    key_prefix = f"pylk-{uuid.uuid4().hex[:8]}"

    def warehouse(name: str, on_bucket: str) -> str:
        return _create_warehouse(
            url, headers, name, on_bucket, key_prefix, region, endpoint, access_key, secret_key
        )

    return Server(
        url,
        endpoint,
        access_key,
        secret_key,
        bucket,
        region,
        warehouse(key_prefix, bucket),
        key_prefix,
        versioned_bucket,
        warehouse(f"{key_prefix}-versioned", versioned_bucket),
    )


def _create_warehouse(
    url: str,
    headers: dict[str, str],
    name: str,
    bucket: str,
    key_prefix: str,
    region: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
) -> str:
    r = httpx.post(
        f"{url}/management/v1/warehouse",
        headers=headers,
        json={
            "warehouse-name": name,
            "storage-profile": {
                "type": "s3",
                "bucket": bucket,
                "key-prefix": key_prefix,
                "region": region,
                "endpoint": endpoint,
                "sts-endpoint": endpoint,
                "path-style-access": True,
                "flavor": "s3-compat",
                "sts-enabled": True,
            },
            "storage-credential": {
                "type": "s3",
                "credential-type": "access-key",
                "access-key-id": access_key,
                "secret-access-key": secret_key,
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    warehouse_id: str = r.json()["warehouse-id"]
    return warehouse_id


@pytest.fixture
def client(server: Server) -> Iterator[Client]:
    c = Client(server.url, server.warehouse_id, StaticToken("dev"), project_id=DEFAULT_PROJECT_ID)
    yield c
    c.close()


@pytest.fixture
def versioned_client(server: Server) -> Iterator[Client]:
    """A client on the warehouse whose bucket keeps object versions."""
    c = Client(
        server.url,
        server.versioned_warehouse_id,
        StaticToken("dev"),
        project_id=DEFAULT_PROJECT_ID,
    )
    yield c
    c.close()


@pytest.fixture
def namespace(server: Server) -> str:
    """A fresh namespace per test, in both warehouses, so scenarios never meet."""
    name = f"ns_{uuid.uuid4().hex[:8]}"
    for warehouse_id in (server.warehouse_id, server.versioned_warehouse_id):
        r = httpx.post(
            f"{server.url}/catalog/v1/{warehouse_id}/namespaces",
            headers={"x-project-id": DEFAULT_PROJECT_ID},
            json={"namespace": [name]},
            timeout=15,
        )
        r.raise_for_status()
    return name
