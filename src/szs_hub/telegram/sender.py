"""Strict allowlist adapter from durable outbox envelopes to Telegram methods."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, Message, MessageId

from szs_hub.jobs.queue import OutboxClaim


class TelegramSendAPI(Protocol):
    async def send_message(self, **kwargs: Any) -> Message: ...

    async def edit_message_text(self, **kwargs: Any) -> Message | bool: ...

    async def copy_message(self, **kwargs: Any) -> MessageId: ...

    async def copy_messages(self, **kwargs: Any) -> list[MessageId]: ...


@dataclass(frozen=True, slots=True)
class TelegramCopyResult:
    """All destination IDs returned by one atomic copyMessages request."""

    message_ids: tuple[int, ...]

    @property
    def primary_message_id(self) -> int:
        return self.message_ids[0]


class IncompleteTelegramCopyError(RuntimeError):
    """Telegram returned fewer album members than the caller requested."""


class UnsupportedOutboxKindError(ValueError):
    """An envelope tried to invoke an operation outside the explicit allowlist."""


class TelegramOutboxSender:
    """Send only text, text edits, and copy-before-delete material operations."""

    def __init__(self, api: TelegramSendAPI) -> None:
        self._api = api

    async def send(self, message: OutboxClaim) -> int | TelegramCopyResult | None:
        if message.kind == "telegram.send_message":
            chat_id = _required_chat_id(message)
            payload = message.payload
            sent = await self._api.send_message(
                chat_id=chat_id,
                message_thread_id=_optional_int(payload, "message_thread_id"),
                text=_required_text(payload),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                disable_notification=_optional_bool(payload, "disable_notification"),
                reply_markup=_keyboard(payload),
            )
            return sent.message_id
        if message.kind == "telegram.edit_message":
            chat_id = _required_chat_id(message)
            payload = message.payload
            edit_result = await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=_required_int(payload, "message_id"),
                text=_required_text(payload),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_keyboard(payload),
            )
            return (
                edit_result.message_id
                if isinstance(edit_result, Message)
                else _required_int(payload, "message_id")
            )
        if message.kind == "telegram.copy_message":
            chat_id = _required_chat_id(message)
            payload = message.payload
            copy_result = await self._api.copy_message(
                chat_id=chat_id,
                from_chat_id=_required_int(payload, "from_chat_id"),
                message_id=_required_int(payload, "message_id"),
                message_thread_id=_optional_int(payload, "message_thread_id"),
                caption=_optional_text(payload, "caption"),
                parse_mode=ParseMode.HTML,
                disable_notification=_optional_bool(payload, "disable_notification"),
            )
            return copy_result.message_id
        if message.kind == "telegram.copy_messages":
            chat_id = _required_chat_id(message)
            payload = message.payload
            source_ids = _required_int_list(payload, "message_ids")
            copied = await self._api.copy_messages(
                chat_id=chat_id,
                from_chat_id=_required_int(payload, "from_chat_id"),
                message_ids=list(source_ids),
                message_thread_id=_optional_int(payload, "message_thread_id"),
                disable_notification=_optional_bool(payload, "disable_notification"),
            )
            destination_ids = tuple(item.message_id for item in copied)
            if len(destination_ids) != len(source_ids):
                raise IncompleteTelegramCopyError(
                    "Telegram did not confirm every requested album member"
                )
            if any(message_id <= 0 for message_id in destination_ids):
                raise IncompleteTelegramCopyError("Telegram returned an invalid message ID")
            return TelegramCopyResult(destination_ids)
        raise UnsupportedOutboxKindError(f"unsupported outbox kind: {message.kind}")


def _required_chat_id(message: OutboxClaim) -> int:
    if message.chat_id is None:
        raise ValueError("Telegram outbox message has no destination chat")
    return message.chat_id


def _required_text(payload: object) -> str:
    value = _mapping_value(payload, "text")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Telegram text is missing")
    if len(value) > 4_096:
        raise ValueError("Telegram text exceeds 4096 characters")
    return value


def _optional_text(payload: object, key: str) -> str | None:
    value = _mapping_value(payload, key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1_024:
        raise ValueError(f"Telegram {key} is invalid")
    return value


def _required_int(payload: object, key: str) -> int:
    value = _mapping_value(payload, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Telegram {key} is missing")
    return value


def _optional_int(payload: object, key: str) -> int | None:
    value = _mapping_value(payload, key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Telegram {key} is invalid")
    return value


def _required_int_list(payload: object, key: str) -> tuple[int, ...]:
    value = _mapping_value(payload, key)
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError(f"Telegram {key} must contain 1..100 IDs")
    if not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ):
        raise ValueError(f"Telegram {key} contains an invalid ID")
    result = tuple(value)
    if tuple(sorted(set(result))) != result:
        raise ValueError(f"Telegram {key} must be strictly increasing")
    return result


def _optional_bool(payload: object, key: str) -> bool | None:
    value = _mapping_value(payload, key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Telegram {key} is invalid")
    return value


def _keyboard(payload: object) -> InlineKeyboardMarkup | None:
    value = _mapping_value(payload, "reply_markup")
    if value is None:
        return None
    return InlineKeyboardMarkup.model_validate(value)


def _mapping_value(payload: object, key: str) -> object:
    if not isinstance(payload, dict):
        raise ValueError("Telegram outbox payload must be an object")
    return payload.get(key)
