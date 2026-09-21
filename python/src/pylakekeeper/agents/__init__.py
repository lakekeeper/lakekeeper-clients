"""Governed agent memory and skills on top of Lakekeeper generic tables.

    pip install 'pylakekeeper[agents]'

.. warning::
   **Preview.** This surface may change in a backward-incompatible way while agent
   conventions settle. The rest of ``pylakekeeper`` is not affected.

Two stores, both enforcing access at Lakekeeper's credential-vending layer rather than in
application code::

    from pylakekeeper import Client, ClientCredentials
    from pylakekeeper.agents import MemoryStore, SkillStore

    with Client(base_url=..., warehouse=..., auth=ClientCredentials(...)) as client:
        memory = MemoryStore(client, "agent_memory.agent_a", embed=my_embedder)
        memory.put("memories/preferences.md", "The user prefers metric units.")
        hits = memory.search("what units does the user want?")

        skills = SkillStore(client, "skills")
        for skill in skills.load_all():        # only what this principal may read
            ...
        skills.propose("summarise-invoices", body)   # filed for human review

Scope: objects and vectors — the two things a client with no ``pyiceberg`` dependency can
own. An Iceberg index for time travel over memory history belongs to the caller.
"""

from __future__ import annotations

from ._common import AgentsError, Embedder, EmbeddingMismatch, NoEmbedder
from .memory import (
    PROP_EMBEDDING_DIM,
    PROP_EMBEDDING_MODEL,
    MemoryHit,
    MemoryStore,
)
from .skills import (
    ProposedSkill,
    Skill,
    SkillNotFound,
    SkillStore,
    format_frontmatter,
    parse_frontmatter,
    path_safe,
    version_of,
)

__all__ = [
    # memory
    "MemoryStore",
    "MemoryHit",
    "PROP_EMBEDDING_MODEL",
    "PROP_EMBEDDING_DIM",
    # skills
    "SkillStore",
    "Skill",
    "ProposedSkill",
    "SkillNotFound",
    "path_safe",
    "parse_frontmatter",
    "format_frontmatter",
    "version_of",
    # shared
    "Embedder",
    "AgentsError",
    "EmbeddingMismatch",
    "NoEmbedder",
]
