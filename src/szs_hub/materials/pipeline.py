"""Orchestration for fail-closed local document extraction."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from szs_hub.materials.extractors import default_extractors
from szs_hub.materials.models import (
    DEFAULT_EXTRACTION_LIMITS,
    AdapterOutput,
    Deadline,
    DeadlineExceeded,
    DocumentExtractor,
    DocumentKind,
    ExtractionLimits,
    ExtractionMetadata,
    ExtractionResult,
    ExtractionStatus,
)
from szs_hub.materials.security import (
    FilePreflight,
    FileSnapshot,
    PreflightFailure,
    capture_file_snapshot,
    preflight_document,
)


class DocumentExtractionPipeline:
    """Hash, identify, bound, and locally parse supported document types."""

    def __init__(self, extractors: Iterable[DocumentExtractor] | None = None) -> None:
        configured = tuple(extractors) if extractors is not None else default_extractors()
        self._extractors: dict[DocumentKind, DocumentExtractor] = {}
        for extractor in configured:
            if extractor.kind in self._extractors:
                raise ValueError(f"duplicate extractor for {extractor.kind}")
            self._extractors[extractor.kind] = extractor

    def extract(
        self,
        path: str | Path,
        *,
        declared_mime_type: str | None = None,
        limits: ExtractionLimits = DEFAULT_EXTRACTION_LIMITS,
    ) -> ExtractionResult:
        document_path = Path(path)
        deadline = Deadline.start(limits.timeout_seconds)
        snapshot: FileSnapshot | None = None
        preflight: FilePreflight | None = None
        try:
            snapshot = capture_file_snapshot(document_path, limits=limits, deadline=deadline)
            preflight = preflight_document(
                document_path,
                snapshot=snapshot,
                declared_mime_type=declared_mime_type,
                limits=limits,
                deadline=deadline,
            )
            extractor = self._extractors.get(preflight.kind)
            if extractor is None:
                return self._result(
                    deadline=deadline,
                    limits=limits,
                    snapshot=snapshot,
                    preflight=preflight,
                    output=AdapterOutput(
                        ExtractionStatus.UNSUPPORTED,
                        error_code="extractor_not_configured",
                        error_message="no local extractor is configured for this document type",
                    ),
                )
            output = extractor.extract(
                document_path,
                limits=limits,
                deadline=deadline,
            )
            deadline.check()
            final_snapshot = capture_file_snapshot(
                document_path,
                limits=limits,
                deadline=deadline,
            )
            if final_snapshot.sha256 != snapshot.sha256:
                output = AdapterOutput(
                    ExtractionStatus.ERROR,
                    error_code="file_changed_during_extraction",
                    error_message="document changed while it was being extracted",
                )
            return self._result(
                deadline=deadline,
                limits=limits,
                snapshot=snapshot,
                preflight=preflight,
                output=output,
            )
        except DeadlineExceeded:
            return self._failure_result(
                ExtractionStatus.TIMEOUT,
                "extraction_timeout",
                "document extraction exceeded its cooperative time budget",
                deadline=deadline,
                limits=limits,
                snapshot=snapshot,
                preflight=preflight,
            )
        except PreflightFailure as exc:
            return self._failure_result(
                exc.status,
                exc.code,
                str(exc),
                deadline=deadline,
                limits=limits,
                snapshot=snapshot,
                preflight=preflight,
            )
        except Exception:
            return self._failure_result(
                ExtractionStatus.ERROR,
                "unexpected_pipeline_error",
                "document extraction failed without executing document content",
                deadline=deadline,
                limits=limits,
                snapshot=snapshot,
                preflight=preflight,
            )

    @staticmethod
    def _result(
        *,
        deadline: Deadline,
        limits: ExtractionLimits,
        snapshot: FileSnapshot,
        preflight: FilePreflight,
        output: AdapterOutput,
    ) -> ExtractionResult:
        page_density = None
        if output.page_count:
            page_density = len(output.text) / output.page_count
        metadata = ExtractionMetadata(
            timeout_seconds=limits.timeout_seconds,
            elapsed_seconds=deadline.elapsed_seconds,
            size_bytes=snapshot.size_bytes,
            sha256=snapshot.sha256,
            document_kind=preflight.kind,
            canonical_mime_type=preflight.canonical_mime_type,
            page_count=output.page_count,
            spreadsheet_cells=output.spreadsheet_cells,
            largest_image_pixels=output.largest_image_pixels,
            text_characters=len(output.text),
            text_characters_per_page=page_density,
            needs_ocr=output.needs_ocr,
            zip=preflight.zip_metadata,
            warnings=preflight.warnings + output.warnings,
        )
        return ExtractionResult(
            output.status,
            output.text,
            metadata,
            output.error_code,
            output.error_message,
        )

    @staticmethod
    def _failure_result(
        status: ExtractionStatus,
        code: str,
        message: str,
        *,
        deadline: Deadline,
        limits: ExtractionLimits,
        snapshot: FileSnapshot | None,
        preflight: FilePreflight | None,
    ) -> ExtractionResult:
        metadata = ExtractionMetadata(
            timeout_seconds=limits.timeout_seconds,
            elapsed_seconds=deadline.elapsed_seconds,
            size_bytes=snapshot.size_bytes if snapshot else None,
            sha256=snapshot.sha256 if snapshot else None,
            document_kind=preflight.kind if preflight else None,
            canonical_mime_type=preflight.canonical_mime_type if preflight else None,
            zip=preflight.zip_metadata if preflight else None,
            warnings=preflight.warnings if preflight else (),
        )
        return ExtractionResult(status, "", metadata, code, message)
