package io.lakekeeper.client.auth;

import io.lakekeeper.client.exception.AuthException;

import java.awt.Desktop;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * OAuth2 Authorization Code grant with PKCE (RFC 7636).
 *
 * <p>Opens a browser for the user to sign in and captures the redirect on a short-lived loopback
 * HTTP server, so no client secret needs to live on the machine (PKCE proves possession instead).
 * For interactive desktop / notebook use. After the first login the token is renewed silently via
 * the refresh token, so the session outlives the access token's ~1h lifetime.
 */
public final class AuthorizationCodeFlow extends RefreshableAuth {

    /** Callback that opens the authorization URL in a browser. Must not block. */
    public interface BrowserOpener {
        void open(String authorizationUrl);
    }

    private static final SecureRandom RNG = new SecureRandom();

    private final String authorizationUrl;
    private final String redirectHost;
    private final int redirectPort;
    private final String redirectPath;
    private final BrowserOpener openBrowser;
    private final long loginTimeoutSeconds;

    /** Public client binding an OS-assigned loopback port, opening the system browser. */
    public AuthorizationCodeFlow(String authorizationUrl, String tokenUrl, String clientId) {
        this(authorizationUrl, tokenUrl, clientId, null, null,
                "localhost", 0, "/callback", null, 60, 30, 300);
    }

    /**
     * @param authorizationUrl the IdP authorization endpoint.
     * @param tokenUrl the IdP token endpoint.
     * @param clientId the OAuth2 client id.
     * @param clientSecret optional; supply only for confidential clients (may be {@code null}).
     * @param scope optional space-delimited scopes; include {@code offline_access} if your IdP
     *     needs it to issue a refresh token (may be {@code null}).
     * @param redirectHost loopback host to bind (typically {@code localhost}).
     * @param redirectPort loopback port; {@code 0} lets the OS pick a free one. The resulting
     *     {@code redirect_uri} must be registered on the IdP client.
     * @param redirectPath path the IdP redirects back to (e.g. {@code /callback}).
     * @param openBrowser callback that opens the authorization URL; {@code null} uses
     *     {@link Desktop}. Must not block.
     * @param refreshMarginSeconds seconds before expiry to renew proactively.
     * @param timeoutSeconds per-HTTP-request timeout.
     * @param loginTimeoutSeconds overall seconds to wait for the browser redirect.
     */
    public AuthorizationCodeFlow(
            String authorizationUrl,
            String tokenUrl,
            String clientId,
            String clientSecret,
            String scope,
            String redirectHost,
            int redirectPort,
            String redirectPath,
            BrowserOpener openBrowser,
            int refreshMarginSeconds,
            int timeoutSeconds,
            long loginTimeoutSeconds) {
        super(tokenUrl, clientId, clientSecret, scope, refreshMarginSeconds, timeoutSeconds);
        this.authorizationUrl = authorizationUrl;
        this.redirectHost = redirectHost;
        this.redirectPort = redirectPort;
        this.redirectPath = redirectPath;
        this.openBrowser = openBrowser != null ? openBrowser : AuthorizationCodeFlow::defaultOpenBrowser;
        this.loginTimeoutSeconds = loginTimeoutSeconds;
    }

    @Override
    protected TokenBundle acquire() {
        String verifier = generateVerifier();
        String challenge = codeChallenge(verifier);
        String state = randomUrlSafe(24);

        RedirectServer server = RedirectServer.start(redirectHost, redirectPort, redirectPath);
        String redirectUri;
        String code;
        try {
            int port = server.port();
            redirectUri = "http://" + redirectHost + ":" + port + redirectPath;
            openBrowser.open(buildAuthorizationUrl(redirectUri, state, challenge));
            code = server.waitForCode(loginTimeoutSeconds, state);
        } finally {
            server.stop();
        }
        return exchangeCode(code, redirectUri, verifier);
    }

