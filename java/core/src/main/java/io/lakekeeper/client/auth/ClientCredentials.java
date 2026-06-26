package io.lakekeeper.client.auth;

import io.lakekeeper.client.exception.AuthException;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.locks.ReentrantLock;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * OAuth2 {@code client_credentials} grant with single-flight, expiry-aware refresh.
 *
 * <p>Under concurrent callers exactly one token request is in-flight at a time.
 */
public final class ClientCredentials implements Auth {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String tokenUrl;
    private final String clientId;
    private final String clientSecret;
    private final String scope;
    private final int refreshMarginSeconds;
    private final Duration timeout;
    private final HttpClient httpClient;

    private final ReentrantLock lock = new ReentrantLock();
    private volatile String token = null;
    private volatile long expiresAtNanos = 0L;

    public ClientCredentials(String tokenUrl, String clientId, String clientSecret) {
        this(tokenUrl, clientId, clientSecret, null, 60, 30);
    }

    public ClientCredentials(
            String tokenUrl,
            String clientId,
            String clientSecret,
            String scope,
            int refreshMarginSeconds,
            int timeoutSeconds) {
        this.tokenUrl = tokenUrl;
        this.clientId = clientId;
        this.clientSecret = clientSecret;
        this.scope = scope;
        this.refreshMarginSeconds = refreshMarginSeconds;
        this.timeout = Duration.ofSeconds(timeoutSeconds);
        this.httpClient = HttpClient.newBuilder().connectTimeout(this.timeout).build();
    }

    @Override
    public String authHeader() {
        if (!isValid()) {
            lock.lock();
            try {
                // Double-check: another thread may have refreshed while we waited.
                if (!isValid()) {
                    refresh();
                }
            } finally {
                lock.unlock();
            }
        }
        return "Bearer " + token;
    }

    @Override
    public void invalidate() {
        lock.lock();
        try {
            token = null;
            expiresAtNanos = 0L;
        } finally {
            lock.unlock();
        }
    }

    private boolean isValid() {
        return token != null && System.nanoTime() < (expiresAtNanos - refreshMarginSeconds * 1_000_000_000L);
    }

    private void refresh() {
        StringBuilder form = new StringBuilder()
                .append("grant_type=client_credentials")
                .append("&client_id=").append(encode(clientId))
                .append("&client_secret=").append(encode(clientSecret));
        if (scope != null) {
            form.append("&scope=").append(encode(scope));
        }

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(tokenUrl))
                .timeout(timeout)
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(form.toString()))
                .build();

        HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new AuthException("token request to " + tokenUrl + " failed: " + e.getMessage(), e);
        }

        if (response.statusCode() != 200) {
            throw new AuthException("token endpoint returned HTTP " + response.statusCode() + ": " + response.body());
        }

        try {
            JsonNode payload = MAPPER.readTree(response.body());
            JsonNode accessToken = payload.get("access_token");
            if (accessToken == null || accessToken.isNull()) {
                throw new AuthException("token response missing access_token: " + response.body());
            }
            JsonNode expiresIn = payload.get("expires_in");
            long expiresInSeconds = (expiresIn != null && !expiresIn.isNull()) ? expiresIn.asLong() : 300L;
            this.token = accessToken.asText();
            this.expiresAtNanos = System.nanoTime() + expiresInSeconds * 1_000_000_000L;
        } catch (IOException e) {
            throw new AuthException("failed to parse token response: " + e.getMessage(), e);
        }
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }
}
