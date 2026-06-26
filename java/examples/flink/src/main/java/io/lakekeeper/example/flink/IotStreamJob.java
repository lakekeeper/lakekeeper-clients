package io.lakekeeper.example.flink;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.lakekeeper.client.LakekeeperClient;
import io.lakekeeper.client.auth.ClientCredentials;
import io.lakekeeper.client.auth.StaticToken;
import io.lakekeeper.client.exception.ConflictException;
import io.lakekeeper.client.model.LoadGenericTableResponse;
import io.lakekeeper.client.model.StorageCredential;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.connector.file.sink.FileSink;
import org.apache.flink.core.fs.FileSystem;
import org.apache.flink.core.fs.Path;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.sink.filesystem.OutputFileConfig;
import org.apache.flink.streaming.api.functions.sink.filesystem.rollingpolicies.OnCheckpointRollingPolicy;

import java.util.HashMap;
import java.util.Map;

/**
 * Flink streaming job: fake IoT sensor data → Lakekeeper generic table (dataset format).
 *
 * <h3>Run locally</h3>
 * <pre>
 * # Copy java/.env.local.example to java/.env.local, fill in values, then:
 * cd java
 * ./gradlew :examples:flink:run
 * </pre>
 *
 * <h3>Submit to a Flink cluster</h3>
 * <pre>
 * ./gradlew :examples:flink:shadowJar
 * flink run examples/flink/build/libs/flink-*-all.jar
 * </pre>
 *
 * <h3>Environment variables</h3>
 * <pre>
 * LAKEKEEPER          Lakekeeper base URL          (default: http://localhost:8181)
 * WAREHOUSE_ID        warehouse UUID or name       (required)
 * TOKEN               static bearer token          (alternative: OAUTH_* vars)
 * OAUTH_TOKEN_URL / OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OAUTH_SCOPE
 * NAMESPACE           Lakekeeper namespace         (default: iot)
 * TABLE               table name                   (default: sensor-readings)
 * NUM_SENSORS         number of virtual sensors    (default: 5)
 * NUM_RECORDS         total records; -1 = forever  (default: -1)
 * BATCH_SIZE          records per file             (default: 10)
 * BATCH_INTERVAL_MS   ms between batches           (default: 15000)
 * </pre>
 */
public class IotStreamJob {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    public static void main(String[] args) throws Exception {
        Map<String, String> env = System.getenv();

        String lakekeeperUrl  = env.getOrDefault("LAKEKEEPER", "http://localhost:8181");
        String warehouseId    = env.getOrDefault("WAREHOUSE_ID", "");
        String namespace      = env.getOrDefault("NAMESPACE", "iot");
        String table          = env.getOrDefault("TABLE", "sensor-readings");
        int    numSensors     = Integer.parseInt(env.getOrDefault("NUM_SENSORS", "5"));
        int    batchSize      = Integer.parseInt(env.getOrDefault("BATCH_SIZE", "10"));
        long   batchIntervalMs = Long.parseLong(env.getOrDefault("BATCH_INTERVAL_MS", "15000"));

        // -----------------------------------------------------------------------
        // 1. Register table in Lakekeeper; get vended S3 credentials
        // -----------------------------------------------------------------------
        LoadGenericTableResponse resp;
        try (LakekeeperClient lk = LakekeeperClient.builder()
                .baseUrl(lakekeeperUrl)
                .warehouse(warehouseId)
                .auth(buildAuth(env))
                .build()) {

            try {
                resp = lk.genericTables().create(namespace, table, "dataset");
                System.out.printf("Created  %s.%s → %s%n", namespace, table, resp.getLocation());
            } catch (ConflictException e) {
                System.out.printf("Existing %s.%s%n", namespace, table);
                resp = null;
            }
            // Always reload with vended=true to get short-lived S3 credentials
            resp = lk.genericTables().load(namespace, table, true);
            System.out.printf("Location: %s%n", resp.getLocation());
        }

        String location = resp.getLocation();

        // -----------------------------------------------------------------------
        // 2. Wire vended S3 credentials into Flink BEFORE building the stream graph
        //
        //    flink-s3-fs-hadoop is registered via ServiceLoader from the classpath.
        //    FileSystem.initialize() with null pluginManager uses ServiceLoader only.
        //    The s3.* Flink config keys map to fs.s3a.* Hadoop config keys inside
        //    flink-s3-fs-hadoop.
        // -----------------------------------------------------------------------
        Configuration flinkConf = vendedCredsToFlinkConf(resp);
        FileSystem.initialize(flinkConf, null);

