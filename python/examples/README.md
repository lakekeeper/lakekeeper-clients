# Running the examples

- **`generic_tables_lance.py`** — reproduces the notebook flow: create a Lance generic table, load
  it with vended credentials, and write/read a dataset through `lance`.
- **`dataset_images.py`** — `dataset` format (unstructured data): upload image files to the table's
  vended location via `boto3` (the S3 case).

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

## Turnkey alternative (no manual setup)

`./examples/run.sh` brings up a self-contained stack (Lakekeeper + Postgres + SeaweedFS + Keycloak),
bootstraps it, creates the warehouse + namespace, and runs the example **in-network** — no env file,
no warehouse lookup, no Docker port juggling. Tear down with `./examples/run.sh --down`.
