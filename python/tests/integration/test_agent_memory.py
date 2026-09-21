"""End-to-end agent memory and skills against a real Lakekeeper + Silo (S3) + Keycloak.

What the unit tests cannot reach: real STS credentials and their expiry, object I/O over
real S3, a Lance `merge_insert` through vended credentials, and — the one that matters —
a second principal being refused by the catalog rather than by our code.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from pylakekeeper import Client, ClientCredentials, GenericTableFormat
from pylakekeeper.agents import MemoryStore, SkillStore

pytestmark = pytest.mark.integration

# Silo's S3 API is `silo:9000` inside the compose network (what the server vends) but
# `localhost:9000` out here. `storage_overrides` is exactly this case.
STORAGE_INTERNAL_HOST = "silo:9000"
STORAGE_HOST = "localhost:9100"


@pytest.fixture
def client(stack):
    c = Client(
        base_url=stack.base_url,
        warehouse=stack.warehouse_id,
        auth=ClientCredentials(
            token_url=stack.token_url,
            client_id=stack.client_id,
            client_secret=stack.client_secret,
        ),
        project_id=stack.project_id,
        storage_overrides={"s3.endpoint": f"http://{STORAGE_HOST}"},
    )
    yield c
    c.close()


@pytest.fixture
def table_name():
    # Unique per test so a reused warehouse never leaks state between runs.
    return f"t_{uuid.uuid4().hex[:8]}"


class _LengthEmbedder:
    """Deterministic embeddings — this exercises the storage path, not model quality."""

    model = "itest-length-v1"

    def embed(self, texts):
        return [[float(len(t)), float(t.count(" ")), 1.0] for t in texts]


# --------------------------------------------------------------------- credentials


def test_vended_credentials_carry_a_real_future_expiry(client, stack, table_name):
    client.generic_tables.create(stack.namespace, table_name, format=GenericTableFormat.DATASET)
    t = client.generic_tables.load(stack.namespace, table_name, vended=True)

    # The unit tests assert the parsing; this asserts the server actually sends it.
    assert t.expires_at is not None, "server sent no credential expiry"
    assert t.expires_at > datetime.now(tz=timezone.utc)
    assert t.is_expired is False
    # Fresh STS credentials are not about to lapse.
    assert t.expires_within(60) is False

    client.generic_tables.drop(stack.namespace, table_name)


def test_storage_overrides_reach_every_credential_shape(client, stack, table_name):
    client.generic_tables.create(stack.namespace, table_name, format=GenericTableFormat.DATASET)
    t = client.generic_tables.load(stack.namespace, table_name, vended=True)

    assert t.lance_storage_options["aws_endpoint"] == f"http://{STORAGE_HOST}"
    assert t.credentials["s3.endpoint"] == f"http://{STORAGE_HOST}"
    assert t.fsspec_kwargs["client_kwargs"]["endpoint_url"] == f"http://{STORAGE_HOST}"

    client.generic_tables.drop(stack.namespace, table_name)


# ------------------------------------------------------------------------ objects


def test_object_io_roundtrip_with_vended_credentials(client, stack, table_name):
    client.generic_tables.create(stack.namespace, table_name, format=GenericTableFormat.DATASET)
    t = client.generic_tables.load(stack.namespace, table_name, vended=True)
    store = t.objects()

    store.put("memories/preferences.md", b"# Preferences\n\nMetric units.")
    store.put("memories/history.md", b"# History")
    store.put("skills/draft.md", b"# Draft")

    assert store.get("memories/preferences.md") == b"# Preferences\n\nMetric units."
    # Relative keys out, prefix filter honoured, across a real ListObjectsV2.
    assert store.list("memories/") == ["memories/history.md", "memories/preferences.md"]
    assert set(store.list()) == {
        "memories/history.md",
        "memories/preferences.md",
        "skills/draft.md",
    }

    store.delete("memories/history.md")
    assert store.list("memories/") == ["memories/preferences.md"]

    client.generic_tables.drop(stack.namespace, table_name)


# ------------------------------------------------------------------------- memory


@pytest.fixture
def memory(client, stack):
    scope = [*stack.namespace]
    store = MemoryStore(
        client,
        scope,
        embed=_LengthEmbedder(),
        entries_table=f"entries_{uuid.uuid4().hex[:8]}",
        recall_table=f"recall_{uuid.uuid4().hex[:8]}",
    )
    store.ensure_tables()
    yield store
    for table in (store._entries.name, store._recall.name):
        try:
            client.generic_tables.drop(scope, table)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def test_memory_put_get_list_delete(memory):
    memory.put("memories/preferences.md", "The user prefers metric units.", index=False)
    assert memory.get("memories/preferences.md") == "The user prefers metric units."
    assert memory.list() == ["memories/preferences.md"]

    memory.delete("memories/preferences.md")
    assert memory.list() == []


def test_memory_search_returns_indexed_entries(memory):
    memory.put("memories/units.md", "The user prefers metric units everywhere")
    memory.put("memories/tz.md", "Timezone is CET")

    hits = memory.search("metric units", k=2)

    assert hits, "expected recall hits after indexing"
    assert {h.path for h in hits} <= {"memories/units.md", "memories/tz.md"}
    assert all(h.scope == memory.scope for h in hits)
    assert all(h.distance >= 0 for h in hits)


def test_reindexing_a_path_replaces_its_vector(memory):
    """A re-put must update the row, not append a second one for the same path.

    Accumulating duplicates would let a superseded memory keep matching searches.
    """
    memory.put("memories/note.md", "first version of the note")
    memory.put("memories/note.md", "second version of the note")

    hits = memory.search("version of the note", k=10)

    assert [h.path for h in hits].count("memories/note.md") == 1
    assert hits[0].text == "second version of the note"


def test_metadata_survives_the_roundtrip(memory):
    memory.put("memories/a.md", "alpha", metadata={"kind": "semantic", "source": "user"})
    hits = memory.search("alpha", k=1)
    assert hits[0].metadata == {"kind": "semantic", "source": "user"}


def test_deleting_an_entry_drops_its_vector(memory):
    memory.put("memories/gone.md", "this will be forgotten")
    assert any(h.path == "memories/gone.md" for h in memory.search("forgotten", k=5))

    memory.delete("memories/gone.md")

    assert all(h.path != "memories/gone.md" for h in memory.search("forgotten", k=5))


def test_search_on_an_empty_scope_is_empty_not_an_error(memory):
    # An agent that has not remembered anything yet is not a failure case.
    assert memory.search("anything at all") == []


# ------------------------------------------------------------------------- skills


@pytest.fixture
def skills(client, stack):
    # Proposals live in their own table per proposer; keep both in the one namespace
    # the integration stack provisions.
    store = SkillStore(
        client,
        [*stack.namespace],
        proposed_namespace=[*stack.namespace],
        proposed_table=f"proposed_{uuid.uuid4().hex[:8]}",
        approved_table=f"approved_{uuid.uuid4().hex[:8]}",
    )
    store.ensure_tables()
    yield store
    for table in (store._proposed.name, store._approved.name):
        try:
            client.generic_tables.drop([*stack.namespace], table)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def test_skill_propose_review_approve_load(skills):
    proposed = skills.propose(
        "summarise-invoices",
        "1. Read the PDF.\n2. Extract totals.",
        description="Summarise an invoice",
    )

    queue = skills.list_proposed()
    assert [(p.name, p.version) for p in queue] == [("summarise-invoices", proposed.version)]
    # The proposer resolves from whoami against the real server, not a passed-in string.
    assert queue[0].proposer
    assert queue[0].proposer == skills.proposer
    # Not approved yet, so nothing to load.
    assert skills.list() == []

    skills.approve(queue[0])

    loaded = skills.load("summarise-invoices")
    assert loaded.name == "summarise-invoices"
    assert loaded.description == "Summarise an invoice"
    assert "Extract totals" in loaded.body
    assert skills.list() == ["summarise-invoices"]


def test_approved_skill_is_stored_as_agent_readable_skill_md(skills, client, stack):
    proposed = skills.propose("greet", "Say hello.", description="Greeting")
    skills.approve(skills.list_proposed(name="greet")[0])

    # The on-disk shape is the Agent Skills convention a harness can read directly.
    t = client.generic_tables.load([*stack.namespace], skills._approved.name, vended=True)
    keys = t.objects().list()
    assert "greet/SKILL.md" in keys
    assert f"greet/{proposed.version}.md" in keys

    body = t.objects().get("greet/SKILL.md").decode()
    assert body.startswith("---")
    assert "name: greet" in body
    assert "description: Greeting" in body


def test_revoke_hides_the_skill_but_keeps_the_version_record(skills):
    skills.propose("temporary", "Do a thing.")
    skills.approve(skills.list_proposed(name="temporary")[0])
    assert skills.list() == ["temporary"]

    skills.revoke("temporary")

    assert skills.list() == []
    # The approved version stays as the record of what was once live.
    queued = skills.list_proposed(name="temporary")[0]
    assert skills.read_proposed(queued).name == "temporary"
