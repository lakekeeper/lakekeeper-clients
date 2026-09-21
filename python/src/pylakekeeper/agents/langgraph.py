"""A LangGraph ``BaseStore`` backed by Lakekeeper-governed memory.

Swap an unmanaged long-term memory store for a governed one without touching your graph::

    from pylakekeeper.agents import MemoryStore
    from pylakekeeper.agents.langgraph import LakekeeperStore

    memory = MemoryStore(client, "agent_memory.agent_a", embed=my_embedder)
    store = LakekeeperStore(memory)

    graph = builder.compile(store=store)

Every operation still goes through credential vending, so an agent whose grant is revoked
stops being able to read its memory — mid-run, without redeploying anything.

**One store is one trust boundary.** LangGraph namespaces (``("memories", "user-123")``)
become path prefixes *inside* a single Lakekeeper scope; they organise, they do not
isolate. Anything that may read this store can read every namespace in it. Where two
namespaces must not see each other, give them two :class:`~pylakekeeper.agents.MemoryStore`
scopes with their own grants — the catalog's isolation boundary is the table.

Needs ``pip install 'pylakekeeper[agents]' langgraph``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from .memory import MemoryStore

#: Entries are stored as a JSON envelope so ``Item``'s timestamps survive a round trip
#: rather than being invented on read.
_SUFFIX = ".json"


class LakekeeperStore(BaseStore):
    """Adapts :class:`~pylakekeeper.agents.MemoryStore` to LangGraph's ``BaseStore``.

    Args:
        memory: the scope to store into. Supply it with an ``embed`` to make
            :meth:`search` semantic; without one, search falls back to listing.

    ``BaseStore`` defines ``get``/``put``/``search``/``delete``/``list_namespaces`` in
    terms of :meth:`batch`, so only ``batch`` and ``abatch`` are implemented here.
    """

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    # ------------------------------------------------------------------ path mapping

    @staticmethod
    def _check_segments(namespace: tuple[str, ...]) -> None:
        for segment in namespace:
            if "/" in segment or not segment:
                raise ValueError(
                    f"namespace segment {segment!r} is empty or contains '/'; "
                    "segments become path components and must not do either"
                )

    @classmethod
    def _path(cls, namespace: tuple[str, ...], key: str) -> str:
        cls._check_segments(namespace)
        if "/" in key or not key:
            raise ValueError(f"key {key!r} is empty or contains '/'")
        return "/".join([*namespace, key]) + _SUFFIX

    @staticmethod
    def _split(path: str) -> tuple[tuple[str, ...], str] | None:
        """Inverse of :meth:`_path`; ``None`` for anything this store did not write."""
        if not path.endswith(_SUFFIX):
            return None
        parts = path[: -len(_SUFFIX)].split("/")
        if len(parts) < 2:
            return None
        return tuple(parts[:-1]), parts[-1]

    # --------------------------------------------------------------------- envelope

    @staticmethod
    def _wrap(value: dict[str, Any], created_at: datetime, updated_at: datetime) -> str:
        return json.dumps(
            {
                "value": value,
                "created_at": created_at.isoformat(),
                "updated_at": updated_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def _unwrap(
        cls, namespace: tuple[str, ...], key: str, body: str
    ) -> tuple[dict[str, Any], datetime, datetime]:
        payload = json.loads(body)
        now = datetime.now(tz=timezone.utc)
        created = payload.get("created_at")
        updated = payload.get("updated_at")
        return (
            payload.get("value", {}),
            datetime.fromisoformat(created) if created else now,
            datetime.fromisoformat(updated) if updated else now,
        )

    @staticmethod
    def _index_text(value: dict[str, Any], index: list[str] | None) -> str:
        """The text to embed: the named fields, or every string in the value."""
        if index:
            parts = [str(value[field]) for field in index if field in value]
        else:
            parts = [str(v) for v in value.values() if isinstance(v, (str, int, float))]
        return "\n".join(parts) if parts else json.dumps(value, sort_keys=True)

    # ------------------------------------------------------------------ operations

    def _get(self, op: GetOp) -> Item | None:
        path = self._path(op.namespace, op.key)
        try:
            body = self.memory.get(path)
        except Exception:  # noqa: BLE001 - a miss is a miss; authz errors surface on write
            return None
        value, created_at, updated_at = self._unwrap(op.namespace, op.key, body)
        return Item(
            value=value,
            key=op.key,
            namespace=op.namespace,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _put(self, op: PutOp) -> None:
        path = self._path(op.namespace, op.key)
        if op.value is None:
            self.memory.delete(path)
            return

        now = datetime.now(tz=timezone.utc)
        created_at = now
        existing = self._get(GetOp(namespace=op.namespace, key=op.key, refresh_ttl=False))
        if existing is not None:
            created_at = existing.created_at

        index_fields = op.index if isinstance(op.index, list) else None
        self.memory.put(
            path,
            self._wrap(op.value, created_at, now),
            index=op.index is not False,
            index_text=self._index_text(op.value, index_fields),
        )

    def _search(self, op: SearchOp) -> list[SearchItem]:
        prefix = "/".join(op.namespace_prefix)
        # (path, body-if-already-fetched, distance-if-ranked)
        found: list[tuple[str, str | None, float | None]]
        if op.query:
            # Over-fetch: hits outside the requested namespace prefix are filtered out
            # below, and the vector index does not know about prefixes.
            hits = self.memory.search(op.query, k=(op.limit + op.offset) * 4 or 10)
            found = [
                (hit.path, hit.text, hit.distance)
                for hit in hits
                if not prefix or hit.path.startswith(prefix + "/")
            ]
        else:
            found = [(path, None, None) for path in self.memory.list(prefix)]

        items: list[SearchItem] = []
        for path, body, distance in found:
            split = self._split(path)
            if split is None:
                continue
            namespace, key = split
            text = body if body is not None else self.memory.get(path)
            try:
                value, created_at, updated_at = self._unwrap(namespace, key, text)
            except json.JSONDecodeError:
                continue  # not written by this store
            if op.filter and any(value.get(k) != v for k, v in op.filter.items()):
                continue
            items.append(
                SearchItem(
                    namespace=namespace,
                    key=key,
                    value=value,
                    created_at=created_at,
                    updated_at=updated_at,
                    # BaseStore scores higher-is-better; vector distance is the inverse.
                    score=(1.0 / (1.0 + distance)) if distance is not None else None,
                )
            )
        return items[op.offset : op.offset + op.limit]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        seen: set[tuple[str, ...]] = set()
        for path in self.memory.list():
            split = self._split(path)
            if split is None:
                continue
            namespace = split[0]
            if op.max_depth is not None:
                namespace = namespace[: op.max_depth]
            seen.add(namespace)

        matched = [ns for ns in seen if self._matches(ns, op)]
        matched.sort()
        return matched[op.offset : op.offset + op.limit]

    @staticmethod
    def _matches(namespace: tuple[str, ...], op: ListNamespacesOp) -> bool:
        for condition in op.match_conditions or ():
            path = tuple(condition.path)
            if condition.match_type == "prefix":
                target = namespace[: len(path)]
            elif condition.match_type == "suffix":
                target = namespace[-len(path) :] if path else ()
            else:  # pragma: no cover - guarded by LangGraph's own validation
                raise ValueError(f"unknown match type {condition.match_type!r}")
            if len(path) > len(namespace):
                return False
            # "*" is LangGraph's single-segment wildcard.
            if any(p != "*" and p != t for p, t in zip(path, target, strict=False)):
                return False
        return True

    # ----------------------------------------------------------------------- BaseStore

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._get(op))
            elif isinstance(op, PutOp):
                self._put(op)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._list_namespaces(op))
            else:  # pragma: no cover - guarded by LangGraph's own op union
                raise NotImplementedError(f"unsupported operation {type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Async surface, off-loaded to a thread.

        pylakekeeper's transport and the storage SDKs are synchronous, so this runs the
        same code off the event loop rather than pretending to be async.
        """
        return await asyncio.to_thread(self.batch, list(ops))
