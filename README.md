# Lakekeeper Clients

Official client libraries for [Lakekeeper](https://github.com/lakekeeper/lakekeeper) — an
open-source Apache Iceberg REST catalog.

These clients make Lakekeeper-specific features easy to use: the **generic-tables API**
(write Lance, Delta, or any dataset format with vended S3 credentials) and **auth**
(static token or OAuth2 client-credentials with automatic refresh).

## Libraries

| Artifact | Language | Install |
|---|---|---|
| [`pylakekeeper`](python/) | Python | `pip install pylakekeeper` |
| [`io.lakekeeper:lakekeeper-client`](java/core/) | Java | [GitHub Packages](https://github.com/lakekeeper/lakekeeper-clients/packages) |
| [`io.lakekeeper:lakekeeper-spark`](java/spark/) | Java / Spark | [GitHub Packages](https://github.com/lakekeeper/lakekeeper-clients/packages) |

## Examples

| Example | Description |
|---|---|
| [Flink IoT stream](java/examples/flink/) | Write IoT sensor data to a Lakekeeper generic table on S3 using Flink `FileSink` |
| [Lance dataset (Python)](python/examples/) | Create a Lance generic table, write and read a dataset with vended credentials |
| [Dataset images (Python)](python/examples/) | Upload image files to a `dataset` generic table via boto3 |

## Quick start

### Python

```sh
pip install pylakekeeper
```

```python
from pylakekeeper import Client, StaticToken

lk = Client(base_url="http://localhost:8181", warehouse="my-warehouse",
            auth=StaticToken("my-token"))

# Create a Lance table and write to it
resp = lk.generic_tables.create("ai.models", "embeddings", format="lance")
resp = lk.generic_tables.load("ai.models", "embeddings", vended=True)

import lance, pyarrow as pa
lance.write_dataset(data, resp.location, storage_options=resp.lance_storage_options)

# Or open as an fsspec filesystem
import fsspec
fs = fsspec.filesystem("s3", **resp.fsspec_kwargs)
files = fs.ls(resp.location)
```

### Java

```xml
<!-- Maven -->
<dependency>
  <groupId>io.lakekeeper</groupId>
  <artifactId>lakekeeper-client</artifactId>
  <version>0.1.0</version>
</dependency>
```

```java
try (LakekeeperClient lk = LakekeeperClient.builder()
        .baseUrl("http://localhost:8181")
        .warehouse("my-warehouse")
        .auth(new StaticToken("my-token"))
        .build()) {

    LoadGenericTableResponse resp =
        lk.genericTables().load("iot", "sensor-readings", true);
    System.out.println(resp.getLocation());
}
```

## License

Apache-2.0 — matches the Lakekeeper server.
