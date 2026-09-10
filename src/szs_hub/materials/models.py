"""Typed contracts for bounded, local document extraction."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class DocumentKind(StrEnum):
    TXT = "txt"
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"


class ExtractionStatus(StrEnum):
    OK = "ok"
    NEEDS_OCR = "needs_ocr"
    UNSUPPORTED = "unsupported"
    TYPE_MISMATCH = "type_mismatch"
    LIMIT_EXCEEDED = "limit_exceeded"
    PASSWORD_PROTECTED = "password_protected"  # noqa: S105 - result status, not a secret
    CORRUPT = "corrupt"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """Security and resource ceilings applied before and during parsing."""

    max_file_bytes: int = 25 * 1024 * 1024
    max_pages: int = 100
    max_image_pixels: int = 50_000_000
    max_spreadsheet_cells: int = 100_000
    timeout_seconds: float = 120.0
    max_zip_members: int = 2_000
    max_zip_uncompressed_bytes: int = 100 * 1024 * 1024
    max_zip_compression_ratio: float = 100.0
    max_text_chars: int = 2_000_000
    min_pdf_text_chars_per_page: int = 50

    def __post_init__(self) -> None:
        numeric_limits = (
            self.max_file_bytes,
            self.max_pages,
            self.max_image_pixels,
            self.max_spreadsheet_cells,
            self.max_zip_members,
            self.max_zip_uncompressed_bytes,
            self.max_text_chars,
            self.min_pdf_text_chars_per_page,
        )
        if any(value <= 0 for value in numeric_limits):
            raise ValueError("extraction limits must be positive")
        if self.timeout_seconds <= 0 or self.max_zip_compression_ratio < 1:
            raise ValueError("timeout and compression ratio limits must be positive")


DEFAULT_EXTRACTION_LIMITS = ExtractionLimits()


@dataclass(frozen=True, slots=True)
class ZipMetadata:
    member_count: int
    compressed_bytes: int
    uncompressed_bytes: int
    maximum_compression_ratio: float


@dataclass(frozen=True, slots=True)
class ExtractionMetadata:
    timeout_seconds: float
    elapsed_seconds: float
    size_bytes: int | None = None
    sha256: str | None = None
    document_kind: DocumentKind | None = None
    canonical_mime_type: str | None = None
    page_count: int | None = None
    spreadsheet_cells: int | None = None
    largest_image_pixels: int | None = None
    text_characters: int = 0
    text_characters_per_page: float | None = None
    needs_ocr: bool = False
    zip: ZipMetadata | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    status: ExtractionStatus
    text: str
    metadata: ExtractionMetadata
    error_code: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {ExtractionStatus.OK, ExtractionStatus.NEEDS_OCR}


@dataclass(frozen=True, slots=True)
class AdapterOutput:
    status: ExtractionStatus
    text: str = ""
    page_count: int | None = None
    spreadsheet_cells: int | None = None
    largest_image_pixels: int | None = None
    needs_ocr: bool = False
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class DeadlineExceeded(RuntimeError):
    """Raised at cooperative cancellation points when the time budget is exhausted."""


@dataclass(frozen=True, slots=True)
class Deadline:
    started_at: float
    timeout_seconds: float

    @classmethod
    def start(cls, timeout_seconds: float) -> Deadline:
        return cls(time.monotonic(), timeout_seconds)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def check(self) -> None:
        if self.elapsed_seconds > self.timeout_seconds:
            raise DeadlineExceeded("document extraction deadline exceeded")


class DocumentExtractor(Protocol):
    kind: DocumentKind

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput: ...
