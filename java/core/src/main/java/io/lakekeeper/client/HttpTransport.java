package io.lakekeeper.client;

import io.lakekeeper.client.auth.Auth;
import io.lakekeeper.client.exception.ConflictException;
import io.lakekeeper.client.exception.LakekeeperHttpException;
import io.lakekeeper.client.exception.NotFoundException;

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

/**
 * HTTP transport: bearer-token injection, {@code x-project-id} plumbing, and 401-retry.
 *
 * <p>Callers pass URL paths with namespace segments already percent-encoded ({@code %1F}).
 */
final class HttpTransport {
    private final String baseUrl;
    private final Auth auth;
    private final String projectId;
    private final HttpClient client;
    private final Duration timeout;

    HttpTransport(String baseUrl, Auth auth, String projectId, Duration timeout) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.auth = auth;
        this.projectId = projectId;
        this.timeout = timeout;
        this.client = HttpClient.newBuilder().connectTimeout(timeout).build();
    }

    HttpResponse<String> request(
            String method,
            String path,
            Map<String, String> queryParams,
            String jsonBody,
            Map<String, String> extraHeaders) {
        HttpResponse<String> response = send(method, path, queryParams, jsonBody, extraHeaders);
        if (response.statusCode() == 401) {
            auth.invalidate();
            response = send(method, path, queryParams, jsonBody, extraHeaders);
        }
        if (response.statusCode() >= 400) {
            String url = baseUrl + path;
            throw errorFor(response.statusCode(), response.body(), method, url);
        }
        return response;
    }

    private HttpResponse<String> send(
            String method,
            String path,
            Map<String, String> queryParams,
            String jsonBody,
            Map<String, String> extraHeaders) {
        String url = buildUrl(path, queryParams);

        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .timeout(timeout)
                .header("Authorization", auth.authHeader());

        if (projectId != null) builder.header("x-project-id", projectId);
        if (extraHeaders != null) extraHeaders.forEach(builder::header);

        if (jsonBody != null) {
            builder.header("Content-Type", "application/json")
                   .method(method, HttpRequest.BodyPublishers.ofString(jsonBody));
        } else {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        }

        try {
            return client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new LakekeeperHttpException(0, e.getMessage(), method, url);
        }
    }

    private String buildUrl(String path, Map<String, String> queryParams) {
        String base = baseUrl + path;
        if (queryParams == null || queryParams.isEmpty()) return base;
        StringJoiner joiner = new StringJoiner("&", base + "?", "");
        queryParams.forEach((k, v) ->
                joiner.add(URLEncoder.encode(k, StandardCharsets.UTF_8)
                        + "=" + URLEncoder.encode(v, StandardCharsets.UTF_8)));
        return joiner.toString();
    }

    private LakekeeperHttpException errorFor(int status, String body, String method, String url) {
        if (status == 404) return new NotFoundException(status, body, method, url);
        if (status == 409) return new ConflictException(status, body, method, url);
        return new LakekeeperHttpException(status, body, method, url);
    }
}
