# pylakekeeper — Test Report

_Generated 2026-06-25. Regenerate with the command in [Reproduce](#reproduce)._

## Summary

| | Result |
|---|---|
| **Total** | **27 passed, 0 failed, 0 errors** |
| Unit tests | 25 passed |
| Integration tests | 2 passed |
| Line coverage | **96%** (251 stmts, 10 missed) |
| Wall time | ~30s (unit < 0.2s; the rest is stack startup/teardown) |
| Python | 3.13.3 |

## Environment

| Component | Value |
|---|---|
| Container engine | podman 5.6 (`podman compose` → docker-compose v2) |
| Lakekeeper server | `quay.io/lakekeeper/catalog:latest-main` |
| Storage | SeaweedFS **4.36** (S3 + IAM/STS), per [lakekeeper#1867](https://github.com/lakekeeper/lakekeeper/pull/1867) |
| Identity provider | Keycloak 26.0.7, realm `iceberg` |
| Database | Postgres 17 |

## Tiers

**Unit (hermetic):** no Docker, no network. HTTP is stubbed with `pytest-httpx`; time is
driven via `monkeypatch`. Runs anywhere in < 0.2s. Deselected marker: none — this is the default
`pytest tests/` run.

**Integration (`-m integration`):** brings up the full stack (Lakekeeper + Postgres + SeaweedFS +
Keycloak) via docker/podman compose in an isolated, auto-torn-down project, and drives the SDK
through **real OAuth2** and **real vended STS credentials**.

## Coverage by module

| Module | Coverage |
|---|---|
| `__init__.py` | 100% |
| `url.py` | 100% |
| `models.py` | 100% |
| `transport.py` | 100% |
| `lance.py` | 100% |
| `generic_tables.py` | 96% |
| `auth.py` | 95% |
| `client.py` | 91% |
| `errors.py` | 86% |
| **Total** | **96%** |

## What's covered

### Namespace encoding — `tests/test_url.py` (11)
Round-trips and edge cases of the `%1F` multi-level namespace quirk: dotted/sequence/raw-separator
parsing, empty-level dropping, raw join for query params, percent-encoded path segments, and
escaping of reserved characters in level names.

### Auth — `tests/test_auth.py` (7)
- `StaticToken` header + empty-token rejection.
- `ClientCredentials`: fetch + cache (1 token request), grant form params.
- **Refresh — explicit invalidate** (the 401 path).
- **Refresh — automatic near expiry** (drives a fake clock across the 60s-before-`exp` threshold;
  asserts 1 → 2 token requests). This is the deterministic "does refresh happen" proof.
- **Single-flight**: 8 concurrent cold callers → exactly 1 token request.
- Error-status raises `AuthError`.

### Generic-tables surface — `tests/test_generic_tables.py` (7)
`%1F` path encoding + `x-project-id` + bearer injection; `vended=True` sets
`X-Iceberg-Access-Delegation`; lance `storage_options` mapping (incl. `allow_http`); create body;
paged `list` following `next-page-token`; 404 → `NotFoundError`; **401 → invalidate + token refetch
+ retry**.

### Integration — `tests/integration/` (2)
| Test | Proves |
|---|---|
| `test_roundtrip::test_generic_tables_vended_lance_roundtrip` | End-to-end through real Keycloak + SeaweedFS: create → load-vended (**real STS creds**) → **lance write + read** → list → drop. |
| `test_token_refresh::test_token_refresh_against_real_keycloak` | A **real** refresh round-trip: Keycloak issues a genuinely new token and Lakekeeper accepts it for a live catalog op. |

## Reproduce

```sh
cd python
pip install -e '.[dev]'

# Unit only (no Docker):
pytest tests/

# Everything + coverage + JUnit XML (needs docker or podman):
mkdir -p reports
pytest -m "integration or not integration" \
  --cov=pylakekeeper --cov-report=term-missing \
  --cov-report=xml:reports/coverage.xml --junitxml=reports/junit.xml
```

Artifacts land in `python/reports/` (`junit.xml`, `coverage.xml`) — gitignored.
