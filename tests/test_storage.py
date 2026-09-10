from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import BigInteger, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from szs_hub.storage import (
    InboxUpdate,
    Message,
    SearchDocument,
    SearchRepository,
    SQLiteJournalMode,
    Topic,
    UnsafeSQLiteVersionError,
    User,
    build_safe_match_query,
    create_database_engine,
    create_schema,
    create_session_factory,
    enqueue_raw_update,
    record_processed_update,
    select_sqlite_journal_mode,
    sqlite_wal_is_safe,
)

EXPECTED_TABLES = {
    "admin_events",
    "ai_conversations",
    "ai_messages",
    "attendance_marks",
    "attendance_sessions",
    "files",
    "import_runs",
    "inbox_updates",
    "jobs",
    "lessons",
    "materials",
    "media_groups",
    "memberships",
    "messages",
    "outbox",
    "processed_updates",
    "schedule_changes",
    "schedule_snapshots",
    "search_documents",
    "search_documents_fts",
    "system_settings",
    "topics",
    "users",
}


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_wal_version_gate_covers_upstream_fix_and_backports() -> None:
    assert sqlite_wal_is_safe((3, 51, 3))
    assert sqlite_wal_is_safe((3, 52, 0))
    assert sqlite_wal_is_safe((3, 44, 6))
    assert sqlite_wal_is_safe((3, 50, 7))
    assert not sqlite_wal_is_safe((3, 51, 2))
    assert not sqlite_wal_is_safe((3, 50, 6))
    assert not sqlite_wal_is_safe((3, 6, 23), backport_confirmed=True)


def test_unsafe_wal_falls_back_locally_and_fails_closed_in_deployment() -> None:
    assert select_sqlite_journal_mode((3, 51, 2), environment="local") is SQLiteJournalMode.DELETE
    with pytest.raises(UnsafeSQLiteVersionError, match="not approved for WAL"):
        select_sqlite_journal_mode((3, 51, 2), environment="staging")
    with pytest.raises(UnsafeSQLiteVersionError, match="not approved for WAL"):
        select_sqlite_journal_mode((3, 51, 2), environment="production")


def test_explicitly_confirmed_vendor_backport_allows_wal() -> None:
    assert (
        select_sqlite_journal_mode((3, 49, 1), environment="production", backport_confirmed=True)
        is SQLiteJournalMode.WAL
    )


def test_engine_rejects_unsafe_sqlite_before_deployment_connects(tmp_path: Path) -> None:
    with pytest.raises(UnsafeSQLiteVersionError, match="SQLite 3.51.2"):
        create_database_engine(
            sqlite_url(tmp_path / "must-not-open.sqlite3"),
            environment="production",
            sqlite_version=(3, 51, 2),
        )
    assert not (tmp_path / "must-not-open.sqlite3").exists()


def test_match_query_treats_fts_syntax_as_plain_words() -> None:
    assert build_safe_match_query('" OR *') == '"or"*'
    with pytest.raises(ValueError, match="at least one word"):
        build_safe_match_query('*** ""')


