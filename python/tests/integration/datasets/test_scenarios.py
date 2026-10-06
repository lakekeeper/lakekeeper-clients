"""Datasets, end to end: the workflows they exist for, against a real server and bucket.

Each test is one story — reproducible training data, write-audit-publish, concurrent
producers, a retry after a lost response, registering a bucket in place — told through
the client API alone, with storage written from outside the catalog where the story
needs another tool. The bucket keeps no object versions unless a story is about them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from pylakekeeper import (
    Client,
    CommitConflictError,
    CommitFile,
    ConfigError,
    ConflictError,
    Constraints,
    IntegrityError,
    LakekeeperHTTPError,
    StaticToken,
)
from pylakekeeper.transport import Transport

PROJECT = "00000000-0000-0000-0000-000000000000"

pytestmark = pytest.mark.integration


def _images(root: Path, n: int, tag: str) -> Path:
    root.mkdir(parents=True)
    for i in range(n):
        (root / f"{i:03}.jpg").write_bytes(f"{tag}-{i}".encode())
    return root


def _keys(ref) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.logical_key for f in ref.files()}


def _published(ref) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.physical_path for f in ref.files()}


def test_a_tag_reads_the_training_set_it_was_cut_from(client: Client, namespace, server, tmp_path):
    ds = client.datasets.create(namespace, "images")
    with ds.branch("main").commit(summary={"run": "r1"}) as c:
        c.upload_dir(_images(tmp_path / "v1", 20, "v1"), prefix="img/")
    v1 = ds.create_tag("train-v1")

    # The producer moves on: rewrites an image in place, drops one, adds one.
    with ds.branch("main").commit(summary={"run": "r2"}) as c:
        c.put("img/000.jpg", b"v2-0")
        c.remove("img/001.jpg")
        c.put("img/new.jpg", b"new")

    # The tag still lists exactly the training set...
    assert _keys(v1) == {f"img/{i:03}.jpg" for i in range(20)}
    # ...and reads the bytes it was trained on, though the file was replaced since and
    # the bucket keeps no versions, and though main has dropped the file.
    assert v1.read("img/000.jpg") == b"v1-0"
    assert v1.read("img/001.jpg") == b"v1-1"
    assert ds.branch("main").read("img/000.jpg") == b"v2-0"
    # Nothing was overwritten: storage holds every version's bytes, and nothing else.
    assert server.stored(ds.location) == _published(v1) | _published(ds.branch("main"))

    changes = {ch.logical_key: ch.change for ch in ds.diff("train-v1", "main")}
    assert changes == {"img/000.jpg": "modified", "img/001.jpg": "removed", "img/new.jpg": "added"}

    # Downloading the tag, then main into the same directory, fetches only what differs.
    first = v1.download(tmp_path / "data")
    assert (first.downloaded, first.skipped) == (20, 0)
    again = v1.download(tmp_path / "data")
    assert (again.downloaded, again.skipped) == (0, 20)
    moved = ds.branch("main").download(tmp_path / "data")
    assert (moved.downloaded, moved.skipped) == (2, 18)
    assert (tmp_path / "data/img/000.jpg").read_bytes() == b"v2-0"
    assert (tmp_path / "data/img/new.jpg").read_bytes() == b"new"


def test_write_audit_publish_through_a_protected_main(client: Client, namespace):
    ds = client.datasets.create(namespace, "events")
    main = ds.branch("main")
    with main.commit() as c:
        c.put("day=1/part-0.csv", b"a,b\n1,2\n")
    main.protect()

    # Nobody commits to main directly any more — owners included.
    with pytest.raises(ConflictError) as refused:
        main.commit_files(["day=1/part-1.csv"])
    assert refused.value.error_type == "DatasetRefProtected"

    # Write: the pipeline commits to a branch of its own.
    etl = ds.create_branch("etl-42", from_="main")
    with etl.commit(summary={"pipeline-run": "42"}) as c:
        c.put("day=2/part-0.csv", b"a,b\n3,4\n")
        c.remove("day=1/part-0.csv")

    # Audit: review exactly what would change.
    changes = sorted((ch.logical_key, ch.change) for ch in ds.diff("main", "etl-42"))
    assert changes == [("day=1/part-0.csv", "removed"), ("day=2/part-0.csv", "added")]

    # Publish: a fast-forward is the one move a protected branch takes.
    main.fast_forward("etl-42")
    assert _keys(main) == {"day=2/part-0.csv"}
    etl.delete()

    # A branch that forked from history main has since left cannot fast-forward it.
    stale = ds.create_branch("stale", from_=c.parent_snapshot_id)  # type: ignore[arg-type]
    with stale.commit() as s:
        s.put("day=3/part-0.csv", b"x")
    with pytest.raises(ConflictError):
        main.fast_forward("stale")


def test_concurrent_producers_rebase_unless_they_touch_the_same_file(
    client: Client, namespace, server
):
    ds = client.datasets.create(namespace, "shards")
    main = ds.branch("main")
    main.commit_files([])  # an empty first snapshot, for both producers to start from

    # Two producers start from the same head and write different shards.
    a, b = main.commit(), main.commit()
    a.put("shard-a/0.bin", b"a")
    b.put("shard-b/0.bin", b"b")
    first = a.commit()
    second = b.commit()  # lost the race: replayed onto a's snapshot, nothing lost
    assert second.parent_snapshot_id == first.snapshot_id
    assert _keys(ds.branch("main")) == {"shard-a/0.bin", "shard-b/0.bin"}

    # Two producers that write the same file: the second is refused, not replayed over
    # the first, and told where the branch is now.
    x, y = main.commit(), main.commit()
    x.put("labels.json", b'{"by": "x"}')
    y.put("labels.json", b'{"by": "y"}')
    x.commit()
    with pytest.raises(CommitConflictError) as conflict:
        y.commit()
    assert conflict.value.current_snapshot_id == x.snapshot.snapshot_id  # type: ignore[union-attr]
    # y wrote its own object, so x's bytes are untouched; refused, y deleted it again.
    assert ds.branch("main").read("labels.json") == b'{"by": "x"}'
    assert server.stored(ds.location) == _published(ds.branch("main"))


def test_a_writer_cannot_touch_files_a_snapshot_records(client: Client, namespace, server):
    ds = client.datasets.create(namespace, "guarded")
    with ds.branch("main").commit() as c:
        c.put("model/weights.bin", b"good weights")
    (recorded,) = _published(ds.branch("main"))

    # A writer — a pipeline, an agent — holds the catalog's write credential directly.
    creds = ds._fetch_credentials()
    s3 = creds.write_objects()._client  # type: ignore[attr-defined]
    bucket = server.s3_bucket
    base = ds.location.removeprefix(f"s3://{bucket}/").rstrip("/")
    write_dir = (creds.write_prefix or "").removeprefix(f"s3://{bucket}/")

    # It writes its own folder...
    s3.put_object(Bucket=bucket, Key=f"{write_dir}/new.bin", Body=b"new")
    # ...and cannot overwrite or delete what main points at, whatever it means to do.
    for attempt in (
        lambda: s3.put_object(Bucket=bucket, Key=f"{base}/{recorded}", Body=b"bad"),
        lambda: s3.delete_object(Bucket=bucket, Key=f"{base}/{recorded}"),
    ):
        with pytest.raises(Exception, match="AccessDenied"):
            attempt()
    assert ds.branch("main").read("model/weights.bin") == b"good weights"


class _LoseFirstCommitResponse(httpx.HTTPTransport):
    """Delivers the first commit to the server, then drops its response on the floor."""

    def __init__(self) -> None:
        super().__init__()
        self.lost = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if request.method == "POST" and request.url.path.endswith("/commits") and not self.lost:
            self.lost += 1
            response.read()
            raise httpx.ReadError("connection reset after the server answered")
        return response


def test_a_commit_whose_response_was_lost_is_published_once(server, namespace):
    flaky = _LoseFirstCommitResponse()
    client = Client(server.url, server.warehouse_id, StaticToken("dev"), project_id=PROJECT)
    client.datasets._t = Transport(
        server.url,
        StaticToken("dev"),
        project_id=PROJECT,
        client=httpx.Client(base_url=server.url, transport=flaky),
    )
    ds = client.datasets.create(namespace, "lossy")
    snapshot = ds.branch("main").commit_files(["a.txt"], summary={"attempt": "one"})

    assert flaky.lost == 1  # the first answer never arrived; the client resent
    refs = {r.name: r.snapshot_id for r in ds.refs()}
    assert refs["main"] == snapshot.snapshot_id
    assert snapshot.parent_snapshot_id is None  # one commit, not one per attempt
    client.close()


def test_registering_a_bucket_prefix_in_place(client: Client, namespace, server):
    s3 = server.s3()
    landing = f"{server.key_prefix}/landing/{uuid.uuid4().hex[:8]}"
    for i in range(30):
        s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/cams/{i:02}.jpg", Body=f"c{i}")
    s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/cams/_SUCCESS", Body=b"")

    ds = client.datasets.create(namespace, "cams", location=f"s3://{server.s3_bucket}/{landing}")
    assert not ds.managed
    result = ds.sync()
    assert (result.imported, result.removed) == (30, 0)  # the job marker is left out
    day1 = ds.create_tag("day-1")

    # The catalog never hands out credentials for a borrowed prefix...
    with pytest.raises(ConfigError):
        ds.credentials()
    # ...so reads go through signed URLs, which need none.
    assert day1.read("cams/05.jpg") == b"c5"

    # Another tool rewrites one object in place, deletes one, adds one.
    s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/cams/00.jpg", Body=b"overwritten")
    s3.delete_object(Bucket=server.s3_bucket, Key=f"{landing}/cams/01.jpg")
    s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/cams/30.jpg", Body=b"c30")

    # The tag recorded no object versions, so the rewritten file is caught, not served.
    with pytest.raises(IntegrityError):
        day1.read("cams/00.jpg")

    # A sync brings main in step, and checks every tag against what is really there.
    result = ds.sync(check_materialization=True)
    assert (result.imported, result.modified, result.removed) == (1, 1, 1)
    report = ds.materialization("day-1")
    assert report.status == "partially-degraded"
    assert {(f.logical_key, f.problem) for f in report.files} == {
        ("cams/00.jpg", "changed"),
        ("cams/01.jpg", "missing"),
    }
    assert ds.materialization("main").status == "fully-materialized"

    # Nothing changed since: a second sync publishes nothing.
    head = ds.branch("main").head()
    assert ds.sync().snapshot_id == head


def test_recorded_versions_keep_a_borrowed_prefix_reproducible(
    versioned_client: Client, namespace, server
):
    # Another tool owns this prefix and writes keys in place; only versions can help.
    s3, bucket = server.s3(), server.versioned_bucket
    landing = f"{server.key_prefix}/landing/{uuid.uuid4().hex[:8]}"
    for name in ("a", "b"):
        s3.put_object(Bucket=bucket, Key=f"{landing}/{name}.txt", Body=f"{name}-1")
    ds = versioned_client.datasets.create(namespace, "pinned", location=f"s3://{bucket}/{landing}")
    ds.sync(record_versions=True)
    v1 = ds.create_tag("v1")

    s3.put_object(Bucket=bucket, Key=f"{landing}/a.txt", Body=b"a-2")
    s3.delete_object(Bucket=bucket, Key=f"{landing}/b.txt")

    # Pinned to object versions, the tag reads what it recorded, whatever the key holds.
    assert v1.read("a.txt") == b"a-1"
    assert v1.read("b.txt") == b"b-1"


def test_curated_names_over_raw_objects(client: Client, namespace, server):
    s3 = server.s3()
    landing = f"{server.key_prefix}/landing/{uuid.uuid4().hex[:8]}"
    s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/raw/0001.jpg", Body=b"a cat")
    s3.put_object(Bucket=server.s3_bucket, Key=f"{landing}/raw/0002.jpg", Body=b"a dog")
    ds = client.datasets.create(namespace, "pets", location=f"s3://{server.s3_bucket}/{landing}")

    # A curated training set names files for what they are; the bytes stay where they are.
    ds.branch("main").commit_files(
        [
            CommitFile(logical_key="train/cat/0.jpg", physical_path="raw/0001.jpg"),
            CommitFile(logical_key="train/dog/0.jpg", physical_path="raw/0002.jpg"),
        ]
    )
    main = ds.branch("main")
    assert main.read("train/cat/0.jpg") == b"a cat"
    assert [f.logical_key for f in main.files(prefix="train/dog/")] == ["train/dog/0.jpg"]
    # A sync over the prefix recognises the objects behind them, and registers nothing twice.
    assert ds.sync().imported == 0


def test_a_large_prefix_imports_on_the_task_queue(client: Client, namespace, server):
    s3 = server.s3()
    landing = f"{server.key_prefix}/landing/{uuid.uuid4().hex[:8]}"
    for i in range(1500):
        s3.put_object(
            Bucket=server.s3_bucket, Key=f"{landing}/part={i % 10}/{i:05}.json", Body=b"{}"
        )
    ds = client.datasets.create(namespace, "logs", location=f"s3://{server.s3_bucket}/{landing}")

    seen: list[str] = []
    task = ds.queue_import(mode="sync")
    result = task.wait(timeout=120, poll_interval=0.2, on_progress=lambda s: seen.append(s.status))
    assert result.imported == 1500
    assert seen[-1] == "SUCCESS"
    assert sum(1 for _ in ds.branch("main").files(page_size=200)) == 1500


def test_constraints_reject_or_skip_what_does_not_belong(client: Client, namespace, server):
    ds = client.datasets.create(
        namespace, "jpegs", constraints=Constraints(allowed_content_types=["image/jpeg"])
    )
    main = ds.branch("main")
    with pytest.raises(LakekeeperHTTPError) as rejected:
        with main.commit() as c:
            c.put("ok.jpg", b"jpeg")
            c.put("notes.txt", b"text")
    assert rejected.value.error_type == "DatasetConstraintViolation"
    assert main.head() is None  # nothing published...
    assert server.stored(ds.location) == set()  # ...and nothing left behind

    # A commit abandoned half-way through cleans up after itself as well.
    with pytest.raises(RuntimeError):
        with main.commit() as c:
            c.put("ok.jpg", b"jpeg")
            raise RuntimeError("the producer crashed")
    assert server.stored(ds.location) == set()

    with main.commit(on_constraint_violation="skip") as c:
        c.put("ok.jpg", b"jpeg")
        c.put("notes.txt", b"text")
    assert [s.logical_key for s in c.snapshot.skipped] == ["notes.txt"]  # type: ignore[union-attr]
    assert _keys(main) == {"ok.jpg"}
    assert server.stored(ds.location) == _published(main)  # the skipped upload is gone


def test_expired_history_is_hidden_until_restored(client: Client, namespace):
    ds = client.datasets.create(namespace, "history")
    main = ds.branch("main")
    old = main.commit_files(["a.txt"])
    main.commit_files(["b.txt"])
    ds.create_tag("keep", from_=old.snapshot_id)

    # A tagged snapshot is held: retention, by hand or by policy, leaves it alone.
    with pytest.raises(ConflictError) as held:
        ds.expire(old.snapshot_id)
    assert held.value.error_type == "DatasetSnapshotHeld"

    ds.tag("keep").delete()
    expired = ds.expire(old.snapshot_id)
    assert expired.purge_after > old.created_at
    with pytest.raises(LakekeeperHTTPError):  # hidden: nothing can point at it now
        ds.create_tag("again", from_=old.snapshot_id)

    ds.restore(old.snapshot_id)
    ds.create_tag("again", from_=old.snapshot_id)
    assert _keys(ds.tag("again")) == {"a.txt"}


def test_a_branch_moves_on_and_a_tag_does_not(client: Client, namespace, tmp_path):
    ds = client.datasets.create(namespace, "moving")
    ds.branch("main").commit_files(["a.txt"])
    main, tag = ds.branch("main"), ds.create_tag("first")
    assert _keys(main) == _keys(tag) == {"a.txt"}

    ds.branch("main").commit_files(["b.txt"])  # someone else moves the branch on

    # The same handles, read again: the branch reads where it is now, the tag where it was.
    assert _keys(main) == {"a.txt", "b.txt"}
    assert _keys(tag) == {"a.txt"}
