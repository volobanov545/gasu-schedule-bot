from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from szs_hub.attendance.service import AttendanceService, render_attendance_summary
from szs_hub.domain.attendance import AttendanceAction, ReactionEvent
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import AttendanceMark, Lesson, Membership, ScheduleSnapshot, User


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_reaction_add_remove_is_durable_and_summary_is_stable(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "attendance.sqlite3"))
    sessions = create_session_factory(engine)
    await create_schema(engine)
    starts = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    try:
        async with sessions() as db, db.begin():
            snapshot = ScheduleSnapshot(
                source="test",
                group_key="SZS",
                week_start=(starts - timedelta(days=1)).date(),
                fetched_at=starts - timedelta(hours=1),
                content_hash="a" * 64,
            )
            db.add(snapshot)
            await db.flush()
            lesson = Lesson(
                snapshot_id=snapshot.id,
                lesson_key="lesson-1",
                day=starts.date(),
                starts_at=starts.time(),
                ends_at=(starts + timedelta(minutes=90)).time(),
                subject="ЖБК <практика>",
            )
            db.add(lesson)
            users = [
                User(telegram_user_id=10, display_name="Анна & Co"),
                User(telegram_user_id=20, display_name="Борис"),
            ]
            db.add_all(users)
            await db.flush()
            db.add_all(
                [
                    Membership(chat_id=-1001, user_id=user.id, status="member")
                    for user in users
                ]
            )
            await db.flush()
            service = AttendanceService(db)
            opened = await service.open_session(
                lesson_id=lesson.id,
                chat_id=-1001,
                message_id=42,
                starts_at=starts,
                window_minutes=25,
                reaction="❤",
            )
            same = await service.open_session(
                lesson_id=lesson.id,
                chat_id=-1001,
                message_id=42,
                starts_at=starts,
                window_minutes=25,
                reaction="❤",
            )
            assert same.id == opened.id

            added = await service.apply_reaction(
                chat_id=-1001,
                message_id=42,
                event=ReactionEvent(10, starts + timedelta(minutes=2), "❤", True),
                display_name="Анна & Co",
            )
            duplicate = await service.apply_reaction(
                chat_id=-1001,
                message_id=42,
                event=ReactionEvent(10, starts + timedelta(minutes=3), "❤", True),
                display_name="Анна & Co",
            )
            assert added.action is AttendanceAction.MARK
            assert added.present_count == 1
            assert duplicate.action is AttendanceAction.IGNORE_DUPLICATE
            summary = await service.summary(session_id=opened.id)
            assert summary.present == ("Анна & Co",)
            assert summary.missing == ("Борис",)
            rendered = render_attendance_summary(
                lesson_title="ЖБК <практика>", starts_at=starts, summary=summary
            )
            assert "ЖБК &lt;практика&gt;" in rendered
            assert "Анна &amp; Co" in rendered

            removed = await service.apply_reaction(
                chat_id=-1001,
                message_id=42,
                event=ReactionEvent(10, starts + timedelta(minutes=4), "❤", False),
                display_name="Анна & Co",
            )
            assert removed.action is AttendanceAction.UNMARK
            assert removed.present_count == 0

        async with sessions() as db:
            marks = tuple(await db.scalars(select(AttendanceMark)))
            assert len(marks) == 1
            assert marks[0].removed_at == starts + timedelta(minutes=4)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_or_unknown_session_fails_closed(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "closed.sqlite3"))
    sessions = create_session_factory(engine)
    await create_schema(engine)
    now = datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    try:
        async with sessions() as db, db.begin():
            service = AttendanceService(db)
            unknown = await service.apply_reaction(
                chat_id=-1001,
                message_id=999,
                event=ReactionEvent(10, now, "❤", True),
                display_name="Test",
            )
            assert unknown.action is AttendanceAction.IGNORE_OUTSIDE_WINDOW
            assert unknown.session_id is None
            assert await service.close_due(now=now) == 0
    finally:
        await engine.dispose()
