"""Durable, non-destructive first-pass material automation.

The Bot API has no album-complete event. Incoming album members therefore extend one
durable debounce job. The eventual assessment treats every stored member as one logical
unit, but deliberately performs no downloads, extraction, OCR, or source deletion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import PurePath

from aiogram.types import Message as TelegramMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.config import Settings
from szs_hub.domain.materials import (
    MaterialAssessment,
    MaterialEvidence,
    MaterialRoute,
    assess_material,
)
from szs_hub.jobs.queue import EnqueueResult, JobClaim, JobQueue, OutboxQueue
from szs_hub.jobs.worker import JobHandler
from szs_hub.storage.base import utc_now
from szs_hub.storage.models import (
    File,
    Material,
    MediaGroup,
    Message,
    OutboxMessage,
    Topic,
)

MATERIAL_ASSESS_KIND = "material.assess"
MATERIAL_CLASSIFIER_VERSION = "transparent-rules-v1"
MATERIAL_DEBOUNCE = timedelta(seconds=3)
_ACADEMIC_SUFFIXES = frozenset(
    {".doc", ".docx", ".dwg", ".pdf", ".ppt", ".pptx", ".txt", ".xls", ".xlsx"}
)
_SOCIAL_TOPIC_MARKERS = ("болтал", "мем", "оффтоп", "флуд", "мерс", "машин")
_REASON_LABELS = {
    "academic_file_type": "учебный тип файла",
    "explicit_academic_context": "явный учебный контекст",
    "explicit_academic_photo": "учебная подпись к фото",
    "known_discipline": "известная дисциплина",
    "extractable_document_text": "извлекаемый текст",
    "photo_requires_context": "фото требует контекста",
    "social_topic": "неучебная тема",
    "explicit_social_context": "явный неучебный контекст",
}


class MaterialSourceNotReadyError(RuntimeError):
    """The admission update committed before all referenced archive rows were visible."""


class MaterialAlbumTooLargeError(RuntimeError):
    """A logical album exceeds Telegram copyMessages' bounded request size."""


@dataclass(frozen=True, slots=True)
class MaterialSource:
    chat_id: int
    message_id: int | None = None
    media_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class _LoadedSource:
    source: MaterialSource
    messages: tuple[Message, ...]
    files: tuple[File, ...]
    topics: tuple[Topic, ...]
    media_group: MediaGroup | None


@dataclass(frozen=True, slots=True)
class _MaterialAction:
    material_id: int
    route: MaterialRoute
    source_chat_id: int
    source_message_ids: tuple[int, ...]
    is_album: bool
    title: str
    assessment: MaterialAssessment


async def admit_material_message(
    queue: JobQueue,
    message: TelegramMessage,
    *,
    debounce: timedelta = MATERIAL_DEBOUNCE,
) -> EnqueueResult | None:
    """Admit one document/photo observation after its live archive transaction commits."""

    if message.document is None and not message.photo:
        return None
    if debounce < timedelta(0):
        raise ValueError("material debounce cannot be negative")

    if message.media_group_id:
        source = MaterialSource(
            chat_id=message.chat.id,
            media_group_id=message.media_group_id,
        )
        identity = f"album:{message.chat.id}:{message.media_group_id}"
        payload: dict[str, object] = {
            "chat_id": message.chat.id,
            "media_group_id": message.media_group_id,
        }
    else:
        source = MaterialSource(chat_id=message.chat.id, message_id=message.message_id)
        identity = f"message:{message.chat.id}:{message.message_id}"
        payload = {"chat_id": source.chat_id, "message_id": source.message_id}

    observed_at = _aware(message.edit_date or message.date)
    observation = f"{message.message_id}:{observed_at.isoformat()}"
    digest = hashlib.sha256(f"{identity}:{observation}".encode()).hexdigest()[:32]
    return await queue.enqueue(
        kind=MATERIAL_ASSESS_KIND,
        idempotency_key=f"material-assess:{digest}",
        payload=payload,
        run_at=observed_at + debounce,
        max_attempts=8,
    )


