from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Message as TelegramMessage
from aiogram.types import Update
from sqlalchemy import func, select

from szs_hub.archive.live import LiveArchive
from szs_hub.attendance.service import AttendanceService
from szs_hub.config import Settings
from szs_hub.jobs.queue import JobClaim, JobQueue, OutboxQueue
from szs_hub.jobs.worker import JobWorker
from szs_hub.materials.automation import (
    MATERIAL_ASSESS_KIND,
    MaterialJobHandlers,
    admit_material_message,
)
from szs_hub.materials.processing import MATERIAL_EXTRACT_KIND
from szs_hub.schedule.coordinator import ScheduleCoordinator
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import Job, Material, MediaGroup, OutboxMessage
from szs_hub.storage.models import Message as StoredMessage
from szs_hub.telegram.handlers import HandlerServices, build_router
from szs_hub.telegram.membership_store import TelegramMembershipStore

TARGET_CHAT_ID = -100_987_654_321
START = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def _telegram_message(message_id: int, **values: object) -> TelegramMessage:
    return TelegramMessage.model_validate(
        {
            "message_id": message_id,
            "date": values.pop("date", START),
            "chat": {
                "id": TARGET_CHAT_ID,
                "type": "supergroup",
                "title": "СЗС",
                "is_forum": True,
            },
            "from": {"id": 101, "is_bot": False, "first_name": "Student"},
            **values,
        }
    )


def _settings(**values: object) -> Settings:
    configured: dict[str, object] = {
        "target_chat_id": TARGET_CHAT_ID,
        "materials_topic_id": 90,
        "headman_user_id": 202,
        "material_extraction_enabled": True,
    }
    configured.update(values)
    return Settings(**configured)


