"""Fail-closed file identification and archive preflight checks."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from szs_hub.materials.models import (
    Deadline,
    DocumentKind,
    ExtractionLimits,
    ExtractionStatus,
    ZipMetadata,
)

_EXTENSION_KINDS = {
    ".txt": DocumentKind.TXT,
    ".pdf": DocumentKind.PDF,
    ".docx": DocumentKind.DOCX,
    ".xlsx": DocumentKind.XLSX,
    ".pptx": DocumentKind.PPTX,
}
_CANONICAL_MIME = {
    DocumentKind.TXT: "text/plain",
    DocumentKind.PDF: "application/pdf",
    DocumentKind.DOCX: ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    DocumentKind.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocumentKind.PPTX: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}
_MIME_KINDS = {value: key for key, value in _CANONICAL_MIME.items()}
_GENERIC_MIME_TYPE = "application/octet-stream"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    size_bytes: int
    sha256: str
    modified_ns: int


@dataclass(frozen=True, slots=True)
class FilePreflight:
    kind: DocumentKind
    canonical_mime_type: str
    snapshot: FileSnapshot
    zip_metadata: ZipMetadata | None = None
    warnings: tuple[str, ...] = ()


class PreflightFailure(RuntimeError):
    def __init__(self, status: ExtractionStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def capture_file_snapshot(
    path: Path,
    *,
    limits: ExtractionLimits,
    deadline: Deadline,
) -> FileSnapshot:
    """Hash a stable regular file in bounded chunks."""

    try:
        if path.is_symlink():
            raise PreflightFailure(
                ExtractionStatus.TYPE_MISMATCH,
                "symbolic_link_rejected",
                "symbolic links are not accepted for document extraction",
            )
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_not_found",
            "document file does not exist",
        ) from exc
    except OSError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_stat_failed",
            "document file metadata could not be read",
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise PreflightFailure(
            ExtractionStatus.TYPE_MISMATCH,
            "not_a_regular_file",
            "document input must be a regular file",
        )
    if file_stat.st_size > limits.max_file_bytes:
        raise PreflightFailure(
            ExtractionStatus.LIMIT_EXCEEDED,
            "file_size_limit",
            "document exceeds the configured file size limit",
        )

    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(128 * 1024):
                deadline.check()
                total += len(chunk)
                if total > limits.max_file_bytes:
                    raise PreflightFailure(
                        ExtractionStatus.LIMIT_EXCEEDED,
                        "file_size_limit",
                        "document grew beyond the configured file size limit",
                    )
                digest.update(chunk)
    except PreflightFailure:
        raise
    except OSError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_read_failed",
            "document bytes could not be read",
        ) from exc

    try:
        final_stat = path.stat()
    except OSError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_stat_failed",
            "document file metadata could not be re-read",
        ) from exc
    if total != file_stat.st_size or _identity_changed(file_stat, final_stat):
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_changed_during_hash",
            "document changed while it was being hashed",
        )
    return FileSnapshot(total, digest.hexdigest(), final_stat.st_mtime_ns)


def preflight_document(
    path: Path,
    *,
    snapshot: FileSnapshot,
    declared_mime_type: str | None,
    limits: ExtractionLimits,
    deadline: Deadline,
) -> FilePreflight:
    extension_kind = _EXTENSION_KINDS.get(path.suffix.casefold())
    if extension_kind is None:
        raise PreflightFailure(
            ExtractionStatus.UNSUPPORTED,
            "unsupported_extension",
            "document extension is not supported",
        )

    header = _read_header(path)
    zip_metadata: ZipMetadata | None = None
    detected_kind: DocumentKind | None
    if header.startswith(b"%PDF-"):
        detected_kind = DocumentKind.PDF
    elif header.startswith(_ZIP_SIGNATURES):
        detected_kind, zip_metadata = inspect_zip_container(
            path,
            limits=limits,
            deadline=deadline,
        )
    elif header.startswith(_OLE_SIGNATURE) and extension_kind in {
        DocumentKind.DOCX,
        DocumentKind.XLSX,
        DocumentKind.PPTX,
    }:
        raise PreflightFailure(
            ExtractionStatus.PASSWORD_PROTECTED,
            "encrypted_or_legacy_office_container",
            "OOXML document is encrypted or uses an unsupported legacy Office container",
        )
    elif detect_text_encoding(header) is not None:
        detected_kind = DocumentKind.TXT
    else:
        detected_kind = None

    if detected_kind is None:
        status = (
            ExtractionStatus.CORRUPT
            if extension_kind
            in {DocumentKind.PDF, DocumentKind.DOCX, DocumentKind.XLSX, DocumentKind.PPTX}
            else ExtractionStatus.TYPE_MISMATCH
        )
        raise PreflightFailure(status, "unrecognized_magic", "document signature is invalid")
    if detected_kind is not extension_kind:
        raise PreflightFailure(
            ExtractionStatus.TYPE_MISMATCH,
            "extension_magic_mismatch",
            "document extension does not match its content signature",
        )

    warnings: list[str] = []
    if declared_mime_type:
        normalized_mime = declared_mime_type.partition(";")[0].strip().casefold()
        declared_kind = _MIME_KINDS.get(normalized_mime)
        if normalized_mime == _GENERIC_MIME_TYPE or (
            normalized_mime == "application/zip"
            and detected_kind in {DocumentKind.DOCX, DocumentKind.XLSX, DocumentKind.PPTX}
        ):
            warnings.append("generic_declared_mime_type")
        elif declared_kind is not detected_kind:
            raise PreflightFailure(
                ExtractionStatus.TYPE_MISMATCH,
                "mime_magic_mismatch",
                "declared MIME type does not match the document signature",
            )

    return FilePreflight(
        detected_kind,
        _CANONICAL_MIME[detected_kind],
        snapshot,
        zip_metadata,
        tuple(warnings),
    )


def inspect_zip_container(
    path: Path,
    *,
    limits: ExtractionLimits,
    deadline: Deadline,
) -> tuple[DocumentKind | None, ZipMetadata]:
    total_uncompressed = 0
    total_compressed = 0
    maximum_ratio = 0.0
    normalized_names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > limits.max_zip_members:
                raise PreflightFailure(
                    ExtractionStatus.LIMIT_EXCEEDED,
                    "zip_member_count_limit",
                    "archive contains too many members",
                )
            for member in members:
                deadline.check()
                normalized_name = _validate_zip_member_name(member.filename)
                if normalized_name in normalized_names:
                    raise PreflightFailure(
                        ExtractionStatus.CORRUPT,
                        "zip_duplicate_member",
                        "archive contains duplicate member paths",
                    )
                normalized_names.add(normalized_name)
                member_mode = (member.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(member_mode):
                    raise PreflightFailure(
                        ExtractionStatus.CORRUPT,
                        "unsafe_zip_member_path",
                        "archive contains a symbolic-link member",
                    )
                if member.flag_bits & 0x1:
                    raise PreflightFailure(
                        ExtractionStatus.PASSWORD_PROTECTED,
                        "encrypted_zip_member",
                        "archive contains encrypted members",
                    )
                total_uncompressed += member.file_size
                total_compressed += member.compress_size
                if total_uncompressed > limits.max_zip_uncompressed_bytes:
                    raise PreflightFailure(
                        ExtractionStatus.LIMIT_EXCEEDED,
                        "zip_uncompressed_size_limit",
                        "archive expands beyond the configured size limit",
                    )
                ratio = _compression_ratio(member.file_size, member.compress_size)
                maximum_ratio = max(maximum_ratio, ratio)
                if ratio > limits.max_zip_compression_ratio:
                    raise PreflightFailure(
                        ExtractionStatus.LIMIT_EXCEEDED,
                        "zip_compression_ratio_limit",
                        "archive member exceeds the configured compression ratio",
                    )
    except PreflightFailure:
        raise
    except (zipfile.BadZipFile, EOFError, ValueError) as exc:
        raise PreflightFailure(
            ExtractionStatus.CORRUPT,
            "corrupt_zip_container",
            "Office archive container is corrupt",
        ) from exc
    except OSError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "zip_read_failed",
            "Office archive container could not be read",
        ) from exc

    aggregate_ratio = _compression_ratio(total_uncompressed, total_compressed)
    if aggregate_ratio > limits.max_zip_compression_ratio:
        raise PreflightFailure(
            ExtractionStatus.LIMIT_EXCEEDED,
            "zip_compression_ratio_limit",
            "archive exceeds the configured aggregate compression ratio",
        )

    package_markers = {
        DocumentKind.DOCX: "word/document.xml",
        DocumentKind.XLSX: "xl/workbook.xml",
        DocumentKind.PPTX: "ppt/presentation.xml",
    }
    detected = [kind for kind, marker in package_markers.items() if marker in normalized_names]
    if len(detected) > 1:
        raise PreflightFailure(
            ExtractionStatus.CORRUPT,
            "ambiguous_ooxml_container",
            "Office archive contains markers for multiple document types",
        )
    kind = detected[0] if detected else None
    metadata = ZipMetadata(
        member_count=len(normalized_names),
        compressed_bytes=total_compressed,
        uncompressed_bytes=total_uncompressed,
        maximum_compression_ratio=round(max(maximum_ratio, aggregate_ratio), 4),
    )
    return kind, metadata


def detect_text_encoding(sample: bytes) -> str | None:
    """Accept UTF text and conservative CP1251 text; reject NUL/control-heavy data."""

    encodings: tuple[str, ...]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp1251")
    for encoding in encodings:
        try:
            decoded = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in decoded:
            continue
        controls = sum(
            1 for character in decoded if ord(character) < 32 and character not in "\n\r\t\f"
        )
        if controls <= max(1, len(decoded) // 100):
            return encoding
    return None


def _read_header(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(8192)
    except OSError as exc:
        raise PreflightFailure(
            ExtractionStatus.ERROR,
            "file_read_failed",
            "document signature could not be read",
        ) from exc


def _validate_zip_member_name(name: str) -> str:
    if not name or "\x00" in name:
        raise PreflightFailure(
            ExtractionStatus.CORRUPT,
            "unsafe_zip_member_path",
            "archive contains an invalid member path",
        )
    portable = name.replace("\\", "/")
    path = PurePosixPath(portable)
    parts = path.parts
    if (
        path.is_absolute()
        or portable.startswith("/")
        or any(part == ".." for part in parts)
        or (parts and ":" in parts[0])
    ):
        raise PreflightFailure(
            ExtractionStatus.CORRUPT,
            "unsafe_zip_member_path",
            "archive contains a traversal or absolute member path",
        )
    return path.as_posix().rstrip("/") or "."


def _compression_ratio(uncompressed: int, compressed: int) -> float:
    if uncompressed <= 0:
        return 0.0
    if compressed <= 0:
        return math.inf
    return uncompressed / compressed


def _identity_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
        or before.st_dev != after.st_dev
    )
