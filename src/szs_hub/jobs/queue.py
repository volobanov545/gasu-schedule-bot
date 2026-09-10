"""Durable SQLite queues for background work and Telegram sends.

The application intentionally runs one queue writer/worker at a time. Claims are still
performed with one conditional ``UPDATE ... RETURNING`` statement so an accidental
second process cannot claim the same ready row. Attempt numbers fence late completions
after an expired lease has been recovered.

The Telegram outbox reuses ``available_at`` as both the next-attempt time and, while a
row is ``sending``, its lease deadline. This provides crash recovery without widening
the already-migrated schema. Delivery is at-least-once: Telegram does not expose an
idempotency header, so senders receive the durable idempotency key and should preserve
it in any provider-specific deduplication they can offer.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.storage import Job, OutboxMessage, utc_now

MIN_LEASE = timedelta(seconds=1)
MAX_LEASE = timedelta(hours=1)
MAX_ATTEMPTS = 100
MAX_RETRY_DELAY = timedelta(hours=24)


class IdempotencyConflictError(ValueError):
    """Raised when one key is reused for a different logical operation."""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Identity and creation outcome of an idempotent enqueue."""

    id: int
    created: bool


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Immutable job attempt handed to a handler."""

    id: int
    kind: str
    idempotency_key: str
    payload: Mapping[str, Any]
    attempt: int
    max_attempts: int
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    """Immutable outbox attempt handed to a Telegram sender."""

    id: int
    kind: str
    idempotency_key: str
    chat_id: int | None
    payload: Mapping[str, Any]
    attempt: int
    max_attempts: int
    leased_until: datetime


class JobQueue:
    """Own transactions for idempotent job admission and leased processing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        clock: Callable[[], datetime] = utc_now,
        retry_base: timedelta = timedelta(seconds=5),
        retry_cap: timedelta = timedelta(minutes=15),
    ) -> None:
        _validate_worker_id(worker_id)
        _validate_retry_policy(retry_base, retry_cap)
        self._sessions = session_factory
        self._worker_id = worker_id
        self._clock = clock
        self._retry_base = retry_base
        self._retry_cap = retry_cap

    async def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        run_at: datetime | None = None,
        max_attempts: int = 5,
        extend_existing_run_at: bool = False,
    ) -> EnqueueResult:
        """Create one logical job, or return the matching existing row.

        ``extend_existing_run_at`` implements a durable debounce without allowing a
        duplicate observation to pull work forward. Only a still-pending matching job
        can be deferred; completed and currently leased work remain immutable.
        """

        _validate_enqueue(kind, idempotency_key, max_attempts)
        timestamp = _aware(run_at or self._clock())
        copied_payload = dict(payload)
        async with self._sessions() as session, session.begin():
            statement = (
                insert(Job)
                .values(
                    kind=kind,
                    idempotency_key=idempotency_key,
                    payload=copied_payload,
                    run_at=timestamp,
                    max_attempts=max_attempts,
                    created_at=self._clock(),
                    updated_at=self._clock(),
                )
                .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
                .returning(Job.id)
            )
            inserted_id = await session.scalar(statement)
            if inserted_id is not None:
                return EnqueueResult(id=inserted_id, created=True)
            existing = await session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if existing is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError("idempotent enqueue lost its existing row")
            if existing.kind != kind or existing.payload != copied_payload:
                raise IdempotencyConflictError(
                    "job idempotency key already belongs to different content"
                )
            if (
                extend_existing_run_at
                and existing.status == "pending"
                and existing.run_at < timestamp
            ):
                existing.run_at = timestamp
                existing.updated_at = self._clock()
            return EnqueueResult(id=existing.id, created=False)

    async def claim_due(
        self,
        *,
        lease: timedelta,
        kinds: Collection[str] | None = None,
    ) -> JobClaim | None:
        """Atomically claim the oldest due job, recovering expired leases."""

        _validate_lease(lease)
        registered_kinds = tuple(sorted(set(kinds))) if kinds is not None else None
        now = _aware(self._clock())
        leased_until = now + lease
        async with self._sessions() as session, session.begin():
            await self._fail_exhausted_expired(session, now, kinds=registered_kinds)
            eligible = and_(
                Job.attempts < Job.max_attempts,
                *(
                    (Job.kind.in_(registered_kinds),)
                    if registered_kinds is not None
                    else ()
                ),
                or_(
                    and_(Job.status == "pending", Job.run_at <= now),
                    and_(
                        Job.status == "running",
                        or_(Job.locked_until.is_(None), Job.locked_until <= now),
                    ),
                ),
            )
            candidate = (
                select(Job.id)
                .where(eligible)
                .order_by(
                    case((Job.status == "pending", 0), else_=1),
                    Job.run_at,
                    Job.id,
                )
                .limit(1)
                .scalar_subquery()
            )
            statement = (
                update(Job)
                .where(Job.id == candidate, eligible)
                .values(
                    status="running",
                    attempts=Job.attempts + 1,
                    locked_at=now,
                    locked_until=leased_until,
                    locked_by=self._worker_id,
                    updated_at=now,
                    last_error=None,
                )
                .returning(
                    Job.id,
                    Job.kind,
                    Job.idempotency_key,
                    Job.payload,
                    Job.attempts,
                    Job.max_attempts,
                )
            )
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            return JobClaim(
                id=row.id,
                kind=row.kind,
                idempotency_key=row.idempotency_key,
                payload=dict(row.payload),
                attempt=row.attempts,
                max_attempts=row.max_attempts,
                leased_until=leased_until,
            )

    async def succeed(self, claim: JobClaim) -> bool:
        """Finish the exact claimed attempt; stale completions are ignored."""

        now = _aware(self._clock())
        async with self._sessions() as session, session.begin():
            statement = (
                update(Job)
                .where(
                    Job.id == claim.id,
                    Job.status == "running",
                    Job.attempts == claim.attempt,
                    Job.locked_by == self._worker_id,
                )
                .values(
                    status="succeeded",
                    completed_at=now,
                    updated_at=now,
                    locked_at=None,
                    locked_until=None,
                    locked_by=None,
                    last_error=None,
                )
                .returning(Job.id)
            )
            return await session.scalar(statement) is not None

    async def fail(self, claim: JobClaim, error: BaseException) -> bool:
        """Retry with bounded backoff, or record a terminal safe error code."""

        now = _aware(self._clock())
        terminal = claim.attempt >= claim.max_attempts
        delay = _retry_delay(claim.attempt, self._retry_base, self._retry_cap)
        values: dict[str, Any] = {
            "status": "failed" if terminal else "pending",
            "run_at": now if terminal else now + delay,
            "completed_at": now if terminal else None,
            "updated_at": now,
            "locked_at": None,
            "locked_until": None,
            "locked_by": None,
            "last_error": _safe_error_code(error),
        }
        async with self._sessions() as session, session.begin():
            statement = (
                update(Job)
                .where(
                    Job.id == claim.id,
                    Job.status == "running",
                    Job.attempts == claim.attempt,
                    Job.locked_by == self._worker_id,
                )
                .values(**values)
                .returning(Job.id)
            )
            return await session.scalar(statement) is not None

    async def _fail_exhausted_expired(
        self,
        session: AsyncSession,
        now: datetime,
        *,
        kinds: tuple[str, ...] | None,
    ) -> None:
        filters: list[Any] = [
            Job.status == "running",
            or_(Job.locked_until.is_(None), Job.locked_until <= now),
            Job.attempts >= Job.max_attempts,
        ]
        if kinds is not None:
            filters.append(Job.kind.in_(kinds))
        await session.execute(
            update(Job)
            .where(*filters)
            .values(
                status="failed",
                completed_at=now,
                updated_at=now,
                locked_at=None,
                locked_until=None,
                locked_by=None,
                last_error="LeaseExpired",
            )
        )


