package io.lakekeeper.client.auth;

import io.lakekeeper.client.exception.AuthException;

import java.io.IOException;
import java.net.http.HttpResponse;
import java.util.LinkedHashMap;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * OAuth2 Device Authorization Grant (RFC 8628).
 *
 * <p>The user approves the login in a browser (on this or any other device); this client polls
 * the token endpoint until they do. Ideal for CLIs and headless hosts where you cannot capture a
 * redirect. After the first login the token is renewed silently via the refresh token, so the
 * session outlives the access token's ~1h lifetime.
 */
public final class DeviceCodeFlow extends RefreshableAuth {

    /** Callback that shows the verification URL + code to the user. */
    public interface Prompt {
        void show(DeviceCodePrompt prompt);
    }

    // RFC 8628 device-flow grant type.
    private static final String DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code";

    private final String deviceAuthorizationUrl;
    private final Prompt prompt;
    private final long pollTimeoutSeconds;

    /** Public client with default timeouts and a stdout prompt. */
    public DeviceCodeFlow(String deviceAuthorizationUrl, String tokenUrl, String clientId) {
        this(deviceAuthorizationUrl, tokenUrl, clientId, null, null, null, 60, 30, 300);
    }

    /**
     * @param deviceAuthorizationUrl the IdP device-authorization endpoint.
     * @param tokenUrl the IdP token endpoint.
     * @param clientId the OAuth2 client id.
     * @param clientSecret optional; only for confidential clients (may be {@code null}).
     * @param scope optional space-delimited scopes; include {@code offline_access} if your IdP
     *     needs it to issue a refresh token (may be {@code null}).
     * @param prompt callback that shows the {@link DeviceCodePrompt}; {@code null} prints to stdout.
     * @param refreshMarginSeconds seconds before expiry to renew proactively.
     * @param timeoutSeconds per-HTTP-request timeout.
     * @param pollTimeoutSeconds overall seconds to wait for the user to approve.
     */
    public DeviceCodeFlow(
            String deviceAuthorizationUrl,
            String tokenUrl,
            String clientId,
            String clientSecret,
            String scope,
            Prompt prompt,
            int refreshMarginSeconds,
            int timeoutSeconds,
            long pollTimeoutSeconds) {
        super(tokenUrl, clientId, clientSecret, scope, refreshMarginSeconds, timeoutSeconds);
        this.deviceAuthorizationUrl = deviceAuthorizationUrl;
        this.prompt = prompt != null ? prompt : DeviceCodeFlow::defaultPrompt;
        this.pollTimeoutSeconds = pollTimeoutSeconds;
    }

    @Override
    protected TokenBundle acquire() {
        JsonNode device = requestDeviceCode();
        String deviceCode = device.get("device_code").asText();
        long interval = device.hasNonNull("interval") ? device.get("interval").asLong() : 5L;
        long expiresIn =
                device.hasNonNull("expires_in") ? device.get("expires_in").asLong() : pollTimeoutSeconds;

        prompt.show(new DeviceCodePrompt(
                text(device, "verification_uri"),
                text(device, "user_code"),
                text(device, "verification_uri_complete"),
                expiresIn));

        long deadline = System.nanoTime() + Math.min(expiresIn, pollTimeoutSeconds) * 1_000_000_000L;
        while (System.nanoTime() < deadline) {
            sleepSeconds(interval);

            Map<String, String> form = new LinkedHashMap<>();
            form.put("grant_type", DEVICE_GRANT);
            form.put("device_code", deviceCode);
            form.put("client_id", clientId);
            if (clientSecret != null) form.put("client_secret", clientSecret);

            HttpResponse<String> resp = postForm(tokenUrl, form, "token request");
            if (resp.statusCode() == 200) {
                return parseTokenResponse(resp.body());
            }
            String error = errorCode(resp.body());
            if ("authorization_pending".equals(error)) {
                continue;
            }
            if ("slow_down".equals(error)) {
                interval += 5; // RFC 8628: back off and keep polling.
                continue;
            }
            throw new AuthException("device authorization failed ("
                    + (error != null ? error : resp.statusCode()) + "): " + resp.body());
        }
        throw new AuthException("device authorization timed out before the user approved");
    }

    private JsonNode requestDeviceCode() {
        Map<String, String> form = new LinkedHashMap<>();
        form.put("client_id", clientId);
        if (clientSecret != null) form.put("client_secret", clientSecret);
        if (scope != null) form.put("scope", scope);

        HttpResponse<String> resp =
                postForm(deviceAuthorizationUrl, form, "device-authorization request");
        if (resp.statusCode() != 200) {
            throw new AuthException("device-authorization endpoint returned HTTP "
                    + resp.statusCode() + ": " + resp.body());
        }
        try {
            JsonNode device = MAPPER.readTree(resp.body());
            if (!device.hasNonNull("device_code")) {
                throw new AuthException(
                        "device-authorization response missing device_code: " + resp.body());
            }
            return device;
        } catch (IOException e) {
            throw new AuthException(
                    "failed to parse device-authorization response: " + e.getMessage(), e);
        }
    }

    private void sleepSeconds(long seconds) {
        if (seconds <= 0) return;
        try {
            Thread.sleep(seconds * 1000L);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AuthException("interrupted while waiting for device authorization", e);
        }
    }

    private static String errorCode(String body) {
        try {
            JsonNode error = MAPPER.readTree(body).get("error");
            return (error != null && !error.isNull()) ? error.asText() : null;
        } catch (IOException e) {
            return null;
        }
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return (value != null && !value.isNull()) ? value.asText() : null;
    }

    private static void defaultPrompt(DeviceCodePrompt prompt) {
        String target = prompt.verificationUriComplete != null
                ? prompt.verificationUriComplete
                : prompt.verificationUri;
        StringBuilder sb = new StringBuilder(
                "\nTo sign in, open the following URL in a browser:\n    ").append(target);
        if (prompt.verificationUriComplete == null && prompt.userCode != null) {
            sb.append("\nand enter the code:  ").append(prompt.userCode);
        }
        System.out.println(sb);
    }
}
