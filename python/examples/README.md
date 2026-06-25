# Running the examples

`generic_tables_lance.py` reproduces the notebook flow: create a Lance generic table, load it
with vended credentials, and write/read a dataset through `lance`.

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

```sh
python examples/generic_tables_lance.py
```

> Want the whole thing turnkey (stack + bootstrap + warehouse + namespace + run) with one command?
> That's essentially what `tests/integration/` already automates — say the word and I'll add an
> `examples/run.sh` that reuses it.
