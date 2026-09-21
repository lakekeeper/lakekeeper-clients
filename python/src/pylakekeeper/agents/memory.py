"""Governed agent memory: markdown entries as objects, with optional vector recall.

One :class:`MemoryStore` covers one **scope** — one agent, one user, one tenant — because
the catalog's isolation boundary is the table. Two scopes that must not see each other get
two stores in two namespaces, each with its own grants; a store never spans a trust
boundary. :meth:`MemoryStore.search_many` fans out over several stores when a caller
legitimately spans more than one.

Entries are plain objects addressed by path (``memories/preferences.md``), mirroring how a
filesystem-shaped agent memory is usually written. Recall is a Lance table of embeddings
alongside them, pinned to the embedding model that built it.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..client import Client
from ..errors import NotFoundError
from ..formats import GenericTableFormat
from ..url import NamespaceLike
from ._common import Embedder, EmbeddingMismatch, NoEmbedder, VendedTable

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa

#: Table properties recording which embedder built a recall table. Generic-table
#: properties are write-once (the API has no update), which is exactly right here: the
#: model a set of vectors was built with is a fact about them, not a setting.
PROP_EMBEDDING_MODEL = "embedding-model"
PROP_EMBEDDING_DIM = "embedding-dim"

_DEFAULT_ENTRIES = "entries"
_DEFAULT_RECALL = "recall"


@dataclass(frozen=True)
class MemoryHit:
    """One result from :meth:`MemoryStore.search`."""

    path: str
    text: str
    #: Vector distance — smaller is nearer. Comparable across stores only when they share
    #: an embedding model, which :meth:`MemoryStore.search_many` enforces.
    distance: float
    metadata: dict[str, str] = field(default_factory=dict)
    #: Which store the hit came from; set when fanning out.
    scope: str | None = None


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


class MemoryStore:
    """Read and write one scope's memory through Lakekeeper-vended credentials.

    Args:
        client: a :class:`~pylakekeeper.Client` authenticated as the calling principal.
        namespace: the namespace holding this scope's tables (e.g. ``"agent_memory.agent_a"``).
        embed: an :class:`~pylakekeeper.agents.Embedder`. Required for :meth:`search` and
            for indexing on :meth:`put`; omit it for a store you only read and write by path.
        entries_table: name of the ``dataset`` table holding the entry objects.
        recall_table: name of the ``lance`` table holding embeddings.

    Every call vends credentials for the underlying table, so **authorization is enforced
    per operation**: a principal without ``read_data`` gets no keys and cannot read the
    objects, whatever this class is asked to do.
    """

    def __init__(
        self,
        client: Client,
        namespace: NamespaceLike,
        *,
        embed: Embedder | None = None,
        entries_table: str = _DEFAULT_ENTRIES,
        recall_table: str = _DEFAULT_RECALL,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._embed = embed
        self._entries = VendedTable(client, namespace, entries_table)
        self._recall = VendedTable(client, namespace, recall_table)

    @property
    def scope(self) -> str:
        """The namespace this store is scoped to, as a dotted string."""
        ns = self._namespace
        return ns if isinstance(ns, str) else ".".join(ns)

    # ------------------------------------------------------------------ provisioning

    def ensure_tables(self, *, doc: str | None = None) -> None:
        """Create the entries and recall tables if absent (setup-time; needs create rights).

        The recall table records the embedder's model name in its properties. Because
        generic-table properties cannot be updated, that pin is permanent for the life of
        the table — which is the point: see :class:`EmbeddingMismatch`.
        """
        self._entries.ensure(
            format=GenericTableFormat.DATASET,
            doc=doc or f"agent memory entries for {self.scope}",
        )
        if self._embed is not None:
            self._recall.ensure(
                format=GenericTableFormat.LANCE,
                doc=f"memory embeddings for {self.scope}",
                properties={PROP_EMBEDDING_MODEL: self._embed.model},
            )

    # ------------------------------------------------------------------ entry access

    def put(
        self,
        path: str,
        text: str,
        *,
        metadata: Mapping[str, str] | None = None,
        index: bool = True,
        index_text: str | None = None,
    ) -> None:
        """Write an entry, and index it for recall when an embedder is configured.

        Args:
            path: key relative to the scope, e.g. ``"memories/preferences.md"``.
            text: the entry body, usually markdown.
            metadata: small string tags stored alongside the vector and returned on hits.
            index: set ``False`` to store the entry without making it searchable.
            index_text: what to embed, when that differs from the stored body — a caller
                storing a JSON envelope embeds the prose inside it, not the braces.
                Hits still carry the body, so nothing downstream has to re-fetch.
        """
        self._entries.objects().put(path, text.encode("utf-8"))
        if index and self._embed is not None:
            self._index(path, text, dict(metadata or {}), index_text)

    def get(self, path: str) -> str:
        """Read one entry. Raises :class:`~pylakekeeper.NotFoundError` if absent."""
        return self._entries.objects().get(path).decode("utf-8")

    def list(self, prefix: str = "") -> builtins.list[str]:
        """List entry paths under ``prefix``, relative to the scope."""
        return self._entries.objects().list(prefix)

    def delete(self, path: str) -> None:
        """Delete an entry and drop its vector, if one was indexed."""
        self._entries.objects().delete(path)
        if self._embed is not None:
            self._forget_vector(path)

    # ------------------------------------------------------------------------ recall

    def search(self, query: str, k: int = 5) -> builtins.list[MemoryHit]:
        """Return the ``k`` nearest entries to ``query`` by vector distance.

        Returns an empty list when the scope has no recall table or no vectors yet —
        an agent with nothing remembered is not an error. Authorization failures are
        *not* swallowed: a principal that may not read this scope raises.
        """
        embedder = self._require_embedder()
        try:
            dataset = self._open_recall()
        except NotFoundError:
            return []
        if dataset is None:
            return []

        vector = list(embedder.embed([query])[0])
        table = dataset.to_table(
            nearest={"column": "vector", "q": vector, "k": k},
            columns=["path", "text", "metadata", "_distance"],
        )
        return [
            MemoryHit(
                path=row["path"],
                text=row["text"],
                distance=float(row["_distance"]),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                scope=self.scope,
            )
            for row in table.to_pylist()
        ]

    @staticmethod
    def search_many(
        stores: Iterable[MemoryStore], query: str, k: int = 5
    ) -> builtins.list[MemoryHit]:
        """Search several scopes and merge by distance, skipping the ones denied to us.

        There is no central recall table — the Lance table is the unit of access, so it
        must equal the unit of trust. A caller that legitimately spans scopes (its own
        plus a shared tier) searches each one.

        The fan-out is **self-governing**: a scope this principal may not read vends no
        credentials and drops out, so the caller never needs to know its own grants — the
        catalog answers by refusing.
        """
        hits: builtins.list[MemoryHit] = []
        for store in stores:
            try:
                hits.extend(store.search(query, k))
            except NotFoundError:
                # Lakekeeper hides a table you may not read as a 404: a denial, not an
                # outage. Anything else (connectivity, storage) propagates.
                continue
        hits.sort(key=lambda hit: hit.distance)
        return hits[:k]

    # ------------------------------------------------------------------------ private

    def _require_embedder(self) -> Embedder:
        if self._embed is None:
            raise NoEmbedder("this MemoryStore has no embedder; pass embed=... to search or index")
        return self._embed

    def _check_pin(self) -> None:
        """Fail loudly if the recall table was built with a different embedding model."""
        embedder = self._require_embedder()
        recorded = self._recall.properties.get(PROP_EMBEDDING_MODEL)
        if recorded and recorded != embedder.model:
            raise EmbeddingMismatch(
                f"recall table {self.scope}.{self._recall.name} was built with "
                f"{recorded!r}, but this store embeds with {embedder.model!r}. "
                "Vectors from different models are not comparable — rebuild the table "
                "or use the original model."
            )

    def _open_recall(self) -> Any:
        """Open the recall Lance dataset, or return None if it has no data yet."""
        import lance  # noqa: PLC0415

        self._check_pin()
        resp = self._recall.resp
        try:
            return lance.dataset(resp.location, storage_options=resp.lance_storage_options)
        except ValueError:
            # Lance raises when the location holds no dataset — nothing indexed yet.
            return None

    def _arrow_batch(self, rows: builtins.list[dict[str, Any]], dim: int) -> pa.Table:
        import pyarrow as pa  # noqa: PLC0415

        schema = pa.schema(
            [
                pa.field("path", pa.string()),
                pa.field("text", pa.string()),
                pa.field("metadata", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
            ]
        )
        return pa.Table.from_pylist(rows, schema=schema)

    def _index(
        self,
        path: str,
        text: str,
        metadata: dict[str, str],
        index_text: str | None = None,
    ) -> None:
        import lance  # noqa: PLC0415

        embedder = self._require_embedder()
        self._check_pin()
        vector = [float(v) for v in embedder.embed([index_text or text])[0]]
        batch = self._arrow_batch(
            [
                {
                    "path": path,
                    "text": text,
                    "metadata": json.dumps(metadata, sort_keys=True),
                    "vector": vector,
                }
            ],
            len(vector),
        )

        resp = self._recall.resp
        options = resp.lance_storage_options
        try:
            dataset = lance.dataset(resp.location, storage_options=options)
        except ValueError:
            lance.write_dataset(batch, resp.location, storage_options=options, mode="create")
            return
        # One row per path: re-putting an entry replaces its vector rather than
        # accumulating stale duplicates that would each match a later search.
        (
            dataset.merge_insert("path")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(batch)
        )

    def _forget_vector(self, path: str) -> None:
        try:
            dataset = self._open_recall()
        except NotFoundError:
            return
        if dataset is None:
            return
        dataset.delete(f"path = '{_escape_sql_literal(path)}'")
