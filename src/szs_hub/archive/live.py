"""Idempotent persistence of live Telegram messages.

The Bot API does not expose arbitrary message deletions or album-completion events.
This module consequently only upserts facts present in ``message``/``edited_message``
updates. It never downloads or removes media.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from aiogram.types import Message as TelegramMessage
from aiogram.types import (
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.archive.indexing import upsert_message_search_document
from szs_hub.storage import File, MediaGroup, Message, Topic, User, utc_now


class LiveArchiveDisposition(StrEnum):
    """Outcome of one live-message ingestion attempt."""

    CREATED = "created"
    UPDATED = "updated"
    IGNORED_CHAT = "ignored_chat"


@dataclass(frozen=True, slots=True)
class LiveArchiveResult:
    disposition: LiveArchiveDisposition
    telegram_message_id: int
    stored_message_id: int | None = None
    stored_file_count: int = 0


@dataclass(frozen=True, slots=True)
class _FileDescriptor:
    file_id: str
    file_unique_id: str
    kind: str
    mime_type: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None


class LiveArchive:
    """Store messages from exactly one configured Telegram chat."""

    def __init__(self, *, target_chat_id: int) -> None:
        self._target_chat_id = target_chat_id

    async def ingest(
        self,
        session: AsyncSession,
        telegram_message: TelegramMessage,
    ) -> LiveArchiveResult:
        """Upsert one Telegram message without committing the caller's transaction."""

        if telegram_message.chat.id != self._target_chat_id:
            return LiveArchiveResult(
                disposition=LiveArchiveDisposition.IGNORED_CHAT,
                telegram_message_id=telegram_message.message_id,
            )

        sender = await _upsert_sender(session, telegram_message)
        topic = await _upsert_topic(session, telegram_message)
        media_group = await _upsert_media_group(session, telegram_message)
        stored = await session.scalar(
            select(Message).where(
                Message.chat_id == telegram_message.chat.id,
                Message.message_id == telegram_message.message_id,
            )
        )
        created = stored is None
        if stored is None:
            stored = Message(
                chat_id=telegram_message.chat.id,
                message_id=telegram_message.message_id,
                sent_at=_aware(telegram_message.date),
            )
            session.add(stored)

        applied = _apply_message_snapshot(
            stored,
            telegram_message,
            sender=sender,
            topic=topic,
            media_group=media_group,
        )
        await session.flush()

        if not applied:
            return LiveArchiveResult(
                disposition=LiveArchiveDisposition.UPDATED,
                telegram_message_id=telegram_message.message_id,
                stored_message_id=stored.id,
            )

        descriptors = _file_descriptors(telegram_message)
        stored_file_count = await _upsert_files(session, stored, descriptors)
        # Session autoflush is intentionally disabled; the canonical index queries
        # durable File rows and must see a file created by this same archive update.
        await session.flush()
        await upsert_message_search_document(
            session,
            stored,
            telegram_link=_telegram_link(telegram_message),
        )
        await session.flush()

        return LiveArchiveResult(
            disposition=(
                LiveArchiveDisposition.CREATED if created else LiveArchiveDisposition.UPDATED
            ),
            telegram_message_id=telegram_message.message_id,
            stored_message_id=stored.id,
            stored_file_count=stored_file_count,
        )


