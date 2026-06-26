package io.lakekeeper.client.exception;

public class LakekeeperHttpException extends LakekeeperException {
    private final int statusCode;
    private final String body;
    private final String method;
    private final String url;

    public LakekeeperHttpException(int statusCode, String body, String method, String url) {
        super(buildMessage(statusCode, body, method, url));
        this.statusCode = statusCode;
        this.body = body;
        this.method = method;
        this.url = url;
    }

    private static String buildMessage(int statusCode, String body, String method, String url) {
        String where = (method != null && url != null) ? " (" + method + " " + url + ")" : "";
        String detail = (body != null && !body.isEmpty()) ? ": " + body : "";
        return "HTTP " + statusCode + where + detail;
    }

    public int getStatusCode() { return statusCode; }
    public String getBody() { return body; }
    public String getMethod() { return method; }
    public String getUrl() { return url; }
}
