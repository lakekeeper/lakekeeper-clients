package io.lakekeeper.client.auth;

import io.lakekeeper.client.exception.AuthException;

public final class StaticToken implements Auth {
    private final String headerValue;

    public StaticToken(String token) {
        if (token == null || token.isEmpty()) {
            throw new AuthException("StaticToken requires a non-empty token");
        }
        this.headerValue = "Bearer " + token;
    }

    @Override
    public String authHeader() {
        return headerValue;
    }
}
