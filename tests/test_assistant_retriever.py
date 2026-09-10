from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from szs_hub.assistant.retriever import SQLiteArchiveRetriever
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import SearchDocument


@pytest.mark.asyncio
async def test_retriever_handles_natural_russian_and_yesterday_scope(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'retrieval.db').as_posix()}"
    )
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                [
                    SearchDocument(
                        source_type="message",
                        source_id=1,
                        content="Вот методичка по железобетонным конструкциям",
                        metadata_json={
                            "chat_id": -1001,
                            "message_id": 101,
                            "sent_at": "2026-08-31T18:00:00+00:00",
                            "sender_display_name": "Анна",
                            "telegram_link": "https://t.me/c/1/101",
                        },
                    ),
                    SearchDocument(
                        source_type="message",
                        source_id=2,
                        content="Сегодня обсуждали машину BMW",
                        metadata_json={
                            "chat_id": -1001,
                            "message_id": 102,
                            "sent_at": "2026-09-01T09:00:00+00:00",
                        },
                    ),
                ]
            )
        retriever = SQLiteArchiveRetriever(
            sessions,
            now=lambda: datetime(2026, 9, 1, 12, tzinfo=UTC),
        )
        hits = await retriever.retrieve(
            question="Кидали вчера методичку по железобетону?",
            limit=10,
        )
        assert [hit.message_id for hit in hits] == [101]
        assert hits[0].sender_display_name == "Анна"

        today = await retriever.retrieve(
            question="Что сегодня обсуждали про машину?",
            limit=10,
        )
        assert [hit.message_id for hit in today] == [102]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retriever_drops_documents_with_untrusted_broken_metadata(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'bad.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        async with sessions() as session, session.begin():
            session.add(
                SearchDocument(
                    source_type="message",
                    source_id=1,
                    content="секретная методичка",
                    metadata_json={"chat_id": "not-an-int", "sent_at": "bad"},
                )
            )
        assert await SQLiteArchiveRetriever(sessions).retrieve(
            question="методичка", limit=10
        ) == ()
    finally:
        await engine.dispose()
