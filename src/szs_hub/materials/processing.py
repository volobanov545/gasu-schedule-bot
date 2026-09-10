"""Durable download and local extraction of newly archived Telegram documents."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from aiogram.types import Message as TelegramMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.archive.indexing import upsert_message_search_document
from szs_hub.jobs.queue import EnqueueResult, JobClaim, JobQueue
from szs_hub.jobs.worker import JobHandler
from szs_hub.materials.automation import MATERIAL_ASSESS_KIND
from szs_hub.materials.models import (
    DEFAULT_EXTRACTION_LIMITS,
    ExtractionLimits,
    ExtractionMetadata,
    ExtractionResult,
    ExtractionStatus,
)
from szs_hub.materials.pipeline import DocumentExtractionPipeline
from szs_hub.storage.base import utc_now
from szs_hub.storage.models import File, MediaGroup, Message
from szs_hub.telegram.file_download import (
    TELEGRAM_CLOUD_DOWNLOAD_LIMIT,
    MaterialFileDownloader,
    MaterialFileTooLargeError,
)

MATERIAL_EXTRACT_KIND = "material.extract"
MATERIAL_EXTRACTION_VERSION = "local-documents-v1"
_SUPPORTED_EXTENSIONS = frozenset({".docx", ".pdf", ".pptx", ".txt", ".xlsx"})
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


class ExtractionPipeline(Protocol):
    def extract(
        self,
        path: str | Path,
        *,
        declared_mime_type: str | None = None,
        limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
    ) -> ExtractionResult: ...


@dataclass(frozen=True, slots=True)
class _Candidate:
    file_id: int
    stored_message_id: int
    chat_id: int
    message_id: int
    media_group_id: str | None
    telegram_file_id: str
    telegram_file_unique_id: str
    mime_type: str | None
    file_name: str | None
    size_bytes: int | None
    sha256: str | None
    extraction_status: str | None
    extraction_error_code: str | None
    extraction_version: str | None


@dataclass(frozen=True, slots=True)
class _Reassessment:
    payload: dict[str, object]
    fingerprint: str


async def admit_material_extraction(
    queue: JobQueue,
    message: TelegramMessage,
) -> EnqueueResult | None:
    """Admit exactly one document version after its archive transaction committed."""

    document = message.document
    if document is None:
        return None
    observed_at = _aware(message.edit_date or message.date)
    identity = (
        f"{message.chat.id}:{message.message_id}:{document.file_unique_id}:"
        f"{MATERIAL_EXTRACTION_VERSION}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return await queue.enqueue(
        kind=MATERIAL_EXTRACT_KIND,
        idempotency_key=f"material-extract:{digest}",
        payload={
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "telegram_file_unique_id": document.file_unique_id,
        },
        run_at=observed_at,
        max_attempts=8,
    )


class MaterialExtractionJobHandlers:
    """Download one bounded document, parse it off-loop, persist, and reassess."""

    def __init__(
        self,
        *,
        target_chat_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: JobQueue,
        downloader: MaterialFileDownloader,
        pipeline: ExtractionPipeline | None = None,
        limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
        temporary_root: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if target_chat_id >= 0:
            raise ValueError("material extraction target must be a Telegram group")
        if limits.max_file_bytes > TELEGRAM_CLOUD_DOWNLOAD_LIMIT:
            limits = ExtractionLimits(
                max_file_bytes=TELEGRAM_CLOUD_DOWNLOAD_LIMIT,
                max_pages=limits.max_pages,
                max_image_pixels=limits.max_image_pixels,
                max_spreadsheet_cells=limits.max_spreadsheet_cells,
                timeout_seconds=limits.timeout_seconds,
                max_zip_members=limits.max_zip_members,
                max_zip_uncompressed_bytes=limits.max_zip_uncompressed_bytes,
                max_zip_compression_ratio=limits.max_zip_compression_ratio,
                max_text_chars=limits.max_text_chars,
                min_pdf_text_chars_per_page=limits.min_pdf_text_chars_per_page,
            )
        if temporary_root is not None and not temporary_root.is_dir():
            raise ValueError("material extraction temporary root must be an existing directory")
        self._target_chat_id = target_chat_id
        self._sessions = session_factory
        self._jobs = jobs
        self._downloader = downloader
        self._pipeline = pipeline or DocumentExtractionPipeline()
        self._limits = limits
        self._temporary_root = temporary_root
        self._clock = clock

    @property
    def mapping(self) -> Mapping[str, JobHandler]:
        return {MATERIAL_EXTRACT_KIND: self.extract}

    async def extract(self, job: JobClaim) -> None:
        chat_id, message_id, unique_id = _payload(job.payload)
        if chat_id != self._target_chat_id:
            raise ValueError("material extraction source chat does not match configured target")
        candidate = await self._load_candidate(chat_id, message_id, unique_id)
        if candidate is None:
            return

        if (
            candidate.extraction_version == MATERIAL_EXTRACTION_VERSION
            and candidate.extraction_status is not None
        ):
            await self._enqueue_reassessment(_reassessment_from_candidate(candidate))
            return

        result = await self._process(candidate)
        reassessment = await self._persist(candidate, result)
        await self._enqueue_reassessment(reassessment)

    async def _load_candidate(
        self,
        chat_id: int,
        message_id: int,
        unique_id: str,
    ) -> _Candidate | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(File, Message, MediaGroup.telegram_media_group_id)
                    .join(Message, Message.id == File.message_id)
                    .outerjoin(MediaGroup, MediaGroup.id == Message.media_group_id)
                    .where(
                        Message.chat_id == chat_id,
                        Message.message_id == message_id,
                        Message.deleted_at.is_(None),
                        File.telegram_file_unique_id == unique_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            file, message, media_group_id = row
            if message.source != "bot_api" or file.kind != "document":
                return None
            if file.telegram_file_id.startswith("telegram-export-local:"):
                return None
            return _Candidate(
                file_id=file.id,
                stored_message_id=message.id,
                chat_id=message.chat_id,
                message_id=message.message_id,
                media_group_id=media_group_id,
                telegram_file_id=file.telegram_file_id,
                telegram_file_unique_id=file.telegram_file_unique_id,
                mime_type=file.mime_type,
                file_name=file.file_name,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                extraction_status=file.extraction_status,
                extraction_error_code=file.extraction_error_code,
                extraction_version=file.extraction_version,
            )

    async def _process(self, candidate: _Candidate) -> ExtractionResult:
        if (
            candidate.size_bytes is not None
            and candidate.size_bytes > self._limits.max_file_bytes
        ):
            return _terminal_result(
                ExtractionStatus.LIMIT_EXCEEDED,
                "telegram_file_size_limit",
                limits=self._limits,
                size_bytes=candidate.size_bytes,
            )
        suffix = _safe_extension(candidate.file_name, candidate.mime_type)
        if suffix is None:
            return _terminal_result(
                ExtractionStatus.UNSUPPORTED,
                "unsupported_extension",
                limits=self._limits,
                size_bytes=candidate.size_bytes,
            )

        parent = str(self._temporary_root) if self._temporary_root is not None else None
        with tempfile.TemporaryDirectory(prefix="szs-hub-material-", dir=parent) as directory:
            path = Path(directory) / f"input{suffix}"
            try:
                await self._downloader.download(
                    file_id=candidate.telegram_file_id,
                    destination=path,
                )
            except MaterialFileTooLargeError:
                return _terminal_result(
                    ExtractionStatus.LIMIT_EXCEEDED,
                    "telegram_download_size_limit",
                    limits=self._limits,
                    size_bytes=candidate.size_bytes,
                )
            return await _extract_off_loop(
                self._pipeline,
                path,
                declared_mime_type=candidate.mime_type,
                limits=self._limits,
            )

    async def _persist(
        self,
        candidate: _Candidate,
        result: ExtractionResult,
    ) -> _Reassessment:
        async with self._sessions() as session, session.begin():
            stored = await session.get(File, candidate.file_id)
            message = await session.get(Message, candidate.stored_message_id)
            if stored is None or message is None:
                raise RuntimeError("archived material disappeared during extraction")
            if stored.telegram_file_unique_id != candidate.telegram_file_unique_id:
                raise RuntimeError("archived material identity changed during extraction")
            new_sha = result.metadata.sha256
            if stored.sha256 is not None and new_sha is not None and stored.sha256 != new_sha:
                raise RuntimeError("Telegram file content changed for a stable unique ID")

            stored.sha256 = new_sha or stored.sha256
            stored.extracted_text = result.text if result.succeeded else None
            stored.extraction_status = result.status.value
            stored.extraction_error_code = (
                result.error_code[:100] if result.error_code is not None else None
            )
            stored.extraction_version = MATERIAL_EXTRACTION_VERSION
            stored.extracted_at = _aware(self._clock())
            # Bot API blobs are reusable in Telegram; plaintext is deliberately temporary.
            stored.storage_path = None
            await session.flush()
            await upsert_message_search_document(session, message)
            await session.flush()
            return _reassessment_from_stored(stored, message, candidate.media_group_id)

    async def _enqueue_reassessment(self, reassessment: _Reassessment) -> None:
        await self._jobs.enqueue(
            kind=MATERIAL_ASSESS_KIND,
            idempotency_key=(
                f"material-reassess:{MATERIAL_EXTRACTION_VERSION}:"
                f"{reassessment.fingerprint}"
            ),
            payload=reassessment.payload,
            run_at=_aware(self._clock()),
            max_attempts=8,
        )


async def _extract_off_loop(
    pipeline: ExtractionPipeline,
    path: Path,
    *,
    declared_mime_type: str | None,
    limits: ExtractionLimits,
) -> ExtractionResult:
    """Delay cancellation until the parser releases its temporary input file."""

    work = asyncio.create_task(
        asyncio.to_thread(
            pipeline.extract,
            path,
            declared_mime_type=declared_mime_type,
            limits=limits,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            cancellation = error
    result = work.result()
    if cancellation is not None:
        raise cancellation
    return result


def _payload(payload: object) -> tuple[int, int, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("material extraction payload must be an object")
    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    unique_id = payload.get("telegram_file_unique_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ValueError("material extraction payload has no chat_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("material extraction payload has no valid message_id")
    if not isinstance(unique_id, str) or not unique_id or len(unique_id) > 256:
        raise ValueError("material extraction payload has no valid file identity")
    return chat_id, message_id, unique_id


def _safe_extension(file_name: str | None, mime_type: str | None) -> str | None:
    suffix: str | None = None
    if file_name:
        suffix = PurePosixPath(file_name.replace("\\", "/")).suffix.casefold()
        if suffix:
            return suffix if suffix in _SUPPORTED_EXTENSIONS else None
    normalized_mime = (mime_type or "").partition(";")[0].strip().casefold()
    return _MIME_EXTENSIONS.get(normalized_mime)


def _terminal_result(
    status: ExtractionStatus,
    error_code: str,
    *,
    limits: ExtractionLimits,
    size_bytes: int | None,
) -> ExtractionResult:
    return ExtractionResult(
        status=status,
        text="",
        metadata=ExtractionMetadata(
            timeout_seconds=limits.timeout_seconds,
            elapsed_seconds=0.0,
            size_bytes=size_bytes,
        ),
        error_code=error_code,
        error_message="document is outside the bounded live extraction policy",
    )


def _reassessment_from_candidate(candidate: _Candidate) -> _Reassessment:
    return _reassessment(
        file_id=candidate.file_id,
        chat_id=candidate.chat_id,
        message_id=candidate.message_id,
        media_group_id=candidate.media_group_id,
        status=candidate.extraction_status,
        sha256=candidate.sha256,
        error_code=candidate.extraction_error_code,
    )


def _reassessment_from_stored(
    file: File,
    message: Message,
    media_group_id: str | None,
) -> _Reassessment:
    return _reassessment(
        file_id=file.id,
        chat_id=message.chat_id,
        message_id=message.message_id,
        media_group_id=media_group_id,
        status=file.extraction_status,
        sha256=file.sha256,
        error_code=file.extraction_error_code,
    )


def _reassessment(
    *,
    file_id: int,
    chat_id: int,
    message_id: int,
    media_group_id: str | None,
    status: str | None,
    sha256: str | None,
    error_code: str | None,
) -> _Reassessment:
    payload: dict[str, object]
    if media_group_id is not None:
        payload = {"chat_id": chat_id, "media_group_id": media_group_id}
    else:
        payload = {"chat_id": chat_id, "message_id": message_id}
    identity = f"{file_id}:{status or 'unknown'}:{sha256 or error_code or 'no-content'}"
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return _Reassessment(payload, fingerprint)


def _aware(value: datetime | int) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("material extraction timestamps must be timezone-aware")
    return value
