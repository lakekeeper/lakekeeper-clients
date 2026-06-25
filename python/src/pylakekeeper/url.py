"""Namespace encoding for Lakekeeper URLs.

Lakekeeper represents a multi-level namespace (e.g. ``ai.test``) on the wire by
joining the levels with the ASCII *unit separator* ``U+001F`` rather than a dot.
When a namespace appears in a URL **path** segment, that separator is
percent-encoded as ``%1F``; when it appears in a **query** parameter (such as the
``parent`` filter on list endpoints) it is sent raw and the HTTP layer encodes it.

This quirk is internal to the protocol — callers of the SDK pass and receive
ordinary level sequences (``("ai", "test")``) or dotted strings (``"ai.test"``)
and never see ``%1F``.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

#: ASCII unit separator (U+001F) — Lakekeeper's multi-level namespace join character.
NAMESPACE_SEPARATOR = "\x1f"

#: A namespace as accepted by the public API: either a dotted string or level sequence.
NamespaceLike = str | Sequence[str]


def parse_namespace(namespace: NamespaceLike) -> tuple[str, ...]:
    """Normalise a namespace into a tuple of levels.

    Accepts a level sequence (returned as-is), a raw ``U+001F``-joined string, or a
    dot-separated string. Empty levels are dropped.

    >>> parse_namespace("ai.test")
    ('ai', 'test')
    >>> parse_namespace(["ai", "test"])
    ('ai', 'test')
    >>> parse_namespace("ai\\x1ftest")
    ('ai', 'test')
    """
    if isinstance(namespace, str):
        sep = NAMESPACE_SEPARATOR if NAMESPACE_SEPARATOR in namespace else "."
        levels = namespace.split(sep)
    else:
        levels = list(namespace)
    return tuple(level for level in levels if level)


def join_namespace(namespace: NamespaceLike) -> str:
    """Join namespace levels with the raw ``U+001F`` separator (no percent-encoding).

    Use for **query parameter** values (e.g. the ``parent`` filter); the HTTP client
    is responsible for URL-encoding query strings.
    """
    return NAMESPACE_SEPARATOR.join(parse_namespace(namespace))


def encode_namespace(namespace: NamespaceLike) -> str:
    """Encode namespace levels as a single percent-encoded URL **path** segment.

    The levels are joined with ``U+001F`` and the whole string is percent-encoded,
    so the separator becomes ``%1F`` and any reserved characters in level names are
    escaped too.

    >>> encode_namespace("ai.test")
    'ai%1Ftest'
    >>> encode_namespace(["ai", "test"])
    'ai%1Ftest'
    """
    return quote(join_namespace(namespace), safe="")
