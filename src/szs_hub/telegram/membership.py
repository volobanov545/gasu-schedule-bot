"""Current Telegram membership checks for private archive authorization."""

from __future__ import annotations

from typing import Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ResultChatMemberUnion

from szs_hub.telegram.access import Membership, MemberStatus


class ChatMemberSource(Protocol):
    async def get_chat_member(self, chat_id: int, user_id: int) -> ResultChatMemberUnion: ...


class TelegramMembershipGateway:
    """Ask Telegram at access time and deny whenever membership cannot be confirmed."""

    def __init__(self, source: ChatMemberSource) -> None:
        self._source = source

    async def current_membership(self, *, chat_id: int, user_id: int) -> Membership | None:
        try:
            member = await self._source.get_chat_member(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError:
            return None

        raw_status = str(member.status)
        try:
            status = MemberStatus(raw_status)
        except ValueError:
            status = MemberStatus.UNKNOWN
        is_member = getattr(member, "is_member", None)
        return Membership(
            status=status,
            is_member=is_member if isinstance(is_member, bool) else None,
        )
