"""Unit tests for the retrieval ranking layer.

Covers two things, neither of which needs Postgres or the network:
  1. The pure cosine_similarity math.
  2. The retrieve() fail-open contract — embed failures AND store failures must
     both degrade (return ([], True)) rather than propagate, and a clean path
     must map store rows to RetrievedTurn and report degraded=False.
"""

from __future__ import annotations

import asyncio

import pytest

import retrieval
from client.infra import InfraEmbedError
from schemas import RetrievedTurn


# --------------------------------------------------------------------------- #
# cosine_similarity                                                            #
# --------------------------------------------------------------------------- #


def test_cosine_identical_vectors_is_one():
    assert retrieval.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert retrieval.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_no_divide_by_zero():
    # Both the zero/zero and zero/nonzero cases must short-circuit to 0.0.
    assert retrieval.cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert retrieval.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_opposite_vectors_is_minus_one():
    assert retrieval.cosine_similarity([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# Fakes for retrieve()                                                         #
# --------------------------------------------------------------------------- #


class FakeInfra:
    """Stands in for InfraClient. embed_one returns a canned vector or raises."""

    def __init__(self, vector=None, error: Exception | None = None):
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self._error = error
        self.calls: list[str] = []

    async def embed_one(self, text: str):
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return self._vector


class FakeStore:
    """Stands in for Store. search returns canned rows or raises."""

    def __init__(self, rows=None, error: Exception | None = None):
        self._rows = rows if rows is not None else []
        self._error = error
        self.calls: list[tuple] = []

    async def search(self, session_id, query_embedding, top_k, min_score):
        self.calls.append((session_id, query_embedding, top_k, min_score))
        if self._error is not None:
            raise self._error
        return self._rows


# --------------------------------------------------------------------------- #
# retrieve() fail-open contract                                               #
# --------------------------------------------------------------------------- #


def test_retrieve_embed_failure_fails_open():
    infra = FakeInfra(error=InfraEmbedError("embedder cold"))
    store = FakeStore(rows=[{"id": "x"}])  # should never be reached

    result, degraded = asyncio.run(
        retrieval.retrieve(infra, store, "sess-1", "hello", top_k=5, min_score=0.2)
    )

    assert result == []
    assert degraded is True
    # The store must not be touched once embedding fails.
    assert store.calls == []


def test_retrieve_store_failure_fails_open():
    """The just-fixed behavior: a store.search exception must degrade, not raise."""
    infra = FakeInfra(vector=[0.5, 0.5])
    store = FakeStore(error=RuntimeError("connection pool exhausted"))

    result, degraded = asyncio.run(
        retrieval.retrieve(infra, store, "sess-1", "hello", top_k=5, min_score=0.2)
    )

    assert result == []
    assert degraded is True
    # Embedding succeeded and the store was actually called (then blew up).
    assert infra.calls == ["hello"]
    assert len(store.calls) == 1


def test_retrieve_success_maps_rows_to_retrieved_turns():
    infra = FakeInfra(vector=[0.5, 0.5])
    rows = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "role": "user",
            "content": "what is the weather",
            "score": 0.92,
            "created_at": "2026-06-13T12:00:00+00:00",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "role": "assistant",
            "content": "it is sunny",
            "score": 0.81,
            "created_at": None,
        },
    ]
    store = FakeStore(rows=rows)

    result, degraded = asyncio.run(
        retrieval.retrieve(infra, store, "sess-1", "weather?", top_k=10, min_score=0.1)
    )

    assert degraded is False
    assert len(result) == 2
    assert all(isinstance(t, RetrievedTurn) for t in result)

    first = result[0]
    assert first.id == "11111111-1111-1111-1111-111111111111"
    assert first.role == "user"
    assert first.content == "what is the weather"
    assert first.score == pytest.approx(0.92)
    assert first.created_at == "2026-06-13T12:00:00+00:00"

    assert result[1].created_at is None

    # The query was embedded and passed straight through to the store.
    assert infra.calls == ["weather?"]
    assert store.calls[0] == ("sess-1", [0.5, 0.5], 10, 0.1)


def test_retrieve_success_empty_is_not_degraded():
    """A clean embed + empty result set is NOT degraded (([], False))."""
    infra = FakeInfra(vector=[0.5, 0.5])
    store = FakeStore(rows=[])

    result, degraded = asyncio.run(
        retrieval.retrieve(infra, store, "sess-1", "nothing relevant", top_k=5, min_score=0.9)
    )

    assert result == []
    assert degraded is False
