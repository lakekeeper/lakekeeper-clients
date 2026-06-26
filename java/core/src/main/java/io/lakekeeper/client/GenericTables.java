package io.lakekeeper.client;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.lakekeeper.client.exception.LakekeeperException;
import io.lakekeeper.client.model.GenericTableIdentifier;
import io.lakekeeper.client.model.ListGenericTablesResponse;
import io.lakekeeper.client.model.LoadGenericTableResponse;

import java.net.URLEncoder;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;

/** CRUD for Lakekeeper generic (non-Iceberg) tables, scoped to one warehouse. */
public final class GenericTables {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String VENDED_HEADER_NAME = "X-Iceberg-Access-Delegation";
    private static final String VENDED_HEADER_VALUE = "vended-credentials";

    private final HttpTransport transport;
    private final String warehouse;

    GenericTables(HttpTransport transport, String warehouse) {
        this.transport = transport;
        this.warehouse = warehouse;
    }

    /**
     * Create a generic table.
     *
     * @param namespace dot-separated string or list of levels
     * @param name      table name
     * @param format    a {@link GenericTableFormat} or any valid format string
     */
    public LoadGenericTableResponse create(
            String namespace, String name, String format) {
        return create(namespace, name, format, null, null, null);
    }

    public LoadGenericTableResponse create(
            String namespace, String name, GenericTableFormat format) {
        return create(namespace, name, GenericTableFormat.normalize(format), null, null, null);
    }

    public LoadGenericTableResponse create(
            String namespace,
            String name,
            String format,
            String baseLocation,
            String doc,
            Map<String, String> properties) {
        Map<String, Object> body = new HashMap<>();
        body.put("name", name);
        body.put("format", GenericTableFormat.normalize(format));
        if (baseLocation != null) body.put("base-location", baseLocation);
        if (doc != null) body.put("doc", doc);
        if (properties != null && !properties.isEmpty()) body.put("properties", properties);

        HttpResponse<String> resp = transport.request("POST", collectionPath(namespace), null, toJson(body), null);
        return parse(resp.body(), LoadGenericTableResponse.class);
    }

    /** Load a generic table. */
    public LoadGenericTableResponse load(String namespace, String name) {
        return load(namespace, name, false);
    }

    /** Load a generic table, optionally requesting vended storage credentials. */
    public LoadGenericTableResponse load(String namespace, String name, boolean vended) {
        Map<String, String> headers = vended
                ? Collections.singletonMap(VENDED_HEADER_NAME, VENDED_HEADER_VALUE)
                : null;
        HttpResponse<String> resp = transport.request("GET", tablePath(namespace, name), null, null, headers);
        return parse(resp.body(), LoadGenericTableResponse.class);
    }

    /**
     * List all tables in a namespace, following pagination automatically.
     *
     * @return a lazy iterator; each call to {@link Iterator#next()} may issue an HTTP request
     */
    public Iterable<GenericTableIdentifier> list(String namespace) {
        return list(namespace, 100);
    }

    public Iterable<GenericTableIdentifier> list(String namespace, int pageSize) {
        return () -> new PageIterator(namespace, pageSize);
    }

    /** Drop a generic table. */
    public void drop(String namespace, String name) {
        transport.request("DELETE", tablePath(namespace, name), null, null, null);
    }

    private String collectionPath(String namespace) {
        return "/lakekeeper/v1/" + encode(warehouse)
                + "/namespaces/" + NamespaceEncoder.encode(namespace)
                + "/generic-tables";
    }

    private String tablePath(String namespace, String name) {
        return collectionPath(namespace) + "/" + encode(name);
    }

    private static String encode(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    private static String toJson(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (JsonProcessingException e) {
            throw new LakekeeperException("failed to serialize request body: " + e.getMessage(), e);
        }
    }

    private static <T> T parse(String json, Class<T> type) {
        try {
            return MAPPER.readValue(json, type);
        } catch (JsonProcessingException e) {
            throw new LakekeeperException("failed to parse response: " + e.getMessage(), e);
        }
    }

    private final class PageIterator implements Iterator<GenericTableIdentifier> {
        private final String namespace;
        private final int pageSize;
        private List<GenericTableIdentifier> current = new ArrayList<>();
        private int index = 0;
        private String nextPageToken = null;
        private boolean firstPage = true;

        PageIterator(String namespace, int pageSize) {
            this.namespace = namespace;
            this.pageSize = pageSize;
        }

        @Override
        public boolean hasNext() {
            if (index < current.size()) return true;
            if (!firstPage && nextPageToken == null) return false;
            fetchPage();
            return index < current.size();
        }

        @Override
        public GenericTableIdentifier next() {
            if (!hasNext()) throw new NoSuchElementException();
            return current.get(index++);
        }

        private void fetchPage() {
            firstPage = false;
            Map<String, String> params = new HashMap<>();
            params.put("pageSize", String.valueOf(pageSize));
            if (nextPageToken != null) params.put("pageToken", nextPageToken);
            HttpResponse<String> resp = transport.request("GET", collectionPath(namespace), params, null, null);
            ListGenericTablesResponse page = parse(resp.body(), ListGenericTablesResponse.class);
            current = page.getIdentifiers();
            index = 0;
            nextPageToken = page.getNextPageToken();
        }
    }
}
