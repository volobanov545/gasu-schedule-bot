from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram.types import Message, MessageId

from szs_hub.jobs.queue import OutboxClaim
from szs_hub.telegram.sender import (
    IncompleteTelegramCopyError,
    TelegramCopyResult,
    TelegramOutboxSender,
    TelegramSendAPI,
    UnsupportedOutboxKindError,
)


class FakeAPI:
    def __init__(self) -> None:
        self.method: str | None = None
        self.kwargs: dict[str, Any] = {}

    async def send_message(self, **kwargs: Any) -> Message:
        self.method, self.kwargs = "send", kwargs
        return Message.model_validate(
            {
                "message_id": 55,
                "date": datetime(2026, 9, 1, tzinfo=UTC),
                "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
            }
        )

    async def edit_message_text(self, **kwargs: Any) -> Message | bool:
        self.method, self.kwargs = "edit", kwargs
        return True

    async def copy_message(self, **kwargs: Any) -> MessageId:
        self.method, self.kwargs = "copy", kwargs
        return MessageId(message_id=88)

    async def copy_messages(self, **kwargs: Any) -> list[MessageId]:
        self.method, self.kwargs = "copy_many", kwargs
        return [MessageId(message_id=88 + index) for index, _ in enumerate(kwargs["message_ids"])]


def _claim(kind: str, payload: dict[str, object], *, chat_id: int | None = -1001) -> OutboxClaim:
    return OutboxClaim(
        id=1,
        kind=kind,
        idempotency_key="key",
        chat_id=chat_id,
        payload=payload,
        attempt=1,
        max_attempts=5,
        leased_until=datetime(2026, 9, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_sender_accepts_only_structured_html_text() -> None:
    api = FakeAPI()
    sender = TelegramOutboxSender(cast(TelegramSendAPI, api))
    message_id = await sender.send(
        _claim(
            "telegram.send_message",
            {
                "text": "<b>Завтра</b>",
                "message_thread_id": 42,
                "reply_markup": {
                    "inline_keyboard": [[{"text": "Сегодня", "callback_data": "x"}]]
                },
            },
        )
    )
    assert message_id == 55
    assert api.method == "send"
    assert api.kwargs["chat_id"] == -1001
    assert api.kwargs["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_sender_rejects_arbitrary_methods_and_oversized_text() -> None:
    sender = TelegramOutboxSender(cast(TelegramSendAPI, FakeAPI()))
    with pytest.raises(UnsupportedOutboxKindError):
        await sender.send(_claim("telegram.delete_chat", {}))
    with pytest.raises(ValueError, match="4096"):
        await sender.send(_claim("telegram.send_message", {"text": "x" * 4_097}))


@pytest.mark.asyncio
async def test_sender_copies_and_verifies_every_album_member() -> None:
    api = FakeAPI()
    sender = TelegramOutboxSender(cast(TelegramSendAPI, api))
    result = await sender.send(
        _claim(
            "telegram.copy_messages",
            {
                "from_chat_id": -1001,
                "message_ids": [10, 11],
                "message_thread_id": 42,
            },
        )
    )

    assert result == TelegramCopyResult((88, 89))
    assert api.method == "copy_many"
    assert api.kwargs["message_ids"] == [10, 11]


@pytest.mark.asyncio
async def test_sender_rejects_partial_album_confirmation() -> None:
    class PartialAPI(FakeAPI):
        async def copy_messages(self, **kwargs: Any) -> list[MessageId]:
            self.method, self.kwargs = "copy_many", kwargs
            return [MessageId(message_id=88)]

    sender = TelegramOutboxSender(cast(TelegramSendAPI, PartialAPI()))
    with pytest.raises(IncompleteTelegramCopyError):
        await sender.send(
            _claim(
                "telegram.copy_messages",
                {"from_chat_id": -1001, "message_ids": [10, 11]},
            )
        )
