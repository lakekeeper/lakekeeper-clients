package io.lakekeeper.client.exception;

public class AuthException extends LakekeeperException {
    public AuthException(String message) {
        super(message);
    }

    public AuthException(String message, Throwable cause) {
        super(message, cause);
    }
}
