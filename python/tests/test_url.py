"""Round-trip and edge-case tests for namespace encoding (the %1F quirk)."""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from pylakekeeper.url import (
    NAMESPACE_SEPARATOR,
    encode_namespace,
    join_namespace,
    parse_namespace,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ai.test", ("ai", "test")),
        (["ai", "test"], ("ai", "test")),
        (("ai", "test"), ("ai", "test")),
        ("single", ("single",)),
        ("a.b.c.d", ("a", "b", "c", "d")),
        (f"ai{NAMESPACE_SEPARATOR}test", ("ai", "test")),  # raw separator input
    ],
)
def test_parse_namespace(value, expected):
    assert parse_namespace(value) == expected


def test_parse_drops_empty_levels():
    assert parse_namespace("ai..test.") == ("ai", "test")
    assert parse_namespace(["ai", "", "test"]) == ("ai", "test")


def test_join_uses_raw_separator():
    assert join_namespace("ai.test") == f"ai{NAMESPACE_SEPARATOR}test"
    # query-param form is NOT percent-encoded
    assert "%1F" not in join_namespace("ai.test")


def test_encode_is_percent_encoded_path_segment():
    assert encode_namespace("ai.test") == "ai%1Ftest"
    assert encode_namespace(["ai", "test"]) == "ai%1Ftest"
    assert encode_namespace("single") == "single"


def test_encode_round_trips_back_to_levels():
    levels = ("ai", "test", "nested")
    encoded = encode_namespace(levels)
    decoded = unquote(encoded)
    assert tuple(decoded.split(NAMESPACE_SEPARATOR)) == levels


def test_encode_escapes_reserved_characters_in_level_names():
    # a slash inside a level name must not break out of the path segment
    encoded = encode_namespace(["a/b", "c d"])
    assert "/" not in encoded
    assert unquote(encoded).split(NAMESPACE_SEPARATOR) == ["a/b", "c d"]
