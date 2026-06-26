package io.lakekeeper.client.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Collections;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
public final class GenericTableData {
    @JsonProperty("name")
    private String name;

    @JsonProperty("format")
    private String format;

    @JsonProperty("base-location")
    private String baseLocation;

    @JsonProperty("doc")
    private String doc;

    @JsonProperty("properties")
    private Map<String, String> properties = Collections.emptyMap();

    @JsonProperty("protected")
    private boolean isProtected = false;

    public String getName() { return name; }
    public String getFormat() { return format; }
    public String getBaseLocation() { return baseLocation; }
    public String getDoc() { return doc; }
    public Map<String, String> getProperties() { return properties; }
    public boolean isProtected() { return isProtected; }
}