async def _upsert_sender(
    session: AsyncSession,
    message: TelegramMessage,
) -> User | None:
    sender = message.from_user
    if sender is None:
        return None

    seen_at = _aware(message.edit_date or message.date)
    stored = await session.scalar(select(User).where(User.telegram_user_id == sender.id))
    display_name = _user_display_name(sender.first_name, sender.last_name)
    if stored is None:
        stored = User(
            telegram_user_id=sender.id,
            username=sender.username,
            display_name=display_name,
            language_code=sender.language_code,
            is_bot=sender.is_bot,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        session.add(stored)
        await session.flush()
        return stored

    # Do not move last_seen_at backwards when an old retry arrives after an edit.
    stored.first_seen_at = min(stored.first_seen_at, seen_at)
    stored.last_seen_at = max(stored.last_seen_at, seen_at)
    stored.username = sender.username
    stored.display_name = display_name
    stored.language_code = sender.language_code
    stored.is_bot = sender.is_bot
    return stored


async def _upsert_topic(
    session: AsyncSession,
    message: TelegramMessage,
) -> Topic | None:
    thread_id = message.message_thread_id
    if thread_id is None and (
        message.general_forum_topic_hidden is not None
        or message.general_forum_topic_unhidden is not None
    ):
        thread_id = 1
    if thread_id is None:
        return None

    topic = await session.scalar(
        select(Topic).where(
            Topic.chat_id == message.chat.id,
            Topic.thread_id == thread_id,
        )
    )
    event_name = None
    if message.forum_topic_created is not None:
        event_name = message.forum_topic_created.name
    elif message.forum_topic_edited is not None:
        event_name = message.forum_topic_edited.name

    now = utc_now()
    if topic is None:
        topic = Topic(
            chat_id=message.chat.id,
            thread_id=thread_id,
            name=event_name or ("General" if thread_id == 1 else f"Topic {thread_id}"),
            kind="forum",
            is_closed=message.forum_topic_closed is not None,
            created_at=_aware(message.date),
            updated_at=now,
        )
        session.add(topic)
        await session.flush()
    else:
        changed = False
        if event_name:
            topic.name = event_name
            changed = True
        if message.forum_topic_closed is not None:
            topic.is_closed = True
            changed = True
        elif message.forum_topic_reopened is not None:
            topic.is_closed = False
            changed = True
        topic.created_at = min(topic.created_at, _aware(message.date))
        if changed:
            topic.updated_at = now
    return topic


async def _upsert_media_group(
    session: AsyncSession,
    message: TelegramMessage,
) -> MediaGroup | None:
    telegram_group_id = message.media_group_id
    if telegram_group_id is None:
        return None
    media_group = await session.scalar(
        select(MediaGroup).where(
            MediaGroup.chat_id == message.chat.id,
            MediaGroup.telegram_media_group_id == telegram_group_id,
        )
    )
    sent_at = _aware(message.date)
    if media_group is None:
        media_group = MediaGroup(
            chat_id=message.chat.id,
            telegram_media_group_id=telegram_group_id,
            first_message_at=sent_at,
        )
        session.add(media_group)
        await session.flush()
    else:
        media_group.first_message_at = min(media_group.first_message_at, sent_at)
    # Telegram has no album completion marker. finalized_at intentionally stays untouched.
    return media_group


def _apply_message_snapshot(
    stored: Message,
    incoming: TelegramMessage,
    *,
    sender: User | None,
    topic: Topic | None,
    media_group: MediaGroup | None,
) -> bool:
    incoming_edit = _aware(incoming.edit_date) if incoming.edit_date is not None else None
    has_newer_snapshot = (
        stored.edited_at is None
        or incoming_edit is not None
        and incoming_edit >= stored.edited_at
    )
    if not has_newer_snapshot:
        return False

    stored.topic_id = topic.id if topic is not None else None
    stored.sender_user_id = sender.id if sender is not None else None
    stored.media_group_id = media_group.id if media_group is not None else None
    stored.reply_to_message_id = (
        incoming.reply_to_message.message_id if incoming.reply_to_message is not None else None
    )
    stored.source = "bot_api"
    stored.message_type = _message_type(incoming)
    stored.sender_display_name = _sender_display_name(incoming)
    stored.forward_metadata = _forward_metadata(incoming)
    stored.text = incoming.text
    stored.caption = incoming.caption
    entities = incoming.caption_entities if incoming.caption is not None else incoming.entities
    stored.entities = (
        [entity.model_dump(mode="json", exclude_none=True) for entity in entities]
        if entities
        else None
    )
    stored.sent_at = _aware(incoming.date)
    stored.edited_at = incoming_edit
    stored.ingested_at = utc_now()
    return True


def _message_type(message: TelegramMessage) -> str:
    candidates: tuple[tuple[str, object], ...] = (
        ("topic_created", message.forum_topic_created),
        ("topic_edited", message.forum_topic_edited),
        ("topic_closed", message.forum_topic_closed),
        ("topic_reopened", message.forum_topic_reopened),
        ("document", message.document),
        ("photo", message.photo),
        ("video", message.video),
        ("audio", message.audio),
        ("voice", message.voice),
        ("video_note", message.video_note),
        ("animation", message.animation),
        ("sticker", message.sticker),
        ("poll", message.poll),
        ("contact", message.contact),
        ("venue", message.venue),
        ("location", message.location),
        ("text", message.text),
    )
    return next((kind for kind, value in candidates if value is not None), "unknown")


def _file_descriptors(message: TelegramMessage) -> tuple[_FileDescriptor, ...]:
    descriptors: list[_FileDescriptor] = []
    if message.document is not None:
        document = message.document
        descriptors.append(
            _FileDescriptor(
                document.file_id,
                document.file_unique_id,
                "document",
                document.mime_type,
                document.file_name,
                document.file_size,
            )
        )
    if message.photo:
        # One physical photo appears as multiple delivery sizes. Persist the largest
        # reusable Bot API file rather than presenting each size as separate content.
        photo = max(
            message.photo,
            key=lambda candidate: (
                candidate.width * candidate.height,
                candidate.file_size or 0,
            ),
        )
        descriptors.append(
            _FileDescriptor(
                photo.file_id,
                photo.file_unique_id,
                "photo",
                "image/jpeg",
                size_bytes=photo.file_size,
            )
        )
    if message.video is not None:
        video = message.video
        descriptors.append(
            _FileDescriptor(
                video.file_id,
                video.file_unique_id,
                "video",
                video.mime_type,
                video.file_name,
                video.file_size,
            )
        )
    if message.audio is not None:
        audio = message.audio
        descriptors.append(
            _FileDescriptor(
                audio.file_id,
                audio.file_unique_id,
                "audio",
                audio.mime_type,
                audio.file_name,
                audio.file_size,
            )
        )
    if message.voice is not None:
        voice = message.voice
        descriptors.append(
            _FileDescriptor(
                voice.file_id,
                voice.file_unique_id,
                "voice",
                voice.mime_type,
                size_bytes=voice.file_size,
            )
        )
    if message.video_note is not None:
        video_note = message.video_note
        descriptors.append(
            _FileDescriptor(
                video_note.file_id,
                video_note.file_unique_id,
                "video_note",
                "video/mp4",
                size_bytes=video_note.file_size,
            )
        )
    if message.animation is not None:
        animation = message.animation
        descriptors.append(
            _FileDescriptor(
                animation.file_id,
                animation.file_unique_id,
                "animation",
                animation.mime_type,
                animation.file_name,
                animation.file_size,
            )
        )
    if message.sticker is not None:
        sticker = message.sticker
        descriptors.append(
            _FileDescriptor(
                sticker.file_id,
                sticker.file_unique_id,
                "sticker",
                size_bytes=sticker.file_size,
            )
        )
    return tuple(descriptors)


async def _upsert_files(
    session: AsyncSession,
    message: Message,
    descriptors: tuple[_FileDescriptor, ...],
) -> int:
    current = {
        item.telegram_file_unique_id: item
        for item in await session.scalars(select(File).where(File.message_id == message.id))
    }
    for descriptor in descriptors:
        stored = current.get(descriptor.file_unique_id)
        if stored is None:
            stored = File(
                message_id=message.id,
                telegram_file_id=descriptor.file_id,
                telegram_file_unique_id=descriptor.file_unique_id,
                kind=descriptor.kind,
            )
            session.add(stored)
            current[descriptor.file_unique_id] = stored
        stored.telegram_file_id = descriptor.file_id
        stored.kind = descriptor.kind
        stored.mime_type = descriptor.mime_type
        stored.file_name = descriptor.file_name
        stored.size_bytes = descriptor.size_bytes
    return len(descriptors)


def _forward_metadata(message: TelegramMessage) -> dict[str, Any] | None:
    origin = message.forward_origin
    if origin is None:
        return None

    metadata: dict[str, Any] = {
        "origin_type": origin.type,
        "forwarded_at": _aware(origin.date).isoformat(),
    }
    # Private user IDs, names and hidden-sender labels are deliberately excluded.
    if isinstance(origin, MessageOriginChannel):
        metadata["origin_message_id"] = origin.message_id
        if origin.chat.username:
            metadata["public_username"] = origin.chat.username
    elif isinstance(origin, MessageOriginChat):
        if origin.sender_chat.username:
            metadata["public_username"] = origin.sender_chat.username
    elif isinstance(origin, (MessageOriginUser, MessageOriginHiddenUser)):
        pass
    if message.is_automatic_forward:
        metadata["automatic"] = True
    return metadata


def _sender_display_name(message: TelegramMessage) -> str | None:
    if message.from_user is not None:
        return _user_display_name(message.from_user.first_name, message.from_user.last_name)
    if message.sender_chat is not None:
        return message.sender_chat.title or message.sender_chat.username
    return None


def _user_display_name(first_name: str, last_name: str | None) -> str:
    return " ".join(part for part in (first_name, last_name) if part).strip()


def _telegram_link(message: TelegramMessage) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"
    rendered = str(message.chat.id)
    if rendered.startswith("-100"):
        return f"https://t.me/c/{rendered[4:]}/{message.message_id}"
    return None


def _aware(value: datetime | int) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        # Aiogram normally supplies UTC-aware values. Accept naive test/custom
        # adapters conservatively as UTC instead of letting SQLite lose an offset.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
