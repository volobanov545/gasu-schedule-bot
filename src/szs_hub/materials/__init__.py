"""Safe, bounded local extraction of supported learning materials."""

from szs_hub.materials.automation import (
    MATERIAL_ASSESS_KIND,
    MATERIAL_CLASSIFIER_VERSION,
    MaterialAlbumTooLargeError,
    MaterialJobHandlers,
    MaterialSourceNotReadyError,
    admit_material_message,
)
from szs_hub.materials.extractors import needs_ocr_by_density
from szs_hub.materials.models import (
    DEFAULT_EXTRACTION_LIMITS,
    AdapterOutput,
    Deadline,
    DocumentExtractor,
    DocumentKind,
    ExtractionLimits,
    ExtractionMetadata,
    ExtractionResult,
    ExtractionStatus,
    ZipMetadata,
)
from szs_hub.materials.pipeline import DocumentExtractionPipeline

__all__ = [
    "DEFAULT_EXTRACTION_LIMITS",
    "MATERIAL_ASSESS_KIND",
    "MATERIAL_CLASSIFIER_VERSION",
    "AdapterOutput",
    "Deadline",
    "DocumentExtractionPipeline",
    "DocumentExtractor",
    "DocumentKind",
    "ExtractionLimits",
    "ExtractionMetadata",
    "ExtractionResult",
    "ExtractionStatus",
    "MaterialAlbumTooLargeError",
    "MaterialJobHandlers",
    "MaterialSourceNotReadyError",
    "ZipMetadata",
    "admit_material_message",
    "needs_ocr_by_density",
]
