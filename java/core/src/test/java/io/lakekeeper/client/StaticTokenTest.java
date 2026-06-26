package io.lakekeeper.client;

import io.lakekeeper.client.auth.StaticToken;
import io.lakekeeper.client.exception.AuthException;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class StaticTokenTest {

    @Test
    void returnsBearer() {
        StaticToken token = new StaticToken("my-token");
        assertEquals("Bearer my-token", token.authHeader());
    }

    @Test
    void rejectsEmpty() {
        assertThrows(AuthException.class, () -> new StaticToken(""));
    }

    @Test
    void rejectsNull() {
        assertThrows(AuthException.class, () -> new StaticToken(null));
    }

    @Test
    void invalidateIsNoop() {
        StaticToken token = new StaticToken("my-token");
        token.invalidate();
        assertEquals("Bearer my-token", token.authHeader());
    }
}
