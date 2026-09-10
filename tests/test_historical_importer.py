from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.archive import (
    ALBUM_IMPORT_LIMITATION,
    TOPIC_IMPORT_LIMITATION,
    ExportChatMismatchError,
    ExportFingerprintMismatchError,
    TelegramDesktopImporter,
    inspect_telegram_desktop_export,
    telegram_export_fingerprint,
)
from szs_hub.archive.importer import _PersistResult
from szs_hub.archive.telegram_desktop import ImportedMessage
from szs_hub.storage import (
    File,
    ImportRun,
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

RAW_CHAT_ID = 1_234_567_890
TARGET_CHAT_ID = -1_001_234_567_890


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def export_payload(*, raw_chat_id: int = RAW_CHAT_ID) -> dict[str, object]:
    return {
        "id": raw_chat_id,
        "name": "СЗС test",
        "type": "private_supergroup",
        "messages": [
            {
                "id": 10,
                "type": "message",
                "date_unixtime": "1788264000",
                "edited_unixtime": "1788264030",
                "from": "Алексей Иванов",
                "from_id": "user777",
                "text": "Методичка по геодезии",
                "file": "files/manual.pdf",
                "file_name": "manual.pdf",
                "mime_type": "application/pdf",
                "file_size": 1234,
            },
            {
                "id": 11,
                "type": "message",
                "date_unixtime": "1788264060",
                "from": "Канал группы",
                "from_id": "channel777",
                "text": "Фото задания",
                "photo": "photos/missing.jpg",
                "media_type": "photo",
                "reply_to_message_id": 10,
            },
        ],
    }


def write_export(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_only_preflight_reports_identity_fingerprint_media_and_limitations(
    tmp_path: Path,
) -> None:
    export_path = tmp_path / "result.json"
    write_export(export_path, export_payload())

    preflight = inspect_telegram_desktop_export(export_path)

    assert preflight.source_fingerprint == telegram_export_fingerprint(export_path)
    assert preflight.plan.report.raw_chat_id == RAW_CHAT_ID
    assert preflight.plan.report.normalized_chat_id == TARGET_CHAT_ID
    assert preflight.plan.report.limitations == (
        TOPIC_IMPORT_LIMITATION,
        ALBUM_IMPORT_LIMITATION,
    )
    assert [
        (item.message_id, item.relative_path, item.reason) for item in preflight.missing_media
    ] == [
        (10, "files/manual.pdf", "missing"),
        (11, "photos/missing.jpg", "missing"),
    ]


@pytest.mark.asyncio
async def test_import_persists_history_and_is_idempotent(tmp_path: Path) -> None:
    export_path = tmp_path / "result.json"
    media = tmp_path / "files" / "manual.pdf"
    media.parent.mkdir()
    media.write_bytes(b"owner-provided fixture")
    write_export(export_path, export_payload())

    engine = create_database_engine(sqlite_url(tmp_path / "archive.sqlite3"))
    factory = create_session_factory(engine)
    importer = TelegramDesktopImporter(target_chat_id=TARGET_CHAT_ID, batch_size=1)
    try:
        await create_schema(engine)
        report = await importer.run(factory, export_path)

        assert report.status == "succeeded"
        assert report.raw_chat_id == RAW_CHAT_ID
        assert report.chat_type == "private_supergroup"
        assert report.normalized_chat_id == TARGET_CHAT_ID
        assert report.messages_processed == 2
        assert report.messages_created == 2
        assert report.users_created == 1
        assert report.topics_created == 0
        assert report.media_groups_created == 0
        assert report.files_created == 2
        assert [
            (item.message_id, item.relative_path, item.reason) for item in report.missing_media
        ] == [(11, "photos/missing.jpg", "missing")]
        assert report.source_fingerprint == telegram_export_fingerprint(export_path)

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 1
            assert await session.scalar(select(func.count()).select_from(Topic)) == 0
            assert await session.scalar(select(func.count()).select_from(MediaGroup)) == 0
            assert await session.scalar(select(func.count()).select_from(Message)) == 2
            assert await session.scalar(select(func.count()).select_from(File)) == 2
            assert await session.scalar(select(func.count()).select_from(SearchDocument)) == 2
            stored = await session.scalar(select(Message).where(Message.message_id == 10))
            channel_message = await session.scalar(
                select(Message).where(Message.message_id == 11)
            )
            stored_file = await session.scalar(
                select(File).join(Message).where(Message.message_id == 10)
            )
            assert stored is not None
            assert channel_message is not None
            assert channel_message.sender_display_name == "Канал группы"
            assert channel_message.sender_user_id is None
            assert stored.source == "telegram_export"
            assert stored.caption == "Методичка по геодезии"
            assert stored.edited_at is not None
            assert stored_file is not None
            assert stored_file.telegram_file_id.startswith("telegram-export-local:")
            assert stored_file.telegram_file_unique_id == stored_file.telegram_file_id
            assert stored_file.storage_path == "telegram-export-relative:files/manual.pdf"
            assert stored_file.sha256 is None
            hits = await SearchRepository(session).search("геодезии")
            assert [hit.source_id for hit in hits] == [stored.id]
            assert hits[0].metadata is not None
            assert "storage_path" not in hits[0].metadata

        repeated = await importer.run(factory, export_path)
        assert repeated.import_run_id == report.import_run_id
        assert repeated.already_completed is True
        assert repeated.messages_created == 2
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ImportRun)) == 1
            assert await session.scalar(select(func.count()).select_from(Message)) == 2
            assert await session.scalar(select(func.count()).select_from(File)) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wrong_chat_is_rejected_before_import_run_is_created(tmp_path: Path) -> None:
    export_path = tmp_path / "result.json"
    write_export(export_path, export_payload(raw_chat_id=999))
    engine = create_database_engine(sqlite_url(tmp_path / "wrong-chat.sqlite3"))
    factory = create_session_factory(engine)
    try:
        await create_schema(engine)
        with pytest.raises(ExportChatMismatchError, match="configured target"):
            await TelegramDesktopImporter(target_chat_id=TARGET_CHAT_ID).run(factory, export_path)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ImportRun)) == 0
            assert await session.scalar(select(func.count()).select_from(Message)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_changed_fingerprint_is_rejected_before_import_run_is_created(
    tmp_path: Path,
) -> None:
    export_path = tmp_path / "result.json"
    write_export(export_path, export_payload())
    engine = create_database_engine(sqlite_url(tmp_path / "wrong-fingerprint.sqlite3"))
    factory = create_session_factory(engine)
    try:
        await create_schema(engine)
        with pytest.raises(ExportFingerprintMismatchError, match="reviewed plan"):
            await TelegramDesktopImporter(target_chat_id=TARGET_CHAT_ID).run(
                factory,
                export_path,
                expected_fingerprint="sha256:" + "0" * 64,
            )
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ImportRun)) == 0
            assert await session.scalar(select(func.count()).select_from(Message)) == 0
    finally:
        await engine.dispose()


