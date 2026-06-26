package io.lakekeeper.client;

import io.lakekeeper.client.exception.ConfigException;

import java.util.regex.Pattern;

/**
 * Well-known generic-table format identifiers.
 *
 * <p>These are a non-exhaustive convenience set. Lakekeeper accepts any identifier matching
 * {@code ^[a-z][a-z0-9_-]{0,63}$}, so pass a plain {@code String} for anything not listed here.
 */
public enum GenericTableFormat {
    LANCE("lance"),
    DELTA("delta"),
    VORTEX("vortex"),
    PAIMON("paimon"),
    DATASET("dataset");

    private static final Pattern FORMAT_PATTERN = Pattern.compile("[a-z][a-z0-9_-]{0,63}");

    private final String value;

    GenericTableFormat(String value) {
        this.value = value;
    }

    public String getValue() {
        return value;
    }

    /**
     * Validate and return the wire string for {@code format}.
     * Accepts a {@link GenericTableFormat} value or a raw string.
     */
    public static String normalize(String format) {
        if (format == null || !FORMAT_PATTERN.matcher(format).matches()) {
            throw new ConfigException(
                    "invalid generic table format '" + format + "': must start with a lowercase letter, "
                    + "contain only lowercase letters, digits, '_' or '-', and be at most 64 characters");
        }
        return format;
    }

    public static String normalize(GenericTableFormat format) {
        return format.value;
    }
}
