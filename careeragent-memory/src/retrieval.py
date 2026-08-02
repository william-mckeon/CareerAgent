"""Retrieval pipeline — the ranking layer.

Keeps ranking/policy separate from storage: `store.py` knows Postgres, this
module knows the retrieve flow (embed the query, search the store, shape the
result) and the fail-open contract. It is storage-backend agnostic — it talks
to the Store interface, not to SQL.

Fail-open: if the embedding call fails for any reason (cold/unreachable/
unconfigured embedder, timeout), retrieve returns an empty result with
degraded=True. Retrieval is an enhancement, never a hard dependency for getting
an answer — so a retrieval outage degrades quality, it never blocks /chat.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np

from client.infra import InfraClient, InfraEmbedError
from schemas import RetrievedTurn
from store import Store

logger = logging.getLogger("careeragent_memory.retrieval")


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Pure-Python cosine similarity (utility / fallback / tests).

    The live path computes cosine in-database via pgvector; this is kept for
    re-ranking experiments and unit tests against a known pair of vectors.
    """
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


async def retrieve(
    infra: InfraClient,
    store: Store,
    session_id: str,
    query: str,
    top_k: int,
    min_score: float,
) -> Tuple[List[RetrievedTurn], bool]:
    """Return (retrieved_turns, degraded).

    degraded is True only when the embedding step failed open. A successful
    embed that simply finds nothing relevant returns ([], False).
    """
    try:
        query_vector = await infra.embed_one(query)
    except InfraEmbedError as exc:
        logger.warning("Retrieve degraded — embedding unavailable: %s", exc)
        return ([], True)

    # The store call must fail open too: a DB hiccup (connection drop, pool
    # exhaustion) or an embedding-dimension mismatch (e.g. the embedding model
    # was swapped for one with a different output size, so pgvector rejects the
    # query against existing rows) must degrade retrieval, never 500 the hot
    # /chat path. Retrieval is an enhancement, never a hard dependency.
    try:
        rows = await store.search(session_id, query_vector, top_k, min_score)
    except Exception as exc:  # noqa: BLE001 - hot path must fail open
        logger.warning("Retrieve degraded — store search failed: %s", exc)
        return ([], True)

    retrieved = [
        RetrievedTurn(
            id=row["id"],
            role=row["role"],
            content=row["content"],
            score=row["score"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return (retrieved, False)
