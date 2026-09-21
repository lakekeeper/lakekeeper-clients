"""Governed agent skills: propose, review, approve — with the catalog holding the line.

Skills live under deliberately asymmetric grants:

``<namespace>.proposed`` — one table per proposer
    Each agent holds ``modify`` on *its own* proposal table and nothing on anyone else's,
    so it files its own drafts and cannot see another agent's. The reviewer holds
    ``select`` on the parent namespace and sees every queue.

``<namespace>.approved`` — one shared library
    Agents hold ``select``: they load approved skills and **cannot write here**.
    :meth:`SkillStore.approve` vends no write credentials for an agent principal, so
    promotion is refused in storage rather than in this code.

That last asymmetry is the whole point: an agent cannot approve its own skill, and the
control is not a check in the application that a prompt could talk its way past.

.. note::
   The asymmetry only runs one way. In the OpenFGA model ``select`` is implied by
   ``modify`` (``define select: ... or modify ...``), so **read can be granted without
   write, but write cannot be granted without read**. A single shared proposal table
   would therefore be readable by every agent that could write to it — hence one table
   per proposer, which also makes attribution structural: an agent has no credentials
   for a table filed under someone else's name.

Bodies are stored as ``SKILL.md`` with YAML-ish frontmatter — the Agent Skills convention
harnesses already read — and versioned by content hash, so approving a skill never
overwrites the version someone already reviewed.

The proposer defaults to the authenticated principal's own id, so a caller cannot file
under someone else's name — and because the proposal table *is* named for the proposer,
the catalog enforces that rather than trusting it.
"""

from __future__ import annotations

import builtins
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..client import Client
from ..formats import GenericTableFormat
from ..url import NamespaceLike
from ._common import AgentsError, VendedTable

_DEFAULT_PROPOSED = "proposed"
_DEFAULT_APPROVED = "approved"
_DEFAULT_REJECTED = "rejected"
_SKILL_FILE = "SKILL.md"

#: Principal ids carry provider prefixes (``oidc~service-account-analyst``); keep the
#: readable characters and flatten the rest so the id survives as a path segment. The
#: authoritative value is recorded in the skill's frontmatter, not derived from the path.
_UNSAFE_IN_PATH = re.compile(r"[^A-Za-z0-9._~@-]")


def path_safe(principal: str) -> str:
    """A principal id reduced to something usable as a path segment or table name."""
    return _UNSAFE_IN_PATH.sub("_", principal) or "unknown"


def _ns_text(namespace: NamespaceLike) -> str:
    return namespace if isinstance(namespace, str) else ".".join(namespace)


class SkillNotFound(AgentsError):
    """No such skill in the table being read."""


@dataclass(frozen=True)
class ProposedSkill:
    """One version of one skill, awaiting review, and who filed it."""

    proposer: str
    name: str
    version: str


@dataclass(frozen=True)
class Skill:
    """A skill document and its identity."""

    name: str
    body: str
    #: First 12 hex characters of the body's SHA-256 — the version identifier.
    version: str
    description: str | None = None
    #: The principal that filed it, when known.
    proposer: str | None = None

    @property
    def frontmatter(self) -> dict[str, str]:
        return parse_frontmatter(self.body)[0]


