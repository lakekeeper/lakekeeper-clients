package io.lakekeeper.spark;

import io.lakekeeper.client.LakekeeperClient;
import io.lakekeeper.client.model.LoadGenericTableResponse;
import io.lakekeeper.client.model.StorageCredential;
import org.apache.spark.sql.Dataset;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

import java.util.HashMap;
import java.util.Map;

/**
 * Spark integration helpers for Lakekeeper.
 *
 * <p>Three use-cases:
 * <ol>
 *   <li><b>Iceberg tables</b> — configure the Lakekeeper REST catalog so
 *       {@code spark.table("lakekeeper.ns.my_table")} works.
 *   <li><b>Generic tables — read</b> — resolve location + vended credentials,
 *       apply them to the SparkSession, and return a {@code Dataset<Row>}.
 *   <li><b>Generic tables — write</b> — write a DataFrame to the table's location;
 *       vended credentials are applied for the duration of the write.
 * </ol>
 *
 * <h3>Lance read example</h3>
 * <pre>{@code
 * Dataset<Row> df = LakekeeperSpark.read(spark, client, "my-ns", "my-lance-table");
 * df.show();
 * }</pre>
 *
 * <h3>Lance write example</h3>
 * <pre>{@code
 * LakekeeperSpark.write(spark, client, "my-ns", "my-lance-table", df,
 *         Map.of("mode", "overwrite"));
 * }</pre>
 */
public final class LakekeeperSpark {
    // Iceberg S3 credential keys → Hadoop/s3a keys (used by most Spark connectors)
    private static final Map<String, String> ICEBERG_TO_HADOOP = new HashMap<>();

    static {
        ICEBERG_TO_HADOOP.put("s3.access-key-id",     "fs.s3a.access.key");
        ICEBERG_TO_HADOOP.put("s3.secret-access-key", "fs.s3a.secret.key");
        ICEBERG_TO_HADOOP.put("s3.session-token",     "fs.s3a.session.token");
        ICEBERG_TO_HADOOP.put("s3.region",            "fs.s3a.endpoint.region");
        ICEBERG_TO_HADOOP.put("client.region",        "fs.s3a.endpoint.region");
        ICEBERG_TO_HADOOP.put("s3.endpoint",          "fs.s3a.endpoint");
    }

    private LakekeeperSpark() {}

    // -------------------------------------------------------------------------
    // Iceberg REST catalog
    // -------------------------------------------------------------------------

    /**
     * Configure a Lakekeeper Iceberg REST catalog in the given SparkSession.
     *
     * @param spark        SparkSession to configure
     * @param catalogName  Spark catalog alias (e.g. {@code "lakekeeper"})
     * @param catalogUri   Lakekeeper Iceberg REST catalog URI
     * @param clientId     OAuth2 client ID
     * @param clientSecret OAuth2 client secret
     * @param scope        OAuth2 scope (nullable)
     */
    public static void configureIcebergCatalog(
            SparkSession spark,
            String catalogName,
            String catalogUri,
            String clientId,
            String clientSecret,
            String scope) {
        String prefix = "spark.sql.catalog." + catalogName;
        spark.conf().set(prefix, "org.apache.iceberg.spark.SparkCatalog");
        spark.conf().set(prefix + ".catalog-impl", "org.apache.iceberg.rest.RESTCatalog");
        spark.conf().set(prefix + ".uri", catalogUri);
        spark.conf().set(prefix + ".credential", clientId + ":" + clientSecret);
        if (scope != null) {
            spark.conf().set(prefix + ".scope", scope);
        }
    }

    // -------------------------------------------------------------------------
    // Generic tables — read
    // -------------------------------------------------------------------------

    /**
     * Resolve a generic table's location and read it as a Spark DataFrame.
     *
     * <p>The format (e.g. {@code "lance"}, {@code "delta"}, {@code "parquet"}) is taken
     * from the Lakekeeper table metadata. The appropriate Spark connector must be on the
     * classpath.
     *
     * <p>If the table's base location is on S3, vended credentials are requested and
     * applied to the Hadoop configuration for the duration of this call.
     *
     * <pre>{@code
     * Dataset<Row> df = LakekeeperSpark.read(spark, client, "my-ns", "lance-table");
     * df.show();
     * }</pre>
     */
    public static Dataset<Row> read(
            SparkSession spark, LakekeeperClient client, String namespace, String tableName) {
        LoadGenericTableResponse resp = client.genericTables().load(namespace, tableName, true);
        applyVendedCredentials(spark, resp);
        return spark.read().format(resp.getTable().getFormat()).load(resp.getLocation());
    }

