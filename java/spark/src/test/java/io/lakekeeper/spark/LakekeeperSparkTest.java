package io.lakekeeper.spark;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import io.lakekeeper.client.LakekeeperClient;
import io.lakekeeper.client.auth.StaticToken;
import io.lakekeeper.client.model.LoadGenericTableResponse;
import org.apache.spark.sql.SparkSession;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.Collections;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Spark integration tests — excluded by default. Run with:
 * {@code ./gradlew :spark:test -PsparkTests}
 */
@Tag("spark")
class LakekeeperSparkTest {
    private static SparkSession spark;
    private WireMockServer server;
    private LakekeeperClient client;

    @BeforeAll
    static void startSpark() {
        spark = SparkSession.builder()
                .master("local[1]")
                .appName("lakekeeper-spark-test")
                .config("spark.ui.enabled", "false")
                .config("spark.sql.shuffle.partitions", "1")
                .getOrCreate();
    }

    @AfterAll
    static void stopSpark() {
        if (spark != null) spark.stop();
    }

    @BeforeEach
    void setUp() {
        server = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
        server.start();
        client = LakekeeperClient.builder()
                .baseUrl(server.baseUrl())
                .warehouse("wh")
                .auth(new StaticToken("tok"))
                .build();
    }

    @AfterEach
    void tearDown() {
        server.stop();
        client.close();
    }

    @Test
    void configureIcebergCatalogSetsSparkConf() {
        LakekeeperSpark.configureIcebergCatalog(
                spark, "lk", "http://lakekeeper/catalog", "my-client", "my-secret", "lakekeeper");

        assertEquals("org.apache.iceberg.spark.SparkCatalog", spark.conf().get("spark.sql.catalog.lk"));
        assertEquals("org.apache.iceberg.rest.RESTCatalog", spark.conf().get("spark.sql.catalog.lk.catalog-impl"));
        assertEquals("http://lakekeeper/catalog", spark.conf().get("spark.sql.catalog.lk.uri"));
        assertEquals("my-client:my-secret", spark.conf().get("spark.sql.catalog.lk.credential"));
        assertEquals("lakekeeper", spark.conf().get("spark.sql.catalog.lk.scope"));
    }

    @Test
    void configureIcebergCatalogWithoutScope() {
        LakekeeperSpark.configureIcebergCatalog(
                spark, "lk2", "http://lakekeeper/catalog", "id", "secret", null);

        assertEquals("org.apache.iceberg.spark.SparkCatalog", spark.conf().get("spark.sql.catalog.lk2"));
        assertThrows(Exception.class, () -> spark.conf().get("spark.sql.catalog.lk2.scope"));
    }

    @Test
    void resolveLocationCallsGenericTablesLoad() {
        stubTableLoad("ns", "my-table", "parquet", "file:///tmp/test-table");

        String location = LakekeeperSpark.resolveLocation(client, "ns", "my-table");
        assertEquals("file:///tmp/test-table", location);
    }

    @Test
    void readDataFrameFromLocalParquet(@TempDir Path tmpDir) throws Exception {
        // Write a small Parquet file to temp dir so Spark can actually read it
        spark.range(3).toDF("id")
                .write().mode("overwrite").parquet(tmpDir.toString());

        stubTableLoad("ns", "parquet-table", "parquet", tmpDir.toUri().toString());

        var df = LakekeeperSpark.read(spark, client, "ns", "parquet-table");
        assertEquals(3, df.count());
    }

    @Test
    void writeDataFrameToLocalParquet(@TempDir Path tmpDir) {
        String location = tmpDir.resolve("output").toUri().toString();
        stubTableLoad("ns", "out-table", "parquet", location);

        var df = spark.range(5).toDF("id");
        LakekeeperSpark.write(spark, client, "ns", "out-table", df, Collections.singletonMap("mode", "overwrite"));

        // Verify the data was written by reading it back directly
        long count = spark.read().parquet(location).count();
        assertEquals(5, count);
    }

    @Test
    void applyVendedCredentialsMapsToHadoop() {
        server.stubFor(get(urlPathMatching(".*/generic-tables/creds-table"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{"
                                + "\"table\":{\"name\":\"creds-table\",\"format\":\"parquet\","
                                + "\"base-location\":\"s3://bucket/path\"},"
                                + "\"storage-credentials\":[{"
                                + "\"prefix\":\"s3://bucket\","
                                + "\"config\":{"
                                + "\"s3.access-key-id\":\"AKIATEST\","
                                + "\"s3.secret-access-key\":\"secret123\","
                                + "\"s3.session-token\":\"session456\","
                                + "\"s3.region\":\"us-east-1\""
                                + "}}]}")));

        LoadGenericTableResponse resp = client.genericTables().load("ns", "creds-table", true);
        LakekeeperSpark.applyVendedCredentials(spark, resp);

        org.apache.hadoop.conf.Configuration conf = spark.sparkContext().hadoopConfiguration();
        assertEquals("AKIATEST",   conf.get("fs.s3a.access.key"));
        assertEquals("secret123",  conf.get("fs.s3a.secret.key"));
        assertEquals("session456", conf.get("fs.s3a.session.token"));
        assertEquals("us-east-1",  conf.get("fs.s3a.endpoint.region"));
    }

    // -------------------------------------------------------------------------

    private void stubTableLoad(String namespace, String name, String format, String location) {
        server.stubFor(get(urlPathMatching(".*/generic-tables/" + name))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"table\":{\"name\":\"" + name + "\","
                                + "\"format\":\"" + format + "\","
                                + "\"base-location\":\"" + location + "\"}}")));
    }
}
