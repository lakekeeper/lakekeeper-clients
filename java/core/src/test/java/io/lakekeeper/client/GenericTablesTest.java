package io.lakekeeper.client;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import io.lakekeeper.client.auth.StaticToken;
import io.lakekeeper.client.exception.ConflictException;
import io.lakekeeper.client.exception.NotFoundException;
import io.lakekeeper.client.model.GenericTableIdentifier;
import io.lakekeeper.client.model.LoadGenericTableResponse;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static org.junit.jupiter.api.Assertions.*;

class GenericTablesTest {
    private static final String WAREHOUSE = "my-warehouse";
    private static final String TOKEN = "test-token";

    private WireMockServer server;
    private LakekeeperClient client;

    @BeforeEach
    void setUp() {
        server = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
        server.start();
        client = LakekeeperClient.builder()
                .baseUrl(server.baseUrl())
                .warehouse(WAREHOUSE)
                .auth(new StaticToken(TOKEN))
                .build();
    }

    @AfterEach
    void tearDown() {
        server.stop();
        client.close();
    }

    @Test
    void loadTable() {
        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/my-table"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{"
                                + "\"table\":{"
                                + "\"name\":\"my-table\","
                                + "\"format\":\"lance\","
                                + "\"base-location\":\"s3://bucket/prefix\""
                                + "}}")));

        LoadGenericTableResponse resp = client.genericTables().load("ns", "my-table");

        assertEquals("my-table", resp.getTable().getName());
        assertEquals("lance", resp.getTable().getFormat());
        assertEquals("s3://bucket/prefix", resp.getLocation());
        server.verify(getRequestedFor(urlPathEqualTo(
                "/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/my-table"))
                .withHeader("Authorization", equalTo("Bearer " + TOKEN)));
    }

    @Test
    void loadTableMultiLevelNamespace() {
        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ai%1Ftest/generic-tables/tbl"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"table\":{\"name\":\"tbl\",\"format\":\"delta\",\"base-location\":\"s3://b/p\"}}")));

        LoadGenericTableResponse resp = client.genericTables().load("ai.test", "tbl");
        assertEquals("tbl", resp.getTable().getName());
    }

    @Test
    void createTable() {
        server.stubFor(post(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"table\":{\"name\":\"new-tbl\",\"format\":\"lance\",\"base-location\":\"s3://b/new\"}}")));

        LoadGenericTableResponse resp = client.genericTables().create("ns", "new-tbl", "lance");
        assertEquals("new-tbl", resp.getTable().getName());
        server.verify(postRequestedFor(urlPathEqualTo(
                "/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables"))
                .withRequestBody(containing("\"format\":\"lance\"")));
    }

    @Test
    void dropTable() {
        server.stubFor(delete(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/tbl"))
                .willReturn(aResponse().withStatus(204)));

        assertDoesNotThrow(() -> client.genericTables().drop("ns", "tbl"));
    }

    @Test
    void listTablesPaginated() {
        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables"))
                .withQueryParam("pageSize", equalTo("2"))
                .withQueryParam("pageToken", absent())
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"identifiers\":[{\"namespace\":[\"ns\"],\"name\":\"t1\"}],"
                                + "\"next-page-token\":\"tok2\"}")));

        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables"))
                .withQueryParam("pageToken", equalTo("tok2"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"identifiers\":[{\"namespace\":[\"ns\"],\"name\":\"t2\"}]}")));

        List<GenericTableIdentifier> all = new ArrayList<>();
        client.genericTables().list("ns", 2).forEach(all::add);

        assertEquals(2, all.size());
        assertEquals("t1", all.get(0).getName());
        assertEquals("t2", all.get(1).getName());
    }

    @Test
    void throwsNotFoundOn404() {
        server.stubFor(get(anyUrl()).willReturn(aResponse().withStatus(404).withBody("not found")));
        assertThrows(NotFoundException.class, () -> client.genericTables().load("ns", "missing"));
    }

    @Test
    void throwsConflictOn409() {
        server.stubFor(post(anyUrl()).willReturn(aResponse().withStatus(409).withBody("conflict")));
        assertThrows(ConflictException.class, () -> client.genericTables().create("ns", "dup", "lance"));
    }

    @Test
    void retries401WithRefreshedToken() {
        // First call returns 401, second (after invalidate) returns 200
        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/tbl"))
                .inScenario("auth-retry")
                .whenScenarioStateIs("Started")
                .willReturn(aResponse().withStatus(401))
                .willSetStateTo("retried"));

        server.stubFor(get(urlPathEqualTo("/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/tbl"))
                .inScenario("auth-retry")
                .whenScenarioStateIs("retried")
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"table\":{\"name\":\"tbl\",\"format\":\"lance\",\"base-location\":\"s3://b/p\"}}")));

        LoadGenericTableResponse resp = client.genericTables().load("ns", "tbl");
        assertEquals("tbl", resp.getTable().getName());
        server.verify(2, getRequestedFor(urlPathEqualTo(
                "/lakekeeper/v1/my-warehouse/namespaces/ns/generic-tables/tbl")));
    }
}
