"""Local parser adapters. No adapter launches a process or evaluates document content."""

from __future__ import annotations

import codecs
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from szs_hub.materials.models import (
    AdapterOutput,
    Deadline,
    DeadlineExceeded,
    DocumentKind,
    ExtractionLimits,
    ExtractionStatus,
)
from szs_hub.materials.security import detect_text_encoding


class AdapterLimitExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _TextAccumulator:
    limit: int
    parts: list[str] = field(default_factory=list)
    characters: int = 0

    def add(self, value: object) -> None:
        text = str(value).strip() if value is not None else ""
        if not text:
            return
        fragment = f"\n{text}" if self.parts else text
        added = len(fragment)
        if self.characters + added > self.limit:
            raise AdapterLimitExceeded("extracted text exceeds the configured character limit")
        self.parts.append(fragment)
        self.characters += added

    def add_fragment(self, text: str) -> None:
        if not text:
            return
        if self.characters + len(text) > self.limit:
            raise AdapterLimitExceeded("extracted text exceeds the configured character limit")
        self.parts.append(text)
        self.characters += len(text)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def needs_ocr_by_density(
    *,
    text_characters: int,
    page_count: int,
    minimum_characters_per_page: int,
) -> bool:
    """Flag image-like PDFs using deterministic extracted-text density."""

    if page_count <= 0:
        return False
    return text_characters / page_count < minimum_characters_per_page


class TxtExtractor:
    kind = DocumentKind.TXT

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput:
        try:
            with path.open("rb") as stream:
                sample = stream.read(8192)
                encoding = detect_text_encoding(sample)
                if encoding is None:
                    return _failure(
                        ExtractionStatus.CORRUPT,
                        "unsupported_text_encoding",
                        "text document is binary or uses an unsupported encoding",
                    )
                stream.seek(0)
                accumulator = _TextAccumulator(limits.max_text_chars)
                decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
                while chunk := stream.read(64 * 1024):
                    deadline.check()
                    accumulator.add_fragment(decoder.decode(chunk))
                accumulator.add_fragment(decoder.decode(b"", final=True))
            return AdapterOutput(ExtractionStatus.OK, text=accumulator.text)
        except UnicodeDecodeError:
            return _failure(
                ExtractionStatus.CORRUPT,
                "invalid_text_encoding",
                "text document contains invalid encoded bytes",
            )
        except AdapterLimitExceeded:
            return _limit_failure("text_character_limit")
        except DeadlineExceeded:
            raise
        except OSError:
            return _failure(
                ExtractionStatus.ERROR,
                "text_read_failed",
                "text document could not be read",
            )


class PdfExtractor:
    kind = DocumentKind.PDF

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput:
        module = _optional_module("pypdf")
        if module is None:
            return _missing_dependency("pypdf")
        try:
            reader = module.PdfReader(str(path), strict=False)
            if bool(reader.is_encrypted):
                return _failure(
                    ExtractionStatus.PASSWORD_PROTECTED,
                    "encrypted_pdf",
                    "PDF is encrypted; passwords are not accepted by this pipeline",
                )
            page_count = len(reader.pages)
            if page_count > limits.max_pages:
                return _limit_failure("page_count_limit", page_count=page_count)

            accumulator = _TextAccumulator(limits.max_text_chars)
            largest_image_pixels = 0
            for page in reader.pages:
                deadline.check()
                page_largest = _pdf_largest_image_pixels(page, deadline=deadline)
                largest_image_pixels = max(largest_image_pixels, page_largest)
                if largest_image_pixels > limits.max_image_pixels:
                    return _limit_failure(
                        "image_pixel_limit",
                        page_count=page_count,
                        largest_image_pixels=largest_image_pixels,
                    )
                accumulator.add(page.extract_text() or "")
            needs_ocr = needs_ocr_by_density(
                text_characters=accumulator.characters,
                page_count=page_count,
                minimum_characters_per_page=limits.min_pdf_text_chars_per_page,
            )
            return AdapterOutput(
                ExtractionStatus.NEEDS_OCR if needs_ocr else ExtractionStatus.OK,
                text=accumulator.text,
                page_count=page_count,
                largest_image_pixels=largest_image_pixels or None,
                needs_ocr=needs_ocr,
            )
        except AdapterLimitExceeded:
            return _limit_failure("text_character_limit")
        except DeadlineExceeded:
            raise
        except Exception as exc:  # parser libraries expose version-specific exception classes
            return _parser_exception(exc, "pdf_parse_failed")


