"""Bounded Telegram file downloads for untrusted material processing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from aiogram import Bot

TELEGRAM_CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024


class MaterialFileTooLargeError(ValueError):
    """Telegram metadata or streamed bytes exceed the fixed cloud download cap."""


class MaterialFileUnavailableError(RuntimeError):
    """Telegram returned no reusable download path for a known file ID."""


@dataclass(frozen=True, slots=True)
class MaterialDownload:
    size_bytes: int


class MaterialFileDownloader(Protocol):
    async def download(
        self,
        *,
        file_id: str,
        destination: Path,
    ) -> MaterialDownload: ...


class AiogramMaterialFileDownloader:
    """Resolve a fresh URL and stream into an exclusively created bounded file."""

    def __init__(
        self,
        bot: Bot,
        *,
        timeout_seconds: int = 30,
        max_bytes: int = TELEGRAM_CLOUD_DOWNLOAD_LIMIT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("download timeout must be positive")
        if not 1 <= max_bytes <= TELEGRAM_CLOUD_DOWNLOAD_LIMIT:
            raise ValueError("download size limit must fit the cloud Bot API limit")
        self._bot = bot
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes

    async def download(
        self,
        *,
        file_id: str,
        destination: Path,
    ) -> MaterialDownload:
        remote = await self._bot.get_file(
            file_id,
            request_timeout=self._timeout_seconds,
        )
        if remote.file_size is not None and remote.file_size > self._max_bytes:
            raise MaterialFileTooLargeError("Telegram file exceeds the download limit")
        if not remote.file_path:
            raise MaterialFileUnavailableError("Telegram returned no download path")

        with destination.open("xb") as stream:
            bounded = _BoundedWriter(stream, max_bytes=self._max_bytes)
            await self._bot.download_file(
                remote.file_path,
                destination=cast(BinaryIO, bounded),
                timeout=self._timeout_seconds,
                chunk_size=DOWNLOAD_CHUNK_SIZE,
                seek=False,
            )
            return MaterialDownload(size_bytes=bounded.bytes_written)


class _BoundedWriter:
    """The small BinaryIO surface used by aiogram's streaming downloader."""

    def __init__(self, stream: BinaryIO, *, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, content: bytes) -> int:
        next_size = self.bytes_written + len(content)
        if next_size > self._max_bytes:
            raise MaterialFileTooLargeError("Telegram stream exceeds the download limit")
        written = self._stream.write(content)
        if written != len(content):
            raise OSError("material download destination accepted a partial write")
        self.bytes_written += written
        return written

    def flush(self) -> None:
        self._stream.flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)