def version_of(body: str) -> str:
    """Content hash identifying a skill body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def parse_frontmatter(body: str) -> tuple[dict[str, str], str]:
    """Split ``---`` delimited leading frontmatter from the document.

    Deliberately not a YAML parser — the client core depends only on ``httpx`` and
    ``pydantic``, and skill frontmatter in practice is flat ``key: value`` lines. Values
    keep their surrounding quotes stripped; anything more structured is left in the body.

    Returns ``({}, body)`` when there is no frontmatter block.
    """
    if not body.startswith("---"):
        return {}, body
    lines = body.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, body

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def format_frontmatter(
    name: str,
    description: str | None,
    body: str,
    *,
    proposer: str | None = None,
) -> str:
    """Prepend a frontmatter block, unless the body already carries one."""
    if body.startswith("---"):
        return body
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if proposer:
        lines.append(f"proposed-by: {proposer}")
    lines += ["---", "", body.lstrip("\n")]
    return "\n".join(lines)


class SkillStore:
    """Propose, review and load skills across the proposed/approved boundary.

    Args:
        client: a :class:`~pylakekeeper.Client` authenticated as the calling principal.
        namespace: the namespace holding both tables (e.g. ``"skills"``).

    Which methods succeed depends entirely on the caller's grants, which is the intended
    demonstration: the same code run by an agent and by a reviewer behaves differently
    because Lakekeeper vends them different credentials.
    """

    def __init__(
        self,
        client: Client,
        namespace: NamespaceLike = "skills",
        *,
        proposed_namespace: NamespaceLike | None = None,
        proposed_table: str | None = None,
        approved_table: str = _DEFAULT_APPROVED,
        rejected_table: str = _DEFAULT_REJECTED,
        proposer: str | None = None,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._proposer = proposer
        self._proposed_namespace = proposed_namespace or (
            f"{_ns_text(namespace)}.{_DEFAULT_PROPOSED}"
        )
        self._proposed_table_override = proposed_table
        self._proposed_cache: VendedTable | None = None
        self._approved = VendedTable(client, namespace, approved_table)
        self._rejected = VendedTable(client, namespace, rejected_table)

    @property
    def _proposed(self) -> VendedTable:
        """This proposer's own queue: one table per proposer, named for them."""
        name = self._proposed_table_override or path_safe(self.proposer)
        if self._proposed_cache is None or self._proposed_cache.name != name:
            self._proposed_cache = VendedTable(self._client, self._proposed_namespace, name)
        return self._proposed_cache

    def _queue_for(self, proposer: str) -> VendedTable:
        """The proposal table of ``proposer`` — our own when it is us.

        A reviewer reaching another agent's queue needs ``select`` on the proposals
        namespace; without it the load returns 404, which is the denial.
        """
        if proposer == self.proposer:
            return self._proposed
        return VendedTable(self._client, self._proposed_namespace, path_safe(proposer))

    @property
    def proposer(self) -> str:
        """Who this store files proposals as — the authenticated principal by default.

        Resolved once from ``whoami`` unless supplied explicitly. Defaulting to the
        server's view of the caller is deliberate: an attribution a caller can set to
        any string is not attribution.
        """
        if self._proposer is None:
            who = self._client.whoami()
            self._proposer = str(who.get("id") or who.get("name") or "unknown")
        return self._proposer

    def ensure_tables(self) -> None:
        """Create both tables if absent (setup-time; needs create rights)."""
        self._proposed.ensure(
            format=GenericTableFormat.DATASET,
            doc=f"skills proposed by {self.proposer}, awaiting review",
        )
        self._approved.ensure(
            format=GenericTableFormat.DATASET,
            doc="approved skills — agents read, reviewers write",
        )
        self._rejected.ensure(
            format=GenericTableFormat.DATASET,
            doc="rejected proposals and why — agents read, reviewers write",
        )

    # ----------------------------------------------------------------- agent-facing

    def propose(self, name: str, body: str, *, description: str | None = None) -> Skill:
        """File a skill for review. Needs ``write_data`` on ``proposed``.

        Filed under this store's :attr:`proposer` and versioned by content hash, so
        proposing twice is idempotent, and two agents proposing the same skill name do
        not collide or overwrite each other.
        """
        proposer = self.proposer
        document = format_frontmatter(name, description, body, proposer=proposer)
        version = version_of(document)
        self._proposed.objects().put(
            f"{name}/{version}.md",
            document.encode("utf-8"),
            content_type="text/markdown",
        )
        return Skill(
            name=name,
            body=document,
            version=version,
            description=description,
            proposer=proposer,
        )

    def load(self, name: str) -> Skill:
        """Load an approved skill. Needs ``read_data`` on ``approved``."""
        try:
            raw = self._approved.objects().get(f"{name}/{_SKILL_FILE}")
        except Exception as exc:  # storage-level miss; surfaced as a clear domain error
            raise SkillNotFound(f"no approved skill {name!r} in {self._ns_text}") from exc
        document = raw.decode("utf-8")
        meta, _ = parse_frontmatter(document)
        return Skill(
            name=meta.get("name", name),
            body=document,
            version=version_of(document),
            description=meta.get("description"),
            proposer=meta.get("proposed-by"),
        )

    def list(self) -> builtins.list[str]:
        """Names of approved skills. Needs ``read_data`` on ``approved``."""
        names = {
            key.split("/", 1)[0]
            for key in self._approved.objects().list()
            if key.endswith(f"/{_SKILL_FILE}")
        }
        return sorted(names)

    def load_all(self) -> builtins.list[Skill]:
        """Every approved skill this principal may read."""
        return [self.load(name) for name in self.list()]

    # -------------------------------------------------------------- reviewer-facing

    def list_proposed(
        self, *, name: str | None = None, proposer: str | None = None
    ) -> builtins.list[ProposedSkill]:
        """This queue's pending versions. Needs ``select`` on the proposal table.

        An agent sees only its own queue, because its grant covers only its own table.
        A reviewer holding ``select`` on the proposals namespace reads any of them by
        passing ``proposer=``.
        """
        filed_by = proposer or self.proposer
        queue = self._queue_for(filed_by)
        found: builtins.list[ProposedSkill] = []
        for key in queue.objects().list(f"{name}/" if name else ""):
            parts = key.split("/")
            if len(parts) != 2 or not parts[1].endswith(".md"):
                continue
            skill_name, filename = parts
            found.append(
                ProposedSkill(proposer=filed_by, name=skill_name, version=filename[: -len(".md")])
            )
        return sorted(found, key=lambda p: (p.proposer, p.name, p.version))

    def read_proposed(self, proposed: ProposedSkill) -> Skill:
        """Read one proposed version for review. Needs ``read_data`` on ``proposed``."""
        queue = self._queue_for(proposed.proposer)
        key = f"{proposed.name}/{proposed.version}.md"
        document = queue.objects().get(key).decode("utf-8")
        meta, _ = parse_frontmatter(document)
        return Skill(
            name=meta.get("name", proposed.name),
            body=document,
            version=version_of(document),
            description=meta.get("description"),
            proposer=meta.get("proposed-by", proposed.proposer),
        )

    def approve(self, proposed: ProposedSkill) -> Skill:
        """Promote a proposed version to ``approved``. Needs ``write_data`` on ``approved``.

        **An agent principal cannot do this.** Lakekeeper vends it no write credentials
        for the approved table, so the write is refused in storage — running this exact
        method as an agent fails, and that failure is the governance boundary.

        The promoted version is kept alongside ``SKILL.md`` under its content hash, so the
        approval history is the object listing.
        """
        skill = self.read_proposed(proposed)
        payload = skill.body.encode("utf-8")
        objects = self._approved.objects()
        objects.put(f"{skill.name}/{proposed.version}.md", payload, content_type="text/markdown")
        objects.put(f"{skill.name}/{_SKILL_FILE}", payload, content_type="text/markdown")
        return skill

    def reject(self, proposed: ProposedSkill, reason: str) -> Skill:
        """Record a refusal. Needs ``write_data`` on ``rejected``.

        Declining to approve would already keep a skill unloadable, but silence is a poor
        review outcome: the same proposal returns tomorrow, and nobody can show that a
        person looked. This writes the decision where the proposer can read it — closing
        the loop for an agent that might otherwise re-file the same thing — while leaving
        the proposal itself untouched as the record of what was asked.

        Like :meth:`approve`, an agent principal cannot do this.
        """
        skill = self.read_proposed(proposed)
        note = format_frontmatter(
            skill.name,
            skill.description,
            f"REJECTED by {self.proposer}\n\nReason: {reason}\n\n---\n\n{skill.body}",
            proposer=skill.proposer,
        )
        self._rejected.objects().put(
            f"{skill.name}/{proposed.version}.md",
            note.encode("utf-8"),
            content_type="text/markdown",
        )
        return skill

    def list_rejected(self) -> builtins.list[tuple[str, str]]:
        """``(name, version)`` of refused proposals. Needs ``read_data`` on ``rejected``."""
        found: builtins.list[tuple[str, str]] = []
        for key in self._rejected.objects().list():
            parts = key.split("/")
            if len(parts) == 2 and parts[1].endswith(".md"):
                found.append((parts[0], parts[1][: -len(".md")]))
        return sorted(found)

    def revoke(self, name: str) -> None:
        """Withdraw an approved skill. Needs ``write_data`` on ``approved``.

        Removes the ``SKILL.md`` pointer so agents stop loading it; the approved versions
        stay as the record of what was once live.
        """
        self._approved.objects().delete(f"{name}/{_SKILL_FILE}")

    @property
    def _ns_text(self) -> str:
        ns = self._namespace
        return ns if isinstance(ns, str) else ".".join(ns)


def skill_properties(skill: Skill) -> Mapping[str, str]:
    """The identity of a skill, for recording on a catalog object."""
    props = {"skill-name": skill.name, "skill-version": skill.version}
    if skill.description:
        props["skill-description"] = skill.description
    return props