    /**
     * Same as {@link #read} but passes extra format options (e.g. schema hints, version pins).
     */
    public static Dataset<Row> read(
            SparkSession spark,
            LakekeeperClient client,
            String namespace,
            String tableName,
            Map<String, String> formatOptions) {
        LoadGenericTableResponse resp = client.genericTables().load(namespace, tableName, true);
        applyVendedCredentials(spark, resp);
        return spark.read().format(resp.getTable().getFormat())
                .options(formatOptions)
                .load(resp.getLocation());
    }

    // -------------------------------------------------------------------------
    // Generic tables — write
    // -------------------------------------------------------------------------

    /**
     * Write a DataFrame to a Lakekeeper generic table.
     *
     * <p>The table must already exist in Lakekeeper. Vended credentials are requested
     * and applied before writing. {@code writeOptions} are passed directly to the
     * Spark format writer — use them for {@code "mode"} ({@code "overwrite"} /
     * {@code "append"}), partitioning hints, or connector-specific options.
     *
     * <pre>{@code
     * Map<String, String> opts = new HashMap<>();
     * opts.put("mode", "overwrite");
     * LakekeeperSpark.write(spark, client, "my-ns", "lance-table", df, opts);
     * }</pre>
     */
    public static void write(
            SparkSession spark,
            LakekeeperClient client,
            String namespace,
            String tableName,
            Dataset<Row> df,
            Map<String, String> writeOptions) {
        LoadGenericTableResponse resp = client.genericTables().load(namespace, tableName, true);
        applyVendedCredentials(spark, resp);

        String mode = writeOptions.getOrDefault("mode", "append");
        Map<String, String> opts = new HashMap<>(writeOptions);
        opts.remove("mode");

        df.write()
                .format(resp.getTable().getFormat())
                .options(opts)
                .mode(mode)
                .save(resp.getLocation());
    }

    // -------------------------------------------------------------------------
    // Low-level helpers
    // -------------------------------------------------------------------------

    /**
     * Resolve the base storage location of a generic table without reading it.
     * Use this when you want full control over the Spark reader.
     */
    public static String resolveLocation(LakekeeperClient client, String namespace, String tableName) {
        return client.genericTables().load(namespace, tableName).getLocation();
    }

    /**
     * Resolve a generic table with vended storage credentials.
     */
    public static LoadGenericTableResponse resolveTable(
            LakekeeperClient client, String namespace, String tableName, boolean vended) {
        return client.genericTables().load(namespace, tableName, vended);
    }

    /**
     * Translate vended Iceberg-style S3 credentials into Hadoop {@code fs.s3a.*} config
     * and apply them to the active SparkSession's Hadoop configuration.
     *
     * <p>This makes the credentials available to any Spark connector that uses the
     * Hadoop FileSystem API (Parquet, ORC, Lance-Spark, Delta, etc.).
     */
    public static void applyVendedCredentials(SparkSession spark, LoadGenericTableResponse resp) {
        // Merge storage-credential config entries first, then top-level config (wins on conflict).
        Map<String, String> merged = new HashMap<>();
        for (StorageCredential cred : resp.getStorageCredentials()) {
            merged.putAll(cred.getConfig());
        }
        if (resp.getConfig() != null) {
            merged.putAll(resp.getConfig());
        }

        org.apache.hadoop.conf.Configuration hadoopConf =
                spark.sparkContext().hadoopConfiguration();
        merged.forEach((icebergKey, value) -> {
            String hadoopKey = ICEBERG_TO_HADOOP.get(icebergKey);
            if (hadoopKey != null) {
                hadoopConf.set(hadoopKey, value);
            }
        });

        // If the S3 endpoint is plain HTTP, enable non-SSL access.
        String endpoint = merged.get("s3.endpoint");
        if (endpoint != null && endpoint.startsWith("http://")) {
            hadoopConf.set("fs.s3a.connection.ssl.enabled", "false");
            hadoopConf.set("fs.s3a.path.style.access", "true");
        }
    }
}
