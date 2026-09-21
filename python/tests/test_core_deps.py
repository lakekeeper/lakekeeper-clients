"""The core install must stay `httpx` + `pydantic`.

Every optional storage and table SDK is imported lazily, so adding a module-level
``import pyiceberg`` (or boto3, lance, ...) anywhere on the main import path would break
plain ``pip install pylakekeeper`` for everyone. That regression is silent in a dev
environment where the extras happen to be installed — which is every dev environment —
so it is pinned here instead.
"""

from __future__ import annotations

import sys
from importlib.abc import MetaPathFinder

import pytest

#: Distributions that must never be needed to import the package or use its core surface.
OPTIONAL_DISTRIBUTIONS = ["pyiceberg", "boto3", "botocore", "lance", "pyarrow"]


class _Blocker(MetaPathFinder):
    """Make the named top-level packages behave as if they were not installed."""

    def __init__(self, blocked: list[str]) -> None:
        self._blocked = tuple(blocked)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        if fullname.startswith(self._blocked):
            raise ImportError(f"blocked for test: {fullname}")
        return None


@pytest.fixture
def without_optional_deps():
    """Run the body as though no optional dependency were installed.

    Restores ``sys.modules`` exactly, including any ``pylakekeeper`` modules re-imported
    under the block: leaving a second copy behind would give the rest of the suite a
    module whose state (e.g. ``iceberg._AUTH_REGISTRY``) diverges from the one under test.
    """
    watched = tuple(OPTIONAL_DISTRIBUTIONS) + ("pylakekeeper",)
    blocker = _Blocker(OPTIONAL_DISTRIBUTIONS)
    saved = {name: module for name, module in sys.modules.items() if name.startswith(watched)}
    # Evict pylakekeeper too, not just the optional distributions: a lazy
    # `from .iceberg import ...` inside an already-imported module would otherwise be
    # served from cache and never reach the blocker, making the test pass or fail
    # depending on what ran before it.
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        for name in [n for n in sys.modules if n.startswith(watched)]:
            del sys.modules[name]
        sys.modules.update(saved)


def test_package_imports_without_any_optional_dependency(without_optional_deps):
    import pylakekeeper  # noqa: PLC0415
    import pylakekeeper.agents  # noqa: PLC0415

    assert pylakekeeper.Client is not None
    assert pylakekeeper.agents.MemoryStore is not None


def test_core_surface_works_without_optional_dependencies(without_optional_deps, httpx_mock):
    from pylakekeeper import Client, StaticToken  # noqa: PLC0415
    from pylakekeeper.agents import MemoryStore, SkillStore  # noqa: PLC0415

    httpx_mock.add_response(
        json={
            "table": {
                "name": "t",
                "format": "dataset",
                "base-location": "s3://b/p",
                "protected": False,
            }
        }
    )
    with Client("http://lk.example.com", "wh", StaticToken("t")) as client:
        # Catalog metadata needs nothing but httpx + pydantic.
        assert client.generic_tables.load("ns", "t").location == "s3://b/p"
        # Constructing the agent stores must not reach for a storage or table SDK.
        MemoryStore(client, "agent_memory.agent_a")
        SkillStore(client, "skills")


def test_iceberg_catalog_fails_only_when_called(without_optional_deps):
    from pylakekeeper import Client, StaticToken  # noqa: PLC0415

    with Client("http://lk.example.com", "wh", StaticToken("t")) as client:
        with pytest.raises(ImportError, match="pyiceberg"):
            client.iceberg_catalog()


def test_object_io_asks_for_the_right_extra(without_optional_deps):
    from pylakekeeper import ConfigError  # noqa: PLC0415
    from pylakekeeper.objects import object_store_for  # noqa: PLC0415

    with pytest.raises(ConfigError) as exc:
        object_store_for("s3://bucket/prefix", {})
    # The error has to name the fix, not just the missing module.
    assert "pylakekeeper[s3]" in str(exc.value)
    assert "boto3" in str(exc.value)
