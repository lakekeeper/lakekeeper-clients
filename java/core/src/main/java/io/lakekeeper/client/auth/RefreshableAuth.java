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
import java.util.Map;
import java.util.StringJoiner;
import java.util.concurrent.locks.ReentrantLock;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Base for interactive OAuth2 flows that acquire a token bundle once, then keep it fresh.
 *
 * <p>Subclasses implement {@link #acquire()} — the interactive first login (device approval
 * or browser redirect). This base handles caching, expiry-aware single-flight renewal, and,
 * crucially, silent renewal via the {@code refresh_token} so the session survives long past
 * the access token's typical ~1h lifetime. A fresh interactive login happens only when there
 * is no usable refresh token.
 */
abstract class RefreshableAuth implements Auth {
    static final ObjectMapper MAPPER = new ObjectMapper();

    protected final String tokenUrl;
    protected final String clientId;
    protected final String clientSecret; // nullable — public clients omit it
    protected final String scope; // nullable
    protected final Duration timeout;
    protected final HttpClient httpClient;

    private final int refreshMarginSeconds;
    private final ReentrantLock lock = new ReentrantLock();
    private volatile String token = null;
    private volatile String refreshToken = null;
    private volatile long expiresAtNanos = 0L;

    protected RefreshableAuth(
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
                    ensureToken();
                }
            } finally {
                lock.unlock();
            }
        }
        return "Bearer " + token;
    }

    @Override
    public void invalidate() {
        // Drop the access token but KEEP the refresh token, so the next call renews
        // silently rather than forcing a fresh interactive login.
        lock.lock();
        try {
            token = null;
            expiresAtNanos = 0L;
        } finally {
            lock.unlock();
        }
    }

    private boolean isValid() {
        return token != null
                && System.nanoTime() < (expiresAtNanos - refreshMarginSeconds * 1_000_000_000L);
    }

    /** Acquire a valid access token. Caller holds the lock. */
    private void ensureToken() {
        if (refreshToken != null) {
            try {
                applyBundle(refresh());
                return;
            } catch (AuthException e) {
                // Refresh token expired/revoked — fall back to interactive login.
                refreshToken = null;
            }
        }
        applyBundle(acquire());
    }

    private void applyBundle(TokenBundle bundle) {
        this.token = bundle.accessToken;
        this.expiresAtNanos = System.nanoTime() + bundle.expiresInSeconds * 1_000_000_000L;
        // Honour refresh-token rotation, but keep the old one if none was returned.
        if (bundle.refreshToken != null) {
            this.refreshToken = bundle.refreshToken;
        }
    }

    private TokenBundle refresh() {
        Map<String, String> form = new java.util.LinkedHashMap<>();
        form.put("grant_type", "refresh_token");
        form.put("refresh_token", refreshToken);
        form.put("client_id", clientId);
        if (clientSecret != null) form.put("client_secret", clientSecret);
        if (scope != null) form.put("scope", scope);
        return tokenRequest(form);
    }

    /** POST a grant to the token endpoint and parse the response. */
    protected TokenBundle tokenRequest(Map<String, String> form) {
        HttpResponse<String> response = postForm(tokenUrl, form, "token request");
        if (response.statusCode() != 200) {
            throw new AuthException(
                    "token endpoint returned HTTP " + response.statusCode() + ": " + response.body());
        }
        return parseTokenResponse(response.body());
    }

    protected HttpResponse<String> postForm(String url, Map<String, String> form, String what) {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .timeout(timeout)
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(encodeForm(form)))
                .build();
        try {
            return httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new AuthException(what + " to " + url + " failed: " + e.getMessage(), e);
        }
    }

    protected static TokenBundle parseTokenResponse(String body) {
        try {
            JsonNode payload = MAPPER.readTree(body);
            JsonNode accessToken = payload.get("access_token");
            if (accessToken == null || accessToken.isNull()) {
                throw new AuthException("token response missing access_token: " + body);
            }
            JsonNode expiresIn = payload.get("expires_in");
            long expiresInSeconds =
                    (expiresIn != null && !expiresIn.isNull()) ? expiresIn.asLong() : 300L;
            JsonNode refreshToken = payload.get("refresh_token");
            String refresh =
                    (refreshToken != null && !refreshToken.isNull()) ? refreshToken.asText() : null;
            return new TokenBundle(accessToken.asText(), expiresInSeconds, refresh);
        } catch (IOException e) {
            throw new AuthException("failed to parse token response: " + e.getMessage(), e);
        }
    }

    protected static String encodeForm(Map<String, String> form) {
        StringJoiner joiner = new StringJoiner("&");
        form.forEach((k, v) -> joiner.add(enc(k) + "=" + enc(v)));
        return joiner.toString();
    }

    protected static String enc(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    /** Run the interactive first login and return the initial token bundle. */
    protected abstract TokenBundle acquire();

    /** A parsed OAuth2 token response. */
    static final class TokenBundle {
        final String accessToken;
        final long expiresInSeconds;
        final String refreshToken; // nullable

        TokenBundle(String accessToken, long expiresInSeconds, String refreshToken) {
            this.accessToken = accessToken;
            this.expiresInSeconds = expiresInSeconds;
            this.refreshToken = refreshToken;
        }
    }
}
