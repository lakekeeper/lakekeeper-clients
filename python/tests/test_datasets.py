"""Datasets client logic against a mocked server: paging, rebase, resend, integrity."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest

from pylakekeeper import (
    Client,
    CommitConflictError,
    ConfigError,
    DatasetCredentials,
    IntegrityError,
    LakekeeperError,
    Retention,
    StaticToken,
)
from pylakekeeper.dataset_models import LoadDatasetCredentialsResponse

BASE = "http://lk.example.com"
DS = f"{BASE}/lakekeeper/v1/demo/namespaces/ai%1Ftest/datasets/images"
HEAD = "01a0fd3d-a6b5-73b1-911e-ca4c3dd40bf6"
NEXT = "01a0fd3d-a6b5-73b1-911e-ca4c3dd40bf7"

INFO = {
    "dataset": {
        "id": "01a0fd3d-a61b-7f41-a263-ce0168a95a69",
        "name": "images",
        "location": "s3://bucket/wh/images",
        "ownership": "managed",
        "protected": False,
    }
}


def _snapshot(snapshot_id: str, parent: str | None) -> dict[str, object]:
    return {
        "snapshot-id": snapshot_id,
        "parent-snapshot-id": parent,
        "created-at": "2026-10-02T12:00:00Z",
        "location": "s3://bucket/wh/images",
    }


def _refs(head: str | None) -> dict[str, object]:
    return {"refs": [{"name": "main", "typ": "branch", "snapshot-id": head, "protected": False}]}


def _conflict(head: str) -> dict[str, object]:
    return {
        "error": {
            "message": "Branch moved while committing",
            "type": "DatasetCommitConflict",
            "code": 409,
            "stack": [f"current-snapshot-id: {head}"],
        }
    }


def _file(key: str, etag: str | None = None) -> dict[str, object]:
    return {"logical-key": key, "physical-path": key, "etag": etag}


@pytest.fixture
def client():
    c = Client(BASE, "demo", StaticToken("catalog-token"))
    yield c
    c.close()


@pytest.fixture
def ds(httpx_mock, client):
    httpx_mock.add_response(method="GET", url=DS, json=INFO)
    return client.datasets.load("ai.test", "images")


def test_create_names_a_borrowed_location_and_encodes_the_namespace(httpx_mock, client):
    httpx_mock.add_response(json={**INFO, "dataset": {**INFO["dataset"], "ownership": "imported"}})
    ds = client.datasets.create("ai.test", "images", location="s3://bucket/landing")

    req = httpx_mock.get_requests()[0]
    assert str(req.url).endswith("/lakekeeper/v1/demo/namespaces/ai%1Ftest/datasets")
    assert json.loads(req.content) == {"name": "images", "location": "s3://bucket/landing"}
    assert not ds.managed


def test_a_listing_goes_on_past_an_empty_page(httpx_mock, ds):
    files = re.compile(re.escape(f"{DS}/refs/main/files") + ".*")
    page = {"access-mode": "presigned", "snapshot-id": HEAD}
    httpx_mock.add_response(url=files, json={**page, "files": [_file("a")], "next-page-token": "1"})
    httpx_mock.add_response(url=files, json={**page, "files": [], "next-page-token": "2"})
    httpx_mock.add_response(url=files, json={**page, "files": [_file("b")]})

    assert [f.logical_key for f in ds.branch("main").files()] == ["a", "b"]


def test_a_prefix_listing_stops_once_past_the_prefix(httpx_mock, ds):
    files = re.compile(re.escape(f"{DS}/refs/main/files") + ".*")
    page = {"access-mode": "presigned", "snapshot-id": HEAD, "next-page-token": "more"}
    httpx_mock.add_response(
        url=files, json={**page, "files": [_file("a/1"), _file("b/1"), _file("b/2"), _file("c/1")]}
    )

    assert [f.logical_key for f in ds.branch("main").files(prefix="b/")] == ["b/1", "b/2"]
    assert len(httpx_mock.get_requests(url=files)) == 1  # the next page is never fetched


def test_a_commit_that_lost_the_race_is_replayed_when_nothing_overlaps(httpx_mock, ds):
    commits = f"{DS}/branches/main/commits"
    httpx_mock.add_response(method="GET", url=f"{DS}/refs", json=_refs(HEAD))
    httpx_mock.add_response(method="POST", url=commits, status_code=409, json=_conflict(NEXT))
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{DS}/diff") + ".*"),
        json={"changes": [{"logical-key": "someone-else.txt", "change": "added"}]},
    )
    httpx_mock.add_response(
        method="POST", url=commits, json=_snapshot("01a0fd3d-0000-7000-8000-00000000000a", NEXT)
    )

    snapshot = ds.branch("main").commit_files(["mine.txt"])

    first, second = httpx_mock.get_requests(method="POST", url=commits)
    assert json.loads(first.content)["parent-snapshot-id"] == HEAD
    assert json.loads(second.content)["parent-snapshot-id"] == NEXT
    # One logical commit, one key: the server may answer the rebased retry from it.
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert str(snapshot.parent_snapshot_id) == NEXT


def test_a_commit_that_lost_the_race_on_its_own_file_is_refused(httpx_mock, ds):
    commits = f"{DS}/branches/main/commits"
    httpx_mock.add_response(method="GET", url=f"{DS}/refs", json=_refs(HEAD))
    httpx_mock.add_response(method="POST", url=commits, status_code=409, json=_conflict(NEXT))
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{DS}/diff") + ".*"),
        json={"changes": [{"logical-key": "labels.json", "change": "modified"}]},
    )

    with pytest.raises(CommitConflictError) as conflict:
        ds.branch("main").commit_files(["labels.json"])
    assert str(conflict.value.current_snapshot_id) == NEXT
    assert len(httpx_mock.get_requests(method="POST", url=commits)) == 1


def test_a_commit_is_resent_with_its_key_after_a_network_failure(httpx_mock, ds):
    commits = f"{DS}/branches/main/commits"
    httpx_mock.add_response(method="GET", url=f"{DS}/refs", json=_refs(None))
    httpx_mock.add_exception(httpx.ReadError("reset"), method="POST", url=commits)
    httpx_mock.add_response(method="POST", url=commits, json=_snapshot(HEAD, None))

    ds.branch("main").commit_files(["a.txt"])

    first, second = httpx_mock.get_requests(method="POST", url=commits)
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]


def _grant(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{DS}/refs/v1/access-grants",
        json={
            "grant-id": "01a0fd3d-0000-7000-8000-0000000000aa",
            "snapshot-id": HEAD,
            "access-mode": "presigned",
            "expires-at": "2099-01-01T00:00:00Z",
            "max-keys-per-request": 100,
            "url-validity-seconds": 900,
        },
    )


def _signed(httpx_mock, etag: str) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{DS}/snapshots/{HEAD}/files/sign",
        json={
            "files": [
                {
                    "logical-key": "a.jpg",
                    "url": "https://storage.example.com/bucket/a.jpg?X-Amz-Signature=x",
                    "expires-at": "2099-01-01T00:00:00Z",
                    "etag": etag,
                }
            ]
        },
    )


def test_a_read_goes_to_storage_without_the_catalog_token(httpx_mock, ds):
    _grant(httpx_mock)
    _signed(httpx_mock, '"abc"')
    httpx_mock.add_response(
        url="https://storage.example.com/bucket/a.jpg?X-Amz-Signature=x",
        content=b"bytes",
        headers={"ETag": '"abc"'},
    )

    assert ds.tag("v1").read("a.jpg") == b"bytes"
    fetch = httpx_mock.get_requests(url=re.compile("https://storage.example.com/.*"))[0]
    assert "Authorization" not in fetch.headers


def test_a_read_refuses_bytes_the_snapshot_did_not_record(httpx_mock, ds):
    _grant(httpx_mock)
    _signed(httpx_mock, '"recorded"')
    httpx_mock.add_response(
        url="https://storage.example.com/bucket/a.jpg?X-Amz-Signature=x",
        content=b"other bytes",
        headers={"ETag": '"rewritten"'},
    )

    with pytest.raises(IntegrityError):
        ds.tag("v1").read("a.jpg")


def test_an_imported_dataset_explains_why_it_has_no_credentials(httpx_mock, ds):
    httpx_mock.add_response(
        url=f"{DS}/credentials",
        status_code=409,
        json={
            "error": {
                "message": "no credentials for imported datasets",
                "type": "CannotVendCredentialsForImportedDataset",
                "code": 409,
            }
        },
    )
    with pytest.raises(ConfigError, match="imported"):
        ds.credentials()


def test_waiting_on_a_queued_import_returns_its_outcome(httpx_mock, ds, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    task = "01a0fd3d-0000-7000-8000-0000000000bb"
    httpx_mock.add_response(
        method="POST",
        url=f"{DS}/import",
        json={
            "imported": 0,
            "modified": 0,
            "removed": 0,
            "skipped": 0,
            "truncated": False,
            "task-id": task,
        },
    )
    by_id = f"{BASE}/management/v1/warehouse/demo/task/by-id/{task}"
    running = {
        "task-id": task,
        "status": "RUNNING",
        "progress": 0.0,
        "execution-details": {"phase": "listing", "objects_listed": 5000},
    }
    done = {
        "task-id": task,
        "status": "SUCCESS",
        "progress": 1.0,
        "execution-details": {
            "phase": "done",
            "objects_listed": 7000,
            "imported": 7000,
            "modified": 0,
            "removed": 0,
            "skipped": 0,
            "truncated": False,
            "skipped_files": [],
        },
    }
    httpx_mock.add_response(url=by_id, json=running)
    httpx_mock.add_response(url=by_id, json=done)

    phases: list[str | None] = []
    result = ds.queue_import(mode="sync").wait(on_progress=lambda s: phases.append(s.phase))
    assert result.imported == 7000
    assert phases == ["listing", "done"]


def test_a_failed_queued_import_raises(httpx_mock, ds, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    task = "01a0fd3d-0000-7000-8000-0000000000cc"
    httpx_mock.add_response(
        method="POST",
        url=f"{DS}/import",
        json={
            "imported": 0,
            "modified": 0,
            "removed": 0,
            "skipped": 0,
            "truncated": False,
            "task-id": task,
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/management/v1/warehouse/demo/task/by-id/{task}",
        json={"task-id": task, "status": "FAILED", "message": "branch was reset"},
    )
    with pytest.raises(LakekeeperError, match="branch was reset"):
        ds.queue_import().wait()


def test_retention_policies_on_the_wire():
    assert Retention.max_count(5, grace_period="P1D").wire() == {
        "mode": "max-count",
        "max-snapshots": 5,
        "grace-period": "P1D",
    }
    assert Retention.ttl(timedelta(days=30), min_snapshots_to_keep=3).wire() == {
        "mode": "ttl",
        "max-snapshot-age": "P30D",
        "min-snapshots-to-keep": 3,
    }
    assert Retention.manual(grace_period=timedelta(hours=6)).wire() == {
        "mode": "manual",
        "grace-period": "PT21600S",
    }


def test_a_writer_reads_with_one_credential_and_writes_with_another():
    def cred(prefix: str, key: str) -> dict[str, object]:
        return {"prefix": prefix, "config": {"s3.access-key-id": key, "s3.secret-access-key": "s"}}

    creds = DatasetCredentials(
        "s3://bucket/wh/images",
        LoadDatasetCredentialsResponse.model_validate(
            {
                "storage-credentials": [
                    cred("s3://bucket/wh/images", "READ"),
                    cred("s3://bucket/wh/images/data/0199", "WRITE"),
                ],
                "write-prefix": "s3://bucket/wh/images/data/0199",
            }
        ),
        {},
    )
    assert creds.props["s3.access-key-id"] == "READ"
    assert creds.fsspec_kwargs["key"] == "READ"
    assert creds._props("s3://bucket/wh/images/data/0199")["s3.access-key-id"] == "WRITE"
    # A sibling folder is not the writer's: only the read credential covers it.
    assert creds._props("s3://bucket/wh/images/data/0198/x")["s3.access-key-id"] == "READ"


def test_a_reader_has_no_write_folder():
    creds = DatasetCredentials(
        "s3://bucket/wh/images",
        LoadDatasetCredentialsResponse.model_validate({"storage-credentials": []}),
        {},
    )
    with pytest.raises(ConfigError, match="may not commit"):
        creds.write_objects()