class DocxExtractor:
    kind = DocumentKind.DOCX

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput:
        module = _optional_module("docx")
        if module is None:
            return _missing_dependency("python-docx")
        try:
            document = module.Document(str(path))
            accumulator = _TextAccumulator(limits.max_text_chars)
            _add_paragraphs(document.paragraphs, accumulator, deadline)
            _add_tables(document.tables, accumulator, deadline)
            for section in document.sections:
                deadline.check()
                _add_paragraphs(section.header.paragraphs, accumulator, deadline)
                _add_tables(section.header.tables, accumulator, deadline)
                _add_paragraphs(section.footer.paragraphs, accumulator, deadline)
                _add_tables(section.footer.tables, accumulator, deadline)
            return AdapterOutput(ExtractionStatus.OK, text=accumulator.text)
        except AdapterLimitExceeded:
            return _limit_failure("text_character_limit")
        except DeadlineExceeded:
            raise
        except Exception as exc:
            return _parser_exception(exc, "docx_parse_failed")


class XlsxExtractor:
    kind = DocumentKind.XLSX

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput:
        module = _optional_module("openpyxl")
        if module is None:
            return _missing_dependency("openpyxl")
        workbook: Any | None = None
        try:
            workbook = module.load_workbook(
                filename=str(path),
                read_only=True,
                data_only=False,
                keep_links=False,
            )
            accumulator = _TextAccumulator(limits.max_text_chars)
            cells_seen = 0
            for worksheet in workbook.worksheets:
                deadline.check()
                rows = int(worksheet.max_row or 0)
                columns = int(worksheet.max_column or 0)
                projected_cells = rows * columns
                if cells_seen + projected_cells > limits.max_spreadsheet_cells:
                    return _limit_failure(
                        "spreadsheet_cell_limit",
                        spreadsheet_cells=cells_seen + projected_cells,
                    )
                accumulator.add(worksheet.title)
                for row in worksheet.iter_rows():
                    deadline.check()
                    cells_seen += len(row)
                    if cells_seen > limits.max_spreadsheet_cells:
                        return _limit_failure(
                            "spreadsheet_cell_limit",
                            spreadsheet_cells=cells_seen,
                        )
                    for cell in row:
                        accumulator.add(cell.value)
            return AdapterOutput(
                ExtractionStatus.OK,
                text=accumulator.text,
                spreadsheet_cells=cells_seen,
            )
        except AdapterLimitExceeded:
            return _limit_failure("text_character_limit")
        except DeadlineExceeded:
            raise
        except Exception as exc:
            return _parser_exception(exc, "xlsx_parse_failed")
        finally:
            if workbook is not None:
                close = getattr(workbook, "close", None)
                if callable(close):
                    close()


class PptxExtractor:
    kind = DocumentKind.PPTX

    def extract(
        self,
        path: Path,
        *,
        limits: ExtractionLimits,
        deadline: Deadline,
    ) -> AdapterOutput:
        module = _optional_module("pptx")
        if module is None:
            return _missing_dependency("python-pptx")
        try:
            presentation = module.Presentation(str(path))
            page_count = len(presentation.slides)
            if page_count > limits.max_pages:
                return _limit_failure("page_count_limit", page_count=page_count)
            accumulator = _TextAccumulator(limits.max_text_chars)
            for slide in presentation.slides:
                deadline.check()
                _add_pptx_shapes(slide.shapes, accumulator, deadline)
                if bool(getattr(slide, "has_notes_slide", False)):
                    notes_slide = slide.notes_slide
                    notes_frame = getattr(notes_slide, "notes_text_frame", None)
                    if notes_frame is not None:
                        accumulator.add(notes_frame.text)
            return AdapterOutput(
                ExtractionStatus.OK,
                text=accumulator.text,
                page_count=page_count,
            )
        except AdapterLimitExceeded:
            return _limit_failure("text_character_limit")
        except DeadlineExceeded:
            raise
        except Exception as exc:
            return _parser_exception(exc, "pptx_parse_failed")


def default_extractors() -> tuple[
    TxtExtractor,
    PdfExtractor,
    DocxExtractor,
    XlsxExtractor,
    PptxExtractor,
]:
    return (TxtExtractor(), PdfExtractor(), DocxExtractor(), XlsxExtractor(), PptxExtractor())