class _FailOnSecondMessage(TelegramDesktopImporter):
    async def _persist_message(
        self,
        session: AsyncSession,
        imported: ImportedMessage,
        *,
        export_root: Path,
    ) -> _PersistResult:
        if imported.message_id == 11:
            raise RuntimeError("secret source path must not be persisted")
        return await super()._persist_message(session, imported, export_root=export_root)


@pytest.mark.asyncio
async def test_failed_batch_resumes_from_last_committed_cursor(tmp_path: Path) -> None:
    export_path = tmp_path / "result.json"
    write_export(export_path, export_payload())
    engine = create_database_engine(sqlite_url(tmp_path / "resume.sqlite3"))
    factory = create_session_factory(engine)
    try:
        await create_schema(engine)
        failing = _FailOnSecondMessage(target_chat_id=TARGET_CHAT_ID, batch_size=1)
        with pytest.raises(RuntimeError, match="secret source"):
            await failing.run(factory, export_path)

        async with factory() as session:
            run = await session.scalar(select(ImportRun))
            assert run is not None
            assert run.status == "failed"
            assert run.stats is not None and run.stats["next_index"] == 1
            assert run.error == "RuntimeError: import batch failed"
            assert await session.scalar(select(func.count()).select_from(Message)) == 1

        resumed = await TelegramDesktopImporter(
            target_chat_id=TARGET_CHAT_ID,
            batch_size=1,
        ).run(factory, export_path)
        assert resumed.status == "succeeded"
        assert resumed.resumed_from == 1
        assert resumed.messages_processed == 2
        assert resumed.messages_created == 2
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ImportRun)) == 1
            assert await session.scalar(select(func.count()).select_from(Message)) == 2
    finally:
        await engine.dispose()
