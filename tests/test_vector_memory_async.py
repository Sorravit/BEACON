#!/usr/bin/env python3
"""
tests/test_vector_memory_async.py
==================================
Unit tests for the async-fixed ``core/vector_memory.py``.

All Weaviate network I/O and sentence-transformers CPU work are mocked
so tests run offline with no Weaviate instance or GPU.

KEY CHANGE from original tests (test_agent_memory.py)
------------------------------------------------------
* Patch target: ``core.vector_memory.weaviate.use_async_with_local``
  (async factory) instead of the removed ``weaviate.connect_to_local``.
* ``client.connect`` and ``client.close`` are ``AsyncMock``.
* ``collections.list_all``, ``collections.exists``, ``collections.create``,
  ``collections.delete``, ``collections.get`` are all ``AsyncMock``.
* ``col.config.get``, ``col.data.insert``, ``col.data.update``,
  ``col.data.delete_by_id``, ``col.query.near_vector``,
  ``col.query.fetch_objects`` are all ``AsyncMock``.
* ``_embed`` is patched with an ``AsyncMock`` so sentence-transformers
  never loads and asyncio.to_thread is never invoked.
* ``close()`` is now ``async def`` - tests await it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch target
# ---------------------------------------------------------------------------

_PATCH = "core.vector_memory.weaviate.use_async_with_local"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_obj(topic="apple", fact="fruit", stored_at="2024-01-01",
              tool_name=None, query=None, result=None,
              uuid="aaaa-0001") -> MagicMock:
    obj = MagicMock()
    props: dict = {"topic": topic, "fact": fact, "stored_at": stored_at}
    if tool_name is not None:
        props = {"tool_name": tool_name, "query": query or "",
                 "result": result or "", "stored_at": stored_at}
    obj.properties = props
    obj.uuid = uuid
    return obj


def _mock_result(objects=None) -> MagicMock:
    r = MagicMock()
    r.objects = objects or []
    return r


def _mock_collection(
    insert_return="new-uuid",
    near_vector_objects=None,
    fetch_objects_result=None,
) -> MagicMock:
    col = MagicMock()
    col.config.get = AsyncMock(return_value=MagicMock(vectorizer_config="none"))
    col.data.insert = AsyncMock(return_value=insert_return)
    col.data.update = AsyncMock(return_value=None)
    col.data.delete_by_id = AsyncMock(return_value=None)
    col.query.near_vector = AsyncMock(
        return_value=_mock_result(near_vector_objects)
    )
    col.query.fetch_objects = AsyncMock(
        return_value=fetch_objects_result or _mock_result()
    )
    return col


def _mock_client(col: MagicMock | None = None, exists_return=True) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock(return_value=None)
    client.close = AsyncMock(return_value=None)
    client.is_ready = AsyncMock(return_value=True)
    # list_all returns a dict-like; we only need keys()
    list_all_mock = MagicMock()
    list_all_mock.__iter__ = MagicMock(return_value=iter([]))
    client.collections.list_all = AsyncMock(return_value=list_all_mock)
    client.collections.exists = AsyncMock(return_value=exists_return)
    client.collections.create = AsyncMock(return_value=None)
    client.collections.delete = AsyncMock(return_value=None)
    client.collections.get = AsyncMock(return_value=col or _mock_collection())
    return client


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_connects_client():
    """initialize() awaits client.connect() exactly once."""
    mock_client = _mock_client()

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        # Stub out encoder loading so no real download happens
        vm._encoder = MagicMock()
        await vm.initialize()

    mock_client.connect.assert_awaited_once()
    assert vm._ready is True


@pytest.mark.asyncio
async def test_initialize_failure_marks_not_ready():
    """initialize() leaves _ready=False when connect raises."""
    mock_client = _mock_client()
    mock_client.connect = AsyncMock(side_effect=Exception("Connection refused"))

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()
        result = await vm.initialize()

    assert result is False
    assert vm._ready is False


# ---------------------------------------------------------------------------
# store_personal_fact()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_personal_fact_inserts_with_vector():
    """store_personal_fact awaits collection.data.insert with vector."""
    col = _mock_collection(insert_return="uuid-fact-1")
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()
        vm._ready = True
        vm._client = mock_client
        # Patch _embed to return a fixed vector without real CPU work
        vm._embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        # Patch gRPC check so we don't need a real socket
        vm._is_grpc_reachable = MagicMock(return_value=True)

        result = await vm.store_personal_fact("oven", "Samsung NV75K5571RS")

    assert result is True
    col.data.insert.assert_awaited_once()
    call_kw = col.data.insert.call_args.kwargs
    assert call_kw["vector"] == [0.1, 0.2, 0.3]
    assert call_kw["properties"]["topic"] == "oven"
    assert call_kw["properties"]["fact"] == "Samsung NV75K5571RS"


@pytest.mark.asyncio
async def test_store_personal_fact_returns_none_when_unreachable():
    """store_personal_fact returns None when gRPC is unreachable."""
    mock_client = _mock_client()

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()
        vm._ready = True
        vm._client = mock_client
        vm._is_grpc_reachable = MagicMock(return_value=False)

        result = await vm.store_personal_fact("topic", "fact")

    assert result is None


# ---------------------------------------------------------------------------
# search_personal_facts()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_personal_facts_returns_mapped_list():
    """search_personal_facts awaits near_vector and maps results."""
    obj = _mock_obj(topic="oven", fact="Samsung NV75K5571RS")
    col = _mock_collection(near_vector_objects=[obj])
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        vm._is_grpc_reachable = MagicMock(return_value=True)

        results = await vm.search_personal_facts("my oven")

    assert len(results) == 1
    assert results[0]["topic"] == "oven"
    col.query.near_vector.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_personal_facts_returns_empty_list_on_embed_failure():
    """search_personal_facts returns [] when embedding fails."""
    mock_client = _mock_client()

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=None)   # embed failure
        vm._is_grpc_reachable = MagicMock(return_value=True)

        results = await vm.search_personal_facts("anything")

    assert results == []


# ---------------------------------------------------------------------------
# get_all_personal_facts()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_all_personal_facts_awaits_fetch_objects():
    """get_all_personal_facts awaits collection.query.fetch_objects."""
    obj = _mock_obj(topic="coffee", fact="I drink 2 cups a day")
    col = _mock_collection(fetch_objects_result=_mock_result([obj]))
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._is_grpc_reachable = MagicMock(return_value=True)

        results = await vm.get_all_personal_facts()

    assert len(results) == 1
    assert results[0]["topic"] == "coffee"
    col.query.fetch_objects.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete_personal_facts()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_personal_facts_deletes_matching_entries():
    """delete_personal_facts awaits delete_by_id for each matching entry."""
    obj1 = _mock_obj(topic="python", fact="I code in python", uuid="u1")
    obj2 = _mock_obj(topic="java", fact="also java", uuid="u2")
    col = _mock_collection(
        fetch_objects_result=_mock_result([obj1, obj2])
    )
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._is_grpc_reachable = MagicMock(return_value=True)

        count = await vm.delete_personal_facts("python")

    # Only obj1 matches keyword "python"
    assert count == 1
    col.data.delete_by_id.assert_awaited_once_with("u1")


# ---------------------------------------------------------------------------
# store_research()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_research_inserts_with_vector():
    """store_research awaits collection.data.insert."""
    col = _mock_collection(insert_return="uuid-research-1")
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=[0.5, 0.6])
        vm._is_grpc_reachable = MagicMock(return_value=True)

        result = await vm.store_research("web_search", "AI news", "OpenAI released...")

    assert result is True
    col.data.insert.assert_awaited_once()


# ---------------------------------------------------------------------------
# store_auto_fact() — update path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_auto_fact_updates_existing_topic():
    """store_auto_fact awaits data.update when topic already exists."""
    existing_obj = _mock_obj(topic="name", fact="old name", uuid="uuid-existing")
    col = _mock_collection(
        fetch_objects_result=_mock_result([existing_obj])
    )
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=[0.1])
        vm._is_grpc_reachable = MagicMock(return_value=True)

        result = await vm.store_auto_fact("name", "new name")

    assert result is True
    col.data.update.assert_awaited_once()
    # Should NOT insert a new record — update only
    col.data.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_auto_fact_inserts_new_topic():
    """store_auto_fact awaits data.insert when topic is new."""
    col = _mock_collection()  # fetch_objects returns empty list
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=[0.1, 0.2])
        vm._is_grpc_reachable = MagicMock(return_value=True)

        result = await vm.store_auto_fact("hobby", "rock climbing")

    assert result is True
    col.data.insert.assert_awaited_once()
    col.data.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_awaits_client_close():
    """close() is async and awaits the underlying client disconnect."""
    mock_client = _mock_client()

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()
        vm._ready = True
        vm._client = mock_client

        import inspect
        assert inspect.iscoroutinefunction(vm.close), \
            "close() must be async def — was def close() (sync) in original"

        await vm.close()

    mock_client.close.assert_awaited_once()
    assert vm._ready is False
    assert vm._client is None


# ---------------------------------------------------------------------------
# ensure_ready() — lazy reconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_ready_reconnects_when_not_ready():
    """ensure_ready() calls initialize() when _ready is False."""
    mock_client = _mock_client()

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._encoder = MagicMock()  # skip real model load
        assert vm._ready is False

        result = await vm.ensure_ready()

    assert result is True
    mock_client.connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Concurrency smoke-test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_store_does_not_deadlock():
    """Two concurrent store_personal_fact calls complete without blocking."""
    col = _mock_collection(insert_return="uuid-concurrent")
    mock_client = _mock_client(col=col)

    with patch(_PATCH, return_value=mock_client):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm._ready = True
        vm._client = mock_client
        vm._embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        vm._is_grpc_reachable = MagicMock(return_value=True)

        r1, r2 = await asyncio.gather(
            vm.store_personal_fact("lang", "Python"),
            vm.store_personal_fact("os", "macOS"),
        )

    assert r1 is True
    assert r2 is True
    assert col.data.insert.await_count == 2


# ---------------------------------------------------------------------------
# build_context_prompt()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_context_prompt_includes_facts_section():
    """build_context_prompt includes personal facts when they exist."""
    with patch(_PATCH, return_value=_mock_client()):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        # Stub all three search methods directly
        vm.search_personal_facts = AsyncMock(
            return_value=[{"topic": "name", "fact": "Alice"}]
        )
        vm.search_auto_facts = AsyncMock(return_value=[])
        vm.search_research = AsyncMock(return_value=[])

        prompt = await vm.build_context_prompt("who am I?")

    assert "MEMORY CONTEXT" in prompt
    assert "name" in prompt
    assert "Alice" in prompt


@pytest.mark.asyncio
async def test_build_context_prompt_empty_when_no_memories():
    """build_context_prompt returns empty string when all searches empty."""
    with patch(_PATCH, return_value=_mock_client()):
        import importlib
        import core.vector_memory as mod
        importlib.reload(mod)

        vm = mod.VectorMemory()
        vm.search_personal_facts = AsyncMock(return_value=[])
        vm.search_auto_facts = AsyncMock(return_value=[])
        vm.search_research = AsyncMock(return_value=[])

        prompt = await vm.build_context_prompt("hello")

    assert prompt == ""
