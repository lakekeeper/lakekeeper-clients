package io.lakekeeper.client.auth;

public interface Auth {
    /** Returns the full {@code Authorization} header value, e.g. {@code "Bearer <token>"}. */
    String authHeader();

    /** Drop any cached token so the next {@link #authHeader()} call re-acquires. */
    default void invalidate() {}
}
