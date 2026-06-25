# Lakekeeper Clients

Official client libraries for [Lakekeeper](https://github.com/lakekeeper/lakekeeper) — an open-source Apache Iceberg REST catalog.

These clients make Lakekeeper-specific features easy to use — the **generic-tables API** (Lance/Delta) and **auth** (static token, or client_credentials with automatic refresh). They are not general Iceberg REST clients.

| Artifact | Status | Install |
|---|---|---|
| `pylakekeeper` (Python) | in progress — see [PLAN.md](PLAN.md) | `pip install pylakekeeper` |
| `io.lakekeeper:lakekeeper-client` (Java) | planned | Maven Central |
| `io.lakekeeper:lakekeeper-spark` (Spark plugin) | planned | Maven Central |

See [PLAN.md](PLAN.md) for the implementation roadmap.

## License

Apache-2.0 — matches the Lakekeeper server.
