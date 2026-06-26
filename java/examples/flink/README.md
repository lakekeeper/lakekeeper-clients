# Flink Example — IoT Sensor Stream → Lakekeeper

Streams fake IoT sensor readings from N virtual sensors into a Lakekeeper **generic table**
(format: `dataset`) on S3. Each file is a valid JSON array committed roughly every
`BATCH_INTERVAL_MS` milliseconds; the job runs indefinitely until you press Ctrl+C.

```
sensor-001 → {"sensor_id":"sensor-001","timestamp_ms":...,"temperature":23.4,...}
sensor-002 → ...                                                              ↓
...                                              s3://bucket/prefix/iot/sensor-readings/
                                                 └── part-0-0.json   ← 10 records
                                                 └── part-0-1.json   ← 10 records
                                                 └── ...
```

## Prerequisites

- Java 11+
- A running Lakekeeper instance with a warehouse and namespace already created
- The warehouse's S3 backend accessible from your machine

The example creates the table automatically (idempotent — safe to re-run against an existing table).

## 1. Configure

```sh
cp java/.env.local.example java/.env.local
# edit java/.env.local
```

Minimum required values:

| Variable | Description |
|---|---|
| `LAKEKEEPER` | Lakekeeper base URL (default: `http://localhost:8181`) |
| `WAREHOUSE_ID` | Warehouse UUID (find it via the management API or UI) |
| `TOKEN` | Static bearer token **or** use the `OAUTH_*` variables below |
| `OAUTH_TOKEN_URL` / `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `OAUTH_SCOPE` | OAuth2 client credentials (alternative to `TOKEN`) |

Optional tuning:

| Variable | Default | Description |
|---|---|---|
| `NAMESPACE` | `iot` | Lakekeeper namespace |
| `TABLE` | `sensor-readings` | Table name |
| `NUM_SENSORS` | `5` | Number of virtual sensors |
| `BATCH_SIZE` | `10` | Records written per file |
| `BATCH_INTERVAL_MS` | `15000` | Milliseconds between batches (= file roll interval) |

## 2. Run locally

```sh
cd java
./gradlew :examples:flink:run
```

Gradle reads `java/.env.local` automatically. The job streams until Ctrl+C.

Expected output:

```
Created  iot.sensor-readings → s3://your-bucket/prefix/iot/sensor-readings
Location: s3://your-bucket/prefix/iot/sensor-readings
[Lakekeeper] vended credential keys: [s3.access-key-id, s3.secret-access-key, s3.session-token]
Streaming 10 records/file every 15s from 5 sensors → s3://your-bucket/...
```

A new `.json` file appears at the S3 location every ~15 seconds.

## 3. Submit to a Flink cluster

Build the self-contained fat JAR:

```sh
cd java
./gradlew :examples:flink:shadowJar
# → examples/flink/build/libs/flink-<version>-all.jar
```

Submit:

```sh
flink run \
  -Ds3.access-key=... \       # or set via cluster config / IAM role
  examples/flink/build/libs/flink-*-all.jar
```

Environment variables must be available to the TaskManager JVM (use Flink's
`env.java.opts` or cluster-level config for secrets — don't pass credentials on the
command line in production).

## How it works

1. **Table registration** — creates the generic table in Lakekeeper (no-op on conflict),
   then loads it with `vended=true` to get short-lived STS credentials scoped to the
   table's S3 prefix.

2. **Credential wiring** — the vended STS credentials are injected into Hadoop S3A config
   (`fs.s3a.*`) with `TemporaryAWSCredentialsProvider` before the stream graph is built.
   Flink's `flink-s3-fs-hadoop` plugin picks them up via ServiceLoader.

3. **Streaming sink** — `FileSink` with a custom `JsonArrayBulkWriter` writes each rolled
   file as a proper JSON array `[{...},{...}]`. Files roll on every Flink checkpoint
   (every `BATCH_INTERVAL_MS` ms) and are committed with a `.json` extension.