def _optional_module(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _add_paragraphs(
    paragraphs: Any,
    accumulator: _TextAccumulator,
    deadline: Deadline,
) -> None:
    for paragraph in paragraphs:
        deadline.check()
        accumulator.add(paragraph.text)


def _add_tables(tables: Any, accumulator: _TextAccumulator, deadline: Deadline) -> None:
    for table in tables:
        for row in table.rows:
            deadline.check()
            for cell in row.cells:
                _add_paragraphs(cell.paragraphs, accumulator, deadline)


def _add_pptx_shapes(shapes: Any, accumulator: _TextAccumulator, deadline: Deadline) -> None:
    for shape in shapes:
        deadline.check()
        nested_shapes = getattr(shape, "shapes", None)
        if nested_shapes is not None:
            _add_pptx_shapes(nested_shapes, accumulator, deadline)
        if bool(getattr(shape, "has_text_frame", False)):
            accumulator.add(shape.text)
        if bool(getattr(shape, "has_table", False)):
            for row in shape.table.rows:
                for cell in row.cells:
                    accumulator.add(cell.text)


def _pdf_largest_image_pixels(page: Any, *, deadline: Deadline) -> int:
    resources = _resolve_pdf_object(page.get("/Resources"))
    return _walk_pdf_xobjects(resources, deadline=deadline, seen=set(), depth=0)


def _walk_pdf_xobjects(
    resources: Any,
    *,
    deadline: Deadline,
    seen: set[int],
    depth: int,
) -> int:
    if resources is None or depth > 16:
        return 0
    deadline.check()
    resources = _resolve_pdf_object(resources)
    get = getattr(resources, "get", None)
    if not callable(get):
        return 0
    xobjects = _resolve_pdf_object(get("/XObject"))
    values = getattr(xobjects, "values", None)
    if not callable(values):
        return 0
    largest = 0
    for reference in values():
        deadline.check()
        item = _resolve_pdf_object(reference)
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        item_get = getattr(item, "get", None)
        if not callable(item_get):
            continue
        subtype = str(item_get("/Subtype"))
        if subtype == "/Image":
            try:
                width = int(item_get("/Width", 0))
                height = int(item_get("/Height", 0))
            except (TypeError, ValueError):
                continue
            largest = max(largest, max(0, width) * max(0, height))
        elif subtype == "/Form":
            largest = max(
                largest,
                _walk_pdf_xobjects(
                    item_get("/Resources"),
                    deadline=deadline,
                    seen=seen,
                    depth=depth + 1,
                ),
            )
    return largest


def _resolve_pdf_object(value: Any) -> Any:
    resolver = getattr(value, "get_object", None)
    return resolver() if callable(resolver) else value


def _parser_exception(exc: Exception, code: str) -> AdapterOutput:
    class_name = type(exc).__name__.casefold()
    if "password" in class_name or "decrypt" in class_name or "encrypted" in class_name:
        return _failure(
            ExtractionStatus.PASSWORD_PROTECTED,
            "password_protected_document",
            "document is password protected",
        )
    if any(token in class_name for token in ("badzip", "package", "parse", "readerror")):
        return _failure(
            ExtractionStatus.CORRUPT,
            code,
            "document structure is corrupt or unsupported by the local parser",
        )
    return _failure(
        ExtractionStatus.ERROR,
        code,
        "local document parser failed without executing document content",
    )


def _missing_dependency(package: str) -> AdapterOutput:
    return _failure(
        ExtractionStatus.UNSUPPORTED,
        "missing_optional_dependency",
        f"optional local parser dependency is not installed: {package}",
    )


def _limit_failure(
    code: str,
    *,
    page_count: int | None = None,
    spreadsheet_cells: int | None = None,
    largest_image_pixels: int | None = None,
) -> AdapterOutput:
    return AdapterOutput(
        ExtractionStatus.LIMIT_EXCEEDED,
        page_count=page_count,
        spreadsheet_cells=spreadsheet_cells,
        largest_image_pixels=largest_image_pixels,
        error_code=code,
        error_message="document exceeds a configured extraction limit",
    )


def _failure(status: ExtractionStatus, code: str, message: str) -> AdapterOutput:
    return AdapterOutput(status, error_code=code, error_message=message)