def _claim(payload: dict[str, object]) -> JobClaim:
    return JobClaim(
        id=1,
        kind=MATERIAL_ASSESS_KIND,
        idempotency_key="material:test",
        payload=payload,
        attempt=1,
        max_attempts=8,
        leased_until=START + timedelta(minutes=5),
    )


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_album_admission_deduplicates_each_observation_and_keeps_late_followup(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_url(tmp_path / "admission.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        queue = JobQueue(sessions, worker_id="admission", clock=lambda: START)
        first = _telegram_message(
            10,
            media_group_id="album-one",
            photo=[{"file_id": "a", "file_unique_id": "a", "width": 100, "height": 100}],
        )
        second = _telegram_message(
            11,
            date=START + timedelta(seconds=1),
            media_group_id="album-one",
            photo=[{"file_id": "b", "file_unique_id": "b", "width": 100, "height": 100}],
        )

        admitted = await admit_material_message(queue, first)
        extended = await admit_material_message(queue, second)
        duplicate = await admit_material_message(queue, second)

        assert admitted is not None and admitted.created
        assert extended is not None and extended.created
        assert duplicate is not None and not duplicate.created
        assert duplicate.id == extended.id
        async with sessions() as session:
            jobs = list(await session.scalars(select(Job)))
            assert len(jobs) == 2
            assert {job.kind for job in jobs} == {MATERIAL_ASSESS_KIND}
            assert {job.run_at for job in jobs} == {
                START + timedelta(seconds=3),
                START + timedelta(seconds=4),
            }
            assert {tuple(sorted(job.payload.items())) for job in jobs} == {
                (
                    ("chat_id", TARGET_CHAT_ID),
                    ("media_group_id", "album-one"),
                )
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_group_handler_archives_before_admitting_material_job(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "handler.sqlite3"))
    sessions = create_session_factory(engine)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
    try:
        await create_schema(engine)
        settings = _settings()
        queue = JobQueue(sessions, worker_id="updates", clock=lambda: START)
        services = HandlerServices(
            settings=settings,
            sessions=sessions,
            live_archive=LiveArchive(target_chat_id=TARGET_CHAT_ID),
            memberships=TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID),
            attendance=AttendanceService,
            jobs=queue,
            schedule=cast(ScheduleCoordinator, object()),
            assistant=None,
            conversations=None,
        )
        dispatcher = Dispatcher(disable_fsm=True)
        dispatcher.include_router(build_router(services))
        message = _telegram_message(
            12,
            caption="Методичка",
            document={
                "file_id": "handler-file",
                "file_unique_id": "handler-unique",
                "file_name": "handler.pdf",
            },
        )

        await dispatcher.feed_update(bot, Update(update_id=1, message=message))

        async with sessions() as session:
            jobs = tuple(await session.scalars(select(Job).order_by(Job.run_at, Job.id)))
            assert [job.kind for job in jobs] == [
                MATERIAL_EXTRACT_KIND,
                MATERIAL_ASSESS_KIND,
            ]
            assert jobs[0].run_at == START
            assert jobs[1].run_at == START + timedelta(seconds=3)
            assert await session.scalar(select(func.count()).select_from(Material)) == 0
            # Both file processing and assessment are asynchronous; archive is durable first.
            assert await session.scalar(select(func.count()).select_from(StoredMessage)) == 1
    finally:
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_group_handler_keeps_extraction_disabled_by_default(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "handler-disabled.sqlite3"))
    sessions = create_session_factory(engine)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
    try:
        await create_schema(engine)
        services = HandlerServices(
            settings=_settings(material_extraction_enabled=False),
            sessions=sessions,
            live_archive=LiveArchive(target_chat_id=TARGET_CHAT_ID),
            memberships=TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID),
            attendance=AttendanceService,
            jobs=JobQueue(sessions, worker_id="updates", clock=lambda: START),
            schedule=cast(ScheduleCoordinator, object()),
            assistant=None,
            conversations=None,
        )
        dispatcher = Dispatcher(disable_fsm=True)
        dispatcher.include_router(build_router(services))
        message = _telegram_message(
            13,
            caption="Методичка",
            document={
                "file_id": "disabled-file",
                "file_unique_id": "disabled-unique",
                "file_name": "disabled.pdf",
            },
        )

        await dispatcher.feed_update(bot, Update(update_id=2, message=message))

        async with sessions() as session:
            jobs = tuple(await session.scalars(select(Job).order_by(Job.id)))
        assert [job.kind for job in jobs] == [MATERIAL_ASSESS_KIND]
    finally:
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_job_workers_claim_only_their_registered_kinds(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "isolated-workers.sqlite3"))
    sessions = create_session_factory(engine)
    seen: list[str] = []

    async def record(job: JobClaim) -> None:
        seen.append(job.kind)

    try:
        await create_schema(engine)
        admission = JobQueue(sessions, worker_id="admission", clock=lambda: START)
        material = await admission.enqueue(
            kind=MATERIAL_ASSESS_KIND,
            idempotency_key="material:first",
            payload={"chat_id": TARGET_CHAT_ID, "message_id": 1},
        )
        schedule = await admission.enqueue(
            kind="schedule.sync",
            idempotency_key="schedule:second",
            payload={},
        )
        schedule_worker = JobWorker(
            JobQueue(sessions, worker_id="schedule", clock=lambda: START),
            {"schedule.sync": record},
        )
        material_worker = JobWorker(
            JobQueue(sessions, worker_id="material", clock=lambda: START),
            {MATERIAL_ASSESS_KIND: record},
        )

        assert await schedule_worker.run_once()
        assert seen == ["schedule.sync"]
        async with sessions() as session:
            assert (await session.get(Job, material.id)).status == "pending"  # type: ignore[union-attr]
            assert (await session.get(Job, schedule.id)).status == "succeeded"  # type: ignore[union-attr]

        assert await material_worker.run_once()
        assert seen == ["schedule.sync", MATERIAL_ASSESS_KIND]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_auto_copy_persists_evidence_and_never_requests_source_deletion(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_url(tmp_path / "auto.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        message = _telegram_message(
            20,
            message_thread_id=33,
            caption="Методичка по геодезии",
            document={
                "file_id": "file",
                "file_unique_id": "unique",
                "file_name": "geodesy.pdf",
                "mime_type": "application/pdf",
            },
        )
        async with sessions() as session, session.begin():
            await LiveArchive(target_chat_id=TARGET_CHAT_ID).ingest(session, message)

        outbox = OutboxQueue(sessions, clock=lambda: START)
        handlers = MaterialJobHandlers(
            settings=_settings(material_auto_delete_source=True),
            session_factory=sessions,
            outbox=outbox,
            clock=lambda: START,
        )
        await handlers.assess(_claim({"chat_id": TARGET_CHAT_ID, "message_id": 20}))

        async with sessions() as session:
            material = await session.scalar(select(Material))
            envelope = await session.scalar(select(OutboxMessage))
            assert material is not None
            assert material.confidence == pytest.approx(0.97)
            assert material.workflow_state == "copy_pending"
            assert material.classifier_version == "transparent-rules-v1"
            assert material.evidence is not None
            assert material.evidence["source_deletion_enabled"] is False
            assert material.evidence["source_deleted"] is False
            assert envelope is not None
            assert envelope.kind == "telegram.copy_message"
            assert envelope.chat_id == TARGET_CHAT_ID
            assert envelope.payload["message_id"] == 20
            assert envelope.payload["message_thread_id"] == 90
            assert envelope.payload["_material_copy"] == {
                "material_id": material.id,
                "expected_count": 1,
                "source_message_ids": [20],
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_album_is_assessed_and_copied_as_one_logical_unit(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "album.sqlite3"))
    sessions = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    try:
        await create_schema(engine)
        messages = (
            _telegram_message(
                30,
                media_group_id="academic-album",
                photo=[
                    {"file_id": "a", "file_unique_id": "a", "width": 100, "height": 100}
                ],
            ),
            _telegram_message(
                31,
                date=START + timedelta(seconds=1),
                media_group_id="academic-album",
                caption="Фото задания с доски",
                photo=[
                    {"file_id": "b", "file_unique_id": "b", "width": 100, "height": 100}
                ],
            ),
        )
        async with sessions() as session, session.begin():
            for message in messages:
                await archive.ingest(session, message)

        handlers = MaterialJobHandlers(
            settings=_settings(),
            session_factory=sessions,
            outbox=OutboxQueue(sessions, clock=lambda: START),
            clock=lambda: START + timedelta(seconds=4),
        )
        await handlers.assess(
            _claim({"chat_id": TARGET_CHAT_ID, "media_group_id": "academic-album"})
        )

        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Material)) == 1
            material = await session.scalar(select(Material))
            group = await session.scalar(select(MediaGroup))
            envelope = await session.scalar(select(OutboxMessage))
            assert material is not None and material.material_type == "album"
            assert material.confidence == pytest.approx(0.97)
            assert group is not None and group.finalized_at == START + timedelta(seconds=4)
            assert envelope is not None and envelope.kind == "telegram.copy_messages"
            assert envelope.payload["message_ids"] == [30, 31]
            assert envelope.payload["_material_copy"] == {
                "material_id": material.id,
                "expected_count": 2,
                "source_message_ids": [30, 31],
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_late_album_member_copies_only_the_new_message(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "late-album.sqlite3"))
    sessions = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    handlers = MaterialJobHandlers(
        settings=_settings(),
        session_factory=sessions,
        outbox=OutboxQueue(sessions, clock=lambda: START),
        clock=lambda: START + timedelta(seconds=20),
    )
    try:
        await create_schema(engine)
        first = _telegram_message(
            50,
            media_group_id="late-album",
            caption="Задание с доски",
            photo=[{"file_id": "a", "file_unique_id": "a", "width": 100, "height": 100}],
        )
        second = _telegram_message(
            51,
            media_group_id="late-album",
            photo=[{"file_id": "b", "file_unique_id": "b", "width": 100, "height": 100}],
        )
        async with sessions() as session, session.begin():
            await archive.ingest(session, first)
            await archive.ingest(session, second)
        await handlers.assess(
            _claim({"chat_id": TARGET_CHAT_ID, "media_group_id": "late-album"})
        )

        async with sessions() as session, session.begin():
            material = await session.scalar(select(Material))
            envelope = await session.scalar(select(OutboxMessage))
            assert material is not None and envelope is not None
            material.destination_message_id = 500
            material.workflow_state = "completed"
            material.evidence = {
                **dict(material.evidence or {}),
                "copied_source_message_ids": [50, 51],
                "destination_message_ids": [500, 501],
            }
            envelope.status = "sent"
            envelope.telegram_message_id = 500
            envelope.sent_at = START + timedelta(seconds=5)

        late = _telegram_message(
            52,
            date=START + timedelta(seconds=15),
            media_group_id="late-album",
            photo=[{"file_id": "c", "file_unique_id": "c", "width": 100, "height": 100}],
        )
        async with sessions() as session, session.begin():
            await archive.ingest(session, late)
        await handlers.assess(
            _claim({"chat_id": TARGET_CHAT_ID, "media_group_id": "late-album"})
        )

        async with sessions() as session:
            envelopes = list(
                await session.scalars(select(OutboxMessage).order_by(OutboxMessage.id))
            )
            material = await session.scalar(select(Material))
            assert len(envelopes) == 2
            followup = envelopes[1]
            assert followup.kind == "telegram.copy_message"
            assert followup.payload["message_id"] == 52
            assert followup.payload["_material_copy"]["source_message_ids"] == [52]
            assert material is not None
            assert material.destination_message_id == 500
            assert material.workflow_state == "copy_pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_state", "expected_outbox", "expected_confidence"),
    [
        (None, "review", "telegram.send_message", 0.72),
        ("notes.pdf", "completed", None, 0.25),
    ],
)
async def test_review_is_quiet_and_low_confidence_is_left_untouched(
    tmp_path: Path,
    filename: str | None,
    expected_state: str,
    expected_outbox: str | None,
    expected_confidence: float,
) -> None:
    engine = create_database_engine(_url(tmp_path / f"route-{expected_state}.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        message = _telegram_message(
            40,
            caption="Методичка" if filename is None else None,
            document={
                "file_id": "route-file",
                "file_unique_id": "route-unique",
                "file_name": filename,
            },
        )
        async with sessions() as session, session.begin():
            await LiveArchive(target_chat_id=TARGET_CHAT_ID).ingest(session, message)
        handlers = MaterialJobHandlers(
            settings=_settings(),
            session_factory=sessions,
            outbox=OutboxQueue(sessions, clock=lambda: START),
            clock=lambda: START,
        )
        await handlers.assess(_claim({"chat_id": TARGET_CHAT_ID, "message_id": 40}))

        async with sessions() as session:
            material = await session.scalar(select(Material))
            envelope = await session.scalar(select(OutboxMessage))
            assert material is not None
            assert material.workflow_state == expected_state
            assert material.confidence == pytest.approx(expected_confidence)
            assert (envelope.kind if envelope else None) == expected_outbox
            if envelope is not None:
                assert envelope.chat_id == 202
                assert envelope.payload["disable_notification"] is True
    finally:
        await engine.dispose()
