from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from szs_hub.retention import RetentionService
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import (
    AIConversation,
    AIMessage,
    InboxUpdate,
    Job,
    OutboxMessage,
    ProcessedUpdate,
    User,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_retention_redacts_only_expired_scoped_data(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "retention.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        async with sessions() as session, session.begin():
            old_processed = NOW - timedelta(days=31)
            recent = NOW - timedelta(days=1)
            old_failure = NOW - timedelta(days=91)
            for update_id, outcome, at, payload, error in (
                (1, "processed", old_processed, {"message": "private old"}, None),
                (2, "ignored", recent, {"message": "private recent"}, None),
                (3, "failed", old_failure, {"message": "failure evidence"}, "RuntimeError"),
            ):
                session.add(
                    InboxUpdate(
                        update_id=update_id,
                        payload=payload,
                        received_at=at,
                        available_at=at,
                        processed_at=at,
                        last_error=error,
                    )
                )
                session.add(
                    ProcessedUpdate(
                        update_id=update_id,
                        handler="test",
                        outcome=outcome,
                        processed_at=at,
                        detail=error,
                    )
                )

            session.add_all(
                [
                    Job(
                        kind="old",
                        idempotency_key="old-job",
                        payload={},
                        status="failed",
                        run_at=old_failure,
                        last_error="RuntimeError",
                        created_at=old_failure,
                        updated_at=old_failure,
                        completed_at=old_failure,
                    ),
                    Job(
                        kind="recent",
                        idempotency_key="recent-job",
                        payload={},
                        status="failed",
                        run_at=recent,
                        last_error="RuntimeError",
                        created_at=recent,
                        updated_at=recent,
                        completed_at=recent,
                    ),
                    OutboxMessage(
                        kind="old",
                        idempotency_key="old-outbox",
                        payload={},
                        status="failed",
                        available_at=old_failure,
                        last_error="TelegramError",
                        created_at=old_failure,
                    ),
                    OutboxMessage(
                        kind="recent",
                        idempotency_key="recent-outbox",
                        payload={},
                        status="failed",
                        available_at=recent,
                        last_error="TelegramError",
                        created_at=recent,
                    ),
                ]
            )
            user = User(
                telegram_user_id=100,
                is_bot=False,
                first_seen_at=old_failure,
                last_seen_at=recent,
            )
            session.add(user)
            await session.flush()
            old_conversation = AIConversation(
                user_id=user.id,
                status="expired",
                created_at=old_processed,
                updated_at=old_processed,
                expires_at=old_processed,
            )
            recent_conversation = AIConversation(
                user_id=user.id,
                status="active",
                created_at=recent,
                updated_at=recent,
                expires_at=NOW + timedelta(hours=1),
            )
            session.add_all([old_conversation, recent_conversation])
            await session.flush()
            session.add_all(
                [
                    AIMessage(
                        conversation_id=old_conversation.id,
                        role="user",
                        content="old private question",
                        created_at=old_processed,
                    ),
                    AIMessage(
                        conversation_id=recent_conversation.id,
                        role="user",
                        content="recent question",
                        created_at=recent,
                    ),
                ]
            )

        result = await RetentionService(sessions).run_batch(now=NOW)

        assert result.processed_payloads_redacted == 1
        assert result.failed_updates_redacted == 1
        assert result.failed_jobs_redacted == 1
        assert result.failed_outbox_redacted == 1
        assert result.ai_conversations_deleted == 1
        assert result.may_have_more is False
        async with sessions() as session:
            inbox = {
                item.update_id: item
                for item in await session.scalars(select(InboxUpdate))
            }
            processed = {
                item.update_id: item
                for item in await session.scalars(select(ProcessedUpdate))
            }
            jobs = {
                item.idempotency_key: item
                for item in await session.scalars(select(Job))
            }
            outbox = {
                item.idempotency_key: item
                for item in await session.scalars(select(OutboxMessage))
            }
            conversations = tuple(await session.scalars(select(AIConversation)))
            messages = tuple(await session.scalars(select(AIMessage)))

        assert inbox[1].payload == {}
        assert processed[1].outcome == "processed"
        assert inbox[2].payload == {"message": "private recent"}
        assert inbox[3].payload == {}
        assert inbox[3].last_error is None and processed[3].detail is None
        assert jobs["old-job"].status == "failed" and jobs["old-job"].last_error is None
        assert jobs["recent-job"].last_error == "RuntimeError"
        assert outbox["old-outbox"].status == "failed"
        assert outbox["old-outbox"].last_error is None
        assert outbox["recent-outbox"].last_error == "TelegramError"
        assert len(conversations) == 1 and conversations[0].status == "active"
        assert len(messages) == 1 and messages[0].content == "recent question"

        repeated = await RetentionService(sessions).run_batch(now=NOW)
        assert repeated.processed_payloads_redacted == 0
        assert repeated.failed_updates_redacted == 0
        assert repeated.ai_conversations_deleted == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_limit_is_bounded_and_reports_more_work(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "bounded.sqlite3"))
    sessions = create_session_factory(engine)
    old = NOW - timedelta(days=31)
    try:
        await create_schema(engine)
        async with sessions() as session, session.begin():
            for update_id in (10, 11):
                session.add(
                    InboxUpdate(
                        update_id=update_id,
                        payload={"private": update_id},
                        received_at=old,
                        available_at=old,
                        processed_at=old,
                    )
                )
                session.add(
                    ProcessedUpdate(
                        update_id=update_id,
                        handler="test",
                        outcome="processed",
                        processed_at=old,
                    )
                )

        first = await RetentionService(sessions).run_batch(now=NOW, limit=1)
        assert first.processed_payloads_redacted == 1
        assert first.may_have_more is True
        async with sessions() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(InboxUpdate)
                .where(InboxUpdate.payload != {})
            )
        assert remaining == 1

        second = await RetentionService(sessions).run_batch(now=NOW, limit=1)
        assert second.processed_payloads_redacted == 1
        third = await RetentionService(sessions).run_batch(now=NOW, limit=1)
        assert third.processed_payloads_redacted == 0
        assert third.may_have_more is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_rejects_naive_time_and_unbounded_limit(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "validation.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        service = RetentionService(sessions)
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.run_batch(now=datetime(2026, 9, 1))
        with pytest.raises(ValueError, match="between 1 and 5000"):
            await service.run_batch(now=NOW, limit=0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_payload_is_redacted_even_after_errors_were_cleared(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "legacy-failure.sqlite3"))
    sessions = create_session_factory(engine)
    old_failure = NOW - timedelta(days=91)
    try:
        await create_schema(engine)
        async with sessions() as session, session.begin():
            session.add(
                InboxUpdate(
                    update_id=99,
                    payload={"message": "private failure"},
                    received_at=old_failure,
                    available_at=old_failure,
                    processed_at=old_failure,
                    last_error=None,
                )
            )
            session.add(
                ProcessedUpdate(
                    update_id=99,
                    handler="test",
                    outcome="failed",
                    processed_at=old_failure,
                    detail=None,
                )
            )

        result = await RetentionService(sessions).run_batch(now=NOW)
        async with sessions() as session:
            inbox = await session.scalar(select(InboxUpdate).where(InboxUpdate.update_id == 99))
        assert result.failed_updates_redacted == 1
        assert inbox is not None and inbox.payload == {}
    finally:
        await engine.dispose()
