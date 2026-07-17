package io.lakekeeper.client;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.github.tomakehurst.wiremock.verification.LoggedRequest;
import io.lakekeeper.client.auth.AuthorizationCodeFlow;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static org.junit.jupiter.api.Assertions.*;

class AuthorizationCodeFlowTest {
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

    /** A browser stand-in: reads the auth URL and completes the redirect on a background thread. */
    private static AuthorizationCodeFlow.BrowserOpener completingBrowser(
            String code, AtomicInteger opens) {
        return authUrl -> {
            opens.incrementAndGet();
            Map<String, String> q = parseQuery(URI.create(authUrl).getRawQuery());
            String redirectUri = q.get("redirect_uri");
            String state = q.get("state");
            new Thread(() -> {
                try {
                    String target = redirectUri + "?code=" + code + "&state="
                            + URLEncoder.encode(state, StandardCharsets.UTF_8);
                    HttpClient.newHttpClient().send(
                            HttpRequest.newBuilder(URI.create(target)).GET().build(),
                            HttpResponse.BodyHandlers.ofString());
                } catch (Exception e) {
                    // The flow will time out and fail the test if this never lands.
                }
            }).start();
        };
    }

    private AuthorizationCodeFlow flow(AuthorizationCodeFlow.BrowserOpener browser) {
        return new AuthorizationCodeFlow(
                server.baseUrl() + "/authorize",
                server.baseUrl() + "/token",
                "cli",
                null,
                "lakekeeper offline_access",
                "localhost",
                0,
                "/callback",
                browser,
                60,
                30,
                30);
    }

    @Test
    void roundTripExchangesCodeWithPkce() throws Exception {
        server.stubFor(post(urlEqualTo("/token")).willReturn(aResponse().withStatus(200)
                .withHeader("Content-Type", "application/json")
                .withBody("{\"access_token\":\"acc-1\",\"refresh_token\":\"ref-1\","
                        + "\"expires_in\":3600}")));

        AtomicInteger opens = new AtomicInteger();
        Map<String, String> authQuery = new LinkedHashMap<>();
        AuthorizationCodeFlow.BrowserOpener completing = completingBrowser("auth-xyz", opens);
        AuthorizationCodeFlow flow = flow(authUrl -> {
            authQuery.putAll(parseQuery(URI.create(authUrl).getRawQuery()));
            completing.open(authUrl);
        });

        assertEquals("Bearer acc-1", flow.authHeader());
        assertEquals(1, opens.get());

        // The authorization redirect carried an S256 PKCE challenge.
        assertEquals("code", authQuery.get("response_type"));
        assertEquals("S256", authQuery.get("code_challenge_method"));
        assertTrue(authQuery.get("redirect_uri").startsWith("http://localhost:"));

        LoggedRequest exchange =
                server.findAll(postRequestedFor(urlEqualTo("/token"))).get(0);
        Map<String, String> body = parseQuery(exchange.getBodyAsString());
        assertEquals("authorization_code", body.get("grant_type"));
        assertEquals("auth-xyz", body.get("code"));

        // The verifier sent at exchange must be the S256 preimage of the challenge that was
        // put on the authorization URL — i.e. PKCE is wired correctly end to end.
        String verifier = body.get("code_verifier");
        assertNotNull(verifier);
        assertEquals(authQuery.get("code_challenge"), s256(verifier));
    }

    @Test
    void renewsViaRefreshTokenWithoutReopeningBrowser() {
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

        AtomicInteger opens = new AtomicInteger();
        AuthorizationCodeFlow flow = flow(completingBrowser("auth-xyz", opens));

        assertEquals("Bearer acc-1", flow.authHeader());
        flow.invalidate();
        assertEquals("Bearer acc-2", flow.authHeader());

        assertEquals(1, opens.get()); // browser opened once; renewal used the refresh token
        server.verify(postRequestedFor(urlEqualTo("/token"))
                .withRequestBody(containing("refresh_token=ref-1")));
    }

    /** Recompute the base64url(SHA-256(verifier)) PKCE challenge. */
    private static String s256(String verifier) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(verifier.getBytes(StandardCharsets.US_ASCII));
        return Base64.getUrlEncoder().withoutPadding().encodeToString(digest);
    }

    private static Map<String, String> parseQuery(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) return out;
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq >= 0 ? pair.substring(0, eq) : pair;
            String value = eq >= 0 ? pair.substring(eq + 1) : "";
            out.put(
                    URLDecoder.decode(key, StandardCharsets.UTF_8),
                    URLDecoder.decode(value, StandardCharsets.UTF_8));
        }
        return out;
    }
}
