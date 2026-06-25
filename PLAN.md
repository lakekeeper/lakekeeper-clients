# Lakekeeper Clients — tracking plan

Living document. Update checkboxes as PRs land.

## What this is

A small, focused client that turns today's hand-rolled `requests` calls (see the reference
notebook) into real SDK methods, and handles auth — **static token *or* client_id+secret with
automatic refresh on expiry**. Languages: Python first, then Java, then a Spark helper.

The scope is **Lakekeeper's generic-tables API + auth**. It is *not* a general Iceberg REST
client — we do not reimplement or wrap the Iceberg catalog surface.

### Core surface (the whole point)

```python
from pylakekeeper import Client, ClientCredentials, StaticToken

c = Client(
    base_url="http://localhost:8181",
    warehouse="demo",                # name or UUID
    project_id="00000000-...",       # optional; sets x-project-id header
    auth=ClientCredentials(token_url=..., client_id=..., client_secret=..., scope=...),
    # or: auth=StaticToken("ey...")
)

# Generic tables — the methods that replace the notebook's raw requests
t = c.generic_tables.load("ai.test", "image_embeddings", vended=True)
import lance
ds = lance.dataset(t.location, storage_options=t.lance_storage_options)

c.generic_tables.create("ai.test", "image_embeddings", format="lance", properties={...})
for ident in c.generic_tables.list("ai.test"):   # follows next-page-token
    ...
c.generic_tables.drop("ai.test", "image_embeddings")
```

## Decisions locked in

