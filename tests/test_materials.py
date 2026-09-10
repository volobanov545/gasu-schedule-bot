from __future__ import annotations

import pytest

from szs_hub.domain.materials import (
    MaterialEvidence,
    MaterialRoute,
    MaterialWorkflow,
    MaterialWorkflowState,
    assess_material,
)


def test_explicit_methodical_pdf_is_auto_copy_candidate() -> None:
    result = assess_material(
        MaterialEvidence(
            filename="zhbk.pdf",
            caption="Вот методичка по ЖБК",
            known_discipline_aliases=("ЖБК",),
        )
    )

    assert result.route is MaterialRoute.AUTO_COPY
    assert result.probability == 1.0


def test_pdf_alone_is_not_automatically_moved() -> None:
    result = assess_material(MaterialEvidence(filename="document.pdf"))

    assert result.route is MaterialRoute.LEAVE
    assert result.probability < 0.70


def test_academic_signal_found_only_in_extracted_text_can_route_material() -> None:
    result = assess_material(
        MaterialEvidence(
            filename="anonymous.txt",
            extracted_text_excerpt=(
                "Методичка по расчётной работе. "
                "Порядок выполнения задания и пример расчёта приведены ниже. " * 3
            ),
        )
    )

    assert result.route is MaterialRoute.AUTO_COPY
    assert "explicit_academic_context" in result.reasons
    assert "extractable_document_text" in result.reasons


def test_car_photo_stays_in_social_conversation() -> None:
    result = assess_material(
        MaterialEvidence(caption="Смотрите какой мерс", is_photo=True, is_social_topic=True)
    )

    assert result.route is MaterialRoute.LEAVE
    assert result.probability == 0.0


def test_copy_must_succeed_before_delete_can_be_requested() -> None:
    workflow = MaterialWorkflow().route(
        assess_material(MaterialEvidence(filename="task.pdf", caption="Вот методичка"))
    )

    assert workflow.state is MaterialWorkflowState.COPY_PENDING
    with pytest.raises(ValueError, match="expected copied"):
        workflow.request_source_delete(enabled=True)

    copied = workflow.record_copy_success(destination_message_id=99)
    delete_pending = copied.request_source_delete(enabled=True)
    assert delete_pending.state is MaterialWorkflowState.DELETE_PENDING
    assert delete_pending.record_delete_success().state is MaterialWorkflowState.COMPLETED


def test_source_delete_is_off_by_default_policy() -> None:
    workflow = MaterialWorkflow().route(
        assess_material(MaterialEvidence(filename="task.pdf", caption="Вот методичка"))
    )
    copied = workflow.record_copy_success(destination_message_id=99)

    assert copied.request_source_delete(enabled=False).state is MaterialWorkflowState.COMPLETED