        // -----------------------------------------------------------------------
        // 3. Flink streaming job
        // -----------------------------------------------------------------------
        StreamExecutionEnvironment flinkEnv =
                StreamExecutionEnvironment.createLocalEnvironment(1, flinkConf);
        flinkEnv.enableCheckpointing(batchIntervalMs);

        DataStream<String> jsonStream = flinkEnv
                .addSource(new SensorBatchSource(numSensors, batchSize, batchIntervalMs))
                .name("IoT Sensor Generator")
                .map((MapFunction<SensorReading, String>) r -> MAPPER.writeValueAsString(r))
                .name("JSON Serializer");

        // -----------------------------------------------------------------------
        // 4. Sink: rolling JSON array files at the Lakekeeper location.
        //    Files roll on every checkpoint (~10 s), producing valid JSON arrays.
        // -----------------------------------------------------------------------
        FileSink<String> sink = FileSink
                .forBulkFormat(new Path(location), JsonArrayBulkWriter.factory())
                .withRollingPolicy(OnCheckpointRollingPolicy.build())
                .withOutputFileConfig(
                        OutputFileConfig.builder().withPartSuffix(".json").build())
                .build();

        jsonStream.sinkTo(sink).name("Lakekeeper JSON Sink");

        System.out.printf("Streaming %d records/file every %ds from %d sensors → %s%n",
                batchSize, batchIntervalMs / 1000, numSensors, location);
        flinkEnv.execute("IoT → Lakekeeper");
    }

    // -----------------------------------------------------------------------
    // Credential translation: Iceberg S3 keys → Flink + Hadoop S3A keys.
    //
    // flink-s3-fs-hadoop maps s3.access-key / s3.secret-key → fs.s3a.access.key /
    // fs.s3a.secret.key, but s3.session-token is NOT a documented Flink shorthand
    // and does not get mapped. We therefore set fs.s3a.* keys directly alongside
    // the s3.* shorthands so the Hadoop credential provider always finds them.
    //
    // STS vended credentials require TemporaryAWSCredentialsProvider; the default
    // SimpleAWSCredentialsProvider ignores the session token and AWS rejects the
    // bare STS access key with InvalidAccessKeyId.
    // -----------------------------------------------------------------------
    private static Configuration vendedCredsToFlinkConf(LoadGenericTableResponse resp) {
        Map<String, String> s3props = new HashMap<>();
        for (StorageCredential cred : resp.getStorageCredentials()) {
            s3props.putAll(cred.getConfig());
        }
        if (resp.getConfig() != null) s3props.putAll(resp.getConfig());

        System.out.println("[Lakekeeper] vended credential keys: " + s3props.keySet());

        String accessKey    = s3props.get("s3.access-key-id");
        String secretKey    = s3props.get("s3.secret-access-key");
        String sessionToken = s3props.get("s3.session-token");

        Configuration conf = new Configuration();
        // Set fs.s3a.* directly — TemporaryAWSCredentialsProvider reads Hadoop config,
        // not Flink shorthands. s3.session-token has no Flink→Hadoop mapping.
        putIfPresent(conf, "fs.s3a.access.key",    accessKey);
        putIfPresent(conf, "fs.s3a.secret.key",    secretKey);
        putIfPresent(conf, "fs.s3a.session.token", sessionToken);

        if (sessionToken != null && !sessionToken.isEmpty()) {
            conf.setString("fs.s3a.aws.credentials.provider",
                    "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider");
        }

        String endpoint = s3props.getOrDefault("s3.endpoint", s3props.get("client.endpoint"));
        if (endpoint != null && !endpoint.isEmpty()) {
            conf.setString("fs.s3a.endpoint", endpoint);
            conf.setBoolean("fs.s3a.path.style.access", true);
        }

        // STS session policies typically omit s3:DeleteObject. S3A creates directory
        // marker objects and then deletes them; keeping them avoids the AccessDenied error.
        conf.setString("fs.s3a.directory.marker.retention", "keep");

        return conf;
    }

    private static void putIfPresent(Configuration conf, String key, String value) {
        if (value != null && !value.isEmpty()) conf.setString(key, value);
    }

    private static io.lakekeeper.client.auth.Auth buildAuth(Map<String, String> env) {
        String token = env.get("TOKEN");
        if (token != null && !token.isEmpty()) return new StaticToken(token);
        return new ClientCredentials(
                env.get("OAUTH_TOKEN_URL"),
                env.get("OAUTH_CLIENT_ID"),
                env.get("OAUTH_CLIENT_SECRET"),
                env.get("OAUTH_SCOPE"),
                60, 30);
    }
}
