#!/usr/bin/env python3
"""Reproduce the reference notebook flow with the `pylakekeeper` SDK.

Create a Lance generic table, load it with vended credentials, then write and read a
small embeddings dataset through Lance — no static AWS creds, no hand-rolled requests.

Compared to the original notebook this drops the manual %1F encoding, the requests
PreparedRequest hack, and the inline credential mapping; the SDK handles all three.
Warehouse/namespace creation is assumed done (admin API is deferred — see PLAN.md).

Env:
  LAKEKEEPER   default http://localhost:8181
  WAREHOUSE_ID warehouse UUID — the URL path prefix, NOT the name (required)
  PROJECT_ID   x-project-id (optional)
  # auth — either a static token, or client_credentials:
  TOKEN                         static bearer token
  OAUTH_TOKEN_URL / OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OAUTH_SCOPE
"""

from __future__ import annotations

import os

from pylakekeeper import (
    Client,
    ClientCredentials,
    ConflictError,
    GenericTableFormat,
    StaticToken,
)

NAMESPACE = os.environ.get("NAMESPACE", "ai.test")
TABLE = os.environ.get("TABLE", "image_embeddings")
EMBED_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))
ROWS = int(os.environ.get("ROWS", "100"))


def build_auth():
    if token := os.environ.get("TOKEN"):
        return StaticToken(token)
    return ClientCredentials(
        token_url=os.environ["OAUTH_TOKEN_URL"],
        client_id=os.environ["OAUTH_CLIENT_ID"],
        client_secret=os.environ["OAUTH_CLIENT_SECRET"],
        scope=os.environ.get("OAUTH_SCOPE"),
    )


def sample_table():
    import numpy as np
    import pyarrow as pa

    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((ROWS, EMBED_DIM)).astype(np.float32)
    return pa.table(
        {
            "id": pa.array(range(ROWS), type=pa.int64()),
            "sku": pa.array([f"SKU-{i:06d}" for i in range(ROWS)]),
            "embedding": pa.FixedSizeListArray.from_arrays(
                pa.array(embeddings.reshape(-1), type=pa.float32()), EMBED_DIM
            ),
        }
    )


def main() -> None:
    import lance

    with Client(
        base_url=os.environ.get("LAKEKEEPER"),
        warehouse=os.environ.get("WAREHOUSE_ID"),
        auth=build_auth(),
        project_id=os.environ.get("PROJECT_ID"),
    ) as c:
        # Create the table; tolerate re-runs where it already exists.
        try:
            c.generic_tables.create(
                NAMESPACE,
                TABLE,
                format=GenericTableFormat.LANCE,  # or just "lance" — any valid slug works
                properties={"embedding-dim": str(EMBED_DIM)},
            )
        except ConflictError:
            print(f"table {NAMESPACE}.{TABLE} already exists — continuing")

        # Load with vended credentials -> base location + STS creds in one call.
        t = c.generic_tables.load(NAMESPACE, TABLE, vended=True)
        print(f"location = {t.location}")

        # Write, then re-load (STS creds are short-lived) and read back.
        lance.write_dataset(
            sample_table(), t.location, storage_options=t.lance_storage_options, mode="overwrite"
        )
        t = c.generic_tables.load(NAMESPACE, TABLE, vended=True)
        ds = lance.dataset(t.location, storage_options=t.lance_storage_options)
        print(f"row_count = {ds.count_rows()}")
        print(ds.to_table(columns=["id", "sku"], limit=5))  # Arrow table prints fine; no pandas dep


if __name__ == "__main__":
    main()
