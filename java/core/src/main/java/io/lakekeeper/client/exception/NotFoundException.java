package io.lakekeeper.client.exception;

public class NotFoundException extends LakekeeperHttpException {
    public NotFoundException(int statusCode, String body, String method, String url) {
        super(statusCode, body, method, url);
    }
}
