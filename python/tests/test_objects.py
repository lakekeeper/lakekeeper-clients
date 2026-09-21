"""Storage-credential expiry and `dataset`-table object I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pylakekeeper import Client, ConfigError, StaticToken
from pylakekeeper.models import LoadGenericTableResponse
from pylakekeeper.objects import object_store_for

BASE = "http://lk.example.com"


def _load_body(
    *,
    location: str = "s3://bucket/ai/memory/agent_a",
    cred_config: dict[str, str] | None = None,
    config: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "table": {
            "name": "agent_a",
            "format": "dataset",
            "base-location": location,
            "protected": False,
        },
        "config": config if config is not None else {"s3.region": "local-01"},
        "storage-credentials": [
            {
                "prefix": location,
                "config": cred_config
                if cred_config is not None
                else {
                    "s3.access-key-id": "AKIA",
                    "s3.secret-access-key": "secret",
                    "s3.session-token": "sts",
                },
            }
        ],
    }


def _resp(**kwargs: object) -> LoadGenericTableResponse:
    return LoadGenericTableResponse.model_validate(_load_body(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- expiry


def _ms(delta_seconds: float) -> str:
    when = datetime.now(tz=timezone.utc) + timedelta(seconds=delta_seconds)
    return str(int(when.timestamp() * 1000))


def test_expires_at_prefers_backend_agnostic_key():
    # `expiration-time` wins over the S3-specific key when both are present.
    t = _resp(
        cred_config={
            "s3.access-key-id": "AKIA",
            "expiration-time": "1700000000000",
            "s3.session-token-expires-at-ms": "1600000000000",
        }
    )
    assert t.expires_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)


@pytest.mark.parametrize(
    "key",
    [
        "s3.session-token-expires-at-ms",
        "gcs.oauth2.token-expires-at",
        "adls.sas-token-expires-at-ms.myaccount.dfs.core.windows.net",
    ],
)
def test_expires_at_falls_back_to_each_backend_key(key):
    t = _resp(cred_config={key: "1700000000000"})
    assert t.expires_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_expires_at_is_none_when_unstated_or_unparsable():
    assert _resp().expires_at is None
    assert _resp(cred_config={"expiration-time": "soon"}).expires_at is None


def test_unknown_expiry_never_reports_expired():
    # An absent expiry must not push callers into a reload loop.
    t = _resp()
    assert t.is_expired is False
    assert t.expires_within(3600) is False


def test_expires_within_and_is_expired():
    fresh = _resp(cred_config={"expiration-time": _ms(300)})
    assert fresh.is_expired is False
    assert fresh.expires_within(60) is False
    assert fresh.expires_within(600) is True

    lapsed = _resp(cred_config={"expiration-time": _ms(-5)})
    assert lapsed.is_expired is True


def test_expiry_is_read_from_top_level_config_too():
    t = _resp(cred_config={}, config={"expiration-time": "1700000000000"})
    assert t.expires_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_credentials_merges_creds_and_config(httpx_mock):
    with Client(BASE, "demo", StaticToken("t")) as c:
        httpx_mock.add_response(json=_load_body())
        t = c.generic_tables.load("ai.memory", "agent_a", vended=True)
    assert t.credentials["s3.access-key-id"] == "AKIA"
    assert t.credentials["s3.region"] == "local-01"


# ---------------------------------------------------------------- store construction


class _FakeS3:
    """Stand-in for boto3's client; records calls instead of making them."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.deleted: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803
        self.bucket = Bucket
        self.objects[Key] = Body
        self.content_types[Key] = ContentType

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, *, Bucket, Prefix):  # noqa: N803
                yield {
                    "Contents": [{"Key": k} for k in sorted(outer.objects) if k.startswith(Prefix)]
                }

        return _P()


@pytest.fixture
def s3_store(monkeypatch):
    fake = _FakeS3()
    store = object_store_for(
        "s3://bucket/ai/memory/agent_a",
        {"s3.access-key-id": "AKIA", "s3.secret-access-key": "s", "s3.endpoint": "http://mi:9000"},
    )
    monkeypatch.setattr(store, "_client", fake)
    return store, fake


def test_put_get_list_delete_are_relative_to_the_table_location(s3_store):
    store, fake = s3_store

    store.put("memories/preferences.md", b"# prefs")
    # The caller passed a relative key; the table's prefix is applied underneath.
    assert "ai/memory/agent_a/memories/preferences.md" in fake.objects
    assert fake.objects["ai/memory/agent_a/memories/preferences.md"] == b"# prefs"
    # Markdown gets a real content type, not application/octet-stream.
    assert fake.content_types["ai/memory/agent_a/memories/preferences.md"] == "text/markdown"

    assert store.get("memories/preferences.md") == b"# prefs"

    store.put("skills/summarise.md", b"# skill")
    # Listing returns relative keys, and honours the prefix filter.
    assert store.list() == ["memories/preferences.md", "skills/summarise.md"]
    assert store.list(prefix="memories/") == ["memories/preferences.md"]

    store.delete("memories/preferences.md")
    assert fake.deleted == ["ai/memory/agent_a/memories/preferences.md"]
    assert store.list() == ["skills/summarise.md"]


def test_leading_slash_on_a_key_is_tolerated(s3_store):
    store, fake = s3_store
    store.put("/memories/a.md", b"x")
    assert "ai/memory/agent_a/memories/a.md" in fake.objects


def test_unsupported_scheme_is_rejected_with_a_useful_message():
    with pytest.raises(ConfigError) as exc:
        object_store_for("hdfs://nn/path", {})
    assert "unsupported storage scheme" in str(exc.value)
    assert "hdfs" in str(exc.value)


def test_malformed_adls_location_is_rejected():
    with pytest.raises(ConfigError, match="malformed ADLS location"):
        object_store_for("abfss://account.dfs.core.windows.net/prefix", {})


def test_adls_sas_lookup_does_not_match_the_expiry_key():
    # `adls.sas-token-expires-at-ms.<...>` must not be mistaken for the token itself:
    # signing with a timestamp fails at request time, far from the cause.
    pytest.importorskip("azure.storage.blob")
    with pytest.raises(ConfigError, match="no ADLS SAS token"):
        object_store_for(
            "abfss://container@acct.dfs.core.windows.net/prefix",
            {"adls.sas-token-expires-at-ms.acct.dfs.core.windows.net": "1700000000000"},
        )
