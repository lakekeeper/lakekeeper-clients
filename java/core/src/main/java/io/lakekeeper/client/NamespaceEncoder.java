package io.lakekeeper.client;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * Namespace encoding for Lakekeeper URLs.
 *
 * <p>Lakekeeper represents a multi-level namespace (e.g. {@code ai.test}) on the wire by
 * joining levels with the ASCII unit separator {@code U+001F}. In URL path segments that
 * separator is percent-encoded as {@code %1F}.
 */
public final class NamespaceEncoder {
    // ASCII unit separator (U+001F) — Lakekeeper's multi-level namespace join character.
    static final char SEPARATOR = '';
    private static final Pattern SEPARATOR_PATTERN = Pattern.compile(Pattern.quote(String.valueOf(SEPARATOR)));
    private static final Pattern DOT_PATTERN = Pattern.compile("\\.");

    private NamespaceEncoder() {}

    /**
     * Parse a namespace from a dotted string ({@code "ai.test"}), a {@code U+001F}-joined string,
     * or a list of levels. Empty levels are dropped.
     */
    public static List<String> parse(String namespace) {
        if (namespace == null || namespace.isEmpty()) return new ArrayList<>();
        String[] levels = namespace.indexOf(SEPARATOR) >= 0
                ? SEPARATOR_PATTERN.split(namespace)
                : DOT_PATTERN.split(namespace);
        return Arrays.stream(levels).filter(s -> !s.isEmpty()).collect(Collectors.toList());
    }

    public static List<String> parse(List<String> namespace) {
        return namespace.stream().filter(s -> !s.isEmpty()).collect(Collectors.toList());
    }

    /** Join levels with raw {@code U+001F} — for query parameter values. */
    public static String join(String namespace) {
        return String.join(String.valueOf(SEPARATOR), parse(namespace));
    }

    public static String join(List<String> namespace) {
        return String.join(String.valueOf(SEPARATOR), parse(namespace));
    }

    /**
     * Encode a namespace for a URL path segment ({@code U+001F} → {@code %1F}).
     *
     * <p>Example: {@code encode("ai.test")} → {@code "ai%1Ftest"}
     */
    public static String encode(String namespace) {
        return URLEncoder.encode(join(namespace), StandardCharsets.UTF_8).replace("+", "%20");
    }

    public static String encode(List<String> namespace) {
        return URLEncoder.encode(join(namespace), StandardCharsets.UTF_8).replace("+", "%20");
    }
}
