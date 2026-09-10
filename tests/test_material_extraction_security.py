from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from szs_hub.materials import (
    DEFAULT_EXTRACTION_LIMITS,
    DocumentExtractionPipeline,
    ExtractionLimits,
    ExtractionStatus,
)


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_default_limits_match_the_ingestion_security_policy() -> None:
    limits = DEFAULT_EXTRACTION_LIMITS

    assert limits.max_file_bytes == 25 * 1024 * 1024
    assert limits.max_pages == 100
    assert limits.max_image_pixels == 50_000_000
    assert limits.max_spreadsheet_cells == 100_000
    assert limits.timeout_seconds == 120.0


def test_sha256_is_computed_from_file_bytes(tmp_path: Path) -> None:
    content = "учебный материал".encode()
    document = tmp_path / "material.txt"
    document.write_bytes(content)

    result = DocumentExtractionPipeline().extract(document, declared_mime_type="text/plain")

    assert result.status is ExtractionStatus.OK
    assert result.metadata.sha256 == hashlib.sha256(content).hexdigest()
    assert result.metadata.size_bytes == len(content)


def test_file_size_limit_is_fail_closed(tmp_path: Path) -> None:
    document = tmp_path / "large.txt"
    document.write_bytes(b"a" * 65)
    limits = ExtractionLimits(max_file_bytes=64)

    result = DocumentExtractionPipeline().extract(document, limits=limits)

    assert result.status is ExtractionStatus.LIMIT_EXCEEDED
    assert result.error_code == "file_size_limit"
    assert result.text == ""


def test_extension_magic_and_mime_must_be_consistent(tmp_path: Path) -> None:
    disguised_pdf = tmp_path / "disguised.txt"
    disguised_pdf.write_bytes(b"%PDF-1.7\n")
    plain_text = tmp_path / "plain.txt"
    plain_text.write_text("hello", encoding="utf-8")
    pipeline = DocumentExtractionPipeline()

    magic_result = pipeline.extract(disguised_pdf)
    mime_result = pipeline.extract(plain_text, declared_mime_type="application/pdf")

    assert magic_result.status is ExtractionStatus.TYPE_MISMATCH
    assert magic_result.error_code == "extension_magic_mismatch"
    assert mime_result.status is ExtractionStatus.TYPE_MISMATCH
    assert mime_result.error_code == "mime_magic_mismatch"


def test_generic_mime_is_allowed_but_recorded(tmp_path: Path) -> None:
    document = tmp_path / "material.txt"
    document.write_text("hello", encoding="utf-8")

    result = DocumentExtractionPipeline().extract(
        document,
        declared_mime_type="application/octet-stream",
    )

    assert result.status is ExtractionStatus.OK
    assert result.metadata.warnings == ("generic_declared_mime_type",)


def test_zip_mime_is_not_treated_as_generic_for_plain_text(tmp_path: Path) -> None:
    document = tmp_path / "material.txt"
    document.write_text("hello", encoding="utf-8")

    result = DocumentExtractionPipeline().extract(
        document,
        declared_mime_type="application/zip",
    )

    assert result.status is ExtractionStatus.TYPE_MISMATCH
    assert result.error_code == "mime_magic_mismatch"


def test_zip_traversal_is_rejected_before_parser_runs(tmp_path: Path) -> None:
    document = tmp_path / "unsafe.docx"
    _write_zip(
        document,
        {
            "word/document.xml": b"<document/>",
            "../escape": b"not extracted",
        },
    )

    result = DocumentExtractionPipeline().extract(document)

    assert result.status is ExtractionStatus.CORRUPT
    assert result.error_code == "unsafe_zip_member_path"


def test_zip_member_count_and_total_size_are_bounded(tmp_path: Path) -> None:
    member_heavy = tmp_path / "many.docx"
    _write_zip(
        member_heavy,
        {
            "word/document.xml": b"<document/>",
            "word/a.xml": b"a",
            "word/b.xml": b"b",
        },
    )
    size_heavy = tmp_path / "large.docx"
    _write_zip(size_heavy, {"word/document.xml": b"a" * 2_000})
    pipeline = DocumentExtractionPipeline()

    member_result = pipeline.extract(
        member_heavy,
        limits=ExtractionLimits(max_zip_members=2),
    )
    size_result = pipeline.extract(
        size_heavy,
        limits=ExtractionLimits(
            max_zip_uncompressed_bytes=1_000,
            max_zip_compression_ratio=1_000,
        ),
    )

    assert member_result.status is ExtractionStatus.LIMIT_EXCEEDED
    assert member_result.error_code == "zip_member_count_limit"
    assert size_result.status is ExtractionStatus.LIMIT_EXCEEDED
    assert size_result.error_code == "zip_uncompressed_size_limit"


def test_zip_compression_ratio_is_bounded(tmp_path: Path) -> None:
    document = tmp_path / "bomb.docx"
    _write_zip(document, {"word/document.xml": b"0" * 100_000})

    result = DocumentExtractionPipeline().extract(
        document,
        limits=ExtractionLimits(max_zip_compression_ratio=10),
    )

    assert result.status is ExtractionStatus.LIMIT_EXCEEDED
    assert result.error_code == "zip_compression_ratio_limit"


def test_corrupt_and_password_like_office_files_are_never_silent(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"PK\x03\x04not-a-zip")
    encrypted = tmp_path / "encrypted.docx"
    encrypted.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"encrypted package")
    pipeline = DocumentExtractionPipeline()

    corrupt_result = pipeline.extract(corrupt)
    encrypted_result = pipeline.extract(encrypted)

    assert corrupt_result.status is ExtractionStatus.CORRUPT
    assert corrupt_result.error_code == "corrupt_zip_container"
    assert encrypted_result.status is ExtractionStatus.PASSWORD_PROTECTED
    assert encrypted_result.error_code == "encrypted_or_legacy_office_container"
