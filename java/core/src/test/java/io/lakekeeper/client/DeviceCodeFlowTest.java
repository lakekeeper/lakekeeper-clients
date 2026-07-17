package io.lakekeeper.client;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import io.lakekeeper.client.auth.DeviceCodeFlow;
import io.lakekeeper.client.auth.DeviceCodePrompt;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static org.junit.jupiter.api.Assertions.*;

class DeviceCodeFlowTest {
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

    private void stubDeviceEndpoint() {
        server.stubFor(post(urlEqualTo("/device")).willReturn(aResponse()
                .withStatus(200)
                .withHeader("Content-Type", "application/json")
                .withBody("{\"device_code\":\"dev-123\",\"user_code\":\"WDJB-MJHT\","
                        + "\"verification_uri\":\"https://idp/activate\","
                        + "\"verification_uri_complete\":\"https://idp/activate?code=WDJB-MJHT\","
                        + "\"interval\":0,\"expires_in\":600}")));
    }

    private DeviceCodeFlow flow(DeviceCodeFlow.Prompt prompt) {
        return new DeviceCodeFlow(
                server.baseUrl() + "/device",
                server.baseUrl() + "/token",
                "cli",
                null,
                "lakekeeper offline_access",
                prompt,
                60,
                30,
                300);
    }

    @Test
    void pollsUntilApprovedAndPromptsUser() {
        stubDeviceEndpoint();
        // First poll: pending. Second poll: approved.
        server.stubFor(post(urlEqualTo("/token")).inScenario("device")
                .whenScenarioStateIs(com.github.tomakehurst.wiremock.stubbing.Scenario.STARTED)
                .willReturn(aResponse().withStatus(400)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"error\":\"authorization_pending\"}"))
                .willSetStateTo("approved"));
        server.stubFor(post(urlEqualTo("/token")).inScenario("device")
                .whenScenarioStateIs("approved")
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"acc-1\",\"refresh_token\":\"ref-1\","
                                + "\"expires_in\":3600}")));

        List<DeviceCodePrompt> shown = new ArrayList<>();
        DeviceCodeFlow flow = flow(shown::add);

        assertEquals("Bearer acc-1", flow.authHeader());
        assertEquals("Bearer acc-1", flow.authHeader()); // cached

        assertEquals(1, shown.size());
        assertTrue(shown.get(0).verificationUriComplete.endsWith("code=WDJB-MJHT"));

        server.verify(postRequestedFor(urlEqualTo("/token"))
                .withRequestBody(containing(
                        "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code"))
                .withRequestBody(containing("device_code=dev-123")));
    }

    @Test
    void renewsViaRefreshTokenWithoutReapproval() {
        stubDeviceEndpoint();
        server.stubFor(post(urlEqualTo("/token")).atPriority(1)
                .withRequestBody(containing("grant_type=refresh_token"))
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"acc-2\",\"expires_in\":3600}")));
        server.stubFor(post(urlEqualTo("/token")).atPriority(5)
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"acc-1\",\"refresh_token\":\"ref-1\","
                                + "\"expires_in\":3600}")));

        DeviceCodeFlow flow = flow(p -> {});

        assertEquals("Bearer acc-1", flow.authHeader());
        flow.invalidate(); // simulate 401 / expiry
        assertEquals("Bearer acc-2", flow.authHeader());

        // The device endpoint was hit exactly once; renewal used the refresh token.
        server.verify(1, postRequestedFor(urlEqualTo("/device")));
        server.verify(postRequestedFor(urlEqualTo("/token"))
                .withRequestBody(containing("refresh_token=ref-1")));
    }

    @Test
    void reauthenticatesWhenRefreshTokenIsDead() {
        stubDeviceEndpoint();
        server.stubFor(post(urlEqualTo("/token")).atPriority(1)
                .withRequestBody(containing("grant_type=refresh_token"))
                .willReturn(aResponse().withStatus(400)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"error\":\"invalid_grant\"}")));
        server.stubFor(post(urlEqualTo("/token")).atPriority(5)
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"access_token\":\"acc-1\",\"refresh_token\":\"ref-1\","
                                + "\"expires_in\":3600}")));

        DeviceCodeFlow flow = flow(p -> {});

        assertEquals("Bearer acc-1", flow.authHeader());
        flow.invalidate();
        assertEquals("Bearer acc-1", flow.authHeader()); // re-acquired

        // Failed refresh -> a second device approval.
        server.verify(2, postRequestedFor(urlEqualTo("/device")));
    }
}