class MaterialJobHandlers:
    """Assess archived material candidates and emit only durable Telegram effects."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        outbox: OutboxQueue,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._outbox = outbox
        self._clock = clock

    @property
    def mapping(self) -> Mapping[str, JobHandler]:
        return {MATERIAL_ASSESS_KIND: self.assess}

    async def assess(self, job: JobClaim) -> None:
        source = _source_from_payload(job.payload)
        target_chat_id = _required(self._settings.target_chat_id, "target_chat_id")
        if source.chat_id != target_chat_id:
            raise ValueError("material source chat does not match configured target")

        async with self._sessions() as session, session.begin():
            loaded = await _load_source(session, source)
            if not loaded.messages:
                raise MaterialSourceNotReadyError
            action = await self._assess_and_store(session, loaded)

        if action.route is MaterialRoute.AUTO_COPY:
            await self._enqueue_copy(action)
        elif action.route is MaterialRoute.ADMIN_REVIEW:
            await self._enqueue_review(action)

    async def _assess_and_store(
        self,
        session: AsyncSession,
        loaded: _LoadedSource,
    ) -> _MaterialAction:
        if len(loaded.messages) > 100:
            raise MaterialAlbumTooLargeError

        filenames = tuple(file.file_name for file in loaded.files if file.file_name)
        topic_names = tuple(dict.fromkeys(topic.name for topic in loaded.topics if topic.name))
        caption = _bounded_join(
            [
                value
                for message in loaded.messages
                for value in (message.caption, message.text)
                if value
            ],
            limit=12_000,
        )
        nearby = _bounded_join(topic_names, limit=2_000)
        extracted_text_excerpt = _bounded_join(
            [file.extracted_text for file in loaded.files if file.extracted_text],
            limit=8_000,
        )
        primary_filename = _primary_filename(filenames)
        primary_mime_type = next(
            (file.mime_type for file in loaded.files if file.mime_type),
            None,
        )
        has_photo = any(message.message_type == "photo" for message in loaded.messages)
        has_document = any(message.message_type == "document" for message in loaded.messages)
        social_topic = any(
            marker in name.casefold()
            for name in topic_names
            for marker in _SOCIAL_TOPIC_MARKERS
        )
        evidence = MaterialEvidence(
            filename=primary_filename,
            mime_type=primary_mime_type,
            caption=caption or None,
            nearby_text=nearby or None,
            extracted_text_excerpt=extracted_text_excerpt or None,
            is_photo=has_photo,
            is_social_topic=social_topic,
        )
        assessment = assess_material(
            evidence,
            auto_copy_threshold=self._settings.material_auto_copy_threshold,
            review_threshold=self._settings.material_admin_review_threshold,
        )

        anchor = loaded.messages[0]
        material = await _existing_material(session, anchor, loaded.media_group)
        now = _aware(self._clock())
        title = _material_title(primary_filename, caption, loaded.media_group is not None)
        workflow = {
            MaterialRoute.AUTO_COPY: "copy_pending",
            MaterialRoute.ADMIN_REVIEW: "review",
            MaterialRoute.LEAVE: "completed",
        }[assessment.route]
        existing_evidence = dict(material.evidence or {}) if material is not None else {}
        evidence_json: dict[str, object] = {
            **existing_evidence,
            "classifier": MATERIAL_CLASSIFIER_VERSION,
            "route": assessment.route.value,
            "reasons": list(assessment.reasons),
            "message_ids": [message.message_id for message in loaded.messages],
            "filenames": list(filenames),
            "topic_names": list(topic_names),
            "has_photo": has_photo,
            "has_document": has_document,
            "logical_count": len(loaded.messages),
            "extraction": [
                {
                    key: value
                    for key, value in {
                        "file_id": file.id,
                        "status": file.extraction_status,
                        "error_code": file.extraction_error_code,
                        "version": file.extraction_version,
                    }.items()
                    if value is not None
                }
                for file in loaded.files
                if file.extraction_status is not None
            ],
            "source_deletion_enabled": False,
            "source_deleted": False,
        }
        if material is None:
            material = Material(
                source_message_id=anchor.id,
                source_media_group_id=(loaded.media_group.id if loaded.media_group else None),
                destination_chat_id=(
                    self._settings.target_chat_id
                    if assessment.route is MaterialRoute.AUTO_COPY
                    else None
                ),
                title=title,
                discipline=_discipline(topic_names, social=social_topic),
                material_type="album" if loaded.media_group else anchor.message_type,
                confidence=assessment.probability,
                workflow_state=workflow,
                classifier_version=MATERIAL_CLASSIFIER_VERSION,
                evidence=evidence_json,
                created_at=now,
                updated_at=now,
            )
            session.add(material)
        else:
            material.title = title
            material.discipline = _discipline(topic_names, social=social_topic)
            material.material_type = "album" if loaded.media_group else anchor.message_type
            material.confidence = assessment.probability
            material.classifier_version = MATERIAL_CLASSIFIER_VERSION
            material.evidence = evidence_json
            material.updated_at = now
            material.workflow_state = workflow
            if assessment.route is MaterialRoute.AUTO_COPY:
                material.destination_chat_id = self._settings.target_chat_id

        if loaded.media_group is not None:
            loaded.media_group.finalized_at = now
        await session.flush()
        scheduled_ids = await _scheduled_source_ids(session, material.id)
        source_ids = tuple(message.message_id for message in loaded.messages)
        unscheduled_ids = tuple(
            message_id for message_id in source_ids if message_id not in scheduled_ids
        )
        route = assessment.route
        action_ids = source_ids
        if assessment.route is MaterialRoute.AUTO_COPY:
            action_ids = unscheduled_ids
            if action_ids:
                material.workflow_state = "copy_pending"
            else:
                route = MaterialRoute.LEAVE
                material.workflow_state = (
                    "completed" if material.destination_message_id is not None else "copy_pending"
                )
        elif material.destination_message_id is not None or scheduled_ids:
            route = MaterialRoute.LEAVE
            material.workflow_state = (
                "completed" if material.destination_message_id is not None else "copy_pending"
            )
        return _MaterialAction(
            material_id=material.id,
            route=route,
            source_chat_id=loaded.source.chat_id,
            source_message_ids=action_ids,
            is_album=loaded.media_group is not None and len(action_ids) > 1,
            title=title,
            assessment=assessment,
        )

    async def _enqueue_copy(self, action: _MaterialAction) -> None:
        fingerprint = _message_fingerprint(action.source_message_ids)
        metadata = {
            "material_id": action.material_id,
            "expected_count": len(action.source_message_ids),
            "source_message_ids": list(action.source_message_ids),
        }
        payload: dict[str, object] = {
            "from_chat_id": action.source_chat_id,
            "message_thread_id": _required(
                self._settings.materials_topic_id,
                "materials_topic_id",
            ),
            "_material_copy": metadata,
        }
        kind = "telegram.copy_message"
        if action.is_album:
            kind = "telegram.copy_messages"
            payload["message_ids"] = list(action.source_message_ids)
        else:
            payload["message_id"] = action.source_message_ids[0]
        await self._outbox.enqueue(
            kind=kind,
            idempotency_key=f"material-copy:{action.material_id}:{fingerprint}",
            chat_id=_required(self._settings.target_chat_id, "target_chat_id"),
            payload=payload,
            max_attempts=8,
        )

    async def _enqueue_review(self, action: _MaterialAction) -> None:
        fingerprint = _message_fingerprint(action.source_message_ids)
        reasons = ", ".join(
            _REASON_LABELS.get(reason, reason) for reason in action.assessment.reasons
        ) or "недостаточно признаков"
        link = _telegram_link(action.source_chat_id, action.source_message_ids[0])
        text = (
            "🗂 <b>Материал требует проверки</b>\n\n"
            f"{escape(action.title)}\n"
            f"Уверенность: {action.assessment.probability:.0%}\n"
            f"Признаки: {escape(reasons)}\n"
            f'<a href="{escape(link, quote=True)}">Открыть исходное сообщение</a>'
        )
        await self._outbox.enqueue(
            kind="telegram.send_message",
            idempotency_key=f"material-review:{action.material_id}:{fingerprint}",
            chat_id=_required(self._settings.headman_user_id, "headman_user_id"),
            payload={
                "text": text,
                "disable_notification": True,
            },
            max_attempts=8,
        )


async def _load_source(session: AsyncSession, source: MaterialSource) -> _LoadedSource:
    media_group: MediaGroup | None = None
    if source.media_group_id is not None:
        media_group = await session.scalar(
            select(MediaGroup).where(
                MediaGroup.chat_id == source.chat_id,
                MediaGroup.telegram_media_group_id == source.media_group_id,
            )
        )
        if media_group is None:
            raise MaterialSourceNotReadyError
        messages = tuple(
            await session.scalars(
                select(Message)
                .where(
                    Message.chat_id == source.chat_id,
                    Message.media_group_id == media_group.id,
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.message_id)
            )
        )
    else:
        assert source.message_id is not None
        message = await session.scalar(
            select(Message).where(
                Message.chat_id == source.chat_id,
                Message.message_id == source.message_id,
                Message.deleted_at.is_(None),
            )
        )
        messages = (message,) if message is not None else ()

    stored_ids = [message.id for message in messages]
    files = (
        tuple(
            await session.scalars(
                select(File).where(File.message_id.in_(stored_ids)).order_by(File.id)
            )
        )
        if stored_ids
        else ()
    )
    topic_ids = tuple(
        dict.fromkeys(message.topic_id for message in messages if message.topic_id is not None)
    )
    topics = (
        tuple(await session.scalars(select(Topic).where(Topic.id.in_(topic_ids))))
        if topic_ids
        else ()
    )
    return _LoadedSource(source, messages, files, topics, media_group)


async def _existing_material(
    session: AsyncSession,
    anchor: Message,
    media_group: MediaGroup | None,
) -> Material | None:
    if media_group is not None:
        material: Material | None = await session.scalar(
            select(Material)
            .where(Material.source_media_group_id == media_group.id)
            .order_by(Material.id)
            .limit(1)
        )
        return material
    material = await session.scalar(
        select(Material).where(Material.source_message_id == anchor.id)
    )
    return material


async def _scheduled_source_ids(session: AsyncSession, material_id: int) -> set[int]:
    rows = tuple(
        await session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.kind.in_(("telegram.copy_message", "telegram.copy_messages")),
                OutboxMessage.status.in_(("pending", "sending", "sent")),
            )
        )
    )
    scheduled: set[int] = set()
    for row in rows:
        metadata = row.payload.get("_material_copy")
        if not isinstance(metadata, dict) or metadata.get("material_id") != material_id:
            continue
        source_ids = metadata.get("source_message_ids")
        if not isinstance(source_ids, list):
            continue
        scheduled.update(
            item
            for item in source_ids
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )
    return scheduled


def _source_from_payload(payload: object) -> MaterialSource:
    if not isinstance(payload, Mapping):
        raise ValueError("material job payload must be an object")
    chat_id = _payload_int(payload, "chat_id")
    message_id = payload.get("message_id")
    media_group_id = payload.get("media_group_id")
    if message_id is not None and (
        not isinstance(message_id, int) or isinstance(message_id, bool)
    ):
        raise ValueError("material job message_id is invalid")
    if media_group_id is not None and (
        not isinstance(media_group_id, str)
        or not media_group_id
        or len(media_group_id) > 128
    ):
        raise ValueError("material job media_group_id is invalid")
    if (message_id is None) == (media_group_id is None):
        raise ValueError("material job must identify exactly one message or album")
    return MaterialSource(chat_id, message_id, media_group_id)


def _payload_int(payload: Mapping[object, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"material job payload has no {key}")
    return value


def _primary_filename(filenames: Sequence[str]) -> str | None:
    return next(
        (name for name in filenames if PurePath(name).suffix.casefold() in _ACADEMIC_SUFFIXES),
        filenames[0] if filenames else None,
    )


def _material_title(filename: str | None, caption: str, is_album: bool) -> str:
    if filename:
        return filename[:512]
    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:512]
    return "Альбом Telegram" if is_album else "Материал Telegram"


def _discipline(topic_names: Sequence[str], *, social: bool) -> str | None:
    if social:
        return None
    for name in topic_names:
        normalized = name.casefold()
        if normalized != "general" and not normalized.startswith("topic "):
            return name[:256]
    return None


def _bounded_join(values: Sequence[str], *, limit: int) -> str:
    result: list[str] = []
    used = 0
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        separator = 1 if result else 0
        remaining = limit - used - separator
        if remaining <= 0:
            break
        result.append(cleaned[:remaining])
        used += separator + len(result[-1])
    return "\n".join(result)


def _message_fingerprint(message_ids: Sequence[int]) -> str:
    serialized = ",".join(str(message_id) for message_id in message_ids)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def _telegram_link(chat_id: int, message_id: int) -> str:
    rendered = str(chat_id)
    if rendered.startswith("-100"):
        return f"https://t.me/c/{rendered[4:]}/{message_id}"
    raise ValueError("material source must be a Telegram supergroup")


def _required(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"required setting is missing: {name}")
    return value


def _aware(value: datetime | int) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("material timestamps must be timezone-aware")
    return value
