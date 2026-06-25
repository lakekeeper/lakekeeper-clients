# Integration tests

End-to-end tests that run the SDK against a **real** Lakekeeper stack with real OAuth2.

## What it spins up

`docker-compose.yaml` brings up a self-contained stack:

| Service | Purpose | Host port |
|---|---|---|
| `lakekeeper` (+ `migrate`) | the catalog under test | 8182 |
| `keycloak` | OIDC provider, realm `iceberg` (vendored `keycloak/realm.json`) | 31080 |
| `minio` (+ `createbucket`) | S3-compatible storage with STS | 9002 |
| `db` (postgres) | catalog database | — |

Ports are non-default to avoid colliding with other local stacks. The `realm.json` is
vendored from the lakekeeper server repo's `examples/access-control-simple`.

## Running

Requires a container engine — `docker` **or** `podman` (the conftest auto-detects; `podman
compose` delegates to the docker-compose v2 provider). Then:

```sh
cd python
pip install -e '.[dev]'
pytest tests/integration -m integration
```

The unit suite (`pytest tests/`) deselects these automatically — no Docker needed there.

## How it works

`conftest.py` brings the stack up, acquires a `client_credentials` token (Keycloak `spark`
service account), bootstraps the server, and creates an STS-enabled MinIO warehouse +
namespace via raw HTTP (the admin API is not part of the SDK). The test then drives the SDK
through create → vended-load → lance write/read → list → drop.

**Endpoint rewrite:** Lakekeeper vends the docker-internal endpoint (`minio:9000`), which the
host can't reach, so the test rewrites it to `localhost:9002` for the host-side lance I/O.
The vended STS credentials remain valid (MinIO validates them regardless of the hostname used).
