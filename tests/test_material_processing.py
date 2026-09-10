from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import Message as TelegramMessage
from sqlalchemy import func, select

from szs_hub.jobs.queue import JobClaim, JobQueue
from szs_hub.materials.models import (
    DEFAULT_EXTRACTION_LIMITS,
    ExtractionLimits,
    ExtractionMetadata,
    ExtractionResult,
    ExtractionStatus,
)
from szs_hub.materials.pipeline import DocumentExtractionPipeline
from szs_hub.materials.processing import (
    MATERIAL_EXTRACT_KIND,
    MATERIAL_EXTRACTION_VERSION,
    MaterialExtractionJobHandlers,
    admit_material_extraction,
)
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.fts import SearchRepository
from szs_hub.storage.models import File, Job, Message, SearchDocument
from szs_hub.telegram.file_download import (
    TELEGRAM_CLOUD_DOWNLOAD_LIMIT,
    AiogramMaterialFileDownloader,
    MaterialDownload,
    MaterialFileTooLargeError,
)

TARGET_CHAT_ID = -100_987_654_321
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class BytesDownloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[str, Path]] = []

    async def download(self, *, file_id: str, destination: Path) -> MaterialDownload:
        self.calls.append((file_id, destination))
        await asyncio.to_thread(destination.write_bytes, self.content)
        return MaterialDownload(size_bytes=len(self.content))


class RaisingDownloader:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def download(self, *, file_id: str, destination: Path) -> MaterialDownload:
        self.calls += 1
        raise self.error


class RecordingPipeline:
    def __init__(self) -> None:
        self.thread_id: int | None = None

    def extract(
        self,
        path: str | Path,
        *,
        declared_mime_type: str | None = None,
        limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
    ) -> ExtractionResult:
        self.thread_id = threading.get_ident()
        return DocumentExtractionPipeline().extract(
            path,
            declared_mime_type=declared_mime_type,
            limits=limits,
        )


class BlockingPipeline:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.path: Path | None = None

    def extract(
        self,
        path: str | Path,
        *,
        declared_mime_type: str | None = None,
        limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
    ) -> ExtractionResult:
        self.path = Path(path)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test parser was not released")
        assert self.path.is_file()
        content = self.path.read_bytes()
        return ExtractionResult(
            status=ExtractionStatus.OK,
            text=content.decode(),
            metadata=ExtractionMetadata(
                timeout_seconds=limits.timeout_seconds,
                elapsed_seconds=0.01,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                text_characters=len(content),
            ),
        )


@dataclass
class FakeRemoteFile:
    file_path: str | None
    file_size: int | None


class FakeBot:
    def __init__(self, payload: bytes, *, reported_size: int | None = None) -> None:
        self.payload = payload
        self.reported_size = reported_size
        self.get_calls: list[tuple[str, int]] = []
        self.download_calls: list[dict[str, Any]] = []

    async def get_file(self, file_id: str, *, request_timeout: int) -> FakeRemoteFile:
        self.get_calls.append((file_id, request_timeout))
        return FakeRemoteFile("documents/remote", self.reported_size)

    async def download_file(self, file_path: str, **kwargs: Any) -> object:
        self.download_calls.append({"file_path": file_path, **kwargs})
        destination = kwargs["destination"]
        destination.write(self.payload)
        destination.flush()
        return destination


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _claim(message_id: int = 10, unique_id: str = "unique-file") -> JobClaim:
    return JobClaim(
        id=1,
        kind=MATERIAL_EXTRACT_KIND,
        idempotency_key="material-extract:test",
        payload={
            "chat_id": TARGET_CHAT_ID,
            "message_id": message_id,
            "telegram_file_unique_id": unique_id,
        },
        attempt=1,
        max_attempts=8,
        leased_until=NOW + timedelta(minutes=5),
    )


