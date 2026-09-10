"""Transactional inbox operations for Telegram updates."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.storage.base import utc_now
from szs_hub.storage.models import InboxUpdate, ProcessedUpdate


async def enqueue_raw_update(
    session: AsyncSession,
    *,
    update_id: int,
    payload: Mapping[str, Any],
    received_at: datetime | None = None,
) -> bool:
    """Insert a Telegram update once, returning ``False`` for a redelivery.

    The function deliberately does not commit; callers can atomically combine inbox
    admission with their surrounding unit of work.
    """

    if update_id < 0:
        raise ValueError("Telegram update_id must be non-negative")
    timestamp = received_at or utc_now()
    statement = (
        insert(InboxUpdate)
        .values(
            update_id=update_id,
            payload=dict(payload),
            received_at=timestamp,
            available_at=timestamp,
        )
        .on_conflict_do_nothing(index_elements=[InboxUpdate.update_id])
        .returning(InboxUpdate.id)
    )
    inserted_id = await session.scalar(statement)
    return inserted_id is not None


async def record_processed_update(
    session: AsyncSession,
    *,
    update_id: int,
    handler: str,
    outcome: str = "processed",
    detail: str | None = None,
    processed_at: datetime | None = None,
) -> bool:
    """Record a terminal handler result once and close the corresponding inbox row."""

    if outcome not in {"processed", "ignored", "failed"}:
        raise ValueError(f"unsupported update outcome: {outcome}")
    timestamp = processed_at or utc_now()
    statement = (
        insert(ProcessedUpdate)
        .values(
            update_id=update_id,
            handler=handler,
            outcome=outcome,
            detail=detail,
            processed_at=timestamp,
        )
        .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
        .returning(ProcessedUpdate.update_id)
    )
    recorded_id = await session.scalar(statement)
    if recorded_id is None:
        return False
    # ``update_id`` is a unique alternate key, not the ORM identity key.
    inbox = await session.scalar(select(InboxUpdate).where(InboxUpdate.update_id == update_id))
    if inbox is not None:
        inbox.processed_at = timestamp
    return True
