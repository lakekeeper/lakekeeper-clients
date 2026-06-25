"""Generic-tables surface tests: paths (%1F), vended creds, paging, 401-retry."""

from __future__ import annotations

import pytest

from pylakekeeper import Client, ClientCredentials, StaticToken
from pylakekeeper.errors import NotFoundError

BASE = "http://lk.example.com"
TOKEN_URL = "https://idp.example.com/token"

LOAD_BODY = {
    "table": {
        "name": "image_embeddings",
        "format": "lance",
        "base-location": "s3://bucket/ai/test/image_embeddings",
        "protected": False,
    },
    "config": {"s3.region": "us-east-1"},
    "storage-credentials": [
        {
            "prefix": "s3://bucket/ai/test/image_embeddings",
            "config": {
                "s3.access-key-id": "AKIA",
                "s3.secret-access-key": "secret",
                "s3.session-token": "sts",
                "s3.endpoint": "http://minio:9000",
            },
        }
    ],
}


@pytest.fixture
def client():
    c = Client(BASE, "demo", StaticToken("static-tok"), project_id="proj-1")
    yield c
    c.close()


def test_load_vended_encodes_namespace_and_sets_delegation(httpx_mock, client):
    httpx_mock.add_response(json=LOAD_BODY)
    t = client.generic_tables.load("ai.test", "image_embeddings", vended=True)

    req = httpx_mock.get_requests()[0]
    # Multi-level namespace is %1F-encoded in the path, not split into segments.
    assert "/namespaces/ai%1Ftest/generic-tables/image_embeddings" in str(req.url)
    assert req.headers["X-Iceberg-Access-Delegation"] == "vended-credentials"
    assert req.headers["x-project-id"] == "proj-1"
    assert req.headers["Authorization"] == "Bearer static-tok"

    assert t.location == "s3://bucket/ai/test/image_embeddings"


def test_lance_storage_options_mapping(httpx_mock, client):
    httpx_mock.add_response(json=LOAD_BODY)
    t = client.generic_tables.load("ai.test", "image_embeddings", vended=True)
    opts = t.lance_storage_options
    assert opts == {
        "aws_access_key_id": "AKIA",
        "aws_secret_access_key": "secret",
        "aws_session_token": "sts",
        "aws_endpoint": "http://minio:9000",
        "aws_region": "us-east-1",
        "allow_http": "true",  # http:// endpoint
    }


def test_load_without_vended_has_no_delegation_header(httpx_mock, client):
    httpx_mock.add_response(json=LOAD_BODY)
    client.generic_tables.load("ai.test", "image_embeddings")
    req = httpx_mock.get_requests()[0]
    assert "X-Iceberg-Access-Delegation" not in req.headers


def test_create(httpx_mock, client):
    httpx_mock.add_response(method="POST", json=LOAD_BODY)
    client.generic_tables.create(
        "ai.test", "image_embeddings", format="lance", properties={"embedding-dim": "768"}
    )
    req = httpx_mock.get_requests()[0]
    assert req.method == "POST"
    assert "/namespaces/ai%1Ftest/generic-tables" in str(req.url)
    import json as _json

    body = _json.loads(req.read())
    assert body == {
        "name": "image_embeddings",
        "format": "lance",
        "properties": {"embedding-dim": "768"},
    }


def test_list_follows_next_page_token(httpx_mock, client):
    httpx_mock.add_response(
        json={
            "identifiers": [{"namespace": ["ai", "test"], "name": "a"}],
            "next-page-token": "p2",
        }
    )
    httpx_mock.add_response(json={"identifiers": [{"namespace": ["ai", "test"], "name": "b"}]})
    names = [i.name for i in client.generic_tables.list("ai.test", page_size=1)]
    assert names == ["a", "b"]

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert "pageToken=p2" in str(reqs[1].url)


def test_drop_404_raises_not_found(httpx_mock, client):
    httpx_mock.add_response(method="DELETE", status_code=404, text="no such table")
    with pytest.raises(NotFoundError):
        client.generic_tables.drop("ai.test", "missing")


def test_401_triggers_token_refresh_and_retry(httpx_mock):
    # ClientCredentials so invalidate() forces a real re-fetch.
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "t1", "expires_in": 3600})
    httpx_mock.add_response(url=TOKEN_URL, json={"access_token": "t2", "expires_in": 3600})
    httpx_mock.add_response(method="GET", status_code=401, text="expired")
    httpx_mock.add_response(method="GET", json=LOAD_BODY)

    auth = ClientCredentials(TOKEN_URL, "cid", "secret")
    with Client(BASE, "demo", auth) as client:
        t = client.generic_tables.load("ai.test", "image_embeddings", vended=True)
    assert t.location == "s3://bucket/ai/test/image_embeddings"

    token_reqs = httpx_mock.get_requests(url=TOKEN_URL)
    assert len(token_reqs) == 2  # initial + after 401 invalidate
    api_reqs = [r for r in httpx_mock.get_requests() if "generic-tables" in str(r.url)]
    assert len(api_reqs) == 2
    assert api_reqs[0].headers["Authorization"] == "Bearer t1"
    assert api_reqs[1].headers["Authorization"] == "Bearer t2"