class OutboxQueue:
    """Durable, leased, at-least-once Telegram outbox."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        retry_base: timedelta = timedelta(seconds=5),
        retry_cap: timedelta = timedelta(minutes=15),
    ) -> None:
        _validate_retry_policy(retry_base, retry_cap)
        self._sessions = session_factory
        self._clock = clock
        self._retry_base = retry_base
        self._retry_cap = retry_cap

    async def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        chat_id: int | None = None,
        job_id: int | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> EnqueueResult:
        """Create one outgoing operation, rejecting key/content collisions."""

        _validate_enqueue(kind, idempotency_key, max_attempts)
        timestamp = _aware(available_at or self._clock())
        copied_payload = dict(payload)
        async with self._sessions() as session, session.begin():
            statement = (
                insert(OutboxMessage)
                .values(
                    kind=kind,
                    idempotency_key=idempotency_key,
                    payload=copied_payload,
                    chat_id=chat_id,
                    job_id=job_id,
                    available_at=timestamp,
                    max_attempts=max_attempts,
                    created_at=self._clock(),
                )
                .on_conflict_do_nothing(index_elements=[OutboxMessage.idempotency_key])
                .returning(OutboxMessage.id)
            )
            inserted_id = await session.scalar(statement)
            if inserted_id is not None:
                return EnqueueResult(id=inserted_id, created=True)
            existing = await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.idempotency_key == idempotency_key
                )
            )
            if existing is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError("idempotent enqueue lost its existing outbox row")
            if (
                existing.kind != kind
                or existing.payload != copied_payload
                or existing.chat_id != chat_id
                or existing.job_id != job_id
            ):
                raise IdempotencyConflictError(
                    "outbox idempotency key already belongs to different content"
                )
            return EnqueueResult(id=existing.id, created=False)

    async def claim_due(self, *, lease: timedelta) -> OutboxClaim | None:
        """Atomically claim a due send, including an expired send lease."""

        _validate_lease(lease)
        now = _aware(self._clock())
        leased_until = now + lease
        async with self._sessions() as session, session.begin():
            await self._fail_exhausted_expired(session, now)
            eligible = and_(
                OutboxMessage.attempts < OutboxMessage.max_attempts,
                OutboxMessage.available_at <= now,
                OutboxMessage.status.in_(("pending", "sending")),
            )
            candidate = (
                select(OutboxMessage.id)
                .where(eligible)
                .order_by(
                    case((OutboxMessage.status == "pending", 0), else_=1),
                    OutboxMessage.available_at,
                    OutboxMessage.id,
                )
                .limit(1)
                .scalar_subquery()
            )
            statement = (
                update(OutboxMessage)
                .where(OutboxMessage.id == candidate, eligible)
                .values(
                    status="sending",
                    attempts=OutboxMessage.attempts + 1,
                    available_at=leased_until,
                    last_error=None,
                )
                .returning(
                    OutboxMessage.id,
                    OutboxMessage.kind,
                    OutboxMessage.idempotency_key,
                    OutboxMessage.chat_id,
                    OutboxMessage.payload,
                    OutboxMessage.attempts,
                    OutboxMessage.max_attempts,
                )
            )
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            return OutboxClaim(
                id=row.id,
                kind=row.kind,
                idempotency_key=row.idempotency_key,
                chat_id=row.chat_id,
                payload=dict(row.payload),
                attempt=row.attempts,
                max_attempts=row.max_attempts,
                leased_until=leased_until,
            )

    async def sent(self, claim: OutboxClaim, telegram_message_id: int | None) -> bool:
        """Mark the exact send attempt complete; reject a stale lease holder."""

        now = _aware(self._clock())
        async with self._sessions() as session, session.begin():
            statement = (
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == claim.id,
                    OutboxMessage.status == "sending",
                    OutboxMessage.attempts == claim.attempt,
                )
                .values(
                    status="sent",
                    sent_at=now,
                    telegram_message_id=telegram_message_id,
                    available_at=now,
                    last_error=None,
                )
                .returning(OutboxMessage.id)
            )
            return await session.scalar(statement) is not None

    async def fail(self, claim: OutboxClaim, error: BaseException) -> bool:
        """Retry a send with backoff, or finish it as a terminal failure."""

        now = _aware(self._clock())
        terminal = claim.attempt >= claim.max_attempts
        delay = _retry_delay(claim.attempt, self._retry_base, self._retry_cap)
        async with self._sessions() as session, session.begin():
            statement = (
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == claim.id,
                    OutboxMessage.status == "sending",
                    OutboxMessage.attempts == claim.attempt,
                )
                .values(
                    status="failed" if terminal else "pending",
                    available_at=now if terminal else now + delay,
                    last_error=_safe_error_code(error),
                )
                .returning(OutboxMessage.id)
            )
            return await session.scalar(statement) is not None

    async def _fail_exhausted_expired(self, session: AsyncSession, now: datetime) -> None:
        await session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.status == "sending",
                OutboxMessage.available_at <= now,
                OutboxMessage.attempts >= OutboxMessage.max_attempts,
            )
            .values(status="failed", available_at=now, last_error="LeaseExpired")
        )


def _retry_delay(attempt: int, base: timedelta, cap: timedelta) -> timedelta:
    exponent = min(max(attempt - 1, 0), 30)
    seconds = min(base.total_seconds() * (2**exponent), cap.total_seconds())
    return timedelta(seconds=seconds)


def _safe_error_code(error: BaseException) -> str:
    """Store only a bounded exception type, never its potentially secret text."""

    name = type(error).__name__
    safe = "".join(character for character in name if character.isalnum() or character == "_")
    return (safe or "HandlerError")[:100]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def _validate_worker_id(worker_id: str) -> None:
    if not worker_id or len(worker_id) > 128:
        raise ValueError("worker_id must contain 1..128 characters")


def _validate_enqueue(kind: str, idempotency_key: str, max_attempts: int) -> None:
    if not kind or len(kind) > 100:
        raise ValueError("kind must contain 1..100 characters")
    if not idempotency_key or len(idempotency_key) > 255:
        raise ValueError("idempotency_key must contain 1..255 characters")
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")


def _validate_retry_policy(base: timedelta, cap: timedelta) -> None:
    if base <= timedelta(0) or cap < base or cap > MAX_RETRY_DELAY:
        raise ValueError("retry delays must satisfy 0 < base <= cap <= 24 hours")


def _validate_lease(lease: timedelta) -> None:
    if not MIN_LEASE <= lease <= MAX_LEASE:
        raise ValueError("lease must be between 1 second and 1 hour")
