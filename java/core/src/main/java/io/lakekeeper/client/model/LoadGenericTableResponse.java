package io.lakekeeper.client.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Collections;
import java.util.List;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
public final class LoadGenericTableResponse {
    @JsonProperty("table")
    private GenericTableData table;

    @JsonProperty("config")
    private Map<String, String> config;

    @JsonProperty("storage-credentials")
    private List<StorageCredential> storageCredentials;

    public GenericTableData getTable() { return table; }
    public Map<String, String> getConfig() { return config; }
    public List<StorageCredential> getStorageCredentials() {
        return storageCredentials != null ? storageCredentials : Collections.emptyList();
    }

    /** Convenience: the table's base storage location. */
    public String getLocation() {
        return table != null ? table.getBaseLocation() : null;
    }
}
