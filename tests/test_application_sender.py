from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from szs_hub.config import Settings
from szs_hub.jobs.queue import OutboxClaim
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import (
    AttendanceSession,
    Lesson,
    Material,
    Message,
    ScheduleSnapshot,
    SystemSetting,
)
from szs_hub.telegram.access import Membership, MemberStatus
from szs_hub.telegram.application_sender import ApplicationTelegramSender
from szs_hub.telegram.sender import TelegramCopyResult


class FakeSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, message: OutboxClaim) -> int:
        self.calls += 1
        return 77


class FakeAlbumSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, message: OutboxClaim) -> TelegramCopyResult:
        self.calls += 1
        return TelegramCopyResult((301, 302))


class FakeMembershipGateway:
    def __init__(self, membership: Membership | None) -> None:
        self.membership = membership

    async def current_membership(self, *, chat_id: int, user_id: int) -> Membership | None:
        return self.membership


@pytest.mark.asyncio
async def test_schedule_only_sender_accepts_only_static_schedule_envelopes(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'only.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    fake = FakeSender()
    sender = ApplicationTelegramSender(
        sender=fake,  # type: ignore[arg-type]
        session_factory=sessions,
        settings=Settings(target_chat_id=-1001, schedule_topic_id=42),
    )
    now = datetime(2026, 9, 1, tzinfo=UTC)
    schedule = OutboxClaim(
        id=1,
        kind="telegram.send_message",
        idempotency_key="schedule:1",
        chat_id=-1001,
        payload={
            "text": "Расписание",
            "message_thread_id": 42,
            "_state_key": "telegram.schedule_card.2026-09-02",
        },
        attempt=1,
        max_attempts=5,
        leased_until=now + timedelta(minutes=2),
    )
    attendance = OutboxClaim(
        id=2,
        kind="telegram.send_message",
        idempotency_key="attendance:1",
        chat_id=-1001,
        payload={"text": "Я на паре", "message_thread_id": 42, "_attendance": {}},
        attempt=1,
        max_attempts=5,
        leased_until=now + timedelta(minutes=2),
    )
    try:
        assert await sender.send(schedule) == 77
        with pytest.raises(PermissionError, match="non-schedule"):
            await sender.send(attendance)
        assert fake.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_send_opens_attendance_and_retry_does_not_resend(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'sender.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    starts = datetime(2026, 9, 1, 9, tzinfo=UTC)
    try:
        async with sessions() as session, session.begin():
            snapshot = ScheduleSnapshot(
                source="test",
                group_key="SZS",
                week_start=date(2026, 8, 31),
                fetched_at=starts - timedelta(hours=1),
                content_hash="a" * 64,
            )
            session.add(snapshot)
            await session.flush()
            lesson = Lesson(
                snapshot_id=snapshot.id,
                lesson_key="one",
                day=starts.date(),
                starts_at=time(9),
                ends_at=time(10, 30),
                subject="ЖБК",
            )
            session.add(lesson)
            await session.flush()
            lesson_id = lesson.id

        claim = OutboxClaim(
            id=1,
            kind="telegram.send_message",
            idempotency_key="attendance:1",
            chat_id=-1001,
            payload={
                "text": "card",
                "_attendance": {"lesson_id": lesson_id, "starts_at": starts.isoformat()},
                "_state_key": "telegram.attendance.1",
            },
            attempt=1,
            max_attempts=5,
            leased_until=starts + timedelta(minutes=2),
        )
        fake = FakeSender()
        sender = ApplicationTelegramSender(
            sender=fake,  # type: ignore[arg-type]
            session_factory=sessions,
            settings=Settings(feature_profile="full"),
        )
        assert await sender.send(claim) == 77
        assert await sender.send(claim) == 77
        assert fake.calls == 1
        async with sessions() as session:
            attendance = await session.scalar(select(AttendanceSession))
            assert attendance is not None and attendance.message_id == 77
            state = await session.get(SystemSetting, "telegram.attendance.1")
            assert state is not None and state.value["message_id"] == 77  # type: ignore[index]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_private_send_requires_current_headman_membership(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'private.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    fake = FakeSender()
    sender = ApplicationTelegramSender(
        sender=fake,  # type: ignore[arg-type]
        session_factory=sessions,
        settings=Settings(feature_profile="full", target_chat_id=-1001, headman_user_id=10),
        membership_gateway=FakeMembershipGateway(Membership(MemberStatus.LEFT)),
    )
    claim = OutboxClaim(
        id=3,
        kind="telegram.send_message",
        idempotency_key="private:10",
        chat_id=10,
        payload={"text": "private"},
        attempt=1,
        max_attempts=5,
        leased_until=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=2),
    )
    try:
        with pytest.raises(PermissionError, match="not a current group member"):
            await sender.send(claim)
        assert fake.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_send_confirms_full_material_copy_without_source_deletion(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'material.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        async with sessions() as session, session.begin():
            source = Message(
                chat_id=-1001,
                message_id=10,
                source="bot_api",
                message_type="photo",
                sent_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
            session.add(source)
            await session.flush()
            material = Material(
                source_message_id=source.id,
                destination_chat_id=-1001,
                title="Альбом",
                material_type="album",
                confidence=0.97,
                workflow_state="copy_pending",
                evidence={"source_deleted": False},
            )
            session.add(material)
            await session.flush()
            material_id = material.id

        claim = OutboxClaim(
            id=2,
            kind="telegram.copy_messages",
            idempotency_key="material:1",
            chat_id=-1001,
            payload={
                "from_chat_id": -1001,
                "message_ids": [10, 11],
                "_material_copy": {
                    "material_id": material_id,
                    "expected_count": 2,
                    "source_message_ids": [10, 11],
                },
            },
            attempt=1,
            max_attempts=5,
            leased_until=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=2),
        )
        fake = FakeAlbumSender()
        sender = ApplicationTelegramSender(
            sender=fake,  # type: ignore[arg-type]
            session_factory=sessions,
            settings=Settings(feature_profile="full", material_auto_delete_source=True),
        )

        assert await sender.send(claim) == 301
        assert await sender.send(claim) == 301
        assert fake.calls == 1
        async with sessions() as session:
            stored = await session.get(Material, material_id)
            assert stored is not None
            assert stored.workflow_state == "completed"
            assert stored.destination_message_id == 301
            assert stored.evidence is not None
            assert stored.evidence["destination_message_ids"] == [301, 302]
            assert stored.evidence["copied_source_message_ids"] == [10, 11]
            assert stored.evidence["source_deleted"] is False
            assert stored.evidence["source_deletion_enabled"] is False
    finally:
        await engine.dispose()
