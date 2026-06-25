"""Format constants + client-side validation (mirrors the server's shape rule)."""

from __future__ import annotations

import pytest

from pylakekeeper import Client, GenericTableFormat, StaticToken
from pylakekeeper.errors import ConfigError
from pylakekeeper.formats import normalize_format

BASE = "http://lk.example.com"
LOAD_BODY = {"table": {"name": "t", "format": "lance", "base-location": "s3://b/t"}}


def test_members_are_plain_strings():
    assert GenericTableFormat.LANCE == "lance"
    assert GenericTableFormat.DATASET == "dataset"
    assert {f.value for f in GenericTableFormat} == {
        "lance",
        "delta",
        "vortex",
        "paimon",
        "dataset",
    }


def test_normalize_accepts_enum_and_str():
    assert normalize_format(GenericTableFormat.DELTA) == "delta"
    assert normalize_format("delta") == "delta"
    # custom (unlisted) slugs are allowed
    assert normalize_format("my-custom_fmt9") == "my-custom_fmt9"


@pytest.mark.parametrize(
    "bad",
    ["Lance", "delta lake", "1delta", "", "déltä", "x" * 65, "_under"],
)
def test_normalize_rejects_invalid_shapes(bad):
    with pytest.raises(ConfigError):
        normalize_format(bad)


def test_normalize_allows_trailing_punctuation_and_digits():
    # The rule only constrains the first char; '-'/'_'/digits are fine afterwards.
    assert normalize_format("delta-2") == "delta-2"


def test_create_serializes_enum_to_its_value_not_repr(httpx_mock):
    """Guard the (str, Enum) gotcha: the body must carry "lance", not "GenericTableFormat.LANCE"."""
    import json

    httpx_mock.add_response(method="POST", json=LOAD_BODY)
    with Client(BASE, "demo", StaticToken("t")) as c:
        c.generic_tables.create("ns", "t", format=GenericTableFormat.LANCE)
    body = json.loads(httpx_mock.get_requests()[0].read())
    assert body["format"] == "lance"


def test_create_rejects_bad_format_before_sending(httpx_mock):
    with Client(BASE, "demo", StaticToken("t")) as c:
        with pytest.raises(ConfigError):
            c.generic_tables.create("ns", "t", format="Not Valid")
    # No request should have been sent.
    assert httpx_mock.get_requests() == []
