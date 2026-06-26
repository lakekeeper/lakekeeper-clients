package io.lakekeeper.client.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Collections;
import java.util.List;

@JsonIgnoreProperties(ignoreUnknown = true)
public final class ListGenericTablesResponse {
    @JsonProperty("identifiers")
    private List<GenericTableIdentifier> identifiers = Collections.emptyList();

    @JsonProperty("next-page-token")
    private String nextPageToken;

    public List<GenericTableIdentifier> getIdentifiers() { return identifiers; }
    public String getNextPageToken() { return nextPageToken; }
}
