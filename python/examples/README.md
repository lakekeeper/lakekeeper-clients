# Running the examples

- **`generic_tables_lance.py`** — reproduces the notebook flow: create a Lance generic table, load
  it with vended credentials, and write/read a dataset through `lance`.
- **`dataset_images.py`** — `dataset` format (unstructured data): upload image files to the table's
  vended location via `boto3` (the S3 case).

### Notebooks — one per auth flow, each a different table format

Each notebook signs in with a different OAuth2 flow, creates a generic table, round-trips real
data, and ends by proving the session survives access-token expiry via silent `refresh_token`
renewal:

| Notebook | Auth flow | Format | Data write |
| --- | --- | --- | --- |
| `auth_devicecode_lance.ipynb` | Device Code (RFC 8628) | Lance | vended S3 via `lance` |
| `auth_authcode_vortex.ipynb` | Authorization Code + PKCE (RFC 7636) | Vortex | local `.vortex` (see note in nb) |
| `auth_clientcredentials_delta.ipynb` | `client_credentials` (service) | Delta | vended S3 via `deltalake` |
| `auth_devicecode_hdf5.ipynb` | Device Code (RFC 8628) | `dataset` (HDF5) | vended S3 via `h5py` + `boto3` |

## 1. Install deps

```sh
cd python
pip install -e '.[dev]' numpy   # dev brings pylance + pyarrow; the example also uses numpy
```

## 2. Have a Lakekeeper to point at

The example creates a *table* but assumes the **warehouse and namespace already exist** (warehouse
admin is intentionally out of the SDK). Easiest target is Lakekeeper's own **`examples/minimal`**
stack — no auth, ships a `demo` warehouse:

```sh
# in the lakekeeper repo:
cd examples/minimal && docker compose up -d
```

(Or use any Lakekeeper you have. The `access-control-advanced` stack adds Keycloak **and** OpenFGA
authorization, so it needs extra grants — not the easiest for a quick test.)

## 3. Configure env

```sh
cp examples/.env.example examples/.env
# edit examples/.env — for the no-auth minimal stack:  TOKEN=dev
set -a; source examples/.env; set +a
```

## 4. Satisfy the two prerequisites

**Warehouse UUID** (the SDK uses it as the URL prefix — a name won't work):

```sh
curl -s "$LAKEKEEPER/management/v1/warehouse" \
  -H "Authorization: Bearer $TOKEN" -H "x-project-id: $PROJECT_ID" \
  | jq '.warehouses[] | {name, id}'
# put the matching id into WAREHOUSE_ID= in your .env, then re-source it
```

**Namespace** (idempotent — 409 if it already exists, which is fine):

```sh
curl -s -X POST "$LAKEKEEPER/catalog/v1/$WAREHOUSE_ID/namespaces" \
  -H "Authorization: Bearer $TOKEN" -H "x-project-id: $PROJECT_ID" \
  -H 'Content-Type: application/json' -d '{"namespace": ["ai", "test"]}'
```

## 5. Run

**Load the env, then run with the venv's Python** (not your system `python3` — the package and
`pylance`/`pyarrow` wheels live in the venv; bare `python3` will `ModuleNotFoundError`):

```sh
set -a; source examples/.env; set +a          # re-run this whenever you edit .env

.venv/bin/python examples/generic_tables_lance.py   # Lance roundtrip
.venv/bin/python examples/dataset_images.py         # upload images (dataset format)
```

Or `source .venv/bin/activate` once, then plain `python examples/<file>.py`.

## Running the notebooks

The notebooks import `pylakekeeper`, so their kernel must be the venv where it's installed —
**not** whatever Python your Jupyter/VS Code happens to launch. Register the venv as a kernel
once, then select **“Python (pylakekeeper)”** in the notebook's kernel picker:

```sh
cd python
pip install -e '.[examples]'    # pylakekeeper + jupyter + lance/vortex/deltalake + pyarrow/numpy
python -m ipykernel install --user --name pylakekeeper --display-name "Python (pylakekeeper)"
```

> **`ipykernel` must be `<7`.** The `examples` extra pins it: ipykernel 7.x writes a kernelspec
> that negotiates CurveZMQ encryption, which several frontends (VS Code's Jupyter extension,
> older `jupyter_client`) can't attach to — the kernel boots but never connects, i.e. it looks
> like "the kernel won't start." If you hit that, `pip install 'ipykernel<7'` and re-run the
> `ipykernel install` line above, then reload the window and reselect the kernel.

`ModuleNotFoundError: No module named 'pylakekeeper'` in a notebook means the wrong kernel is
selected — switch to "Python (pylakekeeper)".

## Turnkey alternative (no manual setup)

`./examples/run.sh` brings up a self-contained stack (Lakekeeper + Postgres + SeaweedFS + Keycloak),
bootstraps it, creates the warehouse + namespace, and runs the example **in-network** — no env file,
no warehouse lookup, no Docker port juggling. Tear down with `./examples/run.sh --down`.
