"""Durable polling and retryable inbox processing."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Protocol

from aiogram.types import Update
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.storage import InboxUpdate, enqueue_raw_update, record_processed_update, utc_now
from szs_hub.telegram.updates import ALLOWED_UPDATES


class UpdateSource(Protocol):
    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 - Telegram API field name
        allowed_updates: list[str] | None = None,
        request_timeout: int | None = None,
    ) -> list[Update]: ...


class UpdateDispatcher(Protocol):
    async def feed_update(self, bot: object, update: Update, **kwargs: Any) -> Any: ...


class DurableUpdatePump:
    """Persist polling results before advancing the Telegram offset."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        source: UpdateSource,
        dispatcher: UpdateDispatcher,
        bot_context: object,
        max_attempts: int = 8,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("update worker must allow at least one attempt")
        self._sessions = session_factory
        self._source = source
        self._dispatcher = dispatcher
        self._bot_context = bot_context
        self._max_attempts = max_attempts

    async def initial_offset(self) -> int | None:
        async with self._sessions() as session:
            highest = await session.scalar(select(func.max(InboxUpdate.update_id)))
        return int(highest) + 1 if highest is not None else None

    async def poll_once(self, *, offset: int | None, poll_timeout: int = 50) -> int | None:
        updates = await self._source.get_updates(
            offset=offset,
            limit=100,
            timeout=poll_timeout,
            allowed_updates=list(ALLOWED_UPDATES),
            request_timeout=poll_timeout + 10,
        )
        if not updates:
            return offset
        await self.admit(updates)
        return max(update.update_id for update in updates) + 1

    async def admit(self, updates: Sequence[Update]) -> int:
        inserted = 0
        async with self._sessions() as session, session.begin():
            for update in updates:
                payload = update.model_dump(mode="json", exclude_none=True)
                if await enqueue_raw_update(
                    session,
                    update_id=update.update_id,
                    payload=payload,
                ):
                    inserted += 1
        return inserted

    async def process_ready(self, *, limit: int = 50) -> int:
        now = utc_now()
        async with self._sessions() as session:
            update_ids = list(
                await session.scalars(
                    select(InboxUpdate.update_id)
                    .where(
                        InboxUpdate.processed_at.is_(None),
                        InboxUpdate.available_at <= now,
                    )
                    .order_by(InboxUpdate.received_at, InboxUpdate.update_id)
                    .limit(limit)
                )
            )

        processed = 0
        for update_id in update_ids:
            if await self._process_one(update_id):
                processed += 1
        return processed

    async def _process_one(self, update_id: int) -> bool:
        async with self._sessions() as session:
            inbox = await session.scalar(
                select(InboxUpdate).where(InboxUpdate.update_id == update_id)
            )
            if inbox is None or inbox.processed_at is not None:
                return False
            payload = dict(inbox.payload)

        try:
            update = Update.model_validate(payload)
            await self._dispatcher.feed_update(self._bot_context, update)
        except Exception as exc:
            await self._record_failure(update_id, exc)
            return False

        async with self._sessions() as session, session.begin():
            await record_processed_update(
                session,
                update_id=update_id,
                handler="dispatcher",
                outcome="processed",
            )
        return True

    async def _record_failure(self, update_id: int, error: Exception) -> None:
        async with self._sessions() as session, session.begin():
            inbox = await session.scalar(
                select(InboxUpdate).where(InboxUpdate.update_id == update_id)
            )
            if inbox is None or inbox.processed_at is not None:
                return
            inbox.attempts += 1
            inbox.last_error = _safe_error(error)
            if inbox.attempts >= self._max_attempts:
                await record_processed_update(
                    session,
                    update_id=update_id,
                    handler="dispatcher",
                    outcome="failed",
                    detail=inbox.last_error,
                )
                return
            delay = min(300, 2 ** min(inbox.attempts, 8))
            inbox.available_at = utc_now() + timedelta(seconds=delay)


def _safe_error(error: Exception) -> str:
    """Persist a stable error class without exception text that may contain user data."""

    return type(error).__name__[:200]
