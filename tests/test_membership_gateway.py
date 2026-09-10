from __future__ import annotations

from typing import cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import ResultChatMemberUnion

from szs_hub.telegram.access import MemberStatus
from szs_hub.telegram.membership import TelegramMembershipGateway


class FakeMemberSource:
    def __init__(self, result: ResultChatMemberUnion | Exception) -> None:
        self.result = result

    async def get_chat_member(self, chat_id: int, user_id: int) -> ResultChatMemberUnion:
        assert chat_id == -1001
        assert user_id == 77
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_membership_gateway_maps_confirmed_member() -> None:
    from aiogram.types import ChatMemberMember

    member = ChatMemberMember.model_validate(
        {"status": "member", "user": {"id": 77, "is_bot": False, "first_name": "A"}}
    )
    gateway = TelegramMembershipGateway(FakeMemberSource(member))

    result = await gateway.current_membership(chat_id=-1001, user_id=77)

    assert result is not None
    assert result.status is MemberStatus.MEMBER


@pytest.mark.asyncio
async def test_membership_gateway_fails_closed_on_telegram_error() -> None:
    method = GetChatMember(chat_id=-1001, user_id=77)
    error = TelegramBadRequest(method=method, message="not enough rights")
    source = cast(FakeMemberSource, FakeMemberSource(error))
    gateway = TelegramMembershipGateway(source)

    assert await gateway.current_membership(chat_id=-1001, user_id=77) is None
