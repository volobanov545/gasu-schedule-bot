"""Conservative material classification and copy-before-delete workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePath

_ACADEMIC_EXTENSIONS = {
    ".doc",
    ".docx",
    ".dwg",
    ".pdf",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
_STRONG_ACADEMIC_PATTERNS = (
    r"\bметодич(?:ка|еск\w*)\b",
    r"\bзадан(?:ие|ия|ию)\b",
    r"\bпример\s+расч[её]та\b",
    r"\bпрезентаци\w*\b",
    r"\bлекци\w*\b",
    r"\bконспект\w*\b",
    r"\bс\s+доски\b",
    r"\bрасч[её]тн\w*\s+работ\w*\b",
)
_SOCIAL_PATTERNS = (
    r"\bмем\w*\b",
    r"\bмерс\w*\b",
    r"\bтачк\w*\b",
    r"\bах+а+х*\b",
    r"\bрофл\w*\b",
    r"\bприкол\w*\b",
)


class MaterialRoute(StrEnum):
    AUTO_COPY = "auto_copy"
    ADMIN_REVIEW = "admin_review"
    LEAVE = "leave"


@dataclass(frozen=True, slots=True)
class MaterialEvidence:
    filename: str | None = None
    mime_type: str | None = None
    caption: str | None = None
    nearby_text: str | None = None
    extracted_text_excerpt: str | None = None
    known_discipline_aliases: tuple[str, ...] = ()
    is_photo: bool = False
    is_social_topic: bool = False

    @property
    def combined_text(self) -> str:
        return " ".join(
            value
            for value in (
                self.caption,
                self.nearby_text,
                self.filename,
                self.extracted_text_excerpt,
            )
            if value
        ).casefold().replace("ё", "е")


@dataclass(frozen=True, slots=True)
class MaterialAssessment:
    probability: float
    route: MaterialRoute
    reasons: tuple[str, ...]


def assess_material(
    evidence: MaterialEvidence,
    *,
    auto_copy_threshold: float = 0.95,
    review_threshold: float = 0.70,
) -> MaterialAssessment:
    """Apply a transparent fail-safe first-pass classifier.

    An AI classifier may raise confidence later, but a file type alone can never trigger
    destructive automation.
    """

    score = 0.0
    reasons: list[str] = []
    text = evidence.combined_text
    suffix = PurePath(evidence.filename or "").suffix.casefold()

    if suffix in _ACADEMIC_EXTENSIONS:
        score += 0.25
        reasons.append("academic_file_type")
    if evidence.is_photo:
        score += 0.03
        reasons.append("photo_requires_context")

    strong_hits = [pattern for pattern in _STRONG_ACADEMIC_PATTERNS if re.search(pattern, text)]
    if strong_hits:
        score += 0.72
        reasons.append("explicit_academic_context")
        if evidence.is_photo:
            score += 0.22
            reasons.append("explicit_academic_photo")

    aliases = [alias.casefold().replace("ё", "е") for alias in evidence.known_discipline_aliases]
    if any(alias and alias in text for alias in aliases):
        score += 0.12
        reasons.append("known_discipline")

    if evidence.extracted_text_excerpt and len(evidence.extracted_text_excerpt.strip()) >= 120:
        score += 0.05
        reasons.append("extractable_document_text")

    if evidence.is_social_topic:
        score -= 0.20
        reasons.append("social_topic")

    if any(re.search(pattern, text) for pattern in _SOCIAL_PATTERNS):
        score -= 0.75
        reasons.append("explicit_social_context")

    probability = min(1.0, max(0.0, round(score, 4)))
    if probability >= auto_copy_threshold:
        route = MaterialRoute.AUTO_COPY
    elif probability >= review_threshold:
        route = MaterialRoute.ADMIN_REVIEW
    else:
        route = MaterialRoute.LEAVE
    return MaterialAssessment(probability, route, tuple(reasons))


class MaterialWorkflowState(StrEnum):
    DISCOVERED = "discovered"
    REVIEW = "review"
    COPY_PENDING = "copy_pending"
    COPIED = "copied"
    DELETE_PENDING = "delete_pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MaterialWorkflow:
    state: MaterialWorkflowState = MaterialWorkflowState.DISCOVERED
    destination_message_id: int | None = None
    failure_reason: str | None = None

    def route(self, assessment: MaterialAssessment) -> MaterialWorkflow:
        self._require(MaterialWorkflowState.DISCOVERED)
        if assessment.route is MaterialRoute.AUTO_COPY:
            return replace(self, state=MaterialWorkflowState.COPY_PENDING)
        if assessment.route is MaterialRoute.ADMIN_REVIEW:
            return replace(self, state=MaterialWorkflowState.REVIEW)
        return replace(self, state=MaterialWorkflowState.COMPLETED)

    def record_copy_success(self, destination_message_id: int) -> MaterialWorkflow:
        self._require(MaterialWorkflowState.COPY_PENDING)
        if destination_message_id <= 0:
            raise ValueError("destination message id must be positive")
        return replace(
            self,
            state=MaterialWorkflowState.COPIED,
            destination_message_id=destination_message_id,
        )

    def request_source_delete(self, *, enabled: bool) -> MaterialWorkflow:
        self._require(MaterialWorkflowState.COPIED)
        next_state = (
            MaterialWorkflowState.DELETE_PENDING
            if enabled
            else MaterialWorkflowState.COMPLETED
        )
        return replace(self, state=next_state)

    def record_delete_success(self) -> MaterialWorkflow:
        self._require(MaterialWorkflowState.DELETE_PENDING)
        return replace(self, state=MaterialWorkflowState.COMPLETED)

    def fail(self, reason: str) -> MaterialWorkflow:
        if self.state is MaterialWorkflowState.COMPLETED:
            raise ValueError("completed workflow cannot fail")
        return replace(self, state=MaterialWorkflowState.FAILED, failure_reason=reason)

    def _require(self, expected: MaterialWorkflowState) -> None:
        if self.state is not expected:
            raise ValueError(f"expected {expected}, got {self.state}")
