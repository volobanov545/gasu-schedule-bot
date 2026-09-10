from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from szs_hub.config import Settings
from szs_hub.jobs.application import ApplicationJobHandlers
from szs_hub.jobs.queue import JobClaim, JobQueue, OutboxQueue
from szs_hub.schedule.coordinator import ScheduleAccess
from szs_hub.storage.database import create_database_engine, create_schema, create_session_factory
from szs_hub.storage.models import (
    InboxUpdate,
    Lesson,
    OutboxMessage,
    ProcessedUpdate,
    ScheduleSnapshot,
    SystemSetting,
)


class FakeCoordinator:
    async def current_and_next(self, *, force_refresh: bool = False) -> tuple[ScheduleAccess, ...]:
        return ()

    async def get_week(self, week_start: date, *, force_refresh: bool = False) -> ScheduleAccess:
        raise AssertionError("not used")


def _job(kind: str, payload: dict[str, object]) -> JobClaim:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return JobClaim(1, kind, "key", payload, 1, 5, now + timedelta(minutes=2))


@pytest.mark.asyncio
async def test_schedule_only_registers_no_attendance_or_retention_handlers(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'only.db').as_posix()}")
    sessions = create_session_factory(engine)
    try:
        handlers = ApplicationJobHandlers(
            settings=Settings(feature_profile="schedule_only"),
            session_factory=sessions,
            schedule=FakeCoordinator(),  # type: ignore[arg-type]
            jobs=JobQueue(sessions, worker_id="test"),
            outbox=OutboxQueue(sessions),
            clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
        )

        assert set(handlers.mapping) == {
            "schedule.sync",
            "schedule.evening",
            "runtime.heartbeat",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attendance_job_enqueues_one_durable_card(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    now = datetime(2026, 9, 1, 8, 59, tzinfo=UTC)
    try:
        async with sessions() as session, session.begin():
            snapshot = ScheduleSnapshot(
                source="test",
                group_key="SZS",
                week_start=date(2026, 8, 31),
                fetched_at=now,
                content_hash="a" * 64,
            )
            session.add(snapshot)
            await session.flush()
            lesson = Lesson(
                snapshot_id=snapshot.id,
                lesson_key="one",
                day=date(2026, 9, 1),
                starts_at=time(12),
                ends_at=time(13, 30),
                subject="ЖБК",
                room="407",
            )
            session.add(lesson)
            await session.flush()
            lesson_id = lesson.id

        settings = Settings(
            feature_profile="full",
            target_chat_id=-1001,
            schedule_topic_id=42,
            headman_user_id=10,
        )
        handlers = ApplicationJobHandlers(
            settings=settings,
            session_factory=sessions,
            schedule=FakeCoordinator(),  # type: ignore[arg-type]
            jobs=JobQueue(sessions, worker_id="test"),
            outbox=OutboxQueue(sessions),
            clock=lambda: now,
        )
        await handlers.attendance_open(_job("attendance.open", {"lesson_id": lesson_id}))
        await handlers.attendance_open(_job("attendance.open", {"lesson_id": lesson_id}))

        async with sessions() as session:
            rows = tuple(await session.scalars(select(OutboxMessage)))
            assert len(rows) == 1
            assert rows[0].kind == "telegram.send_message"
            assert rows[0].payload["_attendance"]["lesson_id"] == lesson_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_maintenance_job_records_runtime_heartbeat(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'heartbeat.db').as_posix()}"
    )
    sessions = create_session_factory(engine)
    await create_schema(engine)
    now = datetime(2026, 9, 1, 9, tzinfo=UTC)
    try:
        handlers = ApplicationJobHandlers(
            settings=Settings(feature_profile="full"),
            session_factory=sessions,
            schedule=FakeCoordinator(),  # type: ignore[arg-type]
            jobs=JobQueue(sessions, worker_id="test"),
            outbox=OutboxQueue(sessions),
            clock=lambda: now,
        )

        await handlers.maintenance_minute(_job("maintenance.minute", {}))

        async with sessions() as session:
            heartbeat = await session.get(SystemSetting, "runtime.heartbeat")
            assert heartbeat is not None
            assert heartbeat.value == {"at": now.isoformat()}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_daily_retention_job_redacts_expired_update_payload(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'retention-job.db').as_posix()}"
    )
    sessions = create_session_factory(engine)
    await create_schema(engine)
    now = datetime(2026, 9, 1, 9, tzinfo=UTC)
    old = now - timedelta(days=31)
    try:
        async with sessions() as session, session.begin():
            session.add(
                InboxUpdate(
                    update_id=900,
                    payload={"message": "private"},
                    received_at=old,
                    available_at=old,
                    processed_at=old,
                )
            )
            session.add(
                ProcessedUpdate(
                    update_id=900,
                    handler="test",
                    outcome="processed",
                    processed_at=old,
                )
            )
        handlers = ApplicationJobHandlers(
            settings=Settings(feature_profile="full"),
            session_factory=sessions,
            schedule=FakeCoordinator(),  # type: ignore[arg-type]
            jobs=JobQueue(sessions, worker_id="test"),
            outbox=OutboxQueue(sessions),
            clock=lambda: now,
        )

        await handlers.maintenance_retention(_job("maintenance.retention", {}))

        async with sessions() as session:
            inbox = await session.scalar(
                select(InboxUpdate).where(InboxUpdate.update_id == 900)
            )
        assert inbox is not None and inbox.payload == {}
    finally:
        await engine.dispose()
