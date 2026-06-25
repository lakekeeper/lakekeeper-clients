"""Provision a fresh warehouse + namespace, then run generic_tables_lance — in-network.

This is the turnkey entrypoint used by examples/run.sh. It runs INSIDE the integration
compose network, where keycloak/lakekeeper/seaweedfs resolve by service name and the
storage endpoint Lakekeeper vends is actually reachable — so the example needs no
endpoint-rewriting hacks and exercises the real flow end to end.

The bootstrap + warehouse + namespace setup here is the admin work the SDK intentionally
does not cover; it's done with plain HTTP, exactly as a one-time operator step would be.
"""

from __future__ import annotations

import os

import httpx

LAKEKEEPER = os.environ["LAKEKEEPER"]
PROJECT_ID = os.environ.get("PROJECT_ID", "00000000-0000-0000-0000-000000000000")
NAMESPACE = os.environ.get("NAMESPACE", "ai.test")
WAREHOUSE_NAME = os.environ.get("WAREHOUSE_NAME", "demo")


def _token() -> str:
    r = httpx.post(
        os.environ["OAUTH_TOKEN_URL"],
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["OAUTH_CLIENT_ID"],
            "client_secret": os.environ["OAUTH_CLIENT_SECRET"],
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def provision() -> str:
    """Bootstrap the server and ensure a SeaweedFS warehouse + namespace exist; return its id."""
    headers = {"Authorization": f"Bearer {_token()}", "x-project-id": PROJECT_ID}

    httpx.post(
        f"{LAKEKEEPER}/management/v1/bootstrap",
        headers=headers,
        json={"accept-terms-of-use": True, "is-operator": True},
        timeout=15,
    )  # idempotent: 400 if already bootstrapped

    endpoint = "http://seaweedfs:8333"
    body = {
        "warehouse-name": WAREHOUSE_NAME,
        "storage-profile": {
            "type": "s3",
            "bucket": "examples",
            "key-prefix": WAREHOUSE_NAME,
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
    r = httpx.post(f"{LAKEKEEPER}/management/v1/warehouse", headers=headers, json=body, timeout=30)
    if r.status_code < 300:
        warehouse_id = r.json()["warehouse-id"]
    else:  # already exists -> look it up by name
        listing = httpx.get(
            f"{LAKEKEEPER}/management/v1/warehouse", headers=headers, timeout=15
        ).json()
        warehouse_id = next(w["id"] for w in listing["warehouses"] if w["name"] == WAREHOUSE_NAME)

    levels = NAMESPACE.split(".")
    for depth in range(1, len(levels) + 1):
        httpx.post(
            f"{LAKEKEEPER}/catalog/v1/{warehouse_id}/namespaces",
            headers=headers,
            json={"namespace": levels[:depth]},
            timeout=15,
        )  # idempotent: 409 if it exists
    return warehouse_id


if __name__ == "__main__":
    os.environ["WAREHOUSE_ID"] = provision()
    print(f"[demo] provisioned warehouse {os.environ['WAREHOUSE_ID']}; running example…\n")
    import generic_tables_lance

    generic_tables_lance.main()
