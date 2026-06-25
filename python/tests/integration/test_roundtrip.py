"""End-to-end: real Keycloak client_credentials → generic-tables → vended lance roundtrip."""

from __future__ import annotations

import pytest

from pylakekeeper import Client, ClientCredentials

pytestmark = pytest.mark.integration

TABLE = "image_embeddings"
# SeaweedFS S3 is `seaweedfs:8333` inside the compose network (what the server vends)
# but `localhost:8333` from the host where lance runs.
STORAGE_INTERNAL_HOST = "seaweedfs:8333"
STORAGE_HOST = "localhost:8333"


@pytest.fixture
def client(stack):
    auth = ClientCredentials(
        token_url=stack.token_url,
        client_id=stack.client_id,
        client_secret=stack.client_secret,
    )
    c = Client(
        base_url=stack.base_url,
        warehouse=stack.warehouse_id,
        auth=auth,
        project_id=stack.project_id,
    )
    yield c
    c.close()


def _host_storage_options(t) -> dict[str, str]:
    """Rewrite the vended docker-internal storage endpoint to the host-reachable one."""
    opts = dict(t.lance_storage_options)
    opts["aws_endpoint"] = opts["aws_endpoint"].replace(STORAGE_INTERNAL_HOST, STORAGE_HOST)
    return opts


def test_generic_tables_vended_lance_roundtrip(stack, client: Client):
    import lance
    import pyarrow as pa

    ns = stack.namespace

    # Create (idempotent for re-runs against a reused warehouse).
    try:
        client.generic_tables.create(ns, TABLE, format="lance", properties={"dim": "8"})
    except Exception as exc:  # noqa: BLE001
        if "409" not in str(exc):
            raise

    # Load with vended credentials -> real STS creds from MinIO.
    t = client.generic_tables.load(ns, TABLE, vended=True)
    assert t.location.startswith("s3://examples/")
    opts = _host_storage_options(t)
    assert opts["aws_access_key_id"]
    assert opts["aws_session_token"]  # STS, not the static key
    assert opts["allow_http"] == "true"

    # Write + read back through lance using the vended creds.
    table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    lance.write_dataset(table, t.location, storage_options=opts, mode="overwrite")

    # Re-load (STS creds are short-lived) before reading.
    t = client.generic_tables.load(ns, TABLE, vended=True)
    ds = lance.dataset(t.location, storage_options=_host_storage_options(t))
    assert ds.count_rows() == 3

    # List shows the table.
    names = [i.name for i in client.generic_tables.list(ns)]
    assert TABLE in names

    # Drop.
    client.generic_tables.drop(ns, TABLE)
    names_after = [i.name for i in client.generic_tables.list(ns)]
    assert TABLE not in names_after
