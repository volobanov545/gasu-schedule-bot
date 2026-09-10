from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.storage import (
    Membership,
    User,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.telegram.membership_store import (
    MembershipWriteDisposition,
    TelegramMembershipStore,
)

TARGET_CHAT_ID = -100_123_456_789
START = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_database_engine(_sqlite_url(tmp_path / "membership.sqlite3"))
    await create_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _user(
    *,
    user_id: int = 77,
    is_bot: bool = False,
    first_name: str = "Анна",
) -> dict[str, object]:
    return {
        "id": user_id,
        "is_bot": is_bot,
        "first_name": first_name,
        "last_name": "Иванова",
        "username": "student",
        "language_code": "ru",
    }


def _member(status: str, *, user: dict[str, object], is_member: bool = True) -> dict[str, object]:
    base: dict[str, object] = {"status": status, "user": user}
    if status == "creator":
        return {**base, "is_anonymous": False}
    if status == "administrator":
        return {
            **base,
            "can_be_edited": True,
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_manage_video_chats": True,
            "can_restrict_members": True,
            "can_promote_members": False,
            "can_change_info": True,
            "can_invite_users": True,
            "can_post_stories": False,
            "can_edit_stories": False,
            "can_delete_stories": False,
            "can_send_welcome_messages": False,
        }
    if status == "restricted":
        return {
            **base,
            "is_member": is_member,
            "can_send_messages": False,
            "can_send_audios": False,
            "can_send_documents": False,
            "can_send_photos": False,
            "can_send_videos": False,
            "can_send_video_notes": False,
            "can_send_voice_notes": False,
            "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
            "can_react_to_messages": False,
            "can_edit_tag": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": False,
            "can_manage_topics": False,
            "until_date": 0,
        }
    if status == "kicked":
        return {**base, "until_date": 0}
    return base


def _member_update(
    status: str,
    *,
    date: datetime = START,
    chat_id: int = TARGET_CHAT_ID,
    user: dict[str, object] | None = None,
    is_member: bool = True,
) -> ChatMemberUpdated:
    subject = user or _user()
    return ChatMemberUpdated.model_validate(
        {
            "chat": {
                "id": chat_id,
                "type": "supergroup",
                "title": "СЗС",
                "is_forum": True,
            },
            "from": _user(user_id=1, first_name="Староста"),
            "date": date,
            "old_chat_member": _member("left", user=subject),
            "new_chat_member": _member(status, user=subject, is_member=is_member),
        }
    )


def _message(
    *,
    date: datetime,
    chat_id: int = TARGET_CHAT_ID,
    user: dict[str, object] | None = None,
) -> Message:
    return Message.model_validate(
        {
            "message_id": int(date.timestamp()),
            "date": date,
            "chat": {"id": chat_id, "type": "supergroup", "title": "СЗС"},
            "from": user or _user(),
            "text": "Сообщение",
        }
    )


@pytest.mark.asyncio
async def test_join_leave_retry_is_idempotent_and_timestamped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    left_at = START + timedelta(hours=1)

    async with sessions() as session, session.begin():
        joined = await store.apply_chat_member_update(session, _member_update("member"))
        duplicate = await store.apply_chat_member_update(session, _member_update("member"))
        left = await store.apply_chat_member_update(
            session,
            _member_update("left", date=left_at),
        )

    assert joined.disposition is MembershipWriteDisposition.CREATED
    assert duplicate.disposition is MembershipWriteDisposition.UNCHANGED
    assert left.disposition is MembershipWriteDisposition.UPDATED
    async with sessions() as session:
        membership = await session.scalar(select(Membership))
        assert membership is not None
        assert membership.status == "left"
        assert membership.joined_at == START
        assert membership.left_at == left_at
        assert membership.checked_at == left_at
        assert await session.scalar(select(func.count()).select_from(User)) == 1
        assert await session.scalar(select(func.count()).select_from(Membership)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("telegram_status", "is_member", "stored_status"),
    [
        ("creator", True, "creator"),
        ("administrator", True, "administrator"),
        ("member", True, "member"),
        ("restricted", True, "restricted"),
        ("restricted", False, "left"),
        ("left", False, "left"),
        ("kicked", False, "kicked"),
    ],
)
async def test_all_telegram_membership_states_map_conservatively(
    sessions: async_sessionmaker[AsyncSession],
    telegram_status: str,
    is_member: bool,
    stored_status: str,
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    async with sessions() as session, session.begin():
        result = await store.apply_chat_member_update(
            session,
            _member_update(telegram_status, is_member=is_member),
        )

    assert result.status == stored_status


@pytest.mark.asyncio
async def test_wrong_chat_is_ignored_without_persisting_identity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    async with sessions() as session, session.begin():
        member_result = await store.apply_chat_member_update(
            session,
            _member_update("member", chat_id=-100_999),
        )
        message_result = await store.observe_message_sender(
            session,
            _message(date=START, chat_id=-100_999),
        )

    assert member_result.disposition is MembershipWriteDisposition.IGNORED_CHAT
    assert message_result.disposition is MembershipWriteDisposition.IGNORED_CHAT
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(Membership)) == 0


@pytest.mark.asyncio
async def test_stale_join_does_not_override_newer_explicit_leave_or_identity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    later = START + timedelta(hours=2)
    async with sessions() as session, session.begin():
        await store.apply_chat_member_update(
            session,
            _member_update("left", date=later, user=_user(first_name="Новая")),
        )
        stale = await store.apply_chat_member_update(
            session,
            _member_update("member", date=START, user=_user(first_name="Старая")),
        )

    assert stale.disposition is MembershipWriteDisposition.STALE
    async with sessions() as session:
        membership = await session.scalar(select(Membership))
        user = await session.scalar(select(User))
        assert membership is not None and membership.status == "left"
        assert membership.checked_at == later
        assert membership.left_at == later
        assert user is not None and user.display_name == "Новая Иванова"
        assert user.first_seen_at == START
        assert user.last_seen_at == later


@pytest.mark.asyncio
async def test_message_evidence_respects_leave_and_newer_message_restores_membership(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    left_at = START + timedelta(minutes=20)
    after_rejoin = START + timedelta(minutes=21)
    async with sessions() as session, session.begin():
        first = await store.observe_message_sender(session, _message(date=START))
        await store.apply_chat_member_update(session, _member_update("left", date=left_at))
        stale = await store.observe_message_sender(
            session,
            _message(date=START + timedelta(minutes=10)),
        )
        same_second = await store.observe_message_sender(session, _message(date=left_at))
        restored = await store.observe_message_sender(session, _message(date=after_rejoin))

    assert first.disposition is MembershipWriteDisposition.CREATED
    assert stale.disposition is MembershipWriteDisposition.STALE
    assert same_second.disposition is MembershipWriteDisposition.STALE
    assert restored.disposition is MembershipWriteDisposition.UPDATED
    async with sessions() as session:
        membership = await session.scalar(select(Membership))
        assert membership is not None and membership.status == "member"
        assert membership.joined_at == after_rejoin
        assert membership.left_at is None
        assert membership.checked_at == after_rejoin


@pytest.mark.asyncio
async def test_attendance_roster_excludes_bots_but_keeps_their_audit_identity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = TelegramMembershipStore(target_chat_id=TARGET_CHAT_ID)
    async with sessions() as session, session.begin():
        await store.observe_message_sender(session, _message(date=START, user=_user(user_id=10)))
        await store.observe_message_sender(
            session,
            _message(
                date=START + timedelta(seconds=1),
                user=_user(user_id=20, is_bot=True, first_name="ServiceBot"),
            ),
        )
        roster = await store.attendance_roster(session)

    assert [item.telegram_user_id for item in roster] == [10]
    async with sessions() as session:
        bot = await session.scalar(select(User).where(User.telegram_user_id == 20))
        assert bot is not None and bot.is_bot is True
        assert await session.scalar(select(func.count()).select_from(Membership)) == 2
