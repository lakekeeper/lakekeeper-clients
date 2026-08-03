# pylakekeeper — Lakekeeper Python Client

[![Website](https://img.shields.io/badge/https-lakekeeper.io-blue?color=3d4db3&logo=firefox&style=for-the-badge&logoColor=white)](https://lakekeeper.io/)
[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/jkAGG8p93B)
[![PyPI](https://img.shields.io/pypi/v/pylakekeeper?style=for-the-badge&logo=pypi&logoColor=white&color=3775A9)](https://pypi.org/project/pylakekeeper/)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python versions](https://img.shields.io/pypi/pyversions/pylakekeeper.svg)](https://pypi.org/project/pylakekeeper/)
[![python-ci](https://github.com/lakekeeper/lakekeeper-clients/actions/workflows/python-ci.yml/badge.svg)](https://github.com/lakekeeper/lakekeeper-clients/actions/workflows/python-ci.yml)

Please visit [https://docs.lakekeeper.io](https://docs.lakekeeper.io) for Documentation!

`pylakekeeper` is the official Python client for
[Lakekeeper](https://github.com/lakekeeper/lakekeeper), an Apache-Licensed, **secure**,
**fast** and **easy to use** implementation of the
[Apache Iceberg](https://iceberg.apache.org/) REST Catalog specification.

It covers the two things a plain Iceberg REST client cannot do for you: the
**Generic Tables API** — register Lance, Delta, Parquet, images or any other dataset
format as first-class catalog tables — and **Storage Access Management**, mapping
Lakekeeper's vended credentials straight into the keys that `lance`, `boto3`, `fsspec`
and `deltalake` already expect. If you have questions, feature requests or just want a
chat, we are hanging around in [Discord](https://discord.gg/jkAGG8p93B)!

<p align="center">
<img src="https://github.com/lakekeeper/lakekeeper/raw/main/assets/Lakekeeper-Overview.png" width="500">
</p>

# Quickstart

```sh
pip install pylakekeeper
```

```python
from pylakekeeper import Client, StaticToken

lk = Client(
    base_url="http://localhost:8181",
    warehouse="my-warehouse",
    auth=StaticToken("my-token"),
)

# Register a Lance dataset as a generic table
lk.generic_tables.create("ai.models", "embeddings", format="lance")

# Load it back with short-lived, vended S3 credentials
resp = lk.generic_tables.load("ai.models", "embeddings", vended=True)

import lance

lance.write_dataset(data, resp.location, storage_options=resp.lance_storage_options)

# ...or open the same location as an fsspec filesystem
import fsspec

fs = fsspec.filesystem("s3", **resp.fsspec_kwargs)
print(fs.ls(resp.location))
```

Need a running catalog first? The Lakekeeper
[Getting Started Guide](https://docs.lakekeeper.io/getting-started/) brings one up with
`docker compose` in a couple of minutes.

# Scope and Features

`pylakekeeper` is deliberately small: `httpx` + `pydantic`, nothing else. It is **not** a
general-purpose Iceberg REST client — for the standard Iceberg surface use
[PyIceberg](https://py.iceberg.apache.org/), which pairs with this happily
(`pip install 'pylakekeeper[iceberg]'`).

- **Generic Tables**: Register non-Iceberg datasets (Lance, Delta, Parquet, images, HDF5)
  as first-class catalog tables and get `create` / `load` / `list` / `drop` without
  faking Iceberg metadata.
- **Vended Credentials**: `LoadGenericTableResponse` exposes `lance_storage_options` and
  `fsspec_kwargs`, so short-lived credentials flow into your reader with no manual
  key-mapping.
- **OpenID Provider Integration**: `client_credentials`, `device_code` and
  `authorization_code` flows, all with automatic token refresh — or just a
  `StaticToken`.
- **Fully Typed**: Ships `py.typed`; the package is checked under `mypy --strict`.
- **Well-Tested**: Unit tests plus an integration suite that runs a real round-trip
  against Lakekeeper, Postgres, MinIO and Keycloak via `docker compose`.

If you are missing something, we would love to hear about it in a
[GitHub Issue](https://github.com/lakekeeper/lakekeeper-clients/issues/new).

# Status

### Authentication Flows

| Flow                             | Status  | Comment                                        |
|----------------------------------|:-------:|------------------------------------------------|
| Static Token                     | ![done] | `StaticToken` — bring your own bearer token     |
| OAuth2 Client Credentials        | ![done] | `ClientCredentials`, refreshes automatically    |
| OAuth2 Device Code               | ![done] | `DeviceCodeFlow` — for CLIs and notebooks       |
| OAuth2 Authorization Code        | ![done] | `AuthorizationCodeFlow` — local redirect server |

### Generic Table Operations

| Operation | Status  | Comment                                       |
|-----------|:-------:|-----------------------------------------------|
| Create    | ![done] | Any format identifier — see below              |
| Load      | ![done] | Optional credential vending via `vended=True`  |
| List      | ![done] | Paginated                                      |
| Drop      | ![done] |                                                |
| Rename    | ![open] | Not yet exposed by the client                  |

`GenericTableFormat` provides constants for `lance`, `delta`, `vortex`, `paimon` and
`dataset`, but Lakekeeper stores the format as a free identifier — any string matching
`^[a-z][a-z0-9_-]{0,63}$` is accepted, so pass a plain `str` for anything not listed.

### Storage Backends

| Storage              | Status  | Comment                                                     |
|----------------------|:-------:|-------------------------------------------------------------|
| S3 - AWS             | ![done] | Vended credentials mapped to Lance / boto3 / fsspec          |
| S3 - Custom          | ![done] | MinIO, SeaweedFS and friends; `allow_http` set automatically |
| Azure ADLS Gen2      | ![open] | No credential translation yet — raw vended properties are still available on the response |
| Google Cloud Storage | ![open] | No credential translation yet — raw vended properties are still available on the response |

### Optional Extras

| Extra                            | Installs                                         |
|----------------------------------|--------------------------------------------------|
| `pip install 'pylakekeeper[lance]'`    | `pylance` — write and read Lance datasets  |
| `pip install 'pylakekeeper[iceberg]'`  | `pyiceberg` — standard Iceberg catalog surface |
| `pip install 'pylakekeeper[examples]'` | Everything the notebooks and demo scripts need |

# Examples

Runnable scripts and notebooks live in
[`python/examples/`](https://github.com/lakekeeper/lakekeeper-clients/tree/main/python/examples):

| Example | Description |
|---|---|
| [`generic_tables_lance.py`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/generic_tables_lance.py) | Create a Lance generic table, write and read a dataset with vended credentials |
| [`dataset_images.py`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/dataset_images.py) | Upload image files to a `dataset` generic table via boto3 |
| [`auth_clientcredentials_delta.ipynb`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/auth_clientcredentials_delta.ipynb) | OAuth2 client-credentials + a Delta Lake round-trip |
| [`auth_devicecode_lance.ipynb`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/auth_devicecode_lance.ipynb) | Device-code login from a notebook |
| [`auth_devicecode_hdf5.ipynb`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/auth_devicecode_hdf5.ipynb) | Store HDF5 files in a `dataset` table |
| [`auth_authcode_vortex.ipynb`](https://github.com/lakekeeper/lakekeeper-clients/blob/main/python/examples/auth_authcode_vortex.ipynb) | Authorization-code flow with Vortex |

# API Reference

| Object | Purpose |
|---|---|
| `Client` | Entry point; holds base URL, warehouse and auth |
| `client.generic_tables` | `create` / `load` / `list` / `drop` generic tables |
| `StaticToken`, `ClientCredentials`, `DeviceCodeFlow`, `AuthorizationCodeFlow` | Auth strategies |
| `LoadGenericTableResponse` | Location plus `lance_storage_options` / `fsspec_kwargs` |
| `iceberg_creds_to_lance`, `iceberg_creds_to_fsspec` | Credential mapping helpers |

All errors derive from `LakekeeperError`: `NotFoundError`, `ConflictError`, `AuthError`,
`ConfigError`, `LakekeeperHTTPError`.

# Contributing

The client lives in the
[lakekeeper-clients](https://github.com/lakekeeper/lakekeeper-clients) monorepo alongside
the Java and Spark clients.

```sh
git clone https://github.com/lakekeeper/lakekeeper-clients.git
cd lakekeeper-clients/python
pip install -e '.[dev]'
pytest tests/                        # unit tests
pytest tests/integration -m integration  # needs Docker
```

# License

Licensed under the [Apache License, Version 2.0](http://www.apache.org/licenses/LICENSE-2.0)

[open]: https://cdn.jsdelivr.net/gh/Readme-Workflows/Readme-Icons@main/icons/octicons/IssueNeutral.svg
[semi-done]: https://cdn.jsdelivr.net/gh/Readme-Workflows/Readme-Icons@main/icons/octicons/ApprovedChangesGrey.svg
[done]: https://cdn.jsdelivr.net/gh/Readme-Workflows/Readme-Icons@main/icons/octicons/ApprovedChanges.svg
