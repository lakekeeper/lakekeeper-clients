package io.lakekeeper.client.exception;

public class ConflictException extends LakekeeperHttpException {
    public ConflictException(int statusCode, String body, String method, String url) {
        super(statusCode, body, method, url);
    }
}