    private String buildAuthorizationUrl(String redirectUri, String state, String challenge) {
        Map<String, String> params = new LinkedHashMap<>();
        params.put("response_type", "code");
        params.put("client_id", clientId);
        params.put("redirect_uri", redirectUri);
        params.put("state", state);
        params.put("code_challenge", challenge);
        params.put("code_challenge_method", "S256");
        if (scope != null) params.put("scope", scope);
        String sep = authorizationUrl.contains("?") ? "&" : "?";
        return authorizationUrl + sep + encodeForm(params);
    }

    private TokenBundle exchangeCode(String code, String redirectUri, String verifier) {
        Map<String, String> form = new LinkedHashMap<>();
        form.put("grant_type", "authorization_code");
        form.put("code", code);
        form.put("redirect_uri", redirectUri);
        form.put("client_id", clientId);
        form.put("code_verifier", verifier);
        if (clientSecret != null) form.put("client_secret", clientSecret);
        return tokenRequest(form);
    }

    // --- PKCE (RFC 7636) -----------------------------------------------------------------

    private static String randomUrlSafe(int numBytes) {
        byte[] bytes = new byte[numBytes];
        RNG.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    static String generateVerifier() {
        // 64 bytes -> ~86 base64url chars, within RFC 7636's 43..128 range.
        return randomUrlSafe(64);
    }

    static String codeChallenge(String verifier) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(verifier.getBytes(StandardCharsets.US_ASCII));
            return Base64.getUrlEncoder().withoutPadding().encodeToString(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new AuthException("SHA-256 unavailable for PKCE", e);
        }
    }

    private static void defaultOpenBrowser(String url) {
        System.out.println(
                "\nOpening your browser to sign in. If it does not open, visit:\n    " + url + "\n");
        try {
            if (Desktop.isDesktopSupported()
                    && Desktop.getDesktop().isSupported(Desktop.Action.BROWSE)) {
                Desktop.getDesktop().browse(URI.create(url));
            }
        } catch (IOException | UnsupportedOperationException e) {
            // Fall back to the printed URL.
        }
    }

    // --- Loopback redirect capture -------------------------------------------------------

    private static final class RedirectServer {
        private final HttpServer server;
        private final CountDownLatch latch = new CountDownLatch(1);
        private volatile String code;
        private volatile String returnedState;
        private volatile String error;

        private RedirectServer(HttpServer server) {
            this.server = server;
        }

        static RedirectServer start(String host, int port, String path) {
            try {
                HttpServer httpServer = HttpServer.create(new InetSocketAddress(host, port), 0);
                RedirectServer rs = new RedirectServer(httpServer);
                httpServer.createContext(path, rs::handle);
                httpServer.start();
                return rs;
            } catch (IOException e) {
                throw new AuthException(
                        "failed to start loopback redirect server: " + e.getMessage(), e);
            }
        }

        int port() {
            return server.getAddress().getPort();
        }

        private void handle(HttpExchange exchange) throws IOException {
            Map<String, String> query = parseQuery(exchange.getRequestURI().getRawQuery());
            this.code = query.get("code");
            this.returnedState = query.get("state");
            this.error = query.get("error");
            byte[] body = "<html><body>Login complete. You may close this tab.</body></html>"
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "text/html; charset=utf-8");
            exchange.sendResponseHeaders(200, body.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(body);
            }
            latch.countDown();
        }

        String waitForCode(long timeoutSeconds, String expectedState) {
            try {
                if (!latch.await(timeoutSeconds, TimeUnit.SECONDS)) {
                    throw new AuthException("timed out waiting for the authorization redirect");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new AuthException("interrupted waiting for authorization redirect", e);
            }
            if (error != null) {
                throw new AuthException("authorization failed: " + error);
            }
            if (code == null) {
                throw new AuthException("authorization redirect missing code");
            }
            if (!expectedState.equals(returnedState)) {
                throw new AuthException("state mismatch in authorization redirect (possible CSRF)");
            }
            return code;
        }

        void stop() {
            server.stop(0);
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
}
