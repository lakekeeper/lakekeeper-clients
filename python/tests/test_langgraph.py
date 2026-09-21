"""The LangGraph BaseStore adapter, driven through BaseStore's own public surface.

Exercised via `store.put/get/search/delete/list_namespaces` rather than `batch` directly,
so the tests assert what a graph actually calls.
"""

from __future__ import annotations

import asyncio

import pytest

from pylakekeeper import Client, StaticToken

pytest.importorskip("langgraph", reason="needs langgraph")

from pylakekeeper.agents import MemoryStore  # noqa: E402
from pylakekeeper.agents.langgraph import LakekeeperStore  # noqa: E402

BASE = "http://lk.example.com"


class _FakeObjects:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, key, data, *, content_type=None):
        self.data[key.lstrip("/")] = data

    def get(self, key):
        return self.data[key.lstrip("/")]

    def list(self, prefix=""):
        return sorted(k for k in self.data if k.startswith(prefix))

    def delete(self, key):
        self.data.pop(key.lstrip("/"), None)


class _StubEmbedder:
    model = "stub-v1"

    def __init__(self):
        self.embedded: list[str] = []

    def embed(self, texts):
        self.embedded.extend(texts)
        return [[float(len(t)), 1.0] for t in texts]


@pytest.fixture
def store():
    client = Client(BASE, "wh", StaticToken("t"))
    memory = MemoryStore(client, "agent_memory.agent_a")
    objects = _FakeObjects()
    memory._entries.objects = lambda: objects  # type: ignore[method-assign]
    yield LakekeeperStore(memory), objects
    client.close()


def test_put_get_roundtrip_preserves_the_value(store):
    lg, _ = store
    lg.put(("memories", "user-1"), "prefs", {"units": "metric", "tone": "brief"})

    item = lg.get(("memories", "user-1"), "prefs")

    assert item is not None
    assert item.value == {"units": "metric", "tone": "brief"}
    assert item.namespace == ("memories", "user-1")
    assert item.key == "prefs"


def test_namespace_and_key_become_a_path(store):
    lg, objects = store
    lg.put(("memories", "user-1"), "prefs", {"a": 1})
    assert "memories/user-1/prefs.json" in objects.data


def test_missing_key_returns_none(store):
    lg, _ = store
    assert lg.get(("memories", "user-1"), "absent") is None


def test_created_at_is_preserved_across_an_update(store):
    lg, _ = store
    lg.put(("memories",), "k", {"v": 1})
    first = lg.get(("memories",), "k")

    lg.put(("memories",), "k", {"v": 2})
    second = lg.get(("memories",), "k")

    assert second.value == {"v": 2}
    # Timestamps round-trip through the stored envelope rather than being invented.
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


def test_delete_removes_the_entry(store):
    lg, _ = store
    lg.put(("memories",), "k", {"v": 1})
    lg.delete(("memories",), "k")
    assert lg.get(("memories",), "k") is None


def test_put_none_deletes(store):
    lg, _ = store
    lg.put(("memories",), "k", {"v": 1})
    lg.put(("memories",), "k", None)
    assert lg.get(("memories",), "k") is None


def test_search_without_a_query_lists_the_namespace(store):
    lg, _ = store
    lg.put(("memories", "user-1"), "a", {"text": "alpha"})
    lg.put(("memories", "user-1"), "b", {"text": "beta"})
    lg.put(("memories", "user-2"), "c", {"text": "gamma"})

    results = lg.search(("memories", "user-1"))

    assert {r.key for r in results} == {"a", "b"}
    assert all(r.namespace == ("memories", "user-1") for r in results)


def test_search_filter_narrows_by_value(store):
    lg, _ = store
    lg.put(("m",), "a", {"kind": "semantic", "text": "x"})
    lg.put(("m",), "b", {"kind": "episodic", "text": "y"})

    results = lg.search(("m",), filter={"kind": "semantic"})

    assert [r.key for r in results] == ["a"]


def test_search_limit_and_offset(store):
    lg, _ = store
    for i in range(5):
        lg.put(("m",), f"k{i}", {"text": str(i)})

    assert len(lg.search(("m",), limit=2)) == 2
    assert len(lg.search(("m",), limit=2, offset=4)) == 1


def test_list_namespaces_deduplicates_and_sorts(store):
    lg, _ = store
    lg.put(("memories", "user-1"), "a", {"v": 1})
    lg.put(("memories", "user-1"), "b", {"v": 2})
    lg.put(("memories", "user-2"), "c", {"v": 3})
    lg.put(("skills",), "d", {"v": 4})

    assert lg.list_namespaces() == [
        ("memories", "user-1"),
        ("memories", "user-2"),
        ("skills",),
    ]


def test_list_namespaces_respects_prefix_and_max_depth(store):
    lg, _ = store
    lg.put(("memories", "user-1"), "a", {"v": 1})
    lg.put(("memories", "user-2"), "b", {"v": 2})
    lg.put(("skills",), "c", {"v": 3})

    assert lg.list_namespaces(prefix=("memories",)) == [
        ("memories", "user-1"),
        ("memories", "user-2"),
    ]
    assert lg.list_namespaces(prefix=("memories",), max_depth=1) == [("memories",)]


def test_path_segments_are_validated(store):
    lg, _ = store
    # Segments become path components, so a '/' would silently reshape the hierarchy.
    with pytest.raises(ValueError, match="contains '/'"):
        lg.put(("memories", "a/b"), "k", {"v": 1})
    with pytest.raises(ValueError, match="contains '/'"):
        lg.put(("memories",), "a/b", {"v": 1})


# --------------------------------------------------------------------- indexing


@pytest.fixture
def indexed_store():
    client = Client(BASE, "wh", StaticToken("t"))
    embedder = _StubEmbedder()
    memory = MemoryStore(client, "agent_memory.agent_a", embed=embedder)
    objects = _FakeObjects()
    memory._entries.objects = lambda: objects  # type: ignore[method-assign]
    indexed: list[tuple[str, str, str | None]] = []
    memory._index = lambda path, text, metadata, index_text=None: indexed.append(  # type: ignore[method-assign]
        (path, text, index_text)
    )
    yield LakekeeperStore(memory), indexed
    client.close()


def test_indexed_text_is_the_prose_not_the_envelope(indexed_store):
    lg, indexed = indexed_store
    lg.put(("m",), "k", {"note": "the user prefers metric units", "n": 3})

    path, body, index_text = indexed[0]
    # The body stored is the JSON envelope...
    assert body.startswith("{") and "created_at" in body
    # ...but what gets embedded is the content, not the braces and timestamps.
    assert "the user prefers metric units" in index_text
    assert "created_at" not in index_text


def test_index_fields_select_what_is_embedded(indexed_store):
    lg, indexed = indexed_store
    lg.put(("m",), "k", {"note": "embed me", "secret": "not me"}, index=["note"])

    _, _, index_text = indexed[0]
    assert index_text == "embed me"


def test_index_false_skips_embedding(indexed_store):
    lg, indexed = indexed_store
    lg.put(("m",), "k", {"note": "skip"}, index=False)
    assert indexed == []


# ------------------------------------------------------------------------- async


def test_abatch_runs_the_sync_path_off_the_loop(store):
    # Driven with asyncio.run rather than pytest-asyncio: one async test does not
    # justify a new dev dependency.
    lg, _ = store

    async def exercise():
        await lg.aput(("m",), "k", {"v": 1})
        return await lg.aget(("m",), "k")

    item = asyncio.run(exercise())
    assert item.value == {"v": 1}
