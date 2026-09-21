"""Shared plumbing for the agents surface: table creation and credential freshness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from ..client import Client
from ..errors import ConflictError, LakekeeperError
from ..formats import GenericTableFormat
from ..models import LoadGenericTableResponse
from ..objects import ObjectStore
from ..url import NamespaceLike

#: Reload vended credentials this many seconds before they lapse. Generous enough that a
#: slow multi-object write started just under the wire still finishes with valid keys.
REFRESH_MARGIN_SECONDS = 120.0


class AgentsError(LakekeeperError):
    """Base class for errors raised by the agents surface."""


class EmbeddingMismatch(AgentsError):
    """The configured embedder disagrees with the model a recall table was built with.

    Vectors from different models are not comparable, so searching with the wrong one
    returns confident nonsense rather than an error. This turns that into an error.
    """


class NoEmbedder(AgentsError):
    """A vector operation was attempted on a store constructed without an embedder."""


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors, and names the model it used.

    The name travels with the function deliberately: it is recorded on the recall table
    at creation and checked on every use, so a swapped model fails loudly instead of
    silently invalidating every stored vector.
    """

    #: Model identifier, recorded on the recall table (e.g. ``"nomic-embed-text"``).
    model: str

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        """Embed ``texts``, returning one vector per input, in order."""
        ...


class VendedTable:
    """A generic table plus its vended credentials, reloaded as they approach expiry.

    Every operation goes through :meth:`resp`, which reloads only when the current
    credentials are within :data:`REFRESH_MARGIN_SECONDS` of lapsing — so a long-lived
    agent neither re-loads on every call nor writes with dead keys.
    """

    def __init__(self, client: Client, namespace: NamespaceLike, name: str) -> None:
        self._client = client
        self._namespace = namespace
        self._name = name
        self._cached: LoadGenericTableResponse | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def resp(self) -> LoadGenericTableResponse:
        """The current load response, reloading if credentials are near expiry."""
        if self._cached is None or self._cached.expires_within(REFRESH_MARGIN_SECONDS):
            self._cached = self._client.generic_tables.load(
                self._namespace, self._name, vended=True
            )
        return self._cached

    def invalidate(self) -> None:
        """Drop the cached credentials, forcing a reload on next use."""
        self._cached = None

    @property
    def location(self) -> str:
        return self.resp.location

    @property
    def properties(self) -> Mapping[str, str]:
        return self.resp.table.properties

    def objects(self) -> ObjectStore:
        return self.resp.objects()

    def ensure(
        self,
        *,
        format: str | GenericTableFormat,
        doc: str | None = None,
        properties: Mapping[str, str] | None = None,
    ) -> LoadGenericTableResponse:
        """Create the table if it does not exist, then return its loaded state.

        Idempotent: a concurrent creator racing us produces a 409, which we treat as
        success and follow with a load. Requires create permission on the namespace, so
        this is setup-time work — agents normally use tables someone else provisioned.
        """
        try:
            self._client.generic_tables.create(
                self._namespace,
                self._name,
                format=format,
                doc=doc,
                properties=dict(properties) if properties else None,
            )
        except ConflictError:
            pass
        self.invalidate()
        return self.resp
