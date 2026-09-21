"""Iceberg interop: one Auth serving both the generic-tables and Iceberg surfaces."""

from __future__ import annotations

import pytest

from pylakekeeper import Client, StaticToken

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

from pylakekeeper.iceberg import SharedAuthManager, register_auth  # noqa: E402

BASE = "http://lk.example.com"

# RestCatalog fetches /v1/config on construction; this is the minimal valid response.
CONFIG_BODY = {"defaults": {}, "overrides": {}}


class _RotatingAuth:
    """An Auth whose token changes, standing in for a refresh."""

    def __init__(self):
        self.calls = 0

    def auth_header(self) -> str:
        self.calls += 1
        return f"Bearer token-{self.calls}"

    def invalidate(self) -> None:
        pass


def test_shared_auth_manager_reads_the_header_per_request():
    # Captured once, a refreshed token would never reach PyIceberg. Read per call,
    # rotation is picked up with no reconfiguration.
    auth = _RotatingAuth()
    manager = SharedAuthManager(register_auth(auth))

    assert manager.auth_header() == "Bearer token-1"
    assert manager.auth_header() == "Bearer token-2"


def test_unknown_auth_handle_is_a_clear_error():
    with pytest.raises(ValueError, match="unknown pylakekeeper auth handle"):
        SharedAuthManager("not-a-real-handle")


def test_iceberg_catalog_targets_the_catalog_endpoint_and_warehouse(httpx_mock, monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRestCatalog:
        def __init__(self, name, **properties):
            captured["name"] = name
            captured.update(properties)

    monkeypatch.setattr("pylakekeeper.iceberg.RestCatalog", _FakeRestCatalog)

    with Client(BASE, "not-a-uuid", StaticToken("tok"), project_id="proj-1") as client:
        client.iceberg_catalog()

    assert captured["uri"] == f"{BASE}/catalog/"
    assert captured["warehouse"] == "not-a-uuid"
    assert captured["name"] == "not-a-uuid"
    assert captured["header.x-project-id"] == "proj-1"

    auth = captured["auth"]
    assert auth["type"] == "custom"
    assert auth["impl"] == "pylakekeeper.iceberg.SharedAuthManager"
    # The handle resolves to this client's own Auth — not a second credential.
    manager = SharedAuthManager(auth["custom"]["auth_ref"])
    assert manager.auth_header() == "Bearer tok"


def test_a_uuid_warehouse_is_resolved_to_its_name(httpx_mock, monkeypatch):
    """The two surfaces disagree about what identifies a warehouse.

    Generic tables take the UUID as a path prefix; the Iceberg catalog resolves
    `?warehouse=` by NAME and 404s on a UUID. A Client holds the UUID, so without this
    the catalog fails with NoSuchWarehouseException.
    """
    captured: dict[str, object] = {}

    class _FakeRestCatalog:
        def __init__(self, name, **properties):
            captured["name"] = name
            captured.update(properties)

    monkeypatch.setattr("pylakekeeper.iceberg.RestCatalog", _FakeRestCatalog)
    httpx_mock.add_response(json={"id": "8d3e9fc8-b19a-11f1-ba82-3b397722e86c", "name": "agentmem"})

    with Client(BASE, "8d3e9fc8-b19a-11f1-ba82-3b397722e86c", StaticToken("tok")) as client:
        client.iceberg_catalog()

    assert captured["warehouse"] == "agentmem"
    assert captured["name"] == "agentmem"
    # Resolved via the management API, not guessed.
    assert "/management/v1/warehouse/8d3e9fc8" in str(httpx_mock.get_requests()[0].url)


def test_an_unresolvable_uuid_falls_back_rather_than_raising(httpx_mock, monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRestCatalog:
        def __init__(self, name, **properties):
            captured.update(properties)

    monkeypatch.setattr("pylakekeeper.iceberg.RestCatalog", _FakeRestCatalog)
    httpx_mock.add_response(status_code=404)

    with Client(BASE, "8d3e9fc8-b19a-11f1-ba82-3b397722e86c", StaticToken("tok")) as client:
        client.iceberg_catalog()

    # PyIceberg gets the original value and reports its own error; we do not mask it.
    assert captured["warehouse"] == "8d3e9fc8-b19a-11f1-ba82-3b397722e86c"


def test_caller_properties_win_over_defaults(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRestCatalog:
        def __init__(self, name, **properties):
            captured.update(properties)

    monkeypatch.setattr("pylakekeeper.iceberg.RestCatalog", _FakeRestCatalog)

    with Client(BASE, "wh", StaticToken("tok")) as client:
        client.iceberg_catalog(name="custom", warehouse="other", uri="http://elsewhere/catalog/")

    assert captured["uri"] == "http://elsewhere/catalog/"
    assert captured["warehouse"] == "other"


def test_project_header_is_omitted_when_unset(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRestCatalog:
        def __init__(self, name, **properties):
            captured.update(properties)

    monkeypatch.setattr("pylakekeeper.iceberg.RestCatalog", _FakeRestCatalog)

    with Client(BASE, "wh", StaticToken("tok")) as client:
        client.iceberg_catalog()

    assert "header.x-project-id" not in captured
