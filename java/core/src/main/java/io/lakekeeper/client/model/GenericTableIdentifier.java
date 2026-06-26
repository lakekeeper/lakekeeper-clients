package io.lakekeeper.client.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Collections;
import java.util.List;

@JsonIgnoreProperties(ignoreUnknown = true)
public final class GenericTableIdentifier {
    @JsonProperty("namespace")
    private List<String> namespace = Collections.emptyList();

    @JsonProperty("name")
    private String name;

    @JsonProperty("format")
    private String format;

    @JsonProperty("id")
    private String id;

    public List<String> getNamespace() { return namespace; }
    public String getName() { return name; }
    public String getFormat() { return format; }
    public String getId() { return id; }
}
