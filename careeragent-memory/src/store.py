"""Store — the storage seam for careeragent-memory.

This module is the *only* place that knows the vectors live in Postgres+pgvector.
Everything above it (retrieval, the endpoints) talks to this interface, so the
backing store can be swapped (an embedded store, a different vector DB) without
touching the rest of the service. Memory owns this database outright — it is NOT
the logger's shared Postgres.

Design notes:
  - Session-scoped: every row carries a `session_id`; search filters on it.
  - Exact cosine search over a bounded per-session set — no ANN index needed,
    so the `embedding` column is declared as an unsized `vector` and the model's
    dimensionality is whatever the BYOC embedder emits.
  - Dedupe: (session_id, content_hash) is unique; re-ingesting an identical turn
    is a no-op (ON CONFLICT DO NOTHING).
  - DDL lives in database/init.sql (applied at DB first-boot), matching how the
    logger keeps schema in init.sql. This module assumes the table exists.
"""

from __future__ import annotations

import hashlib
import logging
import uuid as uuid_lib
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, String, Text, func, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pgvector.sqlalchemy import Vector

logger = logging.getLogger("careeragent_memory.store")

SCHEMA = "careeragent_memory"


def content_hash(content: str) -> str:
    """Stable hash used as the dedupe key for a turn's content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = {"schema": SCHEMA}

    # gen_random_uuid() (core since PG13) supplies the default on the DB side.
    id: Mapped[uuid_lib.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Unsized vector: dimension follows the embedding model; exact search, no ANN index.
    embedding: Mapped[List[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Store:
    def __init__(self, database_url: str) -> None:
        # Normalise to the async psycopg driver if a bare URL was supplied.
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        self._database_url = database_url
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker] = None

    async def start(self) -> None:
        self._engine = create_async_engine(
            self._database_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        logger.info("Store started")

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("Store closed")

    async def ping(self) -> bool:
        """SELECT 1 against the pool. Returns False on any failure (never raises)."""
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            logger.warning("Store ping failed: %s", type(exc).__name__)
            return False

    async def upsert_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        embedding: List[float],
    ) -> Dict[str, Any]:
        """Insert a turn. Identical (session_id, content_hash) is a no-op.

        Returns {"id": <uuid or None>, "duplicate": bool}. A duplicate returns
        the existing row's id with duplicate=True.
        """
        if self._sessionmaker is None:  # pragma: no cover - lifecycle guard
            raise RuntimeError("Store used before start()")

        chash = content_hash(content)
        async with self._sessionmaker() as session:
            stmt = (
                pg_insert(Turn)
                .values(
                    session_id=session_id,
                    role=role,
                    content=content,
                    content_hash=chash,
                    embedding=embedding,
                )
                .on_conflict_do_nothing(index_elements=["session_id", "content_hash"])
                .returning(Turn.id)
            )
            result = await session.execute(stmt)
            new_id = result.scalar_one_or_none()
            await session.commit()

            if new_id is not None:
                logger.info(
                    "Ingested turn (session=%s, role=%s, id=%s)", session_id, role, new_id
                )
                return {"id": str(new_id), "duplicate": False}

            # Conflict: fetch the existing row's id for the response.
            existing = await session.execute(
                select(Turn.id).where(
                    Turn.session_id == session_id, Turn.content_hash == chash
                )
            )
            existing_id = existing.scalar_one_or_none()
            logger.info(
                "Ingest no-op, duplicate (session=%s, role=%s)", session_id, role
            )
            return {"id": str(existing_id) if existing_id else None, "duplicate": True}

    async def search(
        self,
        session_id: str,
        query_embedding: List[float],
        top_k: int,
        min_score: float,
    ) -> List[Dict[str, Any]]:
        """Top-k turns in this session by cosine similarity, score-floored.

        Cosine distance is computed in-database by pgvector (`<=>`); similarity
        is 1 - distance. Only rows clearing `min_score` are returned, highest
        score first.
        """
        if self._sessionmaker is None:  # pragma: no cover - lifecycle guard
            raise RuntimeError("Store used before start()")

        distance = Turn.embedding.cosine_distance(query_embedding)
        score = (1 - distance).label("score")
        stmt = (
            select(Turn, score)
            .where(Turn.session_id == session_id)
            # Apply the similarity floor in SQL, BEFORE the limit, so it never
            # eats into the top_k budget. If the floor were applied after LIMIT
            # (in Python), a top_k of nearest rows that all fall below the floor
            # would return fewer than top_k qualifying rows even when more exist.
            # similarity >= min_score  ⟺  distance <= 1 - min_score.
            .where(distance <= (1 - min_score))
            .order_by(distance)
            .limit(top_k)
        )

        async with self._sessionmaker() as session:
            rows = (await session.execute(stmt)).all()

        out: List[Dict[str, Any]] = []
        for turn, sim in rows:
            out.append(
                {
                    "id": str(turn.id),
                    "role": turn.role,
                    "content": turn.content,
                    "score": float(sim),
                    "created_at": turn.created_at.isoformat() if turn.created_at else None,
                }
            )
        logger.info(
            "Retrieve (session=%s, candidates_returned=%d, top_k=%d, min_score=%.3f)",
            session_id,
            len(out),
            top_k,
            min_score,
        )
        return out
