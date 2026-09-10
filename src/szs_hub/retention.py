"""Bounded privacy retention for durable operational and assistant data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.storage.base import utc_now
from szs_hub.storage.models import (
    AIConversation,
    InboxUpdate,
    Job,
    OutboxMessage,
    ProcessedUpdate,
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Explicit age boundaries; shared archive/attendance/schedule data is out of scope."""

    processed_update_payload_age: timedelta = timedelta(days=30)
    failed_diagnostic_age: timedelta = timedelta(days=90)
    ai_conversation_age: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        if min(
            self.processed_update_payload_age,
            self.failed_diagnostic_age,
            self.ai_conversation_age,
        ) <= timedelta(0):
            raise ValueError("retention ages must be positive")


@dataclass(frozen=True, slots=True)
class RetentionResult:
    processed_payloads_redacted: int
    failed_updates_redacted: int
    failed_jobs_redacted: int
    failed_outbox_redacted: int
    ai_conversations_deleted: int
    may_have_more: bool


class RetentionService:
    """Apply one bounded, idempotent retention batch using short transactions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        policy: RetentionPolicy | None = None,
    ) -> None:
        self._sessions = session_factory
        self._policy = policy or RetentionPolicy()

    async def run_batch(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
    ) -> RetentionResult:
        checked_at = _aware(now or utc_now())
        if not 1 <= limit <= 5_000:
            raise ValueError("retention batch limit must be between 1 and 5000")

        processed = await self._redact_processed_payloads(
            before=checked_at - self._policy.processed_update_payload_age,
            limit=limit,
        )
        failed_updates = await self._redact_failed_updates(
            before=checked_at - self._policy.failed_diagnostic_age,
            limit=limit,
        )
        failed_jobs = await self._redact_failed_jobs(
            before=checked_at - self._policy.failed_diagnostic_age,
            limit=limit,
        )
        failed_outbox = await self._redact_failed_outbox(
            before=checked_at - self._policy.failed_diagnostic_age,
            limit=limit,
        )
        conversations = await self._delete_ai_conversations(
            before=checked_at - self._policy.ai_conversation_age,
            limit=limit,
        )
        counts = (processed, failed_updates, failed_jobs, failed_outbox, conversations)
        return RetentionResult(
            processed_payloads_redacted=processed,
            failed_updates_redacted=failed_updates,
            failed_jobs_redacted=failed_jobs,
            failed_outbox_redacted=failed_outbox,
            ai_conversations_deleted=conversations,
            may_have_more=any(count == limit for count in counts),
        )

    async def _redact_processed_payloads(self, *, before: datetime, limit: int) -> int:
        async with self._sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(InboxUpdate.id)
                    .join(
                        ProcessedUpdate,
                        ProcessedUpdate.update_id == InboxUpdate.update_id,
                    )
                    .where(
                        ProcessedUpdate.outcome.in_(("processed", "ignored")),
                        ProcessedUpdate.processed_at < before,
                        InboxUpdate.payload != {},
                    )
                    .order_by(ProcessedUpdate.processed_at, InboxUpdate.id)
                    .limit(limit)
                )
            )
            if ids:
                await session.execute(
                    update(InboxUpdate)
                    .where(InboxUpdate.id.in_(ids))
                    .values(payload={})
                )
            return len(ids)

    async def _redact_failed_updates(self, *, before: datetime, limit: int) -> int:
        async with self._sessions() as session, session.begin():
            update_ids = tuple(
                await session.scalars(
                    select(ProcessedUpdate.update_id)
                    .join(
                        InboxUpdate,
                        InboxUpdate.update_id == ProcessedUpdate.update_id,
                    )
                    .where(
                        ProcessedUpdate.outcome == "failed",
                        ProcessedUpdate.processed_at < before,
                        (
                            ProcessedUpdate.detail.is_not(None)
                            | InboxUpdate.last_error.is_not(None)
                            | (InboxUpdate.payload != {})
                        ),
                    )
                    .order_by(ProcessedUpdate.processed_at, ProcessedUpdate.update_id)
                    .limit(limit)
                )
            )
            if update_ids:
                await session.execute(
                    update(ProcessedUpdate)
                    .where(ProcessedUpdate.update_id.in_(update_ids))
                    .values(detail=None)
                )
                await session.execute(
                    update(InboxUpdate)
                    .where(InboxUpdate.update_id.in_(update_ids))
                    .values(last_error=None, payload={})
                )
            return len(update_ids)

    async def _redact_failed_jobs(self, *, before: datetime, limit: int) -> int:
        async with self._sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(Job.id)
                    .where(
                        Job.status == "failed",
                        Job.completed_at.is_not(None),
                        Job.completed_at < before,
                        Job.last_error.is_not(None),
                    )
                    .order_by(Job.completed_at, Job.id)
                    .limit(limit)
                )
            )
            if ids:
                await session.execute(
                    update(Job).where(Job.id.in_(ids)).values(last_error=None)
                )
            return len(ids)

    async def _redact_failed_outbox(self, *, before: datetime, limit: int) -> int:
        async with self._sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(OutboxMessage.id)
                    .where(
                        OutboxMessage.status == "failed",
                        OutboxMessage.available_at < before,
                        OutboxMessage.last_error.is_not(None),
                    )
                    .order_by(OutboxMessage.available_at, OutboxMessage.id)
                    .limit(limit)
                )
            )
            if ids:
                await session.execute(
                    update(OutboxMessage)
                    .where(OutboxMessage.id.in_(ids))
                    .values(last_error=None)
                )
            return len(ids)

    async def _delete_ai_conversations(self, *, before: datetime, limit: int) -> int:
        async with self._sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(AIConversation.id)
                    .where(AIConversation.updated_at < before)
                    .order_by(AIConversation.updated_at, AIConversation.id)
                    .limit(limit)
                )
            )
            if ids:
                await session.execute(
                    delete(AIConversation).where(AIConversation.id.in_(ids))
                )
            return len(ids)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention time must be timezone-aware")
    return value
