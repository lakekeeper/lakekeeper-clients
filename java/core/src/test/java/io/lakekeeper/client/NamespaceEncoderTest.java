package io.lakekeeper.client;

import org.junit.jupiter.api.Test;

import java.util.Arrays;

import static org.junit.jupiter.api.Assertions.*;

class NamespaceEncoderTest {

    @Test
    void encodeDottedString() {
        assertEquals("ai%1Ftest", NamespaceEncoder.encode("ai.test"));
    }

    @Test
    void encodeSingleLevel() {
        assertEquals("ns", NamespaceEncoder.encode("ns"));
    }

    @Test
    void encodeList() {
        assertEquals("ai%1Ftest", NamespaceEncoder.encode(Arrays.asList("ai", "test")));
    }

    @Test
    void encodeUnitSeparatorInput() {
        // Input already using U+001F separator
        assertEquals("ai%1Ftest", NamespaceEncoder.encode("aitest"));
    }

    @Test
    void joinDottedString() {
        assertEquals("aitest", NamespaceEncoder.join("ai.test"));
    }

    @Test
    void parseDottedString() {
        assertEquals(Arrays.asList("ai", "test"), NamespaceEncoder.parse("ai.test"));
    }

    @Test
    void parseDropsEmptyLevels() {
        assertEquals(Arrays.asList("ai", "test"), NamespaceEncoder.parse("ai..test"));
    }
}
