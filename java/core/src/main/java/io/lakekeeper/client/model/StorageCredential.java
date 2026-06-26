package io.lakekeeper.client.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Collections;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
public final class StorageCredential {
    @JsonProperty("prefix")
    private String prefix;

    @JsonProperty("config")
    private Map<String, String> config = Collections.emptyMap();

    public String getPrefix() { return prefix; }
    public Map<String, String> getConfig() { return config; }
}
