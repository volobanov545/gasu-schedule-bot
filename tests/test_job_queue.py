from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from szs_hub.jobs import (
    IdempotencyConflictError,
    JobClaim,
    JobQueue,
    JobWorker,
    OutboxClaim,
    OutboxQueue,
    OutboxWorker,
)
from szs_hub.storage import (
    Job,
    OutboxMessage,
    create_database_engine,
    create_schema,
    create_session_factory,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 30, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@asynccontextmanager
async def queue_database(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_database_engine(sqlite_url(tmp_path / "queues.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        yield engine, sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_job_enqueue_is_idempotent_and_rejects_key_collision(tmp_path: Path) -> None:
    clock = MutableClock()
    async with queue_database(tmp_path) as (_, sessions):
        queue = JobQueue(sessions, worker_id="worker-a", clock=clock)

        first = await queue.enqueue(
            kind="schedule.refresh", idempotency_key="schedule:week-36", payload={"group": 7}
        )
        duplicate = await queue.enqueue(
            kind="schedule.refresh", idempotency_key="schedule:week-36", payload={"group": 7}
        )

        assert first.created
        assert duplicate == type(duplicate)(id=first.id, created=False)
        with pytest.raises(IdempotencyConflictError, match="different content"):
            await queue.enqueue(
                kind="schedule.refresh",
                idempotency_key="schedule:week-36",
                payload={"group": 8},
            )

        async with sessions() as session:
            assert len((await session.scalars(select(Job))).all()) == 1


@pytest.mark.asyncio
async def test_job_worker_succeeds_once(tmp_path: Path) -> None:
    clock = MutableClock()
    handled: list[JobClaim] = []

    class Handler:
        async def __call__(self, job: JobClaim) -> None:
            handled.append(job)

    async with queue_database(tmp_path) as (_, sessions):
        queue = JobQueue(sessions, worker_id="worker-a", clock=clock)
        result = await queue.enqueue(kind="index", idempotency_key="index:1", payload={"id": 1})
        worker = JobWorker(queue, {"index": Handler()})

        assert await worker.run_once()
        assert not await worker.run_once()
        assert [item.id for item in handled] == [result.id]

        async with sessions() as session:
            stored = await session.get(Job, result.id)
            assert stored is not None
            assert stored.status == "succeeded"
            assert stored.attempts == 1
            assert stored.completed_at == clock.now


@pytest.mark.asyncio
async def test_job_retries_then_fails_without_persisting_secret_text(tmp_path: Path) -> None:
    clock = MutableClock()

    class FailingHandler:
        async def __call__(self, job: JobClaim) -> None:
            del job
            raise RuntimeError("token=super-secret-value")

    async with queue_database(tmp_path) as (_, sessions):
        queue = JobQueue(
            sessions,
            worker_id="worker-a",
            clock=clock,
            retry_base=timedelta(seconds=2),
            retry_cap=timedelta(seconds=10),
        )
        result = await queue.enqueue(
            kind="fragile", idempotency_key="fragile:1", payload={}, max_attempts=2
        )
        worker = JobWorker(queue, {"fragile": FailingHandler()})

        assert await worker.run_once()
        async with sessions() as session:
            first = await session.get(Job, result.id)
            assert first is not None
            assert first.status == "pending"
            assert first.attempts == 1
            assert first.run_at == clock.now + timedelta(seconds=2)
            assert first.last_error == "RuntimeError"

        assert not await worker.run_once()
        clock.advance(timedelta(seconds=2))
        assert await worker.run_once()

        async with sessions() as session:
            terminal = await session.get(Job, result.id)
            assert terminal is not None
            assert terminal.status == "failed"
            assert terminal.attempts == 2
            assert terminal.last_error == "RuntimeError"
            assert "secret" not in terminal.last_error.casefold()


@pytest.mark.asyncio
async def test_expired_job_lease_is_recovered_and_fences_stale_worker(tmp_path: Path) -> None:
    clock = MutableClock()
    async with queue_database(tmp_path) as (_, sessions):
        first_queue = JobQueue(sessions, worker_id="worker-a", clock=clock)
        second_queue = JobQueue(sessions, worker_id="worker-b", clock=clock)
        result = await first_queue.enqueue(
            kind="recover", idempotency_key="recover:1", payload={}, max_attempts=3
        )

        first_claim = await first_queue.claim_due(lease=timedelta(seconds=10))
        assert first_claim is not None
        clock.advance(timedelta(seconds=11))
        recovered_claim = await second_queue.claim_due(lease=timedelta(seconds=10))

        assert recovered_claim is not None
        assert recovered_claim.id == result.id
        assert recovered_claim.attempt == 2
        assert not await first_queue.succeed(first_claim)
        assert await second_queue.succeed(recovered_claim)


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[OutboxClaim] = []

    async def send(self, message: OutboxClaim) -> int:
        self.messages.append(message)
        return 4321


@pytest.mark.asyncio
async def test_outbox_enqueue_deduplicates_and_worker_records_send(tmp_path: Path) -> None:
    clock = MutableClock()
    sender = RecordingSender()
    async with queue_database(tmp_path) as (_, sessions):
        queue = OutboxQueue(sessions, clock=clock)
        first = await queue.enqueue(
            kind="send_message",
            idempotency_key="attendance:lesson-1",
            chat_id=-1001,
            payload={"text": "Началась пара"},
        )
        duplicate = await queue.enqueue(
            kind="send_message",
            idempotency_key="attendance:lesson-1",
            chat_id=-1001,
            payload={"text": "Началась пара"},
        )
        worker = OutboxWorker(queue, sender)

        assert first.created and not duplicate.created
        assert duplicate.id == first.id
        assert await worker.run_once()
        assert not await worker.run_once()
        assert [message.idempotency_key for message in sender.messages] == [
            "attendance:lesson-1"
        ]

        async with sessions() as session:
            stored = await session.get(OutboxMessage, first.id)
            assert stored is not None
            assert stored.status == "sent"
            assert stored.attempts == 1
            assert stored.telegram_message_id == 4321
            assert stored.sent_at == clock.now


@pytest.mark.asyncio
async def test_outbox_retries_then_fails_without_persisting_secret_text(tmp_path: Path) -> None:
    clock = MutableClock()

    class FailingSender:
        async def send(self, message: OutboxClaim) -> None:
            del message
            raise ConnectionError("Authorization: Bearer telegram-secret")

    async with queue_database(tmp_path) as (_, sessions):
        queue = OutboxQueue(
            sessions,
            clock=clock,
            retry_base=timedelta(seconds=3),
            retry_cap=timedelta(seconds=10),
        )
        result = await queue.enqueue(
            kind="send_message",
            idempotency_key="send:failure",
            payload={"text": "test"},
            max_attempts=2,
        )
        worker = OutboxWorker(queue, FailingSender())

        assert await worker.run_once()
        async with sessions() as session:
            first = await session.get(OutboxMessage, result.id)
            assert first is not None
            assert first.status == "pending"
            assert first.available_at == clock.now + timedelta(seconds=3)
            assert first.last_error == "ConnectionError"

        clock.advance(timedelta(seconds=3))
        assert await worker.run_once()
        async with sessions() as session:
            terminal = await session.get(OutboxMessage, result.id)
            assert terminal is not None
            assert terminal.status == "failed"
            assert terminal.attempts == 2
            assert terminal.last_error == "ConnectionError"
            assert "secret" not in terminal.last_error.casefold()


@pytest.mark.asyncio
async def test_outbox_expired_lease_is_recovered_and_fences_stale_sender(tmp_path: Path) -> None:
    clock = MutableClock()
    async with queue_database(tmp_path) as (_, sessions):
        queue = OutboxQueue(sessions, clock=clock)
        result = await queue.enqueue(
            kind="send_message",
            idempotency_key="send:recover",
            payload={"text": "test"},
            max_attempts=3,
        )

        first_claim = await queue.claim_due(lease=timedelta(seconds=5))
        assert first_claim is not None
        clock.advance(timedelta(seconds=6))
        recovered_claim = await queue.claim_due(lease=timedelta(seconds=5))

        assert recovered_claim is not None
        assert recovered_claim.id == result.id
        assert recovered_claim.attempt == 2
        assert not await queue.sent(first_claim, 111)
        assert await queue.sent(recovered_claim, 222)


@pytest.mark.asyncio
async def test_worker_cancellation_leaves_attempt_for_lease_recovery(tmp_path: Path) -> None:
    clock = MutableClock()
    started = asyncio.Event()
    never_finishes = asyncio.Event()

    class BlockingHandler:
        async def __call__(self, job: JobClaim) -> None:
            del job
            started.set()
            await never_finishes.wait()

    async with queue_database(tmp_path) as (_, sessions):
        queue = JobQueue(sessions, worker_id="worker-a", clock=clock)
        await queue.enqueue(kind="block", idempotency_key="block:1", payload={})
        worker = JobWorker(
            queue,
            {"block": BlockingHandler()},
            lease=timedelta(seconds=5),
        )

        task = asyncio.create_task(worker.run_once())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with sessions() as session:
            running = await session.scalar(select(Job).where(Job.idempotency_key == "block:1"))
            assert running is not None
            assert running.status == "running"

        clock.advance(timedelta(seconds=6))
        recovery = JobQueue(sessions, worker_id="worker-b", clock=clock)
        claim = await recovery.claim_due(lease=timedelta(seconds=5))
        assert claim is not None
        assert claim.attempt == 2
