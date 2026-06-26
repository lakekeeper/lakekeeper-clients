package io.lakekeeper.client;

import io.lakekeeper.client.auth.Auth;
import io.lakekeeper.client.exception.ConfigException;

import java.time.Duration;

/**
 * Entry point for talking to a Lakekeeper server.
 *
 * <pre>{@code
 * try (LakekeeperClient client = LakekeeperClient.builder()
 *         .baseUrl("http://localhost:8181")
 *         .warehouse("my-warehouse")
 *         .auth(new StaticToken("my-token"))
 *         .build()) {
 *     LoadGenericTableResponse table = client.genericTables().load("ns", "my-table");
 * }
 * }</pre>
 */
public final class LakekeeperClient implements AutoCloseable {
    private final GenericTables genericTables;

    private LakekeeperClient(Builder builder) {
        HttpTransport transport = new HttpTransport(
                builder.baseUrl, builder.auth, builder.projectId, builder.timeout);
        this.genericTables = new GenericTables(transport, builder.warehouse);
    }

    /** Access the generic-tables API. */
    public GenericTables genericTables() {
        return genericTables;
    }

    @Override
    public void close() {
        // HttpClient (Java 11) is managed by GC; no explicit close needed.
    }

    public static Builder builder() {
        return new Builder();
    }

    public static final class Builder {
        private String baseUrl;
        private String warehouse;
        private Auth auth;
        private String projectId;
        private Duration timeout = Duration.ofSeconds(30);

        public Builder baseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
            return this;
        }

        public Builder warehouse(String warehouse) {
            this.warehouse = warehouse;
            return this;
        }

        public Builder auth(Auth auth) {
            this.auth = auth;
            return this;
        }

        public Builder projectId(String projectId) {
            this.projectId = projectId;
            return this;
        }

        public Builder timeout(Duration timeout) {
            this.timeout = timeout;
            return this;
        }

        public LakekeeperClient build() {
            if (baseUrl == null || baseUrl.isEmpty()) throw new ConfigException("baseUrl is required");
            if (warehouse == null || warehouse.isEmpty()) throw new ConfigException("warehouse is required");
            if (auth == null) throw new ConfigException("auth is required");
            return new LakekeeperClient(this);
        }
    }
}
