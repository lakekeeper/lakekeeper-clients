"""Integration fixtures: bring up the full stack and prepare a warehouse + namespace.

Brings up Lakekeeper + Postgres + MinIO + Keycloak via docker compose, then (using raw
HTTP, since the admin API is not part of the SDK) acquires a real client_credentials
token, bootstraps the server, and creates an STS-enabled MinIO warehouse and a namespace.

The yielded ``Stack`` carries everything a test needs to drive the SDK against real OAuth.
Requires Docker. These tests are marked ``integration`` and are deselected by default
(see ``[tool.pytest.ini_options]`` in pyproject); run them with ``pytest -m integration``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).parent
COMPOSE_FILE = HERE / "docker-compose.yaml"
PROJECT = "pylk-itest"

# Container engine: a real `docker` binary if present, otherwise `podman` (whose
# `podman compose` delegates to the docker-compose v2 provider). `docker` is often a
# shell alias to podman, so we resolve the actual executable rather than assume it.
ENGINE = shutil.which("docker") or shutil.which("podman")

LAKEKEEPER_URL = "http://localhost:8182"
TOKEN_URL = "http://localhost:31080/realms/iceberg/protocol/openid-connect/token"
CLIENT_ID = "spark"
CLIENT_SECRET = "2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000000"

# SeaweedFS S3 is reachable as `seaweedfs:8333` inside the compose network (what the server
# vends) but as `localhost:8333` from the host (where lance runs); tests rewrite between them.
STORAGE_INTERNAL_HOST = "seaweedfs:8333"
STORAGE_HOST = "localhost:8333"
STORAGE_BUCKET = "examples"  # pre-created by the seaweedfs `-bucket` flag


@dataclass
class Stack:
    base_url: str
    warehouse_id: str
    project_id: str
    namespace: tuple[str, ...]
    token_url: str
    client_id: str
    client_secret: str


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ENGINE, "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
    )


def _wait_until(label: str, fn, timeout: float = 180.0, interval: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            if fn():
                return
        except Exception as exc:  # noqa: BLE001 — polling, any error means "not ready yet"
            last = repr(exc)
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {label} ({timeout}s); last error: {last}")


def _fetch_token() -> str:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _bootstrap_and_provision() -> Stack:
    token = _fetch_token()
    auth = {"Authorization": f"Bearer {token}", "x-project-id": DEFAULT_PROJECT_ID}

    # Bootstrap: the first authenticated principal becomes the global admin/operator.
    r = httpx.post(
        f"{LAKEKEEPER_URL}/management/v1/bootstrap",
        headers=auth,
        json={"accept-terms-of-use": True, "is-operator": True},
        timeout=15,
    )
    if r.status_code not in (200, 204, 400):  # 400 = already bootstrapped
        r.raise_for_status()

    # Create an STS-enabled SeaweedFS warehouse (mirrors examples/minimal).
    endpoint = f"http://{STORAGE_INTERNAL_HOST}"
    warehouse_body = {
        "warehouse-name": "itest",
        "storage-profile": {
            "type": "s3",
            "bucket": STORAGE_BUCKET,
            "key-prefix": "itest",
            "region": "local-01",
            "endpoint": endpoint,
            "sts-endpoint": endpoint,
            "sts-role-arn": "arn:aws:iam::000000000000:role/LakekeeperVendedRole",
            "path-style-access": True,
            "flavor": "s3-compat",
            "sts-enabled": True,
        },
        "storage-credential": {
            "type": "s3",
            "credential-type": "access-key",
            "access-key-id": "seaweedfs-root-user",
            "secret-access-key": "seaweedfs-root-password",
        },
    }
    r = httpx.post(
        f"{LAKEKEEPER_URL}/management/v1/warehouse",
        headers=auth,
        json=warehouse_body,
        timeout=30,
    )
    r.raise_for_status()
    warehouse_id = r.json()["warehouse-id"]

    # Create the namespace (and its parent) via the Iceberg catalog API.
    namespace = ("ai", "test")
    for depth in range(1, len(namespace) + 1):
        rn = httpx.post(
            f"{LAKEKEEPER_URL}/catalog/v1/{warehouse_id}/namespaces",
            headers=auth,
            json={"namespace": list(namespace[:depth])},
            timeout=15,
        )
        if rn.status_code not in (200, 201, 409):
            rn.raise_for_status()

    return Stack(
        base_url=LAKEKEEPER_URL,
        warehouse_id=warehouse_id,
        project_id=DEFAULT_PROJECT_ID,
        namespace=namespace,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


@pytest.fixture(scope="session")
def stack() -> Stack:
    if ENGINE is None or _compose("version").returncode != 0:
        pytest.skip("no container engine (docker/podman compose) available")

    _compose("down", "-v", "--remove-orphans")
    up = _compose("up", "-d")
    if up.returncode != 0:
        pytest.fail(f"docker compose up failed:\n{up.stderr}")
    try:
        _wait_until(
            "lakekeeper /health",
            lambda: httpx.get(f"{LAKEKEEPER_URL}/health", timeout=5).status_code == 200,
        )
        _wait_until("keycloak token endpoint", lambda: bool(_fetch_token()))
        yield _bootstrap_and_provision()
    finally:
        _compose("down", "-v", "--remove-orphans")
