"""Unit tests for InfraClient.embed batch-alignment guards.

No real httpx / network: we build an InfraClient, then swap its internal
`_client` for a fake whose async `.post()` returns a stub response object
exposing `.status_code` and `.json()` — exactly the surface embed() touches.
This exercises the validation that protects against a mis-aligned provider
response (wrong vector count, missing/duplicate `index`).
"""

from __future__ import annotations

import asyncio

import pytest

from client.infra import InfraClient, InfraEmbedError


class StubResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeHttpClient:
    """Minimal async stand-in for httpx.AsyncClient: only .post is used by embed()."""

    def __init__(self, response: StubResponse):
        self._response = response
        self.calls: list[tuple] = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        return self._response


def make_client(response: StubResponse) -> InfraClient:
    client = InfraClient(base_url="http://infra.test", api_key="k")
    # Inject the fake transport directly; start() is what would normally do this.
    client._client = FakeHttpClient(response)
    return client


def _data(*pairs):
    """Build a /embed-style data list from (index, embedding) pairs."""
    return [{"index": i, "embedding": emb} for i, emb in pairs]


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #


def test_embed_one_single_input_returns_one_vector():
    resp = StubResponse(200, {"data": _data((0, [0.1, 0.2, 0.3]))})
    client = make_client(resp)

    vec = asyncio.run(client.embed_one("hello"))

    assert vec == [0.1, 0.2, 0.3]
    # Sent as a single /embed call with the raw string as input.
    assert client._client.calls == [("/embed", {"input": "hello"})]


def test_embed_batch_well_formed_returns_ordered_vectors():
    # Deliberately out of order — embed() must sort by index.
    resp = StubResponse(
        200,
        {"data": _data((1, [1.0]), (0, [0.0]), (2, [2.0]))},
    )
    client = make_client(resp)

    vectors = asyncio.run(client.embed(["a", "b", "c"]))

    assert vectors == [[0.0], [1.0], [2.0]]


# --------------------------------------------------------------------------- #
# Guard: count mismatch                                                        #
# --------------------------------------------------------------------------- #


def test_embed_vector_count_mismatch_raises():
    # Two inputs but the provider only returned one vector.
    resp = StubResponse(200, {"data": _data((0, [0.1]))})
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed(["a", "b"]))
    assert "1 vectors for 2 input(s)" in str(exc.value)


# --------------------------------------------------------------------------- #
# Guard: missing / duplicate index in a batch                                 #
# --------------------------------------------------------------------------- #


def test_embed_batch_duplicate_index_raises():
    # Right count (2) but both carry index 0 — sort would collapse them.
    resp = StubResponse(
        200,
        {"data": [{"index": 0, "embedding": [0.0]}, {"index": 0, "embedding": [1.0]}]},
    )
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed(["a", "b"]))
    assert "indices missing or not distinct" in str(exc.value)


def test_embed_batch_missing_index_raises():
    # Right count (2) but one item has no `index` key at all.
    resp = StubResponse(
        200,
        {"data": [{"index": 0, "embedding": [0.0]}, {"embedding": [1.0]}]},
    )
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed(["a", "b"]))
    assert "indices missing or not distinct" in str(exc.value)


# --------------------------------------------------------------------------- #
# Other failure modes embed() must surface as InfraEmbedError                  #
# --------------------------------------------------------------------------- #


def test_embed_non_200_raises():
    resp = StubResponse(503, {"detail": "EMBEDDING_MODEL_URL unset"})
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed("hello"))
    assert "HTTP 503" in str(exc.value)


def test_embed_unparseable_body_raises():
    resp = StubResponse(200, {"not_data": []})
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed("hello"))
    assert "unparseable body" in str(exc.value)


def test_embed_empty_vectors_raises():
    resp = StubResponse(200, {"data": []})
    client = make_client(resp)

    with pytest.raises(InfraEmbedError) as exc:
        asyncio.run(client.embed("hello"))
    assert "no vectors" in str(exc.value)