@pytest.mark.asyncio
async def test_schema_pragmas_bigint_ids_and_indexes(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "schema.sqlite3"))
    try:
        await create_schema(engine)
        async with engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            message_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]: column["type"]
                    for column in inspect(sync_connection).get_columns("messages")
                }
            )
            message_indexes = await connection.run_sync(
                lambda sync_connection: {
                    item["name"] for item in inspect(sync_connection).get_indexes("messages")
                }
            )

        version_info = sqlite3.sqlite_version_info
        runtime_version = (version_info[0], version_info[1], version_info[2])
        expected_mode = select_sqlite_journal_mode(runtime_version, environment="local")
        assert foreign_keys == 1
        assert str(journal_mode).upper() == expected_mode.value
        assert tables >= EXPECTED_TABLES
        assert isinstance(message_columns["chat_id"], BigInteger)
        assert isinstance(message_columns["message_id"], BigInteger)
        assert {"message_type", "sender_display_name", "forward_metadata"} <= message_columns.keys()
        assert {
            "ix_messages_sent_at",
            "ix_messages_sender_sent",
            "ix_messages_topic_sent",
            "ix_messages_type_sent",
        } <= message_indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_raw_update_admission_and_completion_are_idempotent(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "inbox.sqlite3"))
    factory = create_session_factory(engine)
    payload = {"update_id": 9001, "message": {"message_id": 7}}
    try:
        await create_schema(engine)
        async with factory() as session, session.begin():
            assert await enqueue_raw_update(session, update_id=9001, payload=payload)
            assert not await enqueue_raw_update(session, update_id=9001, payload=payload)

        async with factory() as session, session.begin():
            assert await record_processed_update(session, update_id=9001, handler="message.created")
            assert not await record_processed_update(
                session, update_id=9001, handler="message.created"
            )

        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(InboxUpdate))
            inbox = await session.scalar(select(InboxUpdate).where(InboxUpdate.update_id == 9001))
            assert count == 1
            assert inbox is not None
            assert inbox.payload == payload
            assert inbox.processed_at is not None
            assert inbox.processed_at.tzinfo is UTC
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "foreign-keys.sqlite3"))
    factory = create_session_factory(engine)
    try:
        await create_schema(engine)
        async with factory() as session:
            session.add(
                Message(
                    chat_id=-100_000_000_001,
                    message_id=1,
                    topic_id=999_999,
                    sent_at=datetime(2026, 8, 29, tzinfo=UTC),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unique_telegram_keys_and_utc_round_trip(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "constraints.sqlite3"))
    factory = create_session_factory(engine)
    timestamp = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
    try:
        await create_schema(engine)
        async with factory() as session, session.begin():
            user = User(
                telegram_user_id=8_000_000_001,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
            )
            session.add(user)
            session.add(Topic(chat_id=-100_000_000_001, thread_id=17, name="Topic"))

        async with factory() as session:
            stored_user = await session.scalar(
                select(User).where(User.telegram_user_id == 8_000_000_001)
            )
            assert stored_user is not None
            assert stored_user.first_seen_at == timestamp
            assert stored_user.first_seen_at.tzinfo is UTC

        async with factory() as session:
            session.add(Topic(chat_id=-100_000_000_001, thread_id=17, name="Duplicate"))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fts_projection_tracks_changes_and_can_rebuild(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "search.sqlite3"))
    factory = create_session_factory(engine)
    try:
        await create_schema(engine)
        async with factory() as session, session.begin():
            session.add_all(
                [
                    SearchDocument(
                        source_type="material",
                        source_id=101,
                        content="Методичка методичка по железобетонным конструкциям",
                        metadata_json={"discipline": "ЖБК"},
                    ),
                    SearchDocument(
                        source_type="message",
                        source_id=202,
                        content="Методичка по геодезии",
                        metadata_json={"discipline": "Геодезия"},
                    ),
                ]
            )

        async with factory() as session:
            repository = SearchRepository(session)
            all_hits = await repository.search("методичка")
            filtered_hits = await repository.search(
                "ЖБК", source_type="material", source_ids=(101,)
            )
            malformed_input_hits = await repository.search('" OR *')
            assert [hit.source_id for hit in all_hits] == [101, 202]
            assert [hit.source_id for hit in filtered_hits] == [101]
            assert filtered_hits[0].metadata == {"discipline": "ЖБК"}
            assert malformed_input_hits == ()

        async with factory() as session, session.begin():
            document = await session.scalar(
                select(SearchDocument).where(SearchDocument.source_id == 101)
            )
            assert document is not None
            document.content = "Геодезическая съемка строительной площадки"
            document.metadata_json = {"discipline": "Геодезия"}

        async with factory() as session:
            repository = SearchRepository(session)
            assert await repository.search("железобетонным") == ()
            assert [hit.source_id for hit in await repository.search("съемка")] == [101]

        async with factory() as session, session.begin():
            await session.execute(text("DELETE FROM search_documents_fts"))
        async with factory() as session:
            assert await SearchRepository(session).search("съемка") == ()

        async with factory() as session, session.begin():
            await SearchRepository(session).rebuild()
        async with factory() as session:
            assert [hit.source_id for hit in await SearchRepository(session).search("съемка")] == [
                101
            ]

        async with factory() as session, session.begin():
            document = await session.scalar(
                select(SearchDocument).where(SearchDocument.source_id == 101)
            )
            assert document is not None
            await session.delete(document)
        async with factory() as session:
            assert await SearchRepository(session).search("съемка") == ()
    finally:
        await engine.dispose()


def test_initial_alembic_migration_builds_the_complete_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))

    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert tables >= EXPECTED_TABLES
        assert "alembic_version" in tables
    finally:
        connection.close()
