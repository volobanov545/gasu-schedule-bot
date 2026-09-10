"""Durable, timestamp-aware Telegram membership observations.

Telegram does not offer a complete member-list endpoint for bots.  This store builds
the roster incrementally from explicit ``chat_member`` updates and conservative
evidence from messages seen in the configured group.  It deliberately owns no
transaction boundary: handlers can combine it atomically with inbox processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from aiogram.types import ChatMemberUpdated
from aiogram.types import Message as TelegramMessage
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.storage.models import Membership, User

_ACTIVE_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
_GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})


class MembershipWriteDisposition(StrEnum):
    """Outcome of one membership observation."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    STALE = "stale"
    IGNORED_CHAT = "ignored_chat"
    IGNORED_SENDER = "ignored_sender"


@dataclass(frozen=True, slots=True)
class MembershipWriteResult:
    """Stable result returned to update handlers."""

    disposition: MembershipWriteDisposition
    telegram_user_id: int | None = None
    status: str | None = None
    membership_id: int | None = None


class TelegramMembershipStore:
    """Maintain the best-known roster for exactly one configured group."""

    def __init__(self, *, target_chat_id: int) -> None:
        self._target_chat_id = target_chat_id

    async def apply_chat_member_update(
        self,
        session: AsyncSession,
        update: ChatMemberUpdated,
    ) -> MembershipWriteResult:
        """Apply an authoritative Telegram membership transition idempotently."""

        if not self._is_target_group(update.chat.id, _enum_value(update.chat.type)):
            return MembershipWriteResult(MembershipWriteDisposition.IGNORED_CHAT)

        observed_at = _aware(update.date)
        telegram_user = update.new_chat_member.user
        user = await _upsert_user(session, telegram_user, seen_at=observed_at)
        status = _member_status(update.new_chat_member)
        membership = await _membership_for(
            session,
            chat_id=self._target_chat_id,
            user_id=user.id,
        )
        if membership is None:
            membership = Membership(
                chat_id=self._target_chat_id,
                user_id=user.id,
                status=status,
                checked_at=observed_at,
            )
            _apply_transition(membership, status=status, observed_at=observed_at)
            session.add(membership)
            await session.flush()
            return _result(MembershipWriteDisposition.CREATED, user, membership)

        if observed_at < membership.checked_at:
            return _result(MembershipWriteDisposition.STALE, user, membership)

        if observed_at == membership.checked_at and membership.status == status:
            return _result(MembershipWriteDisposition.UNCHANGED, user, membership)

        _apply_transition(membership, status=status, observed_at=observed_at)
        await session.flush()
        return _result(MembershipWriteDisposition.UPDATED, user, membership)

    async def observe_message_sender(
        self,
        session: AsyncSession,
        message: TelegramMessage,
    ) -> MembershipWriteResult:
        """Record that a sender was present when a target-group message was sent.

        Message evidence is weaker than an explicit membership update.  In
        particular, an old or same-second message cannot resurrect a user after an
        explicit leave/kick observation.  A genuinely newer message can, because a
        user cannot send it without being present in the group at that later time.
        """

        if not self._is_target_group(message.chat.id, _enum_value(message.chat.type)):
            return MembershipWriteResult(MembershipWriteDisposition.IGNORED_CHAT)
        if message.from_user is None:
            return MembershipWriteResult(MembershipWriteDisposition.IGNORED_SENDER)

        observed_at = _aware(message.date)
        user = await _upsert_user(session, message.from_user, seen_at=observed_at)
        membership = await _membership_for(
            session,
            chat_id=self._target_chat_id,
            user_id=user.id,
        )
        if membership is None:
            membership = Membership(
                chat_id=self._target_chat_id,
                user_id=user.id,
                status="member",
                joined_at=observed_at,
                checked_at=observed_at,
            )
            session.add(membership)
            await session.flush()
            return _result(MembershipWriteDisposition.CREATED, user, membership)

        if observed_at < membership.checked_at:
            return _result(MembershipWriteDisposition.STALE, user, membership)
        if observed_at == membership.checked_at:
            disposition = (
                MembershipWriteDisposition.UNCHANGED
                if membership.status in _ACTIVE_STATUSES
                else MembershipWriteDisposition.STALE
            )
            return _result(disposition, user, membership)

        if membership.status in _ACTIVE_STATUSES:
            # Preserve stronger role information (owner/admin/restricted-member),
            # while advancing the freshness of the presence observation.
            membership.checked_at = observed_at
        else:
            _apply_transition(membership, status="member", observed_at=observed_at)
        await session.flush()
        return _result(MembershipWriteDisposition.UPDATED, user, membership)

    async def attendance_roster(self, session: AsyncSession) -> tuple[User, ...]:
        """Return current human members in stable display order.

        Bot identities are still persisted for audit/idempotency, but are never
        returned as attendance candidates.
        """

        users = await session.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.chat_id == self._target_chat_id,
                Membership.status.in_(_ACTIVE_STATUSES),
                User.is_bot.is_(False),
            )
            .order_by(User.display_name, User.telegram_user_id)
        )
        return tuple(users)

    def _is_target_group(self, chat_id: int, chat_type: str) -> bool:
        return chat_id == self._target_chat_id and chat_type in _GROUP_CHAT_TYPES


