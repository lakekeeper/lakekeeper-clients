package io.lakekeeper.client;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import io.lakekeeper.client.auth.ClientCredentials;
import io.lakekeeper.client.exception.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static org.junit.jupiter.api.Assertions.*;

class ClientCredentialsTest {
    private WireMockServer server;

    @BeforeEach
    void setUp() {
        server = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
        server.start();
    }

    @AfterEach
    void tearDown() {
        server.stop();
    }

    @Test
    void fetchesTokenAndCaches() {
        server.stubFor(post(urlEqualTo("/token"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"tok-abc\",\"expires_in\":3600}")));

        ClientCredentials cc = new ClientCredentials(
                server.baseUrl() + "/token", "client-id", "client-secret");

        assertEquals("Bearer tok-abc", cc.authHeader());
        assertEquals("Bearer tok-abc", cc.authHeader()); // second call uses cache

        server.verify(1, postRequestedFor(urlEqualTo("/token")));
    }

    @Test
    void invalidateForcesRefresh() {
        server.stubFor(post(urlEqualTo("/token"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"tok-abc\",\"expires_in\":3600}")));

        ClientCredentials cc = new ClientCredentials(
                server.baseUrl() + "/token", "client-id", "client-secret");

        cc.authHeader();
        cc.invalidate();
        cc.authHeader();

        server.verify(2, postRequestedFor(urlEqualTo("/token")));
    }

    @Test
    void throwsOnNon200() {
        server.stubFor(post(urlEqualTo("/token"))
                .willReturn(aResponse().withStatus(401).withBody("unauthorized")));

        ClientCredentials cc = new ClientCredentials(
                server.baseUrl() + "/token", "client-id", "bad-secret");

        assertThrows(AuthException.class, cc::authHeader);
    }

    @Test
    void throwsOnMissingAccessToken() {
        server.stubFor(post(urlEqualTo("/token"))
                .willReturn(aResponse()
                        .withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"token_type\":\"bearer\"}")));

        ClientCredentials cc = new ClientCredentials(
                server.baseUrl() + "/token", "client-id", "client-secret");

        assertThrows(AuthException.class, cc::authHeader);
    }
}
