from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.types import Message as TelegramMessage
from sqlalchemy import func, select

from szs_hub.archive import LiveArchive, LiveArchiveDisposition
from szs_hub.storage import (
    File,
    MediaGroup,
    Message,
    SearchDocument,
    SearchRepository,
    Topic,
    User,
    create_database_engine,
    create_schema,
    create_session_factory,
)

TARGET_CHAT_ID = -100_987_654_321
START = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def telegram_message(
    message_id: int,
    *,
    date: datetime = START,
    chat_id: int = TARGET_CHAT_ID,
    **values: object,
) -> TelegramMessage:
    payload: dict[str, object] = {
        "message_id": message_id,
        "date": date,
        "chat": {
            "id": chat_id,
            "type": "supergroup",
            "title": "СЗС",
            "is_forum": True,
        },
        "from": {
            "id": 777,
            "is_bot": False,
            "first_name": "Алексей",
            "last_name": "Иванов",
            "username": "student",
            "language_code": "ru",
        },
        **values,
    }
    return TelegramMessage.model_validate(payload)


@pytest.mark.asyncio
async def test_text_reply_topic_forward_and_file_are_persisted_privately(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "live.sqlite3"))
    factory = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    try:
        await create_schema(engine)
        message = telegram_message(
            42,
            message_thread_id=17,
            text="Методичка по геодезии",
            entities=[{"type": "bold", "offset": 0, "length": 9}],
            reply_to_message={
                "message_id": 41,
                "date": START - timedelta(minutes=1),
                "chat": {"id": TARGET_CHAT_ID, "type": "supergroup", "title": "СЗС"},
                "text": "Есть файл?",
            },
            document={
                "file_id": "current-file-id",
                "file_unique_id": "stable-file-id",
                "file_name": "geodesy.pdf",
                "mime_type": "application/pdf",
                "file_size": 12_345,
            },
            forward_origin={
                "type": "user",
                "date": START - timedelta(days=1),
                "sender_user": {
                    "id": 999,
                    "is_bot": False,
                    "first_name": "Private origin",
                },
            },
        )

        async with factory() as session, session.begin():
            result = await archive.ingest(session, message)

        assert result.disposition is LiveArchiveDisposition.CREATED
        assert result.stored_file_count == 1
        async with factory() as session:
            stored = await session.scalar(select(Message))
            sender = await session.scalar(select(User))
            topic = await session.scalar(select(Topic))
            file = await session.scalar(select(File))
            document = await session.scalar(select(SearchDocument))

            assert stored is not None
            assert stored.text == "Методичка по геодезии"
            assert stored.message_type == "document"
            assert stored.reply_to_message_id == 41
            assert stored.sender_display_name == "Алексей Иванов"
            assert stored.entities == [{"type": "bold", "offset": 0, "length": 9}]
            assert stored.forward_metadata == {
                "origin_type": "user",
                "forwarded_at": "2026-08-29T09:00:00+00:00",
            }
            assert "999" not in str(stored.forward_metadata)
            assert "Private origin" not in str(stored.forward_metadata)
            assert sender is not None and sender.telegram_user_id == 777
            assert sender.display_name == "Алексей Иванов"
            assert topic is not None and topic.thread_id == 17
            assert topic.name == "Topic 17"
            assert file is not None
            assert file.telegram_file_id == "current-file-id"
            assert file.telegram_file_unique_id == "stable-file-id"
            assert file.storage_path is None
            assert document is not None and "geodesy.pdf" in document.content
            hits = await SearchRepository(session).search("геодезии")
            assert [hit.source_id for hit in hits] == [
                stored.id
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_and_retry_update_one_message_file_and_search_projection(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "edit.sqlite3"))
    factory = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    try:
        await create_schema(engine)
        original = telegram_message(
            50,
            text="Старый черновик",
            document={
                "file_id": "file-id-v1",
                "file_unique_id": "stable-document",
                "file_name": "draft.pdf",
                "mime_type": "application/pdf",
            },
        )
        edited = telegram_message(
            50,
            text="Новая методичка",
            edit_date=int((START + timedelta(minutes=5)).timestamp()),
            document={
                "file_id": "file-id-v2",
                "file_unique_id": "stable-document",
                "file_name": "method.pdf",
                "mime_type": "application/pdf",
            },
        )

        async with factory() as session, session.begin():
            created = await archive.ingest(session, original)
            assert created.disposition is LiveArchiveDisposition.CREATED
        async with factory() as session, session.begin():
            extracted = await session.scalar(select(File))
            assert extracted is not None
            extracted.extracted_text = "Редкий термин нивелирныйход"
            extracted.extraction_status = "ok"
            extracted.extraction_version = "test-v1"
            extracted.extracted_at = START + timedelta(minutes=1)
        async with factory() as session, session.begin():
            updated = await archive.ingest(session, edited)
            retried = await archive.ingest(session, edited)
            assert updated.disposition is LiveArchiveDisposition.UPDATED
            assert retried.disposition is LiveArchiveDisposition.UPDATED

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Message)) == 1
            assert await session.scalar(select(func.count()).select_from(File)) == 1
            assert await session.scalar(select(func.count()).select_from(SearchDocument)) == 1
            stored = await session.scalar(select(Message))
            file = await session.scalar(select(File))
            assert stored is not None and stored.text == "Новая методичка"
            assert stored.edited_at == START + timedelta(minutes=5)
            assert file is not None and file.telegram_file_id == "file-id-v2"
            assert file.file_name == "method.pdf"
            assert await SearchRepository(session).search("черновик") == ()
            hits = await SearchRepository(session).search("методичка")
            assert [hit.source_id for hit in hits] == [
                stored.id
            ]
            extracted_hits = await SearchRepository(session).search("нивелирныйход")
            assert [hit.source_id for hit in extracted_hits] == [stored.id]
            assert file.extracted_text == "Редкий термин нивелирныйход"

        # A delayed retry of the unedited update must not undo a newer edit.
        async with factory() as session, session.begin():
            await archive.ingest(session, original)
        async with factory() as session:
            stored = await session.scalar(select(Message))
            file = await session.scalar(select(File))
            assert stored is not None and stored.text == "Новая методичка"
            assert file is not None and file.telegram_file_id == "file-id-v2"
            assert await SearchRepository(session).search("черновик") == ()
            assert len(await SearchRepository(session).search("методичка")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_album_members_share_one_logical_media_group(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "album.sqlite3"))
    factory = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    later_photo = telegram_message(
        61,
        date=START + timedelta(seconds=1),
        media_group_id="album-2026-08",
        caption="Фото задания",
        photo=[
            {"file_id": "small-b", "file_unique_id": "small-unique-b", "width": 90, "height": 90},
            {
                "file_id": "large-b",
                "file_unique_id": "large-unique-b",
                "width": 1280,
                "height": 960,
                "file_size": 99_000,
            },
        ],
    )
    earlier_photo = telegram_message(
        60,
        media_group_id="album-2026-08",
        photo=[
            {
                "file_id": "large-a",
                "file_unique_id": "large-unique-a",
                "width": 1280,
                "height": 960,
                "file_size": 88_000,
            }
        ],
    )
    try:
        await create_schema(engine)
        async with factory() as session, session.begin():
            await archive.ingest(session, later_photo)
            await archive.ingest(session, earlier_photo)

        async with factory() as session:
            groups = list(await session.scalars(select(MediaGroup)))
            messages = list(await session.scalars(select(Message).order_by(Message.message_id)))
            files = list(await session.scalars(select(File).order_by(File.message_id)))
            assert len(groups) == 1
            assert groups[0].first_message_at == START
            assert groups[0].finalized_at is None
            assert len(messages) == 2
            assert {message.media_group_id for message in messages} == {groups[0].id}
            assert {file.telegram_file_id for file in files} == {"large-a", "large-b"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outside_chat_is_ignored_without_database_side_effects(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "ignored.sqlite3"))
    factory = create_session_factory(engine)
    archive = LiveArchive(target_chat_id=TARGET_CHAT_ID)
    try:
        await create_schema(engine)
        async with factory() as session, session.begin():
            result = await archive.ingest(
                session,
                telegram_message(70, chat_id=-100_111_222_333, text="Do not retain"),
            )
        assert result.disposition is LiveArchiveDisposition.IGNORED_CHAT
        assert result.stored_message_id is None
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Message)) == 0
            assert await session.scalar(select(func.count()).select_from(User)) == 0
            assert await session.scalar(select(func.count()).select_from(SearchDocument)) == 0
    finally:
        await engine.dispose()