- **No Rust core / no UniFFI.** Language-native implementations. (A "Rust core + UniFFI in the
  monorepo" brief was considered and rejected 2026-06-13 — overkill for a generic-tables client.)
- **Standalone Python, no pyiceberg dependency.** Scope is only generic-tables (which pyiceberg
  doesn't model), so `client_credentials` refresh is hand-rolled (~50 LOC). Deps: `httpx` +
  `pydantic`. An optional `iceberg_catalog()` helper behind a `[iceberg]` extra is a *maybe*, later.
- **Auth v1 (MVP): `StaticToken` and `ClientCredentials` (auto-refresh).** device_code / PKCE are
  deferred.
- **Python package**: `pylakekeeper` on PyPI — **both the install and import name** (matches the
  `pyiceberg` convention: `pip install pylakekeeper` → `import pylakekeeper`). The bare `lakekeeper`
  name is held by an unrelated Hadoop-compaction project (`ab2dridi/Lakekeeper`). **Java coords**:
  `io.lakekeeper:lakekeeper-client`, `io.lakekeeper:lakekeeper-spark`.
- **License**: Apache-2.0. **Versioning**: independent per language (`python-vX.Y.Z`, `java-vX.Y.Z`,
  `spark-vX.Y.Z`).
- **Integration tests**: pull the published `lakekeeper` Docker image via testcontainers (confirm tag).

## Reference materials

- De-facto spec for the generic-tables surface + lance creds map:
  `/Users/viktor/Biz/notebooks/lakekeeper_generic_tables.py`.
- Server `%1F` namespace join: `lakekeeper/src/api/iceberg/v1/namespace.rs`.
- OpenAPI (server-emitted): `/Users/viktor/Biz/lakekeeper/docs/docs/api/generic-table-open-api.yaml`
  and `management-open-api.yaml`.

## Repo layout (target)

```
lakekeeper-clients/
├── python/                  # `lakekeeper` on PyPI
├── java/                    # `io.lakekeeper:lakekeeper-client`
├── java-spark/              # `io.lakekeeper:lakekeeper-spark`
└── .github/workflows/       # path-scoped CI per language
```

---

## M1 — `lakekeeper` Python package (MVP)

```
python/src/lakekeeper/
├── __init__.py           -- re-exports Client, auth classes, errors
├── errors.py             -- LakekeeperError hierarchy                       [done]
├── url.py                -- namespace %1F encoding                          [done]
├── auth.py               -- StaticToken, ClientCredentials (refresh + lock)
├── transport.py          -- httpx client: inject token, 401 invalidate-and-retry
├── client.py             -- Client (config + .generic_tables)
├── models.py             -- pydantic response types (LoadGenericTable, identifiers, ...)
├── generic_tables.py     -- create / load(vended) / list(paged) / drop
└── lance.py              -- ICEBERG_TO_LANCE map; storage_options helper
```

### PR sequence

#### PR M1-1 — package scaffolding  ✅
- [x] `pyproject.toml` (standalone: `httpx`, `pydantic`; `[lance]`, `[iceberg]`, `[dev]` extras).
- [x] `ruff` + `mypy` config; `.github/workflows/python-ci.yml` (py3.9 + 3.12, path-scoped).
- [x] `errors.py`, `url.py` (namespace `%1F` encoder).
- [x] Unit tests for namespace round-trip (`tests/test_url.py`).

#### PR M1-2 — auth (the second half of the point)  ✅ (pending merge)
- [x] `auth.py`: `StaticToken`; `ClientCredentials` — POST `token_url` with
      `grant_type=client_credentials`, cache `access_token`, refresh 60s before `exp`, single-flight
      under a lock.
- [x] `transport.py`: httpx client that injects the bearer token and retries once on 401
      (invalidate → refetch → retry).
- [x] Unit tests with `pytest-httpx`: refresh-on-expiry, single-flight (8 concurrent callers → 1
      token fetch), error-status, 401-retry (in `test_generic_tables.py`).

#### PR M1-3 — generic-tables surface + lance helper (the first half of the point)  ✅ (pending merge)
- [x] `models.py`: typed `LoadGenericTableResponse` (parses `storage-credentials` + `config`),
      list identifiers. (kebab↔snake aliases; `extra="ignore"`.)
- [x] `client.py` + `generic_tables.py`: `create`, `load(..., vended=True)` (sets
      `X-Iceberg-Access-Delegation: vended-credentials`), `list` (paged iterator following
      `next-page-token`), `drop`. Namespace encoding internal via `url.py` (verified httpx preserves
      `%1F`).
- [x] `lance.py`: `ICEBERG_TO_LANCE` + `.lance_storage_options` on the load response (incl. the
      `allow_http` rule for `http://` endpoints).
- [x] `formats.py`: `GenericTableFormat` open enum (lance/delta/vortex/paimon/dataset) — server has
      no fixed list, so it's convenience constants + client-side shape validation
      (`^[a-z][a-z0-9_-]{0,63}$`); `create(format=...)` accepts the enum or any valid string.
- [x] `examples/generic_tables_lance.py` reproducing the reference notebook flow.
- Note: 24 unit tests green; ruff + mypy clean. Floor bumped to **Python 3.10** (3.9 is EOL).

#### PR M1-4 — integration test + smoke  ✅ (pending merge)
- [x] Full-stack docker-compose harness in `tests/integration/` (Lakekeeper + Postgres + SeaweedFS +
      Keycloak), self-contained — vendored realm.json + seaweedfs iam.json, no kafka/nats/trino/spark/openfga.
- [x] Storage = **SeaweedFS 4.36** (matches lakekeeper examples; STS authorized via role trust policy,
      no `Admin`/`readOnly` override — per lakekeeper#1867).
- [x] Real OAuth: `client_credentials` (Keycloak `spark` SA) → bootstrap → STS warehouse →
      `generic_tables` create/load-vended/list/drop → **lance write+read roundtrip** with vended STS
      creds, plus a real-Keycloak **token-refresh** test. Both pass in ~30s (`pytest -m integration`).
- [x] Engine-agnostic (`docker` or `podman compose`); marked `integration`, deselected from the unit
      run; CI `integration` job added. Published image confirmed: `quay.io/lakekeeper/catalog:latest-main`.
- [x] Test report: `TEST_REPORT.md` + CI coverage/JUnit artifacts. 27 tests pass, **96% coverage**.
- Notes: host ports 8182/31080/8333 (avoid collisions); the vended `seaweedfs:8333` endpoint is
  rewritten to the host-reachable address in-test (server vends the docker-internal hostname).

#### PR M1-5 — publish 0.1.0 as `pylakekeeper`
- [ ] Register `pylakekeeper` on TestPyPI + PyPI; configure Trusted Publishing (GitHub OIDC, no token).
- [ ] `python-release.yml` on tag `python-v*`: build wheel + sdist, publish to TestPyPI →
      smoke-install (`pip install pylakekeeper`; `import pylakekeeper`) on Linux + macOS → promote to PyPI.

### Exit criteria
- [ ] `examples/generic_tables_lance.py` reproduces the notebook flow end-to-end.
- [ ] Refresh + 401-retry verified by a test that expires/revokes a token mid-flight.
- [ ] `pip install lakekeeper` works on Linux + macOS.
- [ ] Public surface signed off — locks the shape Java mirrors.

---

## M2 — `io.lakekeeper:lakekeeper-client` (Java)

Mirror the **trimmed** Python surface (generic-tables + StaticToken/ClientCredentials), not the old
big one. Do not start until M1 surface is signed off.

```
java/src/main/java/io/lakekeeper/client/
├── Client.java, ClientBuilder.java, ClientConfig.java
├── LakekeeperException.java
├── url/NamespacePath.java               -- %1F encoding
├── auth/{TokenProvider,AccessToken,StaticToken,ClientCredentials}.java   -- refresh + single-flight
├── transport/Http.java                  -- OkHttp + 401-retry Authenticator
├── GenericTables.java                   -- create/load(vended)/list/drop
└── lance/StorageOptions.java            -- iceberg→lance creds map
```

### PR sequence
- [ ] PR M2-1 — Gradle skeleton (Java 17 unless Spark 3.5 forces 11), OkHttp, JUnit 5; `StaticToken`,
      `ClientCredentials` (hand-rolled OAuth2 — Nimbus is too heavy), single-flight via
      `CompletableFuture`, 401-retry `Authenticator`.
- [ ] PR M2-2 — Jackson DTOs; `GenericTables.create/load/list/drop`; vended-creds parsing; example.
- [ ] PR M2-3 — testcontainers-java integration test (same image as Python).
- [ ] PR M2-4 — Maven Central publish (`java-v*`).

### Exit criteria
- [ ] Java example reproduces the notebook flow; integration tests green; jar pulls clean from a
      fresh Gradle project.

---

## M3 — `io.lakekeeper:lakekeeper-spark`

A helper for generic-tables access in Spark (Lance/Delta). Iceberg tables stay on the standard
Iceberg `SparkCatalog` config — we don't replace it.

```scala
import io.lakekeeper.spark.Lakekeeper
val t = Lakekeeper.from(spark, "lk").genericTables.load("ai.test", "image_embeddings")
val df = spark.read.format("lance").options(t.sparkOptions).load(t.location)
```

### Open decisions (resolve at M3 start)
- Spark version matrix (3.5 only? + 4.0? include 3.4?), Scala 2.12 + 2.13 cross-build.
- Auth in Spark: reuse `lakekeeper-client` `ClientCredentials`, or read Iceberg's native OAuth2 conf.

### PR sequence (sketch)
- [ ] M3-1 — Gradle/sbt project depending on `lakekeeper-client` (Java) + spark-provided deps.
- [ ] M3-2 — `Lakekeeper.from(spark, name)` reads `spark.sql.catalog.<name>.*`, builds a client.
- [ ] M3-3 — `genericTables.load/list` → `{location, sparkOptions: Map}`.
- [ ] M3-4 — End-to-end Lance read against MinIO-backed Lakekeeper; Maven Central publish.

---

## Later / maybe (out of the critical path)

Pulled out of the active plan on 2026-06-13 to keep the MVP small. Promote back when there's a need.

- **Auth**: device_code flow, authorization_code + PKCE, disk token cache at `~/.lakekeeper/credentials`.
- **Admin endpoints**: `/management/v1/warehouse` + `/project` (the notebook's `ensure_warehouse`).
  Until then, examples assume the warehouse exists.
- **Iceberg interop**: optional `Client.iceberg_catalog()` behind the `[iceberg]` extra (pyiceberg)
  / iceberg-java `RESTCatalog` factory.
- **Conformance suite**: shared `conformance/scenarios/*.yaml` run by both Python and Java runners.
- **Docs site** (mkdocs), `rename` on generic-tables, async Python surface.

---

## Tracking conventions

- Tick checkboxes only when the corresponding PR is **merged**.
- Each PR description links back to this file with the PR ID (`PR M1-2`, etc.).
- Deferred items live in "Later / maybe"; when promoted, move into the relevant milestone with the
  PR/discussion that settled it.
