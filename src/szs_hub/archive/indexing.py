"""Canonical message search documents built only from durable archive rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from szs_hub.storage import File, Message, SearchDocument, Topic, utc_now

MAX_INDEXED_MESSAGE_CHARACTERS = 2_000_000


async def upsert_message_search_document(
    session: AsyncSession,
    message: Message,
    *,
    telegram_link: str | None = None,
) -> SearchDocument:
    """Rebuild one message document without duplicating extracted text on retries."""

    files = tuple(
        await session.scalars(
            select(File).where(File.message_id == message.id).order_by(File.id)
        )
    )
    topic = await session.get(Topic, message.topic_id) if message.topic_id is not None else None
    content = _bounded_content(
        (
            message.text or "",
            message.caption or "",
            *(file.file_name or "" for file in files),
            *(file.extracted_text or "" for file in files),
        )
    )
    if not content:
        content = message.message_type.replace("_", " ")

    document = await session.scalar(
        select(SearchDocument).where(
            SearchDocument.source_type == "message",
            SearchDocument.source_id == message.id,
        )
    )
    metadata: dict[str, Any] = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "message_type": message.message_type,
        "sent_at": message.sent_at.isoformat(),
        "source": message.source,
    }
    if topic is not None:
        metadata["topic_id"] = topic.thread_id
        metadata["topic_name"] = topic.name
    if message.sender_display_name:
        metadata["sender_display_name"] = message.sender_display_name
    if message.edited_at is not None:
        metadata["edited_at"] = message.edited_at.isoformat()

    resolved_link = telegram_link
    if resolved_link is None and document is not None and document.metadata_json is not None:
        existing_link = document.metadata_json.get("telegram_link")
        if isinstance(existing_link, str):
            resolved_link = existing_link
    if resolved_link is None:
        resolved_link = _private_supergroup_link(message.chat_id, message.message_id)
    if resolved_link is not None:
        metadata["telegram_link"] = resolved_link

    file_metadata = [
        {
            key: value
            for key, value in {
                "id": file.id,
                "kind": file.kind,
                "file_name": file.file_name,
                "mime_type": file.mime_type,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "extraction_status": file.extraction_status,
                "extraction_error_code": file.extraction_error_code,
                "extraction_version": file.extraction_version,
            }.items()
            if value is not None
        }
        for file in files
    ]
    if file_metadata:
        metadata["files"] = file_metadata

    if document is None:
        document = SearchDocument(
            source_type="message",
            source_id=message.id,
            content=content,
            metadata_json=metadata,
        )
        session.add(document)
        return document
    if document.content != content or document.metadata_json != metadata:
        document.content = content
        document.metadata_json = metadata
        document.embedding = None
        document.updated_at = utc_now()
    return document


def _bounded_content(parts: tuple[str, ...]) -> str:
    accepted: list[str] = []
    used = 0
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        separator = 1 if accepted else 0
        available = MAX_INDEXED_MESSAGE_CHARACTERS - used - separator
        if available <= 0:
            break
        accepted.append(cleaned[:available])
        used += separator + len(accepted[-1])
    return "\n".join(accepted)


def _private_supergroup_link(chat_id: int, message_id: int) -> str | None:
    rendered = str(chat_id)
    if rendered.startswith("-100"):
        return f"https://t.me/c/{rendered[4:]}/{message_id}"
    return None