async def _upsert_user(
    session: AsyncSession,
    telegram_user: TelegramUser,
    *,
    seen_at: datetime,
) -> User:
    stored = await session.scalar(
        select(User).where(User.telegram_user_id == telegram_user.id)
    )
    display_name = " ".join(
        part for part in (telegram_user.first_name, telegram_user.last_name) if part
    ).strip()
    if stored is None:
        stored = User(
            telegram_user_id=telegram_user.id,
            username=telegram_user.username,
            display_name=display_name,
            language_code=telegram_user.language_code,
            is_bot=telegram_user.is_bot,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        session.add(stored)
        await session.flush()
        return stored

    stored.first_seen_at = min(stored.first_seen_at, seen_at)
    if seen_at >= stored.last_seen_at:
        stored.username = telegram_user.username
        stored.display_name = display_name
        stored.language_code = telegram_user.language_code
        stored.is_bot = telegram_user.is_bot
        stored.last_seen_at = seen_at
    return stored


async def _membership_for(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> Membership | None:
    membership: Membership | None = await session.scalar(
        select(Membership).where(
            Membership.chat_id == chat_id,
            Membership.user_id == user_id,
        )
    )
    return membership


def _member_status(member: object) -> str:
    raw_status = _enum_value(getattr(member, "status", "unknown"))
    if raw_status == "restricted":
        is_member = getattr(member, "is_member", None)
        if is_member is True:
            return "restricted"
        if is_member is False:
            return "left"
        return "unknown"
    return {
        "creator": "creator",
        "administrator": "administrator",
        "member": "member",
        "left": "left",
        "kicked": "kicked",
    }.get(raw_status, "unknown")


def _apply_transition(
    membership: Membership,
    *,
    status: str,
    observed_at: datetime,
) -> None:
    was_active = membership.status in _ACTIVE_STATUSES
    is_active = status in _ACTIVE_STATUSES
    membership.status = status
    membership.checked_at = observed_at
    if is_active:
        if not was_active or membership.joined_at is None:
            membership.joined_at = observed_at
        membership.left_at = None
    elif status in {"left", "kicked"}:
        membership.left_at = observed_at


def _result(
    disposition: MembershipWriteDisposition,
    user: User,
    membership: Membership,
) -> MembershipWriteResult:
    return MembershipWriteResult(
        disposition=disposition,
        telegram_user_id=user.telegram_user_id,
        status=membership.status,
        membership_id=membership.id,
    )


def _enum_value(value: object) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value).casefold()


def _aware(value: datetime | int) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
