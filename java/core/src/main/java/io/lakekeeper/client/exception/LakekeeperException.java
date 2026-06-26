package io.lakekeeper.client.exception;

public class LakekeeperException extends RuntimeException {
    public LakekeeperException(String message) {
        super(message);
    }

    public LakekeeperException(String message, Throwable cause) {
        super(message, cause);
    }
}
