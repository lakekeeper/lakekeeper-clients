"""Generic-table format identifiers.

Lakekeeper does not maintain a fixed set of formats: the server stores the ``format`` as
a free identifier and only validates its shape (``^[a-z][a-z0-9_-]{0,63}$``). The
:class:`GenericTableFormat` constants below are therefore *conveniences* for the common
cases — you may pass any string that satisfies the shape rule.
"""

from __future__ import annotations

import re
from enum import Enum

from .errors import ConfigError

# Mirrors the server's validate_format: a lowercase letter, then [a-z0-9_-], max 64 chars.
_FORMAT_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class GenericTableFormat(str, Enum):
    """Well-known generic-table formats.

    These are a non-exhaustive convenience set — Lakekeeper accepts any identifier matching
    ``^[a-z][a-z0-9_-]{0,63}$``, so pass a plain ``str`` for anything not listed here.
    Members are plain strings (``GenericTableFormat.LANCE == "lance"``).
    """

    LANCE = "lance"
    DELTA = "delta"
    VORTEX = "vortex"
    PAIMON = "paimon"
    DATASET = "dataset"


def normalize_format(fmt: str | GenericTableFormat) -> str:
    """Return the wire string for ``fmt``, validating the server's format-shape rule.

    Accepts a :class:`GenericTableFormat` or a raw string. Validating here turns a typo
    (``"Lance"``, ``"delta lake"``) into an immediate, clear error instead of a server 400.

    Raises:
        ConfigError: if ``fmt`` cannot be a valid generic-table format.
    """
    value = fmt.value if isinstance(fmt, GenericTableFormat) else fmt
    if not isinstance(value, str) or _FORMAT_RE.fullmatch(value) is None:
        raise ConfigError(
            f"invalid generic table format {value!r}: must start with a lowercase letter, "
            "contain only lowercase letters, digits, '_' or '-', and be at most 64 characters"
        )
    return value
