from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.types import Update
from sqlalchemy import select

from szs_hub.storage import (
    InboxUpdate,
    ProcessedUpdate,
    create_database_engine,
    create_schema,
    create_session_factory,
    utc_now,
)
from szs_hub.telegram.runtime import DurableUpdatePump


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def update(update_id: int) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 1_788_264_000,
                "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
                "from": {"id": 7, "is_bot": False, "first_name": "Test"},
                "text": "hello",
            },
        }
    )


class FakeSource:
    def __init__(self, updates: list[Update]) -> None:
        self.updates = updates
        self.allowed_updates: list[str] | None = None

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 - Telegram API field name
        allowed_updates: list[str] | None = None,
        request_timeout: int | None = None,
    ) -> list[Update]:
        self.allowed_updates = allowed_updates
        result, self.updates = self.updates, []
        return result


class FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ids: list[int] = []

    async def feed_update(self, bot: object, update: Update, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("temporary failure")
        self.ids.append(update.update_id)


@pytest.mark.asyncio
async def test_poll_persists_before_processing_and_advances_offset(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "runtime.sqlite3"))
    sessions = create_session_factory(engine)
    source = FakeSource([update(10), update(11)])
    dispatcher = FakeDispatcher()
    pump = DurableUpdatePump(
        session_factory=sessions,
        source=source,
        dispatcher=dispatcher,
        bot_context=object(),
    )
    try:
        await create_schema(engine)
        assert await pump.poll_once(offset=None, poll_timeout=0) == 12
        assert source.allowed_updates is not None
        assert "message_reaction" in source.allowed_updates
        assert dispatcher.ids == []

        assert await pump.process_ready() == 2
        assert dispatcher.ids == [10, 11]
        assert await pump.initial_offset() == 12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_admission_and_dispatch_retry_are_idempotent(tmp_path: Path) -> None:
    engine = create_database_engine(sqlite_url(tmp_path / "retry.sqlite3"))
    sessions = create_session_factory(engine)
    source = FakeSource([])
    dispatcher = FakeDispatcher(fail=True)
    pump = DurableUpdatePump(
        session_factory=sessions,
        source=source,
        dispatcher=dispatcher,
        bot_context=object(),
        max_attempts=2,
    )
    try:
        await create_schema(engine)
        assert await pump.admit([update(20), update(20)]) == 1
        assert await pump.process_ready() == 0

        async with sessions() as session, session.begin():
            inbox = await session.scalar(select(InboxUpdate).where(InboxUpdate.update_id == 20))
            assert inbox is not None
            inbox.available_at = utc_now()
        assert await pump.process_ready() == 0

        async with sessions() as session:
            inbox = await session.scalar(
                select(InboxUpdate).where(InboxUpdate.update_id == 20)
            )
            processed = await session.get(ProcessedUpdate, 20)
            assert inbox is not None
            assert inbox.last_error == "RuntimeError"
            assert "temporary failure" not in inbox.last_error
            assert processed is not None
            assert processed.outcome == "failed"
            assert processed.detail == "RuntimeError"
    finally:
        await engine.dispose()