async def _seed_document(
    sessions: Any,
    *,
    message_id: int = 10,
    unique_id: str = "unique-file",
    file_name: str = "manual.txt",
    mime_type: str = "text/plain",
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> tuple[int, int]:
    async with sessions() as session, session.begin():
        message = Message(
            chat_id=TARGET_CHAT_ID,
            message_id=message_id,
            source="bot_api",
            message_type="document",
            sent_at=NOW,
        )
        session.add(message)
        await session.flush()
        file = File(
            message_id=message.id,
            telegram_file_id=f"file-{unique_id}",
            telegram_file_unique_id=unique_id,
            kind="document",
            mime_type=mime_type,
            file_name=file_name,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        session.add(file)
        await session.flush()
        return message.id, file.id


def _handler(
    sessions: Any,
    downloader: Any,
    tmp_path: Path,
    *,
    pipeline: Any | None = None,
    limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
) -> MaterialExtractionJobHandlers:
    return MaterialExtractionJobHandlers(
        target_chat_id=TARGET_CHAT_ID,
        session_factory=sessions,
        jobs=JobQueue(sessions, worker_id="material-test", clock=lambda: NOW),
        downloader=downloader,
        pipeline=pipeline,
        limits=limits,
        temporary_root=tmp_path,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_document_admission_is_durable_and_idempotent(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "admission.sqlite3"))
    sessions = create_session_factory(engine)
    try:
        await create_schema(engine)
        queue = JobQueue(sessions, worker_id="admission", clock=lambda: NOW)
        document = TelegramMessage.model_validate(
            {
                "message_id": 10,
                "date": NOW,
                "chat": {"id": TARGET_CHAT_ID, "type": "supergroup", "title": "SZS"},
                "document": {
                    "file_id": "file-one",
                    "file_unique_id": "unique-file",
                    "file_name": "manual.txt",
                    "mime_type": "text/plain",
                },
            }
        )
        photo = TelegramMessage.model_validate(
            {
                "message_id": 11,
                "date": NOW,
                "chat": {"id": TARGET_CHAT_ID, "type": "supergroup", "title": "SZS"},
                "photo": [
                    {"file_id": "photo", "file_unique_id": "photo", "width": 10, "height": 10}
                ],
            }
        )

        first = await admit_material_extraction(queue, document)
        duplicate = await admit_material_extraction(queue, document)

        assert first is not None and first.created is True
        assert duplicate is not None and duplicate.created is False
        assert await admit_material_extraction(queue, photo) is None
        async with sessions() as session:
            stored = tuple(await session.scalars(select(Job)))
        assert len(stored) == 1
        assert stored[0].kind == MATERIAL_EXTRACT_KIND
        assert stored[0].payload["telegram_file_unique_id"] == "unique-file"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_txt_extraction_runs_off_loop_indexes_and_reassesses(tmp_path: Path) -> None:
    content = "Методичка по геодезии: нивелирование".encode()
    engine = create_database_engine(_url(tmp_path / "happy.sqlite3"))
    sessions = create_session_factory(engine)
    downloader = BytesDownloader(content)
    pipeline = RecordingPipeline()
    main_thread = threading.get_ident()
    try:
        await create_schema(engine)
        message_pk, file_pk = await _seed_document(
            sessions,
            file_name="../../manual.txt",
            size_bytes=len(content),
        )
        handler = _handler(sessions, downloader, tmp_path, pipeline=pipeline)

        await handler.extract(_claim())
        await handler.extract(_claim())

        assert pipeline.thread_id is not None and pipeline.thread_id != main_thread
        assert len(downloader.calls) == 1
        downloaded_path = downloader.calls[0][1]
        assert downloaded_path.name == "input.txt"
        assert downloaded_path.parent.parent == tmp_path
        assert not downloaded_path.parent.exists()
        async with sessions() as session:
            file = await session.get(File, file_pk)
            document = await session.scalar(
                select(SearchDocument).where(
                    SearchDocument.source_type == "message",
                    SearchDocument.source_id == message_pk,
                )
            )
            jobs = tuple(
                await session.scalars(select(Job).where(Job.kind == "material.assess"))
            )
            hits = await SearchRepository(session).search("нивелирование")
        assert file is not None
        assert file.sha256 == hashlib.sha256(content).hexdigest()
        assert file.extraction_status == ExtractionStatus.OK.value
        assert file.extraction_error_code is None
        assert file.extraction_version == MATERIAL_EXTRACTION_VERSION
        assert file.extracted_at == NOW
        assert file.extracted_text == content.decode()
        assert file.storage_path is None
        assert document is not None and "нивелирование" in document.content
        assert document.metadata_json is not None
        assert document.metadata_json["files"][0]["extraction_status"] == "ok"
        assert len(jobs) == 1
        assert jobs[0].payload == {"chat_id": TARGET_CHAT_ID, "message_id": 10}
        assert len(hits) == 1 and hits[0].source_id == message_pk
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "mime_type", "size_bytes", "expected_code"),
    [
        (
            "manual.txt",
            "text/plain",
            TELEGRAM_CLOUD_DOWNLOAD_LIMIT + 1,
            "telegram_file_size_limit",
        ),
        ("payload.exe", "application/pdf", 100, "unsupported_extension"),
    ],
)
async def test_policy_rejection_is_terminal_without_downloading(
    tmp_path: Path,
    file_name: str,
    mime_type: str,
    size_bytes: int,
    expected_code: str,
) -> None:
    engine = create_database_engine(_url(tmp_path / f"{expected_code}.sqlite3"))
    sessions = create_session_factory(engine)
    downloader = RaisingDownloader(AssertionError("download must not be called"))
    try:
        await create_schema(engine)
        _, file_pk = await _seed_document(
            sessions,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )

        await _handler(sessions, downloader, tmp_path).extract(_claim())

        assert downloader.calls == 0
        async with sessions() as session:
            file = await session.get(File, file_pk)
        assert file is not None
        expected_status = (
            ExtractionStatus.LIMIT_EXCEEDED.value
            if expected_code == "telegram_file_size_limit"
            else ExtractionStatus.UNSUPPORTED.value
        )
        assert file.extraction_status == expected_status
        assert file.extraction_error_code == expected_code
        assert file.extracted_text is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_network_failure_retries_without_persisting_terminal_state(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "network.sqlite3"))
    sessions = create_session_factory(engine)
    downloader = RaisingDownloader(ConnectionError("private network detail"))
    try:
        await create_schema(engine)
        _, file_pk = await _seed_document(sessions, size_bytes=5)

        with pytest.raises(ConnectionError, match="private network detail"):
            await _handler(sessions, downloader, tmp_path).extract(_claim())

        temporary = await asyncio.to_thread(
            lambda: list(tmp_path.glob("szs-hub-material-*"))
        )
        assert temporary == []
        async with sessions() as session:
            file = await session.get(File, file_pk)
            reassessments = await session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "material.assess")
            )
        assert file is not None and file.extraction_status is None
        assert reassessments == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stable_file_identity_rejects_changed_content_hash(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "identity.sqlite3"))
    sessions = create_session_factory(engine)
    content = b"new content"
    try:
        await create_schema(engine)
        _, file_pk = await _seed_document(
            sessions,
            size_bytes=len(content),
            sha256="0" * 64,
        )

        with pytest.raises(RuntimeError, match="stable unique ID"):
            await _handler(sessions, BytesDownloader(content), tmp_path).extract(_claim())

        async with sessions() as session:
            file = await session.get(File, file_pk)
        assert file is not None
        assert file.sha256 == "0" * 64
        assert file.extraction_status is None
        temporary = await asyncio.to_thread(
            lambda: list(tmp_path.glob("szs-hub-material-*"))
        )
        assert temporary == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_waits_for_parser_before_temp_cleanup(tmp_path: Path) -> None:
    engine = create_database_engine(_url(tmp_path / "cancel.sqlite3"))
    sessions = create_session_factory(engine)
    pipeline = BlockingPipeline()
    try:
        await create_schema(engine)
        _, file_pk = await _seed_document(sessions, size_bytes=5)
        handler = _handler(
            sessions,
            BytesDownloader(b"hello"),
            tmp_path,
            pipeline=pipeline,
        )
        task = asyncio.create_task(handler.extract(_claim()))
        assert await asyncio.to_thread(pipeline.started.wait, 2)
        assert pipeline.path is not None and pipeline.path.is_file()

        task.cancel()
        await asyncio.sleep(0)
        assert pipeline.path.is_file()
        pipeline.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert not pipeline.path.parent.exists()
        async with sessions() as session:
            file = await session.get(File, file_pk)
        assert file is not None and file.extraction_status is None
    finally:
        pipeline.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_aiogram_downloader_enforces_metadata_and_stream_caps(tmp_path: Path) -> None:
    oversize_bot = FakeBot(b"", reported_size=TELEGRAM_CLOUD_DOWNLOAD_LIMIT + 1)
    oversize = AiogramMaterialFileDownloader(oversize_bot)  # type: ignore[arg-type]
    with pytest.raises(MaterialFileTooLargeError):
        await oversize.download(file_id="large", destination=tmp_path / "metadata.bin")
    assert not (tmp_path / "metadata.bin").exists()

    streamed_bot = FakeBot(b"x" * 9, reported_size=None)
    streamed = AiogramMaterialFileDownloader(  # type: ignore[arg-type]
        streamed_bot,
        max_bytes=8,
    )
    with pytest.raises(MaterialFileTooLargeError):
        await streamed.download(file_id="stream", destination=tmp_path / "stream.bin")
    assert (tmp_path / "stream.bin").stat().st_size == 0
